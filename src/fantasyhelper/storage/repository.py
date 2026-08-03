"""Escrituras sobre el modelo canonico.

Los adapters hablan con esta capa, nunca con SQL directo. Asi el dia que se
conecte un segundo fantasy no hay que tocar nada de aqui abajo.
"""

from __future__ import annotations

import logging
import sqlite3

from fantasyhelper.storage.db import today, utcnow
from fantasyhelper.utils.names import match_key, slugify

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Entidades canonicas
# --------------------------------------------------------------------------

def upsert_team(
    conn: sqlite3.Connection,
    *,
    name: str,
    short_name: str | None = None,
    provider: str | None = None,
    external_id: str | None = None,
) -> int:
    slug = slugify(name)
    conn.execute(
        "INSERT INTO team (slug, name, short_name) VALUES (?, ?, ?) "
        "ON CONFLICT (slug) DO UPDATE SET name = excluded.name, "
        "short_name = COALESCE(excluded.short_name, team.short_name)",
        (slug, name, short_name),
    )
    team_id = conn.execute("SELECT id FROM team WHERE slug = ?", (slug,)).fetchone()["id"]

    if provider and external_id:
        conn.execute(
            "INSERT INTO team_alias (team_id, provider, external_id, external_name) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (provider, external_id) DO UPDATE "
            "SET team_id = excluded.team_id",
            (team_id, provider, str(external_id), name),
        )
    return team_id


def resolve_player(
    conn: sqlite3.Connection,
    *,
    provider: str,
    external_id: str,
    name: str,
    team_id: int | None = None,
    position: str | None = None,
    slug: str | None = None,
) -> int:
    """Devuelve el player_id canonico para un jugador de un proveedor.

    Tres pasos, de mas fiable a menos:
      1. Alias ya conocido -> id directo.
      2. Mismo slug -> es el mismo jugador, se crea el alias.
      3. Nombre normalizado + mismo equipo -> match difuso, se marca con
         confidence 0.8 para poder revisarlo con `fh dudas`.
    Si nada encaja, se crea un jugador nuevo.

    `slug` es la clave del cruce entre fuentes y conviene pasarlo siempre que
    la fuente lo publique: Mister muestra los nombres abreviados ("A. Sivera")
    pero da el slug completo en el enlace ("antonio-sivera"), que es el mismo
    formato que usa FutbolFantasy. Derivar el slug del nombre mostrado haria
    que las dos fuentes no se encontraran nunca.
    """
    external_id = str(external_id)

    row = conn.execute(
        "SELECT player_id FROM player_alias WHERE provider = ? AND external_id = ?",
        (provider, external_id),
    ).fetchone()
    if row:
        return row["player_id"]

    slug = slug or slugify(name)
    confidence = 1.0

    row = conn.execute("SELECT id FROM player WHERE slug = ?", (slug,)).fetchone()
    player_id = row["id"] if row else None

    if player_id is None and team_id is not None:
        candidates = conn.execute(
            "SELECT id, name FROM player WHERE team_id = ?", (team_id,)
        ).fetchall()
        key = match_key(name)
        matches = [c["id"] for c in candidates if match_key(c["name"]) == key]
        # Solo se acepta si es inequivoco: dos candidatos significa que no lo sabemos.
        if len(matches) == 1:
            player_id = matches[0]
            confidence = 0.8
            log.info("match difuso: %s '%s' -> player %s", provider, name, player_id)

    if player_id is None:
        cur = conn.execute(
            "INSERT INTO player (slug, name, team_id, position, updated_at) VALUES (?, ?, ?, ?, ?)",
            (slug, name, team_id, position, utcnow()),
        )
        player_id = cur.lastrowid
    else:
        # El equipo canonico lo fija la primera fuente que lo sepa y no se
        # pisa despues: cada fuente usa sus propios identificadores de equipo,
        # y el suyo queda guardado en su alias.
        conn.execute(
            "UPDATE player SET team_id = COALESCE(team_id, ?), "
            "position = COALESCE(position, ?), updated_at = ? WHERE id = ?",
            (team_id, position, utcnow(), player_id),
        )

    conn.execute(
        "INSERT INTO player_alias "
        "  (player_id, provider, external_id, external_name, team_id, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET "
        "  team_id = COALESCE(excluded.team_id, player_alias.team_id)",
        (player_id, provider, external_id, name, team_id, confidence),
    )
    return player_id


def upsert_league(
    conn: sqlite3.Connection,
    *,
    provider: str,
    external_id: str,
    name: str,
    season: str | None = None,
) -> int:
    conn.execute(
        "INSERT INTO league (provider, external_id, name, season) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET name = excluded.name",
        (provider, str(external_id), name, season),
    )
    return conn.execute(
        "SELECT id FROM league WHERE provider = ? AND external_id = ?",
        (provider, str(external_id)),
    ).fetchone()["id"]


def upsert_manager(
    conn: sqlite3.Connection,
    *,
    league_id: int,
    external_id: str,
    name: str,
    is_me: bool = False,
) -> int:
    conn.execute(
        "INSERT INTO manager (league_id, external_id, name, is_me) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (league_id, external_id) DO UPDATE SET name = excluded.name, "
        "is_me = MAX(manager.is_me, excluded.is_me)",
        (league_id, str(external_id), name, int(is_me)),
    )
    return conn.execute(
        "SELECT id FROM manager WHERE league_id = ? AND external_id = ?",
        (league_id, str(external_id)),
    ).fetchone()["id"]


# --------------------------------------------------------------------------
# Snapshots (una fila por dia; reejecutar el job actualiza, no duplica)
# --------------------------------------------------------------------------

