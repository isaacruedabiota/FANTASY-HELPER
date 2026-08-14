"""Tests del planificador.

Lo que se protege aqui es que el intervalo haga lo que dice y no capture de mas:
cada pasada son unas 45 peticiones contra Mister con la sesion del usuario, y
una equivocacion aqui no se ve hasta que llega el disgusto.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fantasyhelper import config
from fantasyhelper.jobs import scheduler as modulo


@pytest.fixture
def con_intervalo(monkeypatch):
    """Deja fijar los minutos entre capturas sin tocar el .env."""

    def fijar(minutos):
        monkeypatch.setattr(
            modulo, "settings", replace(config.settings, capture_every_minutes=minutos)
        )

    return fijar


def test_sin_configurar_siguen_las_dos_capturas_diarias(con_intervalo):
    con_intervalo(None)
    ids = {j.id for j in modulo.build_scheduler().get_jobs()}
    assert ids == {"snapshot_madrugada", "snapshot_tarde"}


def test_el_intervalo_sustituye_a_las_diarias_y_no_se_suma(con_intervalo):
    """Con capturas cada media hora, las de las 03:30 y 19:00 no aportan nada.

    Dejarlas seria capturar dos veces de mas al dia y darles ocasion de pisarse
    con la periodica.
    """
    con_intervalo(30)
    trabajos = modulo.build_scheduler().get_jobs()
    assert [j.id for j in trabajos] == ["snapshot_periodico"]
    assert trabajos[0].trigger.interval.total_seconds() == 30 * 60


def test_no_se_solapan_dos_pasadas(con_intervalo):
    """Si una captura tarda mas que el intervalo, la siguiente espera."""
    con_intervalo(30)
    trabajo = modulo.build_scheduler().get_jobs()[0]
    assert trabajo.max_instances == 1
    # Tras un apagon, UNA al volver y no una por cada intervalo perdido.
    assert trabajo.coalesce is True


def test_captura_nada_mas_arrancar(con_intervalo):
    """Volviendo de un corte de luz, lo primero es ponerse al dia.

    Con `next_run_time` sin fijar habria que esperar el primer intervalo entero,
    y con None el trabajo se quedaria PAUSADO para siempre.
    """
    con_intervalo(30)
    assert modulo.build_scheduler().get_jobs()[0].next_run_time is not None


# --- el intervalo, leido del entorno ----------------------------------------


def _minutos(monkeypatch, valor):
    monkeypatch.setenv("FH_CAPTURE_EVERY_MINUTES", valor)
    return config.load_settings().capture_every_minutes


def test_se_lee_el_intervalo_del_entorno(monkeypatch):
    assert _minutos(monkeypatch, "30") == 30


def test_sin_variable_no_hay_captura_periodica(monkeypatch):
    monkeypatch.delenv("FH_CAPTURE_EVERY_MINUTES", raising=False)
    assert config.load_settings().capture_every_minutes is None


def test_un_valor_ilegible_no_tumba_el_planificador(monkeypatch):
    """Se avisa y se sigue con las capturas diarias, que es lo seguro."""
    assert _minutos(monkeypatch, "cada rato") is None


def test_un_intervalo_absurdo_se_sube_al_minimo(monkeypatch):
    """Un cero o un uno encadenaria capturas sin que termine la anterior."""
    assert _minutos(monkeypatch, "1") == config.MIN_CAPTURE_MINUTES
    assert _minutos(monkeypatch, "0") == config.MIN_CAPTURE_MINUTES


# --- el candado compartido --------------------------------------------------


def test_no_se_captura_si_ya_hay_una_en_marcha(db, monkeypatch, tmp_path):
    """El boton de la web captura en OTRO proceso.

    Con dos capturas al dia el solape era improbable; capturando cada media
    hora deja de serlo, y dos a la vez se pisan escribiendo el mismo snapshot.
    """
    from fantasyhelper.jobs.snapshot import JOB_NAME
    from fantasyhelper.storage import db as db_module
    from fantasyhelper.storage import repository as repo

    repo.start_job(db, JOB_NAME)  # queda en 'running'
    monkeypatch.setattr(
        modulo, "connect", lambda: db_module.connect(tmp_path / "test.db")
    )
    llamadas = []
    monkeypatch.setattr(modulo, "run_snapshot", lambda **k: llamadas.append(k))

    assert modulo.capture_unless_busy() == 0
    assert llamadas == [], "no se ha debido lanzar la captura"


def test_si_no_hay_ninguna_en_marcha_se_captura(db, monkeypatch, tmp_path):
    from fantasyhelper.storage import db as db_module

    monkeypatch.setattr(
        modulo, "connect", lambda: db_module.connect(tmp_path / "test.db")
    )
    monkeypatch.setattr(modulo, "run_snapshot", lambda **k: 123)

    assert modulo.capture_unless_busy() == 123
