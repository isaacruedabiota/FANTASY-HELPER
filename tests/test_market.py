"""Tests del modelo de valor de mercado."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from fantasyhelper import market, queries
from fantasyhelper.storage import repository as repo

INICIO = date(2026, 1, 1)


def _serie(db, nombre: str, valores: list[int]) -> int:
    """Crea un jugador con una serie diaria de valores desde INICIO."""
    pid = repo.resolve_player(
        db, provider="mister", external_id=nombre, name=nombre, position="DL"
    )
    for indice, valor in enumerate(valores):
        repo.record_player_value(
            db, provider="mister", source="mister", player_id=pid,
            market_value=valor,
            snapshot_date=(INICIO + timedelta(days=indice)).isoformat(),
        )
    return pid


def _con_momento(base: int, dias: int, *, momento: float, semilla: int) -> list[int]:
    """Serie donde el cambio de cada semana repite parte del de la anterior."""
    azar = random.Random(semilla)
    valores, impulso = [base], 0.0
    for _ in range(dias - 1):
        impulso = momento * impulso + (1 - momento) * azar.gauss(0, 0.02)
        valores.append(max(10_000, round(valores[-1] * (1 + impulso))))
    return valores


# --- tramos -----------------------------------------------------------------


def test_los_tramos_cubren_todo_el_rango():
    assert market.band_of(500_000) == "<1M"
    assert market.band_of(1_000_000) == "1-3M"
    assert market.band_of(7_999_999) == "3-8M"
    assert market.band_of(8_000_000) == "8-15M"
    assert market.band_of(30_000_000) == ">15M"


def test_sin_valor_no_hay_tramo():
    assert market.band_of(None) is None
    assert market.band_of(0) is None


# --- calibracion ------------------------------------------------------------


def test_una_serie_con_momento_se_detecta(db):
    for indice in range(30):
        _serie(db, f"J{indice}", _con_momento(5_000_000, 80, momento=0.8, semilla=indice))

    modelo = market.calibrate(db)
    banda = modelo.bands["3-8M"]
    assert banda.correlation > 0.3, "el momento inyectado tiene que aparecer"
    assert banda.slope > 0


def test_un_paseo_aleatorio_no_da_senal(db):
    """Sin momento real, el modelo tiene que decir que no sabe."""
    for indice in range(30):
        _serie(db, f"J{indice}", _con_momento(5_000_000, 80, momento=0.0, semilla=100 + indice))

    banda = market.calibrate(db).bands.get("3-8M")
    assert banda is None or abs(banda.correlation) < 0.3


def test_sin_historico_no_hay_modelo(db):
    modelo = market.calibrate(db)
    assert modelo.bands == {}
    assert not modelo.useful


# --- el fallo que motivo separar por tramos ---------------------------------


@pytest.fixture
def dos_tramos(db):
    """Baratos disparados y caros planos, que es lo que pasa en pretemporada.

    Reproduce el caso real: en agosto los jugadores de menos de 3M subian un 30%
    semanal y los de mas de 15M bajaban un 1%.
    """
    dias, tirón = 40, 8
    for indice in range(20):
        # Baratos: quietos y de golpe un +4% diario la ultima semana, que los
        # deja en +31% semanal sin sacarlos de su tramo.
        _serie(db, f"barato{indice}",
               [2_000_000] * (dias - tirón)
               + [round(2_000_000 * 1.04**d) for d in range(tirón)])
    for indice in range(20):
        # Caros: planos.
        _serie(db, f"caro{indice}", [20_000_000] * dias)
    return db


def test_un_caro_plano_no_se_compara_con_los_baratos(dos_tramos):
    """El fallo original: Mbappe salia cayendo un 6% por no subir como los baratos.

    Con la mediana del mercado entero en +21%, cualquier jugador caro quedaba muy
    por debajo y el modelo lo daba por hundido. Comparado con los de su precio,
    que tampoco se movian, no estaba cayendo en absoluto.
    """
    modelo = market.MomentumModel(
        bands={
            ">15M": market.BandModel(">15M", slope=0.5, correlation=0.7,
                                     volatility=0.02, samples=5_000),
            "1-3M": market.BandModel("1-3M", slope=0.5, correlation=0.7,
                                     volatility=0.20, samples=5_000),
        },
        dates=40,
    )
    prediccion = market.forecast(dos_tramos, model=modelo)

    caros = [
        v for pid, v in prediccion.items() if v["tramo"] == ">15M"
    ]
    assert caros, "los caros tienen que estar en la prediccion"
    for fila in caros:
        assert fila["ventaja"] == pytest.approx(0.0, abs=1e-9), (
            "un caro que no se mueve, entre caros que no se mueven, no va a ninguna parte"
        )


def test_cada_tramo_se_centra_en_su_propia_mediana(dos_tramos):
    modelo = market.MomentumModel(
        bands={
            ">15M": market.BandModel(">15M", 0.5, 0.7, 0.02, 5_000),
            "1-3M": market.BandModel("1-3M", 0.5, 0.7, 0.20, 5_000),
        },
        dates=40,
    )
    prediccion = market.forecast(dos_tramos, model=modelo)
    centros = {v["tramo"]: v["centro_tramo"] for v in prediccion.values()}

    assert centros[">15M"] == pytest.approx(0.0, abs=1e-9)
    assert centros["1-3M"] > 0.2, "los baratos suben con fuerza"


def test_destacar_dentro_del_tramo_es_lo_que_cuenta(db):
    """Dos caros: uno se mueve como su tramo y otro sube mas. Solo el segundo vale."""
    dias = 40
    for indice in range(20):
        _serie(db, f"caro{indice}", [20_000_000] * dias)
    _serie(db, "destacado",
           [20_000_000] * (dias - 8) + [round(20_000_000 * 1.01**d) for d in range(8)])

    modelo = market.MomentumModel(
        bands={">15M": market.BandModel(">15M", 0.5, 0.7, 0.02, 5_000)}, dates=40
    )
    prediccion = market.forecast(db, model=modelo)
    filas = {f["name"]: f for f in market.attach(db, queries.all_players(db), model=modelo)}

    assert filas["destacado"]["ventaja"] > 0
    assert filas["caro0"]["ventaja"] == pytest.approx(0.0, abs=1e-9)
    assert len(prediccion) == 21


# --- fiabilidad -------------------------------------------------------------


def test_un_tramo_poco_fiable_no_se_usa():
    """Si la correlacion no llega al minimo, mejor no decir nada que decir algo malo."""
    modelo = market.MomentumModel(
        bands={
            "<1M": market.BandModel("<1M", 0.2, 0.05, 0.30, 50_000),
            "3-8M": market.BandModel("3-8M", 0.7, 0.72, 0.13, 20_000),
        },
        dates=300,
    )
    assert modelo.for_value(500_000) is None
    assert modelo.for_value(5_000_000) is not None
    assert modelo.useful, "basta con que sirva un tramo"


def test_pocas_muestras_tampoco_valen():
    banda = market.BandModel("3-8M", slope=0.9, correlation=0.9, volatility=0.1, samples=10)
    assert not banda.useful, "una correlacion alta con diez datos no es una correlacion"


def test_un_tramo_sin_jugadores_suficientes_no_da_mediana(db):
    """Con cuatro jugadores en un tramo, su mediana es uno de ellos y no dice nada."""
    for indice in range(3):
        _serie(db, f"raro{indice}", [20_000_000, 21_000_000] * 10)

    modelo = market.MomentumModel(
        bands={">15M": market.BandModel(">15M", 0.5, 0.7, 0.02, 5_000)}, dates=10
    )
    assert market.forecast(db, model=modelo) == {}
