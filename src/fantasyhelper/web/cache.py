"""Cache de los modelos caros, invalidada por los datos y no por el reloj.

Calibrar el modelo de valor recorre el historico entero: casi un segundo en el
portatil y bastante mas en la Raspberry. Hacerlo en cada peticion HTTP haria la
web inservible en el movil.

La clave no es un TTL sino una huella de los datos: la ultima fecha capturada y
cuantas filas hay. Los datos cambian dos veces al dia, cuando corre la captura,
asi que con un TTL habria que elegir entre recalcular de mas o servir datos
viejos; con la huella se recalcula exactamente cuando hay algo nuevo y ni una vez
mas.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from fantasyhelper import market, queries, xpts

log = logging.getLogger(__name__)


def data_fingerprint(conn: sqlite3.Connection) -> tuple:
    """Huella de los datos que alimentan los modelos.

    La serie de valores cubre la captura: es la unica entrada del modelo de
    mercado, y una captura que escriba cualquier otra cosa la reescribe tambien.

    Pero no cubre `fh mister reprocesar`, que trabaja sobre el crudo ya guardado
    y no toca ni un valor. Ese reproceso si cambia el calendario y los nombres
    de los equipos, y sin mirarlos la web seguia sirviendo el modelo anterior:
    se vio en pantalla, con el rival escrito 'MALAGA' -el nombre viejo, dentro
    del modelo cacheado- al lado de 'MÁLAGA' recien leido de la tabla.
    """
    valores = conn.execute(
        "SELECT MAX(snapshot_date) AS ultima, COUNT(*) AS filas "
        "FROM player_value_snapshot WHERE provider = 'mister'"
    ).fetchone()
    # `updated_at` del calendario y de los equipos: cualquier reproceso los
    # toca, y son dos tablas de veinte y trescientas filas.
    calendario = conn.execute(
        "SELECT COUNT(*) AS filas, MAX(updated_at) AS tocado FROM team_schedule"
    ).fetchone()
    equipos = conn.execute(
        "SELECT COUNT(*) AS filas, MAX(name) AS ultimo FROM team"
    ).fetchone()

    return (
        valores["ultima"], valores["filas"],
        calendario["filas"], calendario["tocado"],
        equipos["filas"], equipos["ultimo"],
    )


@dataclass
class _Entry:
    fingerprint: tuple
    value: Any
    computed_at: float
    seconds: float


class ModelCache:
    """Guarda el modelo calibrado mientras los datos no cambien.

    Con lock porque uvicorn sirve varias peticiones a la vez y calibrar dos veces
    en paralelo en una Raspberry con poca memoria es justo lo que no se quiere.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: str, fingerprint: tuple, build) -> Any:
        with self._lock:
            entrada = self._entries.get(key)
            if entrada is not None and entrada.fingerprint == fingerprint:
                return entrada.value

            inicio = time.perf_counter()
            valor = build()
            transcurrido = time.perf_counter() - inicio
            self._entries[key] = _Entry(fingerprint, valor, time.time(), transcurrido)
            log.info("modelo '%s' recalculado en %.1fs", key, transcurrido)
            return valor

    def stats(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "modelo": clave,
                    "calculado_hace": time.time() - e.computed_at,
                    "tardo": e.seconds,
                }
                for clave, e in self._entries.items()
            ]


CACHE = ModelCache()


def momentum_model(conn: sqlite3.Connection) -> market.MomentumModel:
    """El modelo de valor, calibrado como mucho una vez por captura."""
    return CACHE.get(
        "market", data_fingerprint(conn), lambda: market.calibrate(conn)
    )


def predictions(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Puntos esperados y prediccion de valor de todos, ya calculados.

    Las dos salidas dependen solo de los datos capturados, no de quien pregunte,
    asi que se pueden compartir entre peticiones. La de valor recorre el
    historico entero y era medio segundo por pantalla.
    """
    huella = data_fingerprint(conn)
    modelo = momentum_model(conn)
    return (
        CACHE.get("xpts", huella, lambda: xpts.expected_points(conn)),
        CACHE.get("forecast", huella, lambda: market.forecast(conn, model=modelo)),
    )


def photo_ids(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[int, str]]:
    """Los ids de Mister con los que se arman las URLs de foto y escudo.

    Son dos consultas baratas, pero se cachean igual porque las pide cada
    pantalla y solo cambian cuando entra un jugador nuevo al catalogo.
    """
    return CACHE.get(
        "fotos", data_fingerprint(conn), lambda: queries.mister_ids(conn)
    )
