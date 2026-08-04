"""Tests del API JSON de Mister (/ajax/sw/*), la unica fuente de clausulas."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from fantasyhelper.adapters.mister.parsers import (
    normalize_season,
    parse_matchday_points,
    parse_next_fixture,
    parse_season_history,
    parse_spanish_date,
    parse_team_names,
    parse_user_squad,
    parse_value_history,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def users_payload() -> dict:
    with gzip.open(FIXTURES / "mister_ajax_users.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def test_extrae_la_plantilla_entera(users_payload):
    squad = parse_user_squad(users_payload)
    assert len(squad.players) == 15
    assert squad.manager.name == "Gorje44"
    assert squad.manager.external_id == "10741820"
    assert squad.manager.team_value == 15_610_000


def test_el_id_de_liga_viene_en_la_respuesta(users_payload):
    # Las rutas no llevan el id de liga, pero el API si lo publica.
    squad = parse_user_squad(users_payload)
    assert squad.league_external_id == "1564937"


def test_todas_las_clausulas_estan_presentes(users_payload):
    squad = parse_user_squad(users_payload)
    assert all(p.clause_value for p in squad.players)


def test_clausula_concreta_verificada(users_payload):
    squad = parse_user_squad(users_payload)

    sadiq = next(p for p in squad.players if p.slug == "umar-sadiq")
    assert sadiq.market_value == 3_486_000
    assert sadiq.clause_value == 5_229_000  # valor x 1.5
    assert sadiq.position == "DL"

    # Con valores bajos la clausula no baja de un minimo.
    puerto = next(p for p in squad.players if p.slug == "eric-puerto")
    assert puerto.market_value == 216_000
    assert puerto.clause_value == 1_000_000


def test_el_json_trae_el_nombre_completo(users_payload):
    """El HTML solo da 'E. Puerto'; el JSON da 'Eric Puerto'.

    Importa para el cruce entre fuentes: un nombre completo genera un slug que
    coincide directamente con el de FutbolFantasy.
    """
    squad = parse_user_squad(users_payload)
    puerto = next(p for p in squad.players if p.external_id == "70951")
    assert puerto.name == "Eric Puerto"
    assert puerto.slug == "eric-puerto"


def test_todos_los_jugadores_tienen_dueno(users_payload):
    squad = parse_user_squad(users_payload)
    assert all(p.owner_id == "10741820" for p in squad.players)


def test_payload_vacio_no_revienta():
    squad = parse_user_squad({"status": "ok", "data": {}})
    assert squad.players == []
    assert squad.league_external_id is None


# --- historico de valor de mercado ---------------------------------------

@pytest.fixture
def players_payload() -> dict:
    with gzip.open(FIXTURES / "mister_ajax_players.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def test_parse_spanish_date():
    assert parse_spanish_date("3 ago 2025") == "2025-08-03"
    # 'sept' con cuatro letras es el caso que rompe un mapeo de tres.
    assert parse_spanish_date("24 sept 2025") == "2025-09-24"
    assert parse_spanish_date("10 mar 2026") == "2026-03-10"
    assert parse_spanish_date("1 ene 2026") == "2026-01-01"
    assert parse_spanish_date("no es una fecha") is None
    assert parse_spanish_date("") is None


def test_historico_de_valor_completo(players_payload):
    history = parse_value_history(players_payload)
    assert len(history) > 300, "Mister publica alrededor de un ano de valores diarios"

    fechas = [d for d, _ in history]
    assert fechas == sorted(fechas), "deben venir en orden cronologico"
    assert all(v > 0 for _, v in history)


def test_primer_punto_del_historico_verificado(players_payload):
    history = parse_value_history(players_payload)
    assert history[0] == ("2025-08-03", 6_118_000)


def test_historico_sin_datos_devuelve_lista_vacia():
    assert parse_value_history({"data": {}}) == []
    assert parse_value_history({"data": {"values_chart": {"points": []}}}) == []


# --- rendimiento por temporada, calendario y equipos -------------------------

def test_historico_por_temporada(players_payload):
    """Es la unica base posible para estimar puntos antes de que empiece la liga."""
    seasons = parse_season_history(players_payload)
    assert len(seasons) == 5

    ultima = seasons[0]
    assert ultima.season == "2025-26", "el ano se normaliza a cuatro digitos"
    assert ultima.points == 166
    assert ultima.avg_points == pytest.approx(4.3684)
    # 166 puntos a 4,3684 de media son 38 partidos: los jugo todos.
    assert ultima.matches_played == 38

    temporadas = [s.season for s in seasons]
    assert temporadas == sorted(temporadas, reverse=True), "de la reciente a la vieja"


def test_la_temporada_se_normaliza():
    assert normalize_season("25/26") == "2025-26"
    assert normalize_season(" 09/10 ") == "2009-10"
    assert normalize_season("2025-26") is None, "solo entiende el formato abreviado"
    assert normalize_season("") is None


def test_proximo_partido_con_sede_y_jornada(players_payload):
    """El calendario sale de aqui: es el unico sitio que dice quien juega en casa."""
    fixture = parse_next_fixture(players_payload)
    assert fixture is not None
    assert fixture.matchday == 1, "el id de jornada se traduce a numero"
    assert fixture.home_external_id == "48"
    assert fixture.away_external_id == "9"
    # 15 ago 19:30 en Madrid son las 17:30 UTC.
    assert fixture.kickoff_utc == "2026-08-15T17:30:00Z"


def test_sin_proximo_partido_no_hay_calendario():
    assert parse_next_fixture({"data": {}}) is None
    assert parse_next_fixture({"data": {"next_match": {}}}) is None


def test_los_equipos_traen_nombre(players_payload):
    """Sin esto los equipos de Mister se quedan en 'mister-team-48'."""
    nombres = parse_team_names(players_payload)
    assert nombres["48"] == "Alavés"
    assert nombres["9"] == "Getafe", "tambien el rival del proximo partido"


def test_puntos_por_jornada_vacios_antes_de_empezar(players_payload):
    """En pretemporada no hay ni una jornada jugada, y eso no es un error."""
    assert parse_matchday_points(players_payload) == []


def test_puntos_por_jornada_cuando_se_ha_jugado():
    payload = {"data": {"points": [
        {"number": 1, "points": {"points": 7}},
        {"number": 2, "points": {"points": None}},
        {"number": 3, "points": {"points": 0}},
    ]}}
    # La jornada sin puntos aun no se ha jugado; el cero de la tercera si cuenta.
    assert parse_matchday_points(payload) == [(1, 7), (3, 0)]
