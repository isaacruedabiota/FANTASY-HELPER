"""Reconciliacion entre fuentes: unificar equipos y jugadores.

El cruce por slug resuelve la mayoria de los casos, pero no todos, porque cada
fuente escribe los nombres a su manera:

    Mister              FutbolFantasy         motivo
    pedri               pedri-gonzalez        apodo contra nombre completo
    isco                isco-alarcon          idem
    fede-valverde       federico-valverde     diminutivo
    alejandro-grimaldo  alex-grimaldo         nombre distinto, misma persona
    alvaro-nunez        lvaro-nunez           FutbolFantasy se come la inicial
                                              acentuada al generar su slug

Para esos casos hace falta comparar apellido + inicial, y eso solo es fiable
dentro del mismo equipo. Pero los equipos tampoco venian unificados: Mister
solo publica un numero (mister-team-2) y FutbolFantasy el nombre.

De ahi el orden de este modulo:
    1. Deducir que numero de Mister es que equipo, usando los jugadores que SI
       cruzaron por slug. Con decenas de coincidencias la mayoria es inequivoca.
    2. Fusionar los equipos duplicados.
    3. Reintentar los jugadores sueltos por apellido + inicial dentro del equipo
       ya unificado, aceptando solo cuando hay un unico candidato.

Los enlaces del paso 3 se marcan con confianza 0.7 para poder revisarlos con
`fh dudas`: es un match plausible, no una certeza.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from fantasyhelper.utils.names import strip_accents

log = logging.getLogger(__name__)

#: Confianza que se asigna a un enlace deducido por apellido + inicial.
FUZZY_CONFIDENCE = 0.7
#: Cuantos jugadores cruzados hacen falta para fiarse del mapeo de un equipo.
MIN_TEAM_EVIDENCE = 2


@dataclass
class ReconcileReport:
    teams_merged: int = 0
    players_linked: int = 0
    still_unmatched: int = 0


def _tokens(name: str) -> list[str]:
    return [t for t in strip_accents(name).lower().replace(".", " ").split() if t]


def names_match(left: str, right: str) -> bool:
    """Decide si dos formas de escribir un nombre son la misma persona.

    No es una simple comparacion de conjuntos porque la regla depende de como
    venga escrito cada lado:
      - 'A. Grimaldo' / 'Álex Grimaldo'   -> inicial contra nombre completo
      - 'Rafa Rodríguez' / 'Riki Rodríguez' -> dos nombres completos distintos,
        aunque compartan apellido e inicial: NO son el mismo
      - 'Pedri' / 'Pedri González'        -> apodo contra nombre compuesto
    """
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return False

    if "".join(left_tokens) == "".join(right_tokens):
        return True

    # Uno de los dos es un nombre de una sola palabra ('Pedri', 'Isco').
    if len(left_tokens) == 1 or len(right_tokens) == 1:
        single, other = (
            (left_tokens, right_tokens)
            if len(left_tokens) == 1
            else (right_tokens, left_tokens)
        )
        return single[0] == other[0]

    if left_tokens[-1] != right_tokens[-1]:
        return False

    first_left, first_right = left_tokens[0], right_tokens[0]
    if len(first_left) == 1 or len(first_right) == 1:
        # Un lado viene abreviado: basta con que coincida la inicial.
        return first_left[0] == first_right[0]
    # Los dos nombres vienen completos, asi que se exige mas que la inicial:
    # 'Rafa' y 'Riki' Rodriguez son dos personas distintas.
    return first_left.startswith(first_right) or first_right.startswith(first_left)


def infer_team_mapping(conn: sqlite3.Connection) -> dict[int, int]:
    """Deduce {equipo_de_mister: equipo_canonico} a partir de jugadores cruzados.

    Cada alias guarda el equipo segun su fuente, asi que para un jugador que ya
    cruzo basta con mirar que equipo dice cada una. Con varios jugadores por
    equipo la mayoria es inequivoca.
    """
    votes: dict[int, Counter] = defaultdict(Counter)
    for row in conn.execute(
        """
        SELECT ma.team_id AS mister_team, fa.team_id AS ff_team
        FROM player_alias ma
        JOIN player_alias fa
          ON fa.player_id = ma.player_id AND fa.provider = 'futbolfantasy'
        WHERE ma.provider = 'mister'
          AND ma.team_id IS NOT NULL AND fa.team_id IS NOT NULL
          AND ma.team_id != fa.team_id
        """
    ):
        votes[row["mister_team"]][row["ff_team"]] += 1

    mapping: dict[int, int] = {}
    for mister_team, counter in votes.items():
        best, count = counter.most_common(1)[0]
        if count >= MIN_TEAM_EVIDENCE:
            mapping[mister_team] = best
    return mapping


def merge_team(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Absorbe un equipo dentro de otro y borra el sobrante.

    Hay que repuntar TODAS las tablas que referencian al equipo antes de
    borrarlo, o la clave foranea aborta el borrado. En el calendario, ademas,
    los dos equipos pueden tener ya la misma jornada registrada -es el mismo
    partido visto dos veces-, y ahi 'OR REPLACE' se queda con una sola fila.
    """
    conn.execute("UPDATE player SET team_id = ? WHERE team_id = ?", (target_id, source_id))
    conn.execute("UPDATE player_alias SET team_id = ? WHERE team_id = ?", (target_id, source_id))
    conn.execute("UPDATE team_alias SET team_id = ? WHERE team_id = ?", (target_id, source_id))
    conn.execute(
        "UPDATE player_season_stat SET team_id = ? WHERE team_id = ?", (target_id, source_id)
    )
    for lado in ("home_team_id", "away_team_id"):
        conn.execute(
            f"UPDATE OR REPLACE fixture SET {lado} = ? WHERE {lado} = ?",
            (target_id, source_id),
        )
    conn.execute("DELETE FROM team WHERE id = ?", (source_id,))


