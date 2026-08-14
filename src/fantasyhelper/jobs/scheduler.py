"""Planificador de la captura.

Dos modos, segun `FH_CAPTURE_EVERY_MINUTES`:

  sin configurar   dos capturas al dia. Una de madrugada, cuando Mister ya ha
                   actualizado los valores, y otra por la tarde solo para los
                   onces probables, que cambian con las ruedas de prensa.

  configurado      una captura cada N minutos, y nada mas. Sustituye a las dos
                   diarias en vez de sumarse a ellas: con capturas cada media
                   hora, la de las 03:30 y la de las 19:00 no aportan nada y
                   solo darian ocasion de pisarse.

QUE DA Y QUE NO DA CAPTURAR MAS A MENUDO

Da frescura: la pagina deja de estar a diez horas de los hechos. NO da mas
historico, y conviene tenerlo claro antes de bajar el intervalo. Los snapshots
son una fila por dia y entidad -asi esta la clave unica en `schema.sql`- de modo
que capturar cuarenta y ocho veces al dia reescribe cuarenta y ocho veces las
mismas filas. Para tener el valor de un jugador hora a hora habria que cambiar
el esquema, no el intervalo.

Y cuesta: cada pasada son unas 45 peticiones entre Mister y FutbolFantasy. Cada
media hora son unas 2.100 al dia frente a las 65 de las dos capturas diarias.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from fantasyhelper.config import settings
from fantasyhelper.jobs.snapshot import capture_running, run_snapshot
from fantasyhelper.storage.db import connect

log = logging.getLogger(__name__)

#: La liga es espanola y los horarios de Mister tambien: las 03:30 son las
#: 03:30 de aqui, con cambio de hora incluido.
TZ = ZoneInfo("Europe/Madrid")


def capture_unless_busy(**kwargs) -> int:
    """Captura, salvo que ya haya una en marcha.

    El candado hace falta desde que se captura cada pocos minutos: el boton de
    la web lanza la captura en OTRO proceso, del que el planificador no tiene
    noticia, y con capturas frecuentes el solape deja de ser improbable. Dos
    capturas a la vez se pisarian escribiendo el mismo snapshot del dia.
    """
    conn = connect()
    try:
        if capture_running(conn):
            log.info("ya hay una captura en marcha; esta se salta")
            return 0
    finally:
        conn.close()
    return run_snapshot(**kwargs)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=TZ)
    minutos = settings.capture_every_minutes

    if minutos:
        scheduler.add_job(
            capture_unless_busy,
            IntervalTrigger(minutes=minutos),
            id="snapshot_periodico",
            # Una sola a la vez: si una pasada tarda mas que el intervalo, la
            # siguiente espera en vez de arrancar encima.
            max_instances=1,
            # Tras un apagon se ejecuta UNA vez al arrancar, no una por cada
            # intervalo perdido.
            coalesce=True,
            misfire_grace_time=minutos * 60,
            # Y una nada mas arrancar, sin esperar el primer intervalo: si la
            # Pi vuelve de un corte de luz, lo primero que hay que hacer es
            # ponerse al dia. El candado evita que se pise con nada.
            next_run_time=datetime.now(TZ),
        )
        return scheduler

    # Captura principal: despues de que Mister actualice valores de madrugada.
    scheduler.add_job(
        capture_unless_busy,
        CronTrigger(hour=3, minute=30),
        id="snapshot_madrugada",
        # Si el ordenador estaba apagado a las 3:30, que se ejecute al arrancar
        # dentro de un margen de 6 horas en vez de saltarse el dia.
        misfire_grace_time=6 * 3600,
        coalesce=True,
        max_instances=1,
    )

    # Segunda pasada solo para onces probables, ya por la tarde.
    scheduler.add_job(
        capture_unless_busy,
        CronTrigger(hour=19, minute=0),
        id="snapshot_tarde",
        kwargs={"sources": ("futbolfantasy",)},
        misfire_grace_time=3 * 3600,
        coalesce=True,
        max_instances=1,
    )
    return scheduler


def main() -> None:
    from fantasyhelper.config import setup_logging

    setup_logging()
    scheduler = build_scheduler()
    log.info("planificador arrancado. Ctrl+C para parar.")
    if settings.capture_every_minutes:
        log.info(
            "captura cada %d minutos (~%d peticiones al dia). Da frescura, no "
            "mas historico: los snapshots son una fila por dia.",
            settings.capture_every_minutes,
            round(24 * 60 / settings.capture_every_minutes) * 45,
        )
    for job in scheduler.get_jobs():
        log.info("  %s -> %s", job.id, job.trigger)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("planificador detenido")


if __name__ == "__main__":
    main()
