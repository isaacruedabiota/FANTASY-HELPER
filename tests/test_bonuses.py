"""Tests de las bonificaciones de la liga."""

from __future__ import annotations

from fantasyhelper import bonuses
from fantasyhelper.storage import repository as repo

REGLAS = bonuses.LA_LIGA_2627


def test_por_punto():
    assert REGLAS.matchday_total(points=40) == 40 * 75_000


def test_por_jugador_en_el_once_ideal():
    assert REGLAS.matchday_total(ideal_xi_players=3) == 3 * 250_000


def test_por_acierto_de_quiniela():
    assert REGLAS.matchday_total(quiniela_hits=7) == 7 * 50_000


def test_conceptos_desactivados_no_suman():
    # En esta liga no hay bonus por gol ni cantidad fija.
    assert REGLAS.matchday_total(goals=5) == 0


def test_por_puesto_en_la_jornada():
    assert REGLAS.matchday_total(rank=1) == 200_000
    assert REGLAS.matchday_total(rank=10) == 1_400_000
    # Un puesto fuera de la tabla no inventa dinero.
    assert REGLAS.matchday_total(rank=25) == 0


def test_una_jornada_completa_suma_todos_los_conceptos():
    total = REGLAS.matchday_total(
        points=52, rank=3, ideal_xi_players=2, quiniela_hits=6
    )
    assert total == 52 * 75_000 + 400_000 + 2 * 250_000 + 6 * 50_000


def test_puntos_negativos_no_restan_saldo():
    """Se puede puntuar negativo, pero la liga no te cobra por ello."""
    assert REGLAS.matchday_total(points=-8) == 0


def test_ida_y_vuelta_por_json():
    recuperadas = bonuses.BonusRules.from_json(REGLAS.to_json())
    assert recuperadas == REGLAS


def test_sin_configurar_no_paga_nada():
    vacias = bonuses.BonusRules.from_json(None)
    assert vacias.matchday_total(points=100, rank=1, ideal_xi_players=5) == 0


def test_se_guardan_y_se_leen_de_la_liga(db):
    repo.upsert_league(db, provider="mister", external_id="1", name="Liga")
    bonuses.save_rules(db, REGLAS)

    leidas = bonuses.load_rules(db)
    assert leidas.per_point == 75_000
    assert leidas.by_matchday_rank[8] == 1_000_000
