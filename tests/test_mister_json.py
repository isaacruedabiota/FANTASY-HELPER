"""Tests del API JSON de Mister (/ajax/sw/*), la unica fuente de clausulas."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from fantasyhelper.adapters.mister.parsers import (
    parse_spanish_date,
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