def _has_players(conn: sqlite3.Connection, team_id: int) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM player WHERE team_id = ? LIMIT 1", (team_id,)
        ).fetchone()
    )


def link_team_alias(
    conn: sqlite3.Connection, *, provider: str, external_id: str, team_id: int
) -> None:
    """Apunta el equipo de una fuente al equipo canonico, absorbiendo el anterior.

    Es la forma fiable de saber que equipo es cada numero de Mister: en la ficha
    de un jugador vienen a la vez su equipo segun Mister y el jugador, cuyo
    equipo canonico ya conocemos por el crosswalk.

    Si el alias apuntaba a otro equipo y ese otro no tiene jugadores, era un
    duplicado creado a partir del nombre ('Athletic Club' frente a 'Athletic')
    y se fusiona aqui mismo. Sin esto los duplicados se acumulan y el calendario
    acaba con el mismo partido dos veces.
    """
    fila = conn.execute(
        "SELECT team_id FROM team_alias WHERE provider = ? AND external_id = ?",
        (provider, str(external_id)),
    ).fetchone()

    anterior = fila["team_id"] if fila else None
    if anterior is not None and anterior != team_id and not _has_players(conn, anterior):
        merge_team(conn, anterior, team_id)

    conn.execute(
        "INSERT INTO team_alias (team_id, provider, external_id) VALUES (?, ?, ?) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET team_id = excluded.team_id",
        (team_id, provider, str(external_id)),
    )


#: Nombre provisional que se le pone a un equipo del que solo sabemos el numero.
PLACEHOLDER_TEAM_RE = re.compile(r"^mister-team-(\d+)$")


def merge_placeholder_teams(conn: sqlite3.Connection) -> int:
    """Absorbe los equipos que solo tenian numero dentro de los que ya tienen nombre.

    Durante un tiempo el unico dato de equipo que daban las paginas de jugadores
    era un numero, y se creaba un equipo llamado 'mister-team-9' a la espera de
    cruzarlo con FutbolFantasy. La ficha del jugador si trae el nombre, asi que
    ahora el alias de ese numero apunta al equipo de verdad y el marcador sobra.

    No hace falta deducir nada: el propio nombre lleva el numero, y el alias de
    ese numero dice cual es el equipo bueno.
    """
    fusionados = 0
    for team in conn.execute(
        "SELECT id, slug FROM team WHERE slug LIKE 'mister-team-%'"
    ).fetchall():
        match = PLACEHOLDER_TEAM_RE.match(team["slug"])
        if not match:
            continue
        alias = conn.execute(
            "SELECT team_id FROM team_alias WHERE provider = 'mister' AND external_id = ?",
            (match.group(1),),
        ).fetchone()
        # Si el alias sigue apuntando al propio marcador es que aun no sabemos
        # como se llama ese equipo; se queda como esta.
        if alias and alias["team_id"] != team["id"]:
            merge_team(conn, team["id"], alias["team_id"])
            fusionados += 1
    return fusionados


