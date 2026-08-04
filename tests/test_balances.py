"""Tests de la estimacion de saldos de los rivales."""

from __future__ import annotations

import pytest

from fantasyhelper import queries
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import today


@pytest.fixture
def liga(db):
    league_id = repo.upsert_league(
        db, provider="mister", external_id="1", name="Liga", season="2026-27"
    )
    yo = repo.upsert_manager(db, league_id=league_id, external_id="10", name="Yo", is_me=True)
    rival = repo.upsert_manager(db, league_id=league_id, external_id="20", name="Rival")

    def fichar(manager, nombre, valor, *, nivel=0, suelo=None):
        pid = repo.resolve_player(
            db, provider="mister", external_id=nombre, name=nombre, position="DF"
        )
        repo.record_player_value(db, provider="mister", player_id=pid, market_value=valor)
        repo.record_ownership(
            db, league_id=league_id, player_id=pid, manager_id=manager,
            clause_value=int((suelo or valor) * 1.5),
            clause_level=nivel, clause_floor=suelo or valor,
        )
        return pid

    return {"league_id": league_id, "yo": yo, "rival": rival, "fichar": fichar}


def test_sin_ancla_no_estima(db, liga):
    """Adivinar el dia de partida desplazaria el saldo de todos: mejor no estimar."""
    liga["fichar"](liga["yo"], "Uno", 10_000_000)
    assert queries.estimated_balances(db) == []


def test_saldo_es_el_presupuesto_menos_la_plantilla(db, liga):
    liga["fichar"](liga["yo"], "Uno", 10_000_000)
    liga["fichar"](liga["yo"], "Dos", 5_000_000)
    queries.set_baseline_date(db, today())

    fila = next(f for f in queries.estimated_balances(db) if f["name"] == "Yo")
    assert fila["valor_inicial"] == 15_000_000
    assert fila["saldo_estimado"] == queries.INITIAL_BUDGET - 15_000_000


def test_descuenta_lo_gastado_en_subir_clausulas(db, liga):
    """Cada escalon cuesta el 20% del suelo (listeners.js)."""
    liga["fichar"](liga["rival"], "Caro", 2_000_000, nivel=3, suelo=2_000_000)
    queries.set_baseline_date(db, today())

    fila = next(f for f in queries.estimated_balances(db) if f["name"] == "Rival")
    assert fila["gasto_clausulas"] == pytest.approx(2_000_000 * 0.2 * 3)
    assert fila["saldo_estimado"] == pytest.approx(
        queries.INITIAL_BUDGET - 2_000_000 - 1_200_000
    )


def test_nivel_cero_no_cuesta_nada(db, liga):
    liga["fichar"](liga["rival"], "Normal", 1_000_000)
    queries.set_baseline_date(db, today())

    fila = next(f for f in queries.estimated_balances(db) if f["name"] == "Rival")
    assert fila["gasto_clausulas"] == 0


def test_devuelve_el_saldo_real_cuando_se_conoce(db, liga):
    """Solo el propio: es lo unico que publica Mister, y sirve de contraste."""
    liga["fichar"](liga["yo"], "Uno", 10_000_000)
    liga["fichar"](liga["rival"], "Otro", 10_000_000)
    repo.record_manager_state(db, manager_id=liga["yo"], balance=40_000_000)
    queries.set_baseline_date(db, today())

    filas = {f["name"]: f for f in queries.estimated_balances(db)}
    assert filas["Yo"]["saldo_real"] == 40_000_000
    assert filas["Yo"]["saldo_estimado"] == 40_000_000, "estimacion y realidad deben cuadrar"
    assert filas["Rival"]["saldo_real"] is None


def test_prune_quita_los_jugadores_que_ya_no_estan(db, liga):
    """Sin esto la plantilla acumula fantasmas y el saldo sale mal."""
    uno = liga["fichar"](liga["yo"], "Sigue", 1_000_000)
    liga["fichar"](liga["yo"], "Vendido", 2_000_000)

    borrados = repo.prune_ownership(
        db, league_id=liga["league_id"], manager_id=liga["yo"], keep_player_ids={uno}
    )
    assert borrados == 1

    quedan = db.execute(
        "SELECT COUNT(*) n FROM ownership_snapshot WHERE manager_id = ?", (liga["yo"],)
    ).fetchone()["n"]
    assert quedan == 1