def record_player_value(
    conn: sqlite3.Connection,
    *,
    provider: str,
    player_id: int,
    market_value: int,
    delta_1d: int | None = None,
    source: str | None = None,
) -> None:
    """`provider` = de quien es el valor; `source` = de donde lo hemos leido."""
    conn.execute(
        """
        INSERT INTO player_value_snapshot
            (snapshot_date, captured_at, provider, source, player_id, market_value, delta_1d)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, provider, source, player_id) DO UPDATE SET
            captured_at = excluded.captured_at,
            market_value = excluded.market_value,
            delta_1d = COALESCE(excluded.delta_1d, player_value_snapshot.delta_1d)
        """,
        (today(), utcnow(), provider, source or provider, player_id, market_value, delta_1d),
    )


def record_player_context(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    season: str,
    matchday: int,
    jerarquia: int | None = None,
    form: float | None = None,
    opponent: str | None = None,
    opponent_difficulty: int | None = None,
    is_home: bool | None = None,
    penalties: int | None = None,
    free_kicks: int | None = None,
    corners: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO player_context_snapshot
            (snapshot_date, captured_at, player_id, season, matchday, jerarquia, form,
             opponent, opponent_difficulty, is_home, penalties, free_kicks, corners)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, player_id, matchday) DO UPDATE SET
            captured_at = excluded.captured_at,
            jerarquia = excluded.jerarquia,
            form = excluded.form,
            opponent = excluded.opponent,
            opponent_difficulty = excluded.opponent_difficulty,
            is_home = excluded.is_home,
            penalties = excluded.penalties,
            free_kicks = excluded.free_kicks,
            corners = excluded.corners
        """,
        (
            today(), utcnow(), player_id, season, matchday, jerarquia, form,
            opponent, opponent_difficulty,
            None if is_home is None else int(is_home),
            penalties, free_kicks, corners,
        ),
    )


def record_ownership(
    conn: sqlite3.Connection,
    *,
    league_id: int,
    player_id: int,
    manager_id: int | None,
    clause_value: int | None = None,
    clause_locked_until: str | None = None,
    buy_price: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO ownership_snapshot
            (snapshot_date, captured_at, league_id, player_id, manager_id,
             clause_value, clause_locked_until, buy_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, league_id, player_id) DO UPDATE SET
            captured_at = excluded.captured_at,
            manager_id = excluded.manager_id,
            clause_value = excluded.clause_value,
            clause_locked_until = excluded.clause_locked_until,
            buy_price = COALESCE(excluded.buy_price, ownership_snapshot.buy_price)
        """,
        (
            today(), utcnow(), league_id, player_id, manager_id,
            clause_value, clause_locked_until, buy_price,
        ),
    )


def record_manager_state(
    conn: sqlite3.Connection,
    *,
    manager_id: int,
    balance: int | None = None,
    team_value: int | None = None,
    points: int | None = None,
    position: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO manager_snapshot
            (snapshot_date, captured_at, manager_id, balance, team_value, points, position)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, manager_id) DO UPDATE SET
            captured_at = excluded.captured_at,
            balance = excluded.balance,
            team_value = excluded.team_value,
            points = excluded.points,
            position = excluded.position
        """,
        (today(), utcnow(), manager_id, balance, team_value, points, position),
    )


def record_market_listing(
    conn: sqlite3.Connection,
    *,
    league_id: int,
    player_id: int,
    seller_id: int | None = None,
    asking_price: int | None = None,
    expires_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO market_listing_snapshot
            (snapshot_date, captured_at, league_id, player_id, seller_id, asking_price, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, league_id, player_id) DO UPDATE SET
            captured_at = excluded.captured_at,
            seller_id = excluded.seller_id,
            asking_price = excluded.asking_price,
            expires_at = excluded.expires_at
        """,
        (today(), utcnow(), league_id, player_id, seller_id, asking_price, expires_at),
    )


def record_lineup_probability(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    season: str,
    matchday: int,
    probability: float | None,
    status: str | None = None,
    note: str | None = None,
    provider: str = "futbolfantasy",
) -> None:
    conn.execute(
        """
        INSERT INTO lineup_probability_snapshot
            (snapshot_date, captured_at, provider, player_id, season, matchday,
             probability, status, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, provider, player_id, matchday) DO UPDATE SET
            captured_at = excluded.captured_at,
            probability = excluded.probability,
            status = excluded.status,
            note = excluded.note
        """,
        (
            today(), utcnow(), provider, player_id, season, matchday,
            probability, status, note,
        ),
    )


def record_player_points(
    conn: sqlite3.Connection,
    *,
    provider: str,
    player_id: int,
    season: str,
    matchday: int,
    points: int | None,
    breakdown_json: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO player_points
            (provider, player_id, season, matchday, points, breakdown_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (provider, player_id, season, matchday) DO UPDATE SET
            points = excluded.points,
            breakdown_json = excluded.breakdown_json,
            updated_at = excluded.updated_at
        """,
        (provider, player_id, season, matchday, points, breakdown_json, utcnow()),
    )


# --------------------------------------------------------------------------
# Trazabilidad del job
# --------------------------------------------------------------------------

def start_job(conn: sqlite3.Connection, job_name: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_run (job_name, started_at, status) VALUES (?, ?, 'running')",
        (job_name, utcnow()),
    )
    return cur.lastrowid


def finish_job(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    rows_written: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE job_run SET finished_at = ?, status = ?, rows_written = ?, error = ? WHERE id = ?",
        (utcnow(), status, rows_written, error, run_id),
    )