def link_remaining(conn: sqlite3.Connection) -> tuple[int, int]:
    """Enlaza jugadores de Mister sin cruzar con su equivalente de FutbolFantasy.

    Devuelve (enlazados, sin_enlazar). Solo acepta candidato unico: ante dos
    posibles se deja sin enlazar antes que arriesgar una fusion incorrecta.
    """
    unmatched = conn.execute(
        """
        SELECT p.id, a.external_name AS name, a.team_id
        FROM player p
        JOIN player_alias a ON a.player_id = p.id AND a.provider = 'mister'
        WHERE NOT EXISTS (
            SELECT 1 FROM player_alias f
            WHERE f.player_id = p.id AND f.provider = 'futbolfantasy')
        """
    ).fetchall()

    if not unmatched:
        return 0, 0

    # Candidatos: jugadores que solo conoce FutbolFantasy, indexados por equipo.
    by_team: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT p.id, a.external_name AS name, a.team_id
        FROM player p
        JOIN player_alias a ON a.player_id = p.id AND a.provider = 'futbolfantasy'
        WHERE NOT EXISTS (
            SELECT 1 FROM player_alias m
            WHERE m.player_id = p.id AND m.provider = 'mister')
        """
    ):
        by_team[row["team_id"]].append(row)

    linked = 0
    for player in unmatched:
        candidates = [
            candidate
            for candidate in by_team.get(player["team_id"], [])
            if names_match(player["name"], candidate["name"])
        ]
        if len(candidates) != 1:
            continue

        target = candidates[0]
        _absorb_player(conn, source_id=player["id"], target_id=target["id"])
        log.info("enlazado: '%s' -> '%s'", player["name"], target["name"])
        # Un jugador ya enlazado deja de ser candidato para los siguientes.
        by_team[player["team_id"]].remove(target)
        linked += 1

    return linked, len(unmatched) - linked


def _absorb_player(conn: sqlite3.Connection, *, source_id: int, target_id: int) -> None:
    """Mueve alias y series temporales del jugador duplicado al canonico."""
    conn.execute(
        "UPDATE player_alias SET player_id = ?, confidence = ? WHERE player_id = ?",
        (target_id, FUZZY_CONFIDENCE, source_id),
    )
    for table in (
        "player_value_snapshot",
        "ownership_snapshot",
        "market_listing_snapshot",
        "lineup_probability_snapshot",
        "player_context_snapshot",
        "player_match_stat",
        "player_points",
    ):
        # OR IGNORE por si ambos jugadores ya tenian fila el mismo dia: la del
        # canonico manda y la duplicada se descarta.
        conn.execute(
            f"UPDATE OR IGNORE {table} SET player_id = ? WHERE player_id = ?",
            (target_id, source_id),
        )
        conn.execute(f"DELETE FROM {table} WHERE player_id = ?", (source_id,))
    conn.execute("DELETE FROM player WHERE id = ?", (source_id,))


def reconcile(conn: sqlite3.Connection) -> ReconcileReport:
    """Unifica equipos y jugadores entre fuentes. Idempotente."""
    report = ReconcileReport()

    # Primero los que se resuelven solos por el nombre que da la ficha, y
    # despues los que hay que deducir votando con jugadores ya cruzados.
    report.teams_merged = merge_placeholder_teams(conn)

    mapping = infer_team_mapping(conn)
    for source_id, target_id in mapping.items():
        merge_team(conn, source_id, target_id)
        report.teams_merged += 1
    if report.teams_merged:
        log.info("equipos fusionados: %d", report.teams_merged)

    report.players_linked, report.still_unmatched = link_remaining(conn)
    return report
