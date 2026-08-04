"""Consultas de lectura sobre el modelo canonico.

Todo el SQL de analisis vive aqui y no en la CLI, para que la web de la Fase 2
consuma exactamente las mismas consultas y no haya dos versiones de la verdad.

Las funciones devuelven filas de sqlite3 y no imprimen nada: el formato es cosa
de quien las llame.
"""

from __future__ import annotations

import sqlite3

#: Probabilidad supuesta cuando la fuente no dice nada del jugador.
#: Deliberadamente baja: no saber si va a jugar no es lo mismo que jugar a
#: medias, y con 0.5 los desconocidos se colaban arriba en los rankings.
UNKNOWN_PROBABILITY = 0.2

#: Estados que descartan a un jugador como objetivo: nadie paga una clausula
#: por alguien que no puede jugar.
UNAVAILABLE = ("lesionado", "sancionado", "no_disponible")

#: Ultimo valor conocido de cada jugador.
#: Un jugador puede tener valor de dos procedencias el mismo dia (leido de
#: Mister y leido de FutbolFantasy); se prefiere el de Mister por ser la fuente
#: primaria, y el otro sirve de respaldo cuando aun no lo hemos visto en Mister.
LATEST_VALUE_CTE = """
latest_value AS (
    SELECT player_id, market_value, delta_1d, snapshot_date,
           ROW_NUMBER() OVER (
               PARTITION BY player_id
               ORDER BY snapshot_date DESC,
                        CASE source WHEN 'mister' THEN 0 ELSE 1 END
           ) AS rn
    FROM player_value_snapshot
    WHERE provider = 'mister'
)
"""

#: Ultima probabilidad de once y estado conocidos.
LATEST_PROB_CTE = """
latest_prob AS (
    SELECT player_id, probability, status, matchday,
           ROW_NUMBER() OVER (
               PARTITION BY player_id ORDER BY snapshot_date DESC, matchday DESC
           ) AS rn
    FROM lineup_probability_snapshot
)
"""

#: Ultimo contexto de jornada (rival, dificultad, jerarquia, balon parado).
LATEST_CONTEXT_CTE = """
latest_context AS (
    SELECT player_id, jerarquia, opponent, opponent_difficulty, is_home,
           penalties, free_kicks, corners,
           ROW_NUMBER() OVER (
               PARTITION BY player_id ORDER BY snapshot_date DESC, matchday DESC
           ) AS rn
    FROM player_context_snapshot
)
"""

#: Propiedad mas reciente de cada jugador dentro de una liga.
LATEST_OWNERSHIP_CTE = """
latest_ownership AS (
    SELECT player_id, manager_id, clause_value, clause_locked_until, snapshot_date,
           ROW_NUMBER() OVER (
               PARTITION BY player_id ORDER BY snapshot_date DESC
           ) AS rn
    FROM ownership_snapshot
)
"""

#: Valor de hace una semana, para la tendencia.
#: La variacion diaria que reporta la fuente suele venir vacia, pero con el
#: historico descargado la tendencia se calcula aqui y es mas informativa.
VALUE_7D_CTE = """
value_7d AS (
    SELECT player_id, MAX(market_value) AS market_value
    FROM player_value_snapshot
    WHERE provider = 'mister'
      AND snapshot_date = date(
          (SELECT MAX(snapshot_date) FROM player_value_snapshot), '-7 day')
    GROUP BY player_id
)
"""

BASE_CTES = (
    f"WITH {LATEST_VALUE_CTE}, {LATEST_PROB_CTE}, {LATEST_CONTEXT_CTE}, {VALUE_7D_CTE}"
)
OWNERSHIP_CTES = f"{BASE_CTES}, {LATEST_OWNERSHIP_CTE}"

#: Columnas comunes de una fila de jugador, ya unidas a sus CTEs.
PLAYER_COLUMNS = """
    p.id, p.name, p.position, t.name AS team,
    v.market_value, v.delta_1d,
    v.market_value - v7.market_value AS change_7d,
    lp.probability, lp.status,
    c.jerarquia, c.opponent, c.opponent_difficulty, c.is_home, c.penalties
"""

