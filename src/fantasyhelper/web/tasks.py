"""Lanzar la captura desde la web sin bloquear la peticion.

Una captura completa tarda medio minuto: decenas de peticiones a Mister y a
FutbolFantasy. Eso no cabe dentro de una peticion HTTP, asi que el boton arranca
un hilo y devuelve el control enseguida; la pagina pregunta cada pocos segundos
como va.

Un hilo y no un proceso porque el trabajo es esperar a la red, no calcular:
mientras httpx espera, el hilo suelta el GIL. Y con hilo se comparte el estado
sin inventar un canal entre procesos.

DOS CANDADOS, QUE HACEN FALTA LOS DOS

El de este proceso evita que dos pulsaciones seguidas lancen dos capturas. El de
la base de datos evita algo distinto: que el boton coincida con la captura
programada de las 03:30 o las 19:00, que corre en OTRO proceso -el servicio del
planificador- y del que aqui no hay ni noticia. Sin el segundo, dos capturas
simultaneas se pisarian escribiendo el mismo snapshot del dia.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from fantasyhelper.jobs.snapshot import (
    STALE_MINUTES,
    capture_running,
    run_snapshot,
)
from fantasyhelper.storage.db import connect

log = logging.getLogger(__name__)

#: El candado de la base de datos vive en `jobs.snapshot` porque lo necesitan
#: los dos procesos que capturan, y el planificador no puede importar de la web:
#: la web es un extra opcional del paquete.
other_capture_running = capture_running

__all__ = ["RUNNER", "STALE_MINUTES", "CaptureRunner", "CaptureState",
           "other_capture_running"]


@dataclass
class CaptureState:
    running: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    rows: int | None = None
    error: str | None = None
    sources: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "corriendo": self.running,
            "segundos": round(time.time() - self.started_at, 1) if self.started_at else None,
            "filas": self.rows,
            "error": self.error,
            "fuentes": list(self.sources),
            "terminada_hace": (
                round(time.time() - self.finished_at) if self.finished_at else None
            ),
        }


class CaptureRunner:
    """Una captura en segundo plano, y solo una."""

    def __init__(self) -> None:
        self._state = CaptureState()
        self._lock = threading.Lock()

    def state(self) -> CaptureState:
        with self._lock:
            return self._state

    def start(self, sources: tuple[str, ...] = ("mister", "futbolfantasy")) -> tuple[bool, str]:
        """Arranca la captura. Devuelve (arrancada, motivo si no)."""
        with self._lock:
            if self._state.running:
                return False, "Ya hay una captura en marcha."

            # La conexion de comprobacion se abre y cierra aqui: la del hilo
            # tiene que ser suya, porque SQLite no deja compartir conexiones
            # entre hilos.
            conn = connect()
            try:
                if other_capture_running(conn):
                    return False, "La captura programada esta corriendo ahora mismo."
            finally:
                conn.close()

            self._state = CaptureState(
                running=True, started_at=time.time(), sources=tuple(sources)
            )

        hilo = threading.Thread(
            target=self._run, args=(tuple(sources),), name="captura-web", daemon=True
        )
        hilo.start()
        return True, "Captura arrancada."

    def _run(self, sources: tuple[str, ...]) -> None:
        filas, error = None, None
        try:
            filas = run_snapshot(sources=sources)
        except Exception as exc:  # el hilo no puede dejar caer la excepcion
            log.exception("la captura lanzada desde la web ha fallado")
            error = str(exc)

        with self._lock:
            self._state.running = False
            self._state.finished_at = time.time()
            self._state.rows = filas
            self._state.error = error


RUNNER = CaptureRunner()
