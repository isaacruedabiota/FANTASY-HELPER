"""Job diario de captura.

Esta es la pieza que hay que tener funcionando desde el primer dia de liga.
El historico de valores de mercado, clausulas y probabilidades de once no se
puede reconstruir a posteriori: o se captura cada dia, o se pierde.

Por eso el job esta pensado para degradar, no para fallar: si Mister cambia un
endpoint o FutbolFantasy cambia el HTML, se registra el error y se sigue con el
resto. Y el crudo siempre se guarda antes de parsear.
"""

from __future__ import annotations

import logging
import sqlite3

from fantasyhelper.adapters.futbolfantasy.scraper import FutbolFantasyScraper
from fantasyhelper.adapters.mister.adapter import MisterAdapter
from fantasyhelper.config import settings
from fantasyhelper.reconcile import reconcile
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import connect, transaction

log = logging.getLogger(__name__)

JOB_NAME = "daily_snapshot"


def current_matchday(conn: sqlite3.Connection) -> int:
    """Jornada en curso. Se deduce de los partidos ya cargados; 1 si no hay nada."""
    row = conn.execute(
        "SELECT MAX(matchday) AS md FROM fixture WHERE season = ? AND status != 'finished'",
        (settings.season,),
    ).fetchone()
    if row and row["md"]:
        return int(row["md"])

    row = conn.execute(
        "SELECT MAX(matchday) AS md FROM lineup_probability_snapshot WHERE season = ?",
        (settings.season,),
    ).fetchone()
    return int(row["md"]) if row and row["md"] else 1


def run_snapshot(
    conn: sqlite3.Connection | None = None,
    *,
    sources: tuple[str, ...] = ("mister", "futbolfantasy"),
) -> int:
    owns_connection = conn is None
    conn = conn or connect()
    run_id = repo.start_job(conn, JOB_NAME)
    total = 0
    errors: list[str] = []

    try:
        # Ojo: nada de envolver la captura entera en una transaccion. Dentro hay
        # decenas de peticiones HTTP, y si fallase la ultima se perderia tambien
        # el crudo de todas las anteriores. Cada adapter confirma por bloques.
        if "mister" in sources:
            try:
                adapter = MisterAdapter()
                total += adapter.snapshot(conn)
                adapter.client.close()
            except Exception as exc:
                log.error("Mister: %s", exc)
                errors.append(f"mister: {exc}")

        if "futbolfantasy" in sources:
            try:
                matchday = current_matchday(conn)
                with FutbolFantasyScraper() as scraper:
                    rows = scraper.snapshot(conn, matchday)
                log.info("futbolfantasy -> %d filas (jornada %d)", rows, matchday)
                total += rows
            except Exception as exc:
                log.error("FutbolFantasy: %s", exc)
                errors.append(f"futbolfantasy: {exc}")

        # Al final y no por fuente: unificar equipos y jugadores necesita tener
        # delante los datos de las dos fuentes a la vez.
        if len(sources) > 1:
            try:
                with transaction(conn):
                    report = reconcile(conn)
                if report.teams_merged or report.players_linked:
                    log.info(
                        "reconciliacion: %d equipos fusionados, %d jugadores enlazados, "
                        "%d sin cruzar",
                        report.teams_merged, report.players_linked, report.still_unmatched,
                    )
            except Exception as exc:
                log.error("reconciliacion: %s", exc)
                errors.append(f"reconciliacion: {exc}")

        status = "error" if errors and total == 0 else "ok"
        repo.finish_job(
            conn, run_id, status=status, rows_written=total,
            error="; ".join(errors) or None,
        )
        return total
    except Exception as exc:
        repo.finish_job(conn, run_id, status="error", rows_written=total, error=str(exc))
        raise
    finally:
        if owns_connection:
            conn.close()
