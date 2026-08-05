"""Tests de la web. Comprueban que responde y que no se inventa cifras."""

from __future__ import annotations

import pytest

from fantasyhelper.web import charts

fastapi = pytest.importorskip("fastapi", reason="la web es opcional")
from fastapi.testclient import TestClient


@pytest.fixture
def cliente(db, monkeypatch, tmp_path):
    """App apuntando a una base de datos de prueba vacia pero valida."""
    from fantasyhelper.storage import db as db_module
    from fantasyhelper.web import app as modulo

    monkeypatch.setattr(modulo, "_conn", lambda: db_module.connect(tmp_path / "test.db"))
    return TestClient(modulo.app, raise_server_exceptions=False)


def test_sin_capturar_lo_dice_en_vez_de_romperse(cliente):
    """Una base vacia no puede dar un 500: hay que decir que falta capturar."""
    assert cliente.get("/").status_code == 503


def test_las_paginas_que_no_dependen_de_ti_responden(cliente):
    for ruta in ("/mercado", "/liga", "/valor"):
        assert cliente.get(ruta).status_code == 200, ruta


def test_un_jugador_inexistente_da_404(cliente):
    assert cliente.get("/jugador/999999").status_code == 404


def test_la_api_de_salud_dice_de_cuando_son_los_datos(cliente):
    datos = cliente.get("/api/salud").json()
    assert "ultima_captura" in datos
    assert "modelos" in datos


# --- el grafico -------------------------------------------------------------


def _serie(valores):
    return [
        {"snapshot_date": f"2026-01-{i + 1:02d}", "market_value": v}
        for i, v in enumerate(valores)
    ]


def test_un_solo_punto_no_dibuja_una_linea(db):
    assert charts.value_chart(_serie([1_000_000])).empty
    assert charts.value_chart([]).empty


def test_el_grafico_lleva_la_linea_y_las_marcas():
    grafico = charts.value_chart(_serie([1_000_000, 1_200_000, 1_100_000, 1_400_000]))
    assert not grafico.empty
    assert "polyline" in grafico.svg
    assert grafico.ticks, "sin marcas el eje no dice nada"
    assert grafico.desde == "2026-01-01"
    assert grafico.hasta == "2026-01-04"


def test_las_etiquetas_no_van_dentro_del_svg():
    """Van en HTML aparte: el SVG se estira y deformaria el texto."""
    grafico = charts.value_chart(_serie([1_000_000, 2_000_000, 1_500_000]))
    assert "<text" not in grafico.svg


def test_cada_punto_sabe_donde_esta_en_porcentaje():
    """La capa de hover coloca en porcentaje, no en coordenadas del SVG."""
    grafico = charts.value_chart(_serie([1_000_000, 2_000_000, 1_500_000]))
    assert grafico.points[0]["left"] == 0
    assert grafico.points[-1]["left"] == 100
    assert all(0 <= p["top"] <= 100 for p in grafico.points)


def test_las_marcas_del_eje_son_numeros_redondos():
    marcas = charts._nice_ticks(1_000_000, 5_000_000)
    assert marcas == sorted(marcas)
    assert all(m % 1_000_000 == 0 for m in marcas), "nada de 1.234.567 en un eje"


# --- el boton de actualizar -------------------------------------------------

def test_actualizar_es_post_y_no_get(cliente):
    """Un GET con efectos lo dispara cualquier precarga del navegador."""
    assert cliente.get("/api/capturar").status_code == 200, "el GET solo consulta"
    assert cliente.request("PUT", "/api/capturar").status_code == 405


def test_el_estado_dice_de_cuando_son_los_datos(cliente):
    datos = cliente.get("/api/capturar").json()
    assert datos["corriendo"] is False
    assert "ultima_captura" in datos


def test_no_se_lanzan_dos_capturas_a_la_vez():
    """Dos pulsaciones seguidas no pueden arrancar dos capturas."""
    from fantasyhelper.web.tasks import CaptureRunner

    corredor = CaptureRunner()
    corredor._state.running = True

    arrancada, motivo = corredor.start()
    assert not arrancada
    assert "marcha" in motivo


def test_no_se_pisa_a_la_captura_programada(db, monkeypatch, tmp_path):
    """El planificador corre en OTRO proceso; el candado va en la base de datos."""
    from fantasyhelper.storage import db as db_module
    from fantasyhelper.storage import repository as repo
    from fantasyhelper.web import tasks

    repo.start_job(db, tasks.JOB_NAME)  # queda en 'running'
    monkeypatch.setattr(tasks, "connect", lambda: db_module.connect(tmp_path / "test.db"))

    arrancada, motivo = tasks.CaptureRunner().start()
    assert not arrancada
    assert "programada" in motivo


def test_una_captura_colgada_no_bloquea_para_siempre(db):
    """Si el servicio muere a media captura, su fila se queda en 'running'."""
    from fantasyhelper.web.tasks import other_capture_running

    db.execute(
        "INSERT INTO job_run (job_name, started_at, status) VALUES (?, ?, 'running')",
        ("daily_snapshot", "2020-01-01T00:00:00Z"),
    )
    assert not other_capture_running(db), "una de hace anos no cuenta como viva"


def test_una_captura_recien_arrancada_si_bloquea(db):
    from fantasyhelper.storage import repository as repo
    from fantasyhelper.web.tasks import other_capture_running

    repo.start_job(db, "daily_snapshot")
    assert other_capture_running(db)
