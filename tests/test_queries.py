"""Tests de las consultas de analisis."""

from __future__ import annotations

import pytest

from fantasyhelper import queries
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import today


@pytest.fixture
def liga(db):
    """Una liga minima: yo, un rival y jugadores con datos completos."""
    league_id = repo.upsert_league(
        db, provider="mister", external_id="1", name="Liga", season="2026-27"
    )
    yo = repo.upsert_manager(
        db, league_id=league_id, external_id="10", name="Glok", is_me=True
    )
    rival = repo.upsert_manager(db, league_id=league_id, external_id="20", name="Rival")
    equipo = repo.upsert_team(db, name="alaves", provider="mister", external_id="48")

    def jugador(nombre, *, valor, prob, status="ok", jerarquia=50, dueno=None, clausula=None):
        pid = repo.resolve_player(
            db, provider="mister", external_id=nombre, name=nombre,
            team_id=equipo, position="DF",
        )
        repo.record_player_value(
            db, provider="mister", player_id=pid, market_value=valor
        )
        repo.record_lineup_probability(
            db, player_id=pid, season="2026-27", matchday=1,
            probability=prob, status=status,
        )
        repo.record_player_context(
            db, player_id=pid, season="2026-27", matchday=1,
            jerarquia=jerarquia, opponent="GET", opponent_difficulty=3, is_home=True,
        )
        if dueno is not None:
            repo.record_ownership(
                db, league_id=league_id, player_id=pid, manager_id=dueno,
                clause_value=clausula,
            )
        return pid

    return {
        "league_id": league_id,
        "yo": yo,
        "rival": rival,
        "jugador": jugador,
    }


def test_me_identifica(db, liga):
    me = queries.my_manager(db)
    assert me is not None
    assert me["name"] == "Glok"


def test_plantilla_propia(db, liga):
    liga["jugador"]("Mio Uno", valor=1_000_000, prob=0.9, dueno=liga["yo"], clausula=1_500_000)
    liga["jugador"]("De Otro", valor=2_000_000, prob=0.9, dueno=liga["rival"], clausula=3_000_000)

    filas = queries.squad(db, liga["yo"])
    assert [f["name"] for f in filas] == ["Mio Uno"]
    assert filas[0]["clause_value"] == 1_500_000


def test_radar_excluye_los_mios_y_los_no_disponibles(db, liga):
    liga["jugador"]("Mio", valor=1_000_000, prob=0.9, dueno=liga["yo"], clausula=1_500_000)
    liga["jugador"]("Sano", valor=1_000_000, prob=0.9, dueno=liga["rival"], clausula=1_500_000)
    liga["jugador"](
        "Lesionado", valor=1_000_000, prob=0.9, status="lesionado",
        dueno=liga["rival"], clausula=1_000_000,
    )

    objetivos = queries.clause_targets(db, manager_id=liga["yo"])
    assert [o["name"] for o in objetivos] == ["Sano"]


def test_radar_ordena_por_coste_ajustado(db, liga):
    # Misma clausula, distinta probabilidad: gana el que seguro que juega.
    liga["jugador"]("Titular", valor=1_000_000, prob=0.9, dueno=liga["rival"], clausula=2_000_000)
    liga["jugador"]("Suplente", valor=1_000_000, prob=0.6, dueno=liga["rival"], clausula=2_000_000)

    objetivos = queries.clause_targets(db, manager_id=liga["yo"])
    assert [o["name"] for o in objetivos] == ["Titular", "Suplente"]
    assert objetivos[0]["adjusted_cost"] < objetivos[1]["adjusted_cost"]


def test_radar_respeta_el_presupuesto(db, liga):
    liga["jugador"]("Barato", valor=500_000, prob=0.9, dueno=liga["rival"], clausula=750_000)
    liga["jugador"]("Caro", valor=9_000_000, prob=0.9, dueno=liga["rival"], clausula=13_500_000)

    objetivos = queries.clause_targets(db, manager_id=liga["yo"], budget=1_000_000)
    assert [o["name"] for o in objetivos] == ["Barato"]


def test_riesgo_ignora_a_mis_lesionados(db, liga):
    """A un lesionado no te lo van a clausular, por barato que sea."""
    liga["jugador"](
        "Lesionado", valor=500_000, prob=0.9, status="lesionado",
        dueno=liga["yo"], clausula=750_000,
    )
    liga["jugador"]("Sano", valor=500_000, prob=0.9, dueno=liga["yo"], clausula=800_000)

    riesgo = queries.clause_risk(db, liga["yo"])
    assert [r["name"] for r in riesgo] == ["Sano"]


def test_probabilidad_desconocida_no_sube_en_el_ranking(db, liga):
    """Sin dato de probabilidad, el jugador debe quedar por debajo, no en medio."""
    liga["jugador"]("Conocido", valor=1_000_000, prob=0.9, dueno=liga["yo"], clausula=1_500_000)
    pid = repo.resolve_player(
        db, provider="mister", external_id="x", name="Desconocido", position="DF"
    )
    repo.record_player_value(db, provider="mister", player_id=pid, market_value=1_000_000)
    repo.record_ownership(
        db, league_id=liga["league_id"], player_id=pid,
        manager_id=liga["yo"], clause_value=1_500_000,
    )

    riesgo = queries.clause_risk(db, liga["yo"])
    assert [r["name"] for r in riesgo] == ["Conocido", "Desconocido"]


def test_libres_excluyen_a_los_que_tienen_dueno(db, liga):
    liga["jugador"]("Libre", valor=800_000, prob=0.9)
    liga["jugador"]("Con dueno", valor=800_000, prob=0.9, dueno=liga["rival"], clausula=1_200_000)

    libres = queries.free_agents(db)
    assert [f["name"] for f in libres] == ["Libre"]


def test_busqueda_encuentra_por_nombre_de_la_fuente(db, liga):
    pid = liga["jugador"]("Pedri Gonzalez", valor=20_000_000, prob=0.9)
    # Otra fuente lo llama de otra forma; buscar por ese nombre tambien vale.
    repo.resolve_player(
        db, provider="futbolfantasy", external_id="ff1", name="Pedri Gonzalez",
    )
    db.execute(
        "UPDATE player_alias SET external_name = 'Pedri' WHERE provider = 'futbolfantasy'"
    )

    assert [r["id"] for r in queries.find_players(db, "Pedri")] == [pid]


def test_historico_de_valor(db, liga):
    pid = liga["jugador"]("Con historia", valor=1_000_000, prob=0.9)
    repo.record_player_value(
        db, provider="mister", player_id=pid, market_value=900_000,
        snapshot_date="2026-07-01",
    )

    historia = queries.value_history(db, pid, days=365)
    assert [h["market_value"] for h in historia] == [900_000, 1_000_000]
    assert historia[-1]["snapshot_date"] == today()


def test_clasificacion_marca_quien_soy(db, liga):
    repo.record_manager_state(db, manager_id=liga["yo"], points=10, position=1)
    repo.record_manager_state(db, manager_id=liga["rival"], points=5, position=2)

    filas = queries.standings(db)
    assert [f["name"] for f in filas] == ["Glok", "Rival"]
    assert filas[0]["is_me"] == 1
