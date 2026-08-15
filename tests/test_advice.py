"""Tests de la recomendacion diaria."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fantasyhelper import advice, market, queries
from fantasyhelper.bonuses import BonusRules
from fantasyhelper.storage import repository as repo

INICIO = date(2026, 1, 1)
DIAS = 30

#: Modelo de valor fijo, para que los tests no dependan de una calibracion.
MODELO = market.MomentumModel(
    bands={
        "3-8M": market.BandModel("3-8M", slope=0.5, correlation=0.7,
                                 volatility=0.13, samples=5_000),
    },
    dates=DIAS,
)
REGLAS = BonusRules(per_point=100_000)


@pytest.fixture
def liga(db):
    league_id = repo.upsert_league(
        db, provider="mister", external_id="1", name="Liga", season="2026-27"
    )
    yo = repo.upsert_manager(db, league_id=league_id, external_id="10", name="Yo", is_me=True)
    rival = repo.upsert_manager(db, league_id=league_id, external_id="20", name="Rival")
    equipo = repo.upsert_team(db, name="Equipo", provider="mister", external_id="1")
    contrario = repo.upsert_team(db, name="Contrario", provider="mister", external_id="2")
    repo.upsert_fixture(db, season="2026-27", matchday=1,
                        home_team_id=equipo, away_team_id=contrario)

    def fichar(
        nombre, *, media=None, valores=None, dueno=None, probabilidad=0.9,
        en_mercado=False, precio=None,
    ):
        valores = valores or [5_000_000] * DIAS
        pid = repo.resolve_player(db, provider="mister", external_id=nombre,
                                  name=nombre, team_id=equipo, position="DL")
        for indice, valor in enumerate(valores):
            repo.record_player_value(
                db, provider="mister", source="mister", player_id=pid,
                market_value=valor,
                snapshot_date=(INICIO + timedelta(days=indice)).isoformat(),
            )
        if media is not None:
            repo.record_season_stat(db, provider="mister", player_id=pid,
                                    season="2025-26", points=int(media * 38),
                                    avg_points=media, matches_played=38, team_id=equipo)
        repo.record_lineup_probability(db, player_id=pid, season="2026-27",
                                       matchday=1, probability=probabilidad)
        if dueno is not None:
            repo.record_ownership(db, league_id=league_id, player_id=pid,
                                  manager_id=dueno, clause_value=7_500_000,
                                  clause_level=0, clause_floor=5_000_000)
        # Estar en venta es una condicion aparte de tener dueno: en el mercado
        # del dia hay libres que pone el juego y jugadores que pone un rival.
        if en_mercado:
            repo.record_market_listing(
                db, league_id=league_id, player_id=pid, seller_id=dueno,
                asking_price=precio or valores[-1],
            )
        return pid

    return {"yo": yo, "rival": rival, "fichar": fichar, "league_id": league_id}


def _fila(db, liga, nombre):
    filas = advice.weekly_euros(db, queries.all_players(db), rules=REGLAS, model=MODELO)
    return next(f for f in filas if f["name"] == nombre)


# --- la conversion ----------------------------------------------------------


def test_los_puntos_se_convierten_a_euros(db, liga):
    """El cambio lo pone la liga: si paga 100.000 por punto, un punto vale eso."""
    liga["fichar"]("Bueno", media=4.0)

    fila = _fila(db, liga, "Bueno")
    assert fila["euros_por_puntos"] == pytest.approx(fila["xpts"] * 100_000, rel=1e-6)


def test_sin_bonificacion_por_punto_no_se_convierte(db, liga):
    """Sin cambio no hay conversion posible, y fingir una seria peor que nada."""
    liga["fichar"]("Bueno", media=4.0)

    filas = advice.weekly_euros(db, queries.all_players(db),
                                rules=BonusRules(), model=MODELO)
    assert filas[0]["euros_por_puntos"] is None


def test_el_rendimiento_suma_las_dos_mitades(db, liga):
    # Hacen falta bastantes en el tramo para que su mediana signifique algo y
    # haya prediccion de valor; con uno solo no la hay, y es correcto.
    for indice in range(20):
        liga["fichar"](f"Plano{indice}", media=3.0)
    liga["fichar"]("Completo", media=4.0)

    fila = _fila(db, liga, "Completo")
    assert fila["euros_ventaja"] is not None
    assert fila["rendimiento_semanal"] == (
        fila["euros_por_puntos"] + fila["euros_ventaja"]
    )


def test_sin_ninguna_mitad_no_se_inventa_un_cero(db, liga):
    """Un cero diria 'no rinde nada'; lo cierto es que no lo sabemos."""
    pid = repo.resolve_player(db, provider="mister", external_id="x",
                              name="Desconocido", position="DL")
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=pid, market_value=5_000_000)

    fila = _fila(db, liga, "Desconocido")
    assert fila["rendimiento_semanal"] is None


def test_una_mitad_basta_para_tener_rendimiento(db, liga):
    """Un jugador sin historico pero que se revaloriza si genera dinero."""
    subiendo = [5_000_000] * (DIAS - 7) + [round(5_000_000 * 1.02**d) for d in range(7)]
    for indice in range(20):
        liga["fichar"](f"Plano{indice}")
    liga["fichar"]("Subiendo", valores=subiendo)

    fila = _fila(db, liga, "Subiendo")
    assert fila["euros_por_puntos"] is None
    assert fila["rendimiento_semanal"] > 0


# --- el barato que vale mas que el caro -------------------------------------


def test_un_barato_que_sube_puede_ganar_a_un_caro_estancado(db, liga):
    """Lo que solo se ve al juntar las dos cosas en la misma moneda."""
    for indice in range(20):
        liga["fichar"](f"Plano{indice}", media=3.0)
    liga["fichar"]("Caro estancado", media=5.0)
    liga["fichar"](
        "Barato al alza", media=3.0,
        valores=[5_000_000] * (DIAS - 7) + [round(5_000_000 * 1.03**d) for d in range(7)],
    )

    caro = _fila(db, liga, "Caro estancado")
    barato = _fila(db, liga, "Barato al alza")

    assert caro["euros_por_puntos"] > barato["euros_por_puntos"], "el caro puntua mas"
    assert barato["rendimiento_semanal"] > caro["rendimiento_semanal"], (
        "pero el barato genera mas dinero a la semana"
    )


# --- el resumen del dia -----------------------------------------------------


def test_no_se_recomienda_vender_lo_que_no_se_conoce(db, liga):
    """No saber lo que rinde un jugador no es motivo para venderlo.

    Su media se deduce ahora de lo que cuesta, que sirve para fichar pero no
    para vender: deshacerse de alguien a quien no hemos visto jugar ni una vez,
    por una cifra sacada de su precio, es ruido y ademas irreversible.
    """
    liga["fichar"]("Conocido", media=3.0, dueno=liga["yo"])
    pid = repo.resolve_player(db, provider="mister", external_id="y",
                              name="Sin datos", team_id=None, position="DL")
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=pid, market_value=5_000_000)
    repo.record_ownership(db, league_id=liga["league_id"], player_id=pid,
                          manager_id=liga["yo"], clause_value=7_500_000)

    datos = advice.briefing(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    assert "Sin datos" not in [f["name"] for f in datos["vender"]]


def test_solo_se_compra_lo_que_cabe_en_el_saldo(db, liga):
    liga["fichar"]("Caro", media=5.0, valores=[7_000_000] * DIAS, en_mercado=True)
    liga["fichar"]("Asequible", media=5.0, valores=[3_500_000] * DIAS, en_mercado=True)

    nombres = [f["name"] for f in advice.briefing(
        db, manager_id=liga["yo"], budget=4_000_000,
        rules=REGLAS, model=MODELO)["comprar"]]
    assert nombres == ["Asequible"]


def test_solo_se_compra_lo_que_esta_hoy_en_el_mercado(db, liga):
    """En Mister no se ficha a quien te apetece, sino de la lista del dia.

    Antes salia aqui cualquier jugador sin dueno del catalogo entero -mas de
    quinientos-, y la inmensa mayoria no se podian fichar aunque sobrase saldo.
    """
    liga["fichar"]("En venta", media=5.0, en_mercado=True)
    liga["fichar"]("Libre pero fuera del mercado", media=6.0)

    nombres = [f["name"] for f in advice.briefing(
        db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)["comprar"]]
    assert nombres == ["En venta"]


def test_no_se_recomienda_comprar_lo_que_ya_es_tuyo(db, liga):
    """Los propios aparecen en el mercado cuando los pones tu a la venta."""
    liga["fichar"]("Mio en venta", media=6.0, dueno=liga["yo"], en_mercado=True)
    liga["fichar"]("De un rival", media=5.0, dueno=liga["rival"], en_mercado=True)

    nombres = [f["name"] for f in advice.briefing(
        db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)["comprar"]]
    assert nombres == ["De un rival"]


def test_el_precio_pedido_manda_sobre_el_valor_de_mercado(db, liga):
    """Un rival puede pedir mas de lo que vale, y es lo que hay que pagar."""
    liga["fichar"]("Sobrevalorado", media=5.0, dueno=liga["rival"],
                   en_mercado=True, precio=9_000_000)

    nombres = [f["name"] for f in advice.briefing(
        db, manager_id=liga["yo"], budget=6_000_000,
        rules=REGLAS, model=MODELO)["comprar"]]
    assert nombres == [], "vale 5M pero piden 9M, y no caben en el saldo"


def test_el_aviso_de_blindaje_ignora_a_los_suplentes(db, liga):
    """Nadie paga una clausula por un suplente, por barata que sea.

    Es lo que salia antes de filtrar: encabezaba la lista un jugador de 235.000 €
    que generaba 91.000 € a la semana. Baratisimo en proporcion, pero no le
    mejora el equipo a nadie.
    """
    liga["fichar"]("Titular", media=6.0, dueno=liga["yo"])
    liga["fichar"]("Suplente", media=0.5, dueno=liga["yo"], probabilidad=0.3)

    blindar = advice.briefing(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)["blindar"]
    assert [f["name"] for f in blindar] == ["Titular"]


def test_el_blindaje_se_ordena_por_lo_rapido_que_se_amortiza(db, liga):
    """Es la cuenta que haria el ladron, y por tanto la que dice quien peligra."""
    liga["fichar"]("Rentable", media=6.0, dueno=liga["yo"])
    liga["fichar"]("Menos", media=5.0, dueno=liga["yo"])

    blindar = advice.briefing(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)["blindar"]
    semanas = [f["semanas_amortizacion"] for f in blindar]
    assert semanas == sorted(semanas)
    assert all(s > 0 for s in semanas)