PLAYER_JOINS = """
    FROM player p
    LEFT JOIN team t ON t.id = p.team_id
    LEFT JOIN latest_value v ON v.player_id = p.id AND v.rn = 1
    LEFT JOIN latest_prob lp ON lp.player_id = p.id AND lp.rn = 1
    LEFT JOIN latest_context c ON c.player_id = p.id AND c.rn = 1
    LEFT JOIN value_7d v7 ON v7.player_id = p.id
"""


def my_manager(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """El participante que soy yo, identificado al capturar /team."""
    return conn.execute(
        "SELECT id, external_id, name, league_id FROM manager WHERE is_me = 1 LIMIT 1"
    ).fetchone()


def my_balance(conn: sqlite3.Connection) -> int | None:
    """Mi saldo disponible, o None si aun no se ha capturado.

    Mister solo publica el saldo del usuario de la sesion; el de los rivales no
    aparece en ninguna respuesta, asi que esta columna queda vacia para ellos.
    """
    row = conn.execute(
        """
        SELECT s.balance FROM manager_snapshot s
        JOIN manager m ON m.id = s.manager_id
        WHERE m.is_me = 1 AND s.balance IS NOT NULL
        ORDER BY s.snapshot_date DESC LIMIT 1
        """
    ).fetchone()
    return row["balance"] if row else None


def squad(conn: sqlite3.Connection, manager_id: int) -> list[sqlite3.Row]:
    """Plantilla de un participante, con valor, clausula y estado."""
    return conn.execute(
        f"""
        {OWNERSHIP_CTES}
        SELECT {PLAYER_COLUMNS}, o.clause_value
        {PLAYER_JOINS}
        JOIN latest_ownership o ON o.player_id = p.id AND o.rn = 1
        WHERE o.manager_id = ?
        ORDER BY
            CASE p.position WHEN 'PT' THEN 1 WHEN 'DF' THEN 2
                            WHEN 'MC' THEN 3 ELSE 4 END,
            v.market_value DESC
        """,
        (manager_id,),
    ).fetchall()


def market(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Mercado del dia: quien esta a la venta y a que precio."""
    return conn.execute(
        f"""
        {BASE_CTES}
        SELECT {PLAYER_COLUMNS}, m.asking_price, seller.name AS seller
        {PLAYER_JOINS}
        JOIN market_listing_snapshot m ON m.player_id = p.id
        LEFT JOIN manager seller ON seller.id = m.seller_id
        WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM market_listing_snapshot)
        ORDER BY lp.probability DESC NULLS LAST, m.asking_price ASC
        """
    ).fetchall()


def clause_targets(
    conn: sqlite3.Connection,
    *,
    manager_id: int | None = None,
    budget: int | None = None,
    min_probability: float = 0.6,
) -> list[sqlite3.Row]:
    """Radar de clausulas: jugadores de rivales ordenados por coste ajustado.

    El "coste ajustado" divide la clausula entre la probabilidad de jugar y la
    jerarquia del jugador en su equipo. No es todavia una prediccion de puntos
    (eso llega en la Fase 2), pero ya ordena por lo que se busca: pagar poco por
    alguien que va a jugar y que es importante en su equipo.
    """
    params: list[object] = [min_probability, *UNAVAILABLE]
    where = [
        "o.clause_value IS NOT NULL",
        "o.manager_id IS NOT NULL",
        "lp.probability >= ?",
        f"COALESCE(lp.status, 'ok') NOT IN ({', '.join('?' * len(UNAVAILABLE))})",
    ]
    if manager_id is not None:
        where.append("o.manager_id != ?")
        params.append(manager_id)
    if budget is not None:
        where.append("o.clause_value <= ?")
        params.append(budget)

    return conn.execute(
        f"""
        {OWNERSHIP_CTES}
        SELECT {PLAYER_COLUMNS}, o.clause_value, m.name AS owner,
               o.clause_value / (lp.probability * (1.0 + COALESCE(c.jerarquia, 0) / 100.0))
                   AS adjusted_cost
        {PLAYER_JOINS}
        JOIN latest_ownership o ON o.player_id = p.id AND o.rn = 1
        JOIN manager m ON m.id = o.manager_id
        WHERE {' AND '.join(where)}
        ORDER BY adjusted_cost ASC
        """,
        params,
    ).fetchall()


def clause_risk(conn: sqlite3.Connection, manager_id: int) -> list[sqlite3.Row]:
    """Mis jugadores mas apetecibles para un rival, es decir los que hay que blindar.

    Mismo criterio que el radar pero mirando hacia dentro: cuanto mejor sea el
    jugador y mas barata su clausula, mas facil es que te lo quiten. Se excluyen
    los no disponibles, porque estando lesionado o sancionado nadie los quiere.
    """
    placeholders = ", ".join("?" * len(UNAVAILABLE))
    return conn.execute(
        f"""
        {OWNERSHIP_CTES}
        SELECT {PLAYER_COLUMNS}, o.clause_value,
               o.clause_value / (COALESCE(lp.probability, ?)
                                 * (1.0 + COALESCE(c.jerarquia, 0) / 100.0)) AS adjusted_cost
        {PLAYER_JOINS}
        JOIN latest_ownership o ON o.player_id = p.id AND o.rn = 1
        WHERE o.manager_id = ? AND o.clause_value IS NOT NULL
          AND COALESCE(lp.status, 'ok') NOT IN ({placeholders})
        ORDER BY adjusted_cost ASC
        """,
        (UNKNOWN_PROBABILITY, manager_id, *UNAVAILABLE),
    ).fetchall()


def free_agents(
    conn: sqlite3.Connection, *, min_probability: float = 0.7, limit: int = 20
) -> list[sqlite3.Row]:
    """Jugadores sin dueno conocido con alta probabilidad de jugar, por valor."""
    return conn.execute(
        f"""
        {OWNERSHIP_CTES}
        SELECT {PLAYER_COLUMNS}
        {PLAYER_JOINS}
        LEFT JOIN latest_ownership o ON o.player_id = p.id AND o.rn = 1
        WHERE (o.manager_id IS NULL)
          AND lp.probability >= ?
          AND COALESCE(lp.status, 'ok') = 'ok'
          AND v.market_value IS NOT NULL
        ORDER BY v.market_value ASC
        LIMIT ?
        """,
        (min_probability, limit),
    ).fetchall()


def find_players(conn: sqlite3.Connection, term: str) -> list[sqlite3.Row]:
    """Busca jugadores por nombre, incluyendo como los llama cada fuente."""
    like = f"%{term}%"
    return conn.execute(
        f"""
        {OWNERSHIP_CTES}
        SELECT DISTINCT {PLAYER_COLUMNS}, o.clause_value, m.name AS owner
        {PLAYER_JOINS}
        LEFT JOIN latest_ownership o ON o.player_id = p.id AND o.rn = 1
        LEFT JOIN manager m ON m.id = o.manager_id
        WHERE p.name LIKE ? OR p.slug LIKE ? OR EXISTS (
            SELECT 1 FROM player_alias a
            WHERE a.player_id = p.id AND a.external_name LIKE ?)
        ORDER BY v.market_value DESC
        LIMIT 25
        """,
        (like, like, like),
    ).fetchall()


def value_history(
    conn: sqlite3.Connection, player_id: int, *, days: int = 90
) -> list[sqlite3.Row]:
    """Serie de valor de mercado de un jugador, del mas antiguo al mas reciente."""
    return conn.execute(
        """
        SELECT snapshot_date, market_value
        FROM player_value_snapshot
        WHERE player_id = ? AND provider = 'mister'
          AND snapshot_date >= date((SELECT MAX(snapshot_date) FROM player_value_snapshot),
                                    ?)
        GROUP BY snapshot_date
        ORDER BY snapshot_date
        """,
        (player_id, f"-{int(days)} day"),
    ).fetchall()


def standings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Clasificacion con el ultimo estado conocido de cada participante."""
    return conn.execute(
        """
        WITH last_state AS (
            SELECT manager_id, points, team_value, position,
                   ROW_NUMBER() OVER (
                       PARTITION BY manager_id ORDER BY snapshot_date DESC
                   ) AS rn
            FROM manager_snapshot
        )
        SELECT m.id, m.name, m.is_me, s.points, s.team_value, s.position,
               (SELECT COUNT(*) FROM ownership_snapshot o
                WHERE o.manager_id = m.id
                  AND o.snapshot_date = (SELECT MAX(snapshot_date)
                                         FROM ownership_snapshot)) AS squad_size
        FROM manager m
        LEFT JOIN last_state s ON s.manager_id = m.id AND s.rn = 1
        ORDER BY COALESCE(s.position, 999), m.name
        """
    ).fetchall()
