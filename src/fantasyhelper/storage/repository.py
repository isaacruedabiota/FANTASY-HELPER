"""Escrituras sobre el modelo canonico.

Los adapters hablan con esta capa, nunca con SQL directo. Asi el dia que se
conecte un segundo fantasy no hay que tocar nada de aqui abajo.
"""

from __future__ import annotations

import json
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
        """
        INSERT INTO team (slug, name, short_name) VALUES (?, ?, ?)
        ON CONFLICT (slug) DO UPDATE SET
            -- Un nombre que es su propio slug no es un nombre: es lo que da
            -- FutbolFantasy, que identifica al equipo por la URL
            -- ('rayo-vallecano'). Mister si publica el nombre completo, y sin
            -- esta guarda la siguiente captura lo pisaba y volvia a dejar
            -- 'REAL-SOCIEDAD' en pantalla.
            name = CASE WHEN excluded.name = team.slug THEN team.name
                        ELSE excluded.name END,
            short_name = COALESCE(excluded.short_name, team.short_name)
        """,
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


def set_player_team(
    conn: sqlite3.Connection, *, player_id: int, team_id: int
) -> None:
    """Cambia de equipo a un jugador. Es lo que `resolve_player` NO hace.

    Alli el equipo se fija la primera vez y no se pisa, y con razon: cada fuente
    numera los equipos a su manera y la primera que lo sepa suele acertar. Pero
    eso deja fuera los traspasos, y un jugador en el equipo equivocado arrastra
    con el un calendario que no es el suyo.

    Por eso esto es explicito y se llama solo desde la ficha del jugador, que es
    la unica fuente que da el equipo por su nombre.
    """
    conn.execute(
        "UPDATE player SET team_id = ?, updated_at = ? WHERE id = ?",
        (team_id, utcnow(), player_id),
    )


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
    snapshot_date: str | None = None,
) -> None:
    """`provider` = de quien es el valor; `source` = de donde lo hemos leido.

    `snapshot_date` permite escribir valores de dias pasados, que es como entra
    el historico que publica Mister. Por defecto, hoy.
    """
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
        (
            snapshot_date or today(), utcnow(), provider, source or provider,
            player_id, market_value, delta_1d,
        ),
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
    clause_level: int | None = None,
    clause_floor: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO ownership_snapshot
            (snapshot_date, captured_at, league_id, player_id, manager_id,
             clause_value, clause_locked_until, buy_price, clause_level, clause_floor)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, league_id, player_id) DO UPDATE SET
            captured_at = excluded.captured_at,
            manager_id = excluded.manager_id,
            clause_value = excluded.clause_value,
            clause_locked_until = excluded.clause_locked_until,
            buy_price = COALESCE(excluded.buy_price, ownership_snapshot.buy_price),
            clause_level = COALESCE(excluded.clause_level, ownership_snapshot.clause_level),
            clause_floor = COALESCE(excluded.clause_floor, ownership_snapshot.clause_floor)
        """,
        (
            today(), utcnow(), league_id, player_id, manager_id,
            clause_value, clause_locked_until, buy_price, clause_level, clause_floor,
        ),
    )


def record_feed_event(
    conn: sqlite3.Connection,
    *,
    league_id: int | None,
    external_id: str,
    kind: str | None,
    summary: str,
    html: str,
    relative_time: str | None = None,
    player_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    amounts: list[int] | None = None,
) -> bool:
    """Guarda un movimiento del feed. Devuelve True si es nuevo.

    Solo se inserta: un movimiento ya ocurrio y no cambia. Reejecutar la captura
    no duplica nada, y `first_seen` conserva cuando lo vimos por primera vez,
    que es lo mas parecido a una fecha real que da Mister (muestra '17h', no
    una marca de tiempo).
    """
    cur = conn.execute(
        """
        INSERT INTO feed_event
            (external_id, league_id, first_seen, snapshot_date, kind,
             relative_time, summary, player_ids, user_ids, amounts, html)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (external_id) DO NOTHING
        """,
        (
            external_id, league_id, utcnow(), today(), kind, relative_time, summary,
            json.dumps(player_ids or []), json.dumps(user_ids or []),
            json.dumps(amounts or []), html,
        ),
    )
    return cur.rowcount > 0


def prune_ownership(
    conn: sqlite3.Connection,
    *,
    league_id: int,
    manager_id: int,
    keep_player_ids: set[int],
) -> int:
    """Borra del snapshot de hoy los jugadores que este participante ya no tiene.

    Sin esto la plantilla diaria acumula fantasmas: si alguien vende (o la liga
    se reinicia) despues de una captura, la fila de propiedad anterior sigue ahi
    y el participante aparece con mas jugadores de los que tiene. Eso rompe
    cualquier calculo que sume el valor de una plantilla.
    """
    rows = conn.execute(
        "SELECT player_id FROM ownership_snapshot "
        "WHERE snapshot_date = ? AND league_id = ? AND manager_id = ?",
        (today(), league_id, manager_id),
    ).fetchall()

    sobrantes = [r["player_id"] for r in rows if r["player_id"] not in keep_player_ids]
    for player_id in sobrantes:
        conn.execute(
            "DELETE FROM ownership_snapshot "
            "WHERE snapshot_date = ? AND league_id = ? AND player_id = ?",
            (today(), league_id, player_id),
        )
    return len(sobrantes)


def record_manager_state(
    conn: sqlite3.Connection,
    *,
    manager_id: int,
    balance: int | None = None,
    future_balance: int | None = None,
    max_debt: int | None = None,
    team_value: int | None = None,
    points: int | None = None,
    position: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO manager_snapshot
            (snapshot_date, captured_at, manager_id, balance, future_balance,
             max_debt, team_value, points, position)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        -- COALESCE y no asignacion directa: el estado de un participante se
        -- escribe en dos pasos (la clasificacion da puntos y posicion, la
        -- plantilla da el valor). Sin esto, el segundo borraria lo del primero.
        ON CONFLICT (snapshot_date, manager_id) DO UPDATE SET
            captured_at = excluded.captured_at,
            balance = COALESCE(excluded.balance, manager_snapshot.balance),
            future_balance = COALESCE(excluded.future_balance,
                                      manager_snapshot.future_balance),
            max_debt = COALESCE(excluded.max_debt, manager_snapshot.max_debt),
            team_value = COALESCE(excluded.team_value, manager_snapshot.team_value),
            points = COALESCE(excluded.points, manager_snapshot.points),
            position = COALESCE(excluded.position, manager_snapshot.position)
        """,
        (
            today(), utcnow(), manager_id, balance, future_balance,
            max_debt, team_value, points, position,
        ),
    )


