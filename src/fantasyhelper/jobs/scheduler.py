"""Planificador del job diario.

Dos capturas al dia y no una porque los valores de mercado de Mister se
actualizan de madrugada, mientras que los onces probables de FutbolFantasy
cambian durante la tarde (ruedas de prensa, entrenamientos).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from fantasyhelper.jobs.snapshot import run_snapshot

log = logging.getLogger(__name__)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Europe/Madrid")

    # Captura principal: despues de que Mister actualice valores de madrugada.
    scheduler.add_job(
        run_snapshot,
        CronTrigger(hour=3, minute=30),
        id="snapshot_madrugada",
        # Si el ordenador estaba apagado a las 3:30, que se ejecute al arrancar
        # dentro de un margen de 6 horas en vez de saltarse el dia.
        misfire_grace_time=6 * 3600,
        coalesce=True,
    )

    # Segunda pasada solo para onces probables, ya por la tarde.
    scheduler.add_job(
        run_snapshot,
        CronTrigger(hour=19, minute=0),
        id="snapshot_tarde",
        kwargs={"sources": ("futbolfantasy",)},
        misfire_grace_time=3 * 3600,
        coalesce=True,
    )
    return scheduler


def main() -> None:
    from fantasyhelper.config import setup_logging

    setup_logging()
    scheduler = build_scheduler()
    log.info("planificador arrancado. Ctrl+C para parar.")
    for job in scheduler.get_jobs():
        log.info("  %s -> %s", job.id, job.trigger)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("planificador detenido")


if __name__ == "__main__":
    main()
