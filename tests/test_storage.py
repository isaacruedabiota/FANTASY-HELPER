"""Tests de la capa de almacenamiento: idempotencia, crosswalk y crudo."""

from __future__ import annotations

from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.raw import load_raw, save_raw


def test_snapshot_es_idempotente(db):
    """Reejecutar la captura el mismo dia actualiza, no duplica.

    Es la garantia que permite relanzar el job sin miedo si algo fallo.
    """
    player_id = repo.resolve_player(db, provider="mister", external_id="1", name="Mbappe")

    repo.record_player_value(db, provider="mister", player_id=player_id, market_value=100)
    repo.record_player_value(db, provider="mister", player_id=player_id, market_value=120)

    rows = db.execute("SELECT market_value FROM player_value_snapshot").fetchall()
    assert len(rows) == 1
    assert rows[0]["market_value"] == 120, "debe quedarse el ultimo valor del dia"


def test_provider_y_source_conviven(db):
    """El valor de Mister leido de FutbolFantasy y el leido de Mister no chocan."""
    player_id = repo.resolve_player(db, provider="mister", external_id="1", name="Mbappe")

    repo.record_player_value(
        db, provider="mister", source="futbolfantasy", player_id=player_id, market_value=100
    )
    repo.record_player_value(
        db, provider="mister", source="mister", player_id=player_id, market_value=101
    )

    rows = db.execute("SELECT source, market_value FROM player_value_snapshot ORDER BY source")
    assert [(r["source"], r["market_value"]) for r in rows] == [
        ("futbolfantasy", 100),
        ("mister", 101),
    ]


def test_crosswalk_une_las_dos_fuentes(db):
    """El mismo jugador en Mister y FutbolFantasy debe ser un unico player_id."""
    a = repo.resolve_player(db, provider="mister", external_id="77", name="César Tárrega")
    b = repo.resolve_player(db, provider="futbolfantasy", external_id="9762", name="César Tárrega")
    assert a == b

    # Y cada fuente conserva su propio identificador externo.
    aliases = db.execute(
        "SELECT provider, external_id FROM player_alias WHERE player_id = ? ORDER BY provider",
        (a,),
    ).fetchall()
    assert [(r["provider"], r["external_id"]) for r in aliases] == [
        ("futbolfantasy", "9762"),
        ("mister", "77"),
    ]


def test_jugadores_distintos_no_se_fusionan(db):
    a = repo.resolve_player(db, provider="mister", external_id="1", name="Javi Guerra")
    b = repo.resolve_player(db, provider="mister", external_id="2", name="Javi Galán")
    assert a != b


def test_crudo_se_guarda_comprimido_y_se_lee_igual(db):
    original = b"<html>" + b"x" * 50_000 + b"</html>"
    save_raw(db, source="futbolfantasy", endpoint="equipo/valencia", content=original)

    assert load_raw(db, source="futbolfantasy", endpoint="equipo/valencia") == original

    row = db.execute("SELECT content, content_encoding FROM raw_payload").fetchone()
    assert row["content_encoding"] == "gzip"
    assert len(row["content"]) < len(original) / 5, "deberia comprimir bastante"


def test_crudo_pequeno_no_se_comprime(db):
    original = b'{"ok": true}'
    save_raw(db, source="mister", endpoint="squad", content=original)

    row = db.execute("SELECT content_encoding FROM raw_payload").fetchone()
    assert row["content_encoding"] == "identity"
    assert load_raw(db, source="mister", endpoint="squad") == original


def test_crudo_no_duplica_el_mismo_contenido(db):
    for _ in range(3):
        save_raw(db, source="mister", endpoint="squad", content=b'{"ok": true}')
    assert db.execute("SELECT COUNT(*) c FROM raw_payload").fetchone()["c"] == 1