def record_manager_avatar(
    conn: sqlite3.Connection,
    *,
    manager_id: int,
    url: str | None = None,
    color: str | None = None,
    initials: str | None = None,
) -> None:
    """Foto de perfil del participante, o el circulo de color que la sustituye.

    Va en `manager` y no en un snapshot: cambiar de foto no es un hecho del dia
    que interese seguir en el tiempo, es un atributo que se pisa y ya esta.
    """
    conn.execute(
        """
        UPDATE manager SET
            avatar_url = COALESCE(?, avatar_url),
            avatar_color = COALESCE(?, avatar_color),
            avatar_initials = COALESCE(?, avatar_initials)
        WHERE id = ?
        """,
        (url, color, initials, manager_id),
    )


def record_manager_formation(
    conn: sqlite3.Connection, *, manager_id: int, formation: str | None
) -> None:
    """La formacion que tiene puesta el participante ahora mismo.

    Se pisa como el avatar y no se historifica: lo que interesa es si hay que
    cambiarla hoy, no cuando dejo de jugar con tres delanteros. Solo se sabe la
    propia, porque viene de la configuracion del usuario de la sesion.
    """
    if not formation:
        return
    conn.execute(
        "UPDATE manager SET formation = ? WHERE id = ?", (formation, manager_id)
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


def record_season_stat(
    conn: sqlite3.Connection,
    *,
    provider: str,
    player_id: int,
    season: str,
    points: int | None,
    avg_points: float | None,
    matches_played: int | None,
    team_id: int | None = None,
) -> None:
    """Guarda el rendimiento de un jugador en una temporada.

    Se reescribe en cada lectura porque la temporada en curso sigue creciendo;
    las pasadas ya no cambian y el UPDATE es inofensivo.
    """
    conn.execute(
        """
        INSERT INTO player_season_stat
            (provider, player_id, season, points, avg_points, matches_played,
             team_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (provider, player_id, season) DO UPDATE SET
            points = excluded.points,
            avg_points = excluded.avg_points,
            matches_played = excluded.matches_played,
            team_id = COALESCE(excluded.team_id, player_season_stat.team_id),
            updated_at = excluded.updated_at
        """,
        (provider, player_id, season, points, avg_points, matches_played,
         team_id, utcnow()),
    )


def rename_team(conn: sqlite3.Connection, *, team_id: int, name: str) -> None:
    """Le pone nombre de verdad a un equipo que no tenia uno legible.

    Dos casos, y solo esos dos:
      - los provisionales ('mister-team-9'), que solo tenian un numero;
      - los que llegaron de FutbolFantasy, cuyo "nombre" es el propio slug
        ('deportivo', 'racing'). Mister si publica el nombre completo.

    Un equipo que ya tenga un nombre distinto de su slug se respeta: cambiarlo
    no aportaria nada y el slug, que es la clave del cruce, no se toca nunca.
    """
    conn.execute(
        "UPDATE team SET name = ? WHERE id = ? AND (slug LIKE 'mister-team-%' OR name = slug)",
        (name, team_id),
    )


def team_id_for_alias(
    conn: sqlite3.Connection, *, provider: str, external_id: str
) -> int | None:
    """Equipo canonico a partir del id que usa una fuente, si ya se conoce."""
    row = conn.execute(
        "SELECT team_id FROM team_alias WHERE provider = ? AND external_id = ?",
        (provider, str(external_id)),
    ).fetchone()
    return row["team_id"] if row else None


def upsert_fixture(
    conn: sqlite3.Connection,
    *,
    season: str,
    matchday: int,
    home_team_id: int,
    away_team_id: int,
    kickoff_utc: str | None = None,
) -> int:
    """Registra un partido del calendario. Idempotente por jornada y rivales."""
    conn.execute(
        """
        INSERT INTO fixture (season, matchday, home_team_id, away_team_id, kickoff_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (season, matchday, home_team_id, away_team_id) DO UPDATE SET
            kickoff_utc = COALESCE(excluded.kickoff_utc, fixture.kickoff_utc)
        """,
        (season, matchday, home_team_id, away_team_id, kickoff_utc),
    )
    return conn.execute(
        "SELECT id FROM fixture WHERE season = ? AND matchday = ? "
        "AND home_team_id = ? AND away_team_id = ?",
        (season, matchday, home_team_id, away_team_id),
    ).fetchone()["id"]


def record_prediction(
    conn: sqlite3.Connection,
    *,
    model: str,
    player_id: int,
    season: str,
    predicted: float,
    matchday: int | None = None,
    horizon_date: str | None = None,
    baseline: float | None = None,
    detail: dict | None = None,
    made_on: str | None = None,
) -> None:
    """Guarda lo que el modelo dice hoy, para poder juzgarlo manana.

    Se reescribe si vuelve a correr el mismo dia -la captura de la tarde pisa a
    la de la madrugada- porque lo que interesa de un dia es su ultima palabra,
    con las alineaciones probables ya publicadas.
    """
    conn.execute(
        """
        INSERT INTO prediction
            (made_on, made_at, model, player_id, season, matchday, horizon_date,
             predicted, baseline, detail_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (model, made_on, player_id) DO UPDATE SET
            made_at = excluded.made_at,
            matchday = excluded.matchday,
            horizon_date = excluded.horizon_date,
            predicted = excluded.predicted,
            baseline = excluded.baseline,
            detail_json = excluded.detail_json
        """,
        (
            made_on or today(), utcnow(), model, player_id, season, matchday,
            horizon_date, predicted, baseline,
            json.dumps(detail, ensure_ascii=False) if detail else None,
        ),
    )


def record_schedule(
    conn: sqlite3.Connection,
    *,
    season: str,
    matchday: int,
    team_id: int,
    opponent_id: int,
    is_home: bool | None = None,
) -> None:
    """Contra quien juega un equipo una jornada, y donde si se sabe.

    La sede se guarda con COALESCE porque llega mas tarde y por otra via: el
    calendario de la ficha da el rival de quince jornadas pero no dice donde se
    juega, y eso solo aparece cuando ese partido pasa a ser el siguiente. Sin el
    COALESCE, la siguiente captura del calendario borraria la sede recien
    averiguada.
    """
    conn.execute(
        """
        INSERT INTO team_schedule (season, matchday, team_id, opponent_id, is_home)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (season, matchday, team_id) DO UPDATE SET
            opponent_id = excluded.opponent_id,
            is_home = COALESCE(excluded.is_home, team_schedule.is_home),
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (season, matchday, team_id, opponent_id,
         None if is_home is None else int(is_home)),
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
