"""Adapter de Mister: del JSON de Mister al modelo canonico.

Estado: el guardado en crudo es definitivo y funciona con cualquier formato.
El parseo es defensivo (acepta varios nombres posibles para cada campo) porque
todavia no se ha visto un payload real de Mister. En cuanto haya un HAR se
ajustan los nombres exactos en PARSERS y se reprocesa el historico con
`fh reprocesar`, sin haber perdido un solo dia de datos.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fantasyhelper.adapters.base import NotConfiguredError
from fantasyhelper.adapters.mister.client import MisterClient
from fantasyhelper.adapters.mister.endpoints import load_endpoints
from fantasyhelper.config import settings
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import transaction

log = logging.getLogger(__name__)

PROVIDER = "mister"

# Mister puede llamar a cada campo de varias formas segun el endpoint.
# Centralizado aqui para que ajustarlo sea cambiar una lista, no buscar por el codigo.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "playerId", "player_id", "idPlayer"),
    "name": ("name", "nombre", "playerName", "nickname"),
    "value": ("value", "marketValue", "market_value", "valor", "price"),
    "delta": ("delta", "variation", "valueChange", "diff"),
    "clause": ("clause", "clauseValue", "clausula", "buyoutClause"),
    "clause_until": ("clauseLockedEnd", "clause_until", "blindada_hasta"),
    "buy_price": ("buyPrice", "purchasePrice", "precio_compra"),
    "owner": ("ownerId", "owner", "userId", "managerId"),
    "position": ("position", "posicion", "role"),
    "team": ("teamId", "team", "equipo", "clubId"),
    "team_name": ("teamName", "team_name", "club"),
    "balance": ("balance", "money", "saldo", "budget"),
    "team_value": ("teamValue", "squadValue", "valor_equipo"),
    "points": ("points", "puntos", "score"),
    "manager_name": ("name", "nombre", "username", "displayName"),
    "seller": ("sellerId", "seller", "ownerId"),
    "asking_price": ("salePrice", "askingPrice", "price", "precio"),
    "expires": ("expiresAt", "expires", "endsAt"),
}

# Mister usa nomenclatura propia de posiciones; se normaliza a PT/DF/MC/DL.
POSITION_MAP = {
    "1": "PT", "gk": "PT", "portero": "PT", "goalkeeper": "PT",
    "2": "DF", "df": "DF", "defensa": "DF", "defender": "DF",
    "3": "MC", "mf": "MC", "medio": "MC", "midfielder": "MC",
    "4": "DL", "fw": "DL", "delantero": "DL", "forward": "DL",
}


def pick(data: dict[str, Any], field: str) -> Any:
    """Devuelve el primer alias presente para un campo logico."""
    for alias in FIELD_ALIASES.get(field, ()):
        if alias in data and data[alias] is not None:
            return data[alias]
    return None


def as_int(value: Any) -> int | None:
    """Normaliza dinero a euros enteros. Mister mezcla '12.500.000', 12500000 y '12,5M'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(" ", "").replace("€", "")
    multiplier = 1
    if text.lower().endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    elif text.lower().endswith("k"):
        multiplier, text = 1_000, text[:-1]
    text = text.replace(".", "") if text.count(".") > 1 else text
    text = text.replace(",", ".")
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def normalize_position(value: Any) -> str | None:
    if value is None:
        return None
    return POSITION_MAP.get(str(value).strip().lower())


def iter_records(payload: Any) -> list[dict[str, Any]]:
    """Extrae la lista de registros de una respuesta.

    Tolera las tres formas habituales: lista directa, {"data": [...]} y
    {"players": [...]}. Si no encuentra lista, devuelve vacio sin reventar.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "players", "items", "results", "records", "team", "squad"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # A veces el contenido util cuelga un nivel mas abajo.
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


class MisterAdapter:
    """Implementa el protocolo FantasyAdapter."""

    provider = PROVIDER

    def __init__(self, client: MisterClient | None = None) -> None:
        self.client = client or MisterClient()
        self.endpoints = load_endpoints()

    def login(self) -> None:
        self.client.login()

    # -- parsers -----------------------------------------------------------

    def _player_id(self, conn: sqlite3.Connection, record: dict[str, Any]) -> int | None:
        external_id = pick(record, "id")
        name = pick(record, "name")
        if external_id is None or not name:
            return None

        team_id = None
        if team_name := pick(record, "team_name"):
            team_id = repo.upsert_team(
                conn, name=str(team_name), provider=self.provider,
                external_id=str(pick(record, "team") or team_name),
            )

        return repo.resolve_player(
            conn,
            provider=self.provider,
            external_id=str(external_id),
            name=str(name),
            team_id=team_id,
            position=normalize_position(pick(record, "position")),
        )

    def parse_players(self, conn: sqlite3.Connection, payload: Any, league_id: int | None) -> int:
        """Catalogo de jugadores con su valor de mercado: la serie temporal clave."""
        rows = 0
        for record in iter_records(payload):
            player_id = self._player_id(conn, record)
            value = as_int(pick(record, "value"))
            if player_id is None or value is None:
                continue
            repo.record_player_value(
                conn,
                provider=self.provider,
                player_id=player_id,
                market_value=value,
                delta_1d=as_int(pick(record, "delta")),
            )
            rows += 1
        return rows

    def parse_squad(self, conn: sqlite3.Connection, payload: Any, league_id: int | None) -> int:
        """Plantillas y clausulas: la base del radar de clausulas."""
        if league_id is None:
            return 0
        rows = 0
        for record in iter_records(payload):
            player_id = self._player_id(conn, record)
            if player_id is None:
                continue
            owner_external = pick(record, "owner")
            manager_id = None
            if owner_external is not None:
                manager_id = repo.upsert_manager(
                    conn, league_id=league_id, external_id=str(owner_external),
                    name=str(pick(record, "manager_name") or owner_external),
                )
            repo.record_ownership(
                conn,
                league_id=league_id,
                player_id=player_id,
                manager_id=manager_id,
                clause_value=as_int(pick(record, "clause")),
                clause_locked_until=pick(record, "clause_until"),
                buy_price=as_int(pick(record, "buy_price")),
            )
            rows += 1
        return rows

    def parse_market(self, conn: sqlite3.Connection, payload: Any, league_id: int | None) -> int:
        if league_id is None:
            return 0
        rows = 0
        for record in iter_records(payload):
            player_id = self._player_id(conn, record)
            if player_id is None:
                continue
            seller_external = pick(record, "seller")
            seller_id = None
            if seller_external is not None:
                seller_id = repo.upsert_manager(
                    conn, league_id=league_id, external_id=str(seller_external),
                    name=str(seller_external),
                )
            repo.record_market_listing(
                conn,
                league_id=league_id,
                player_id=player_id,
                seller_id=seller_id,
                asking_price=as_int(pick(record, "asking_price")),
                expires_at=pick(record, "expires"),
            )
            rows += 1
        return rows

    def parse_standings(self, conn: sqlite3.Connection, payload: Any, league_id: int | None) -> int:
        """Saldo de cada rival: sin esto no se puede saber quien puede clausularte."""
        if league_id is None:
            return 0
        rows = 0
        for position, record in enumerate(iter_records(payload), start=1):
            external_id = pick(record, "id")
            if external_id is None:
                continue
            manager_id = repo.upsert_manager(
                conn, league_id=league_id, external_id=str(external_id),
                name=str(pick(record, "manager_name") or external_id),
            )
            repo.record_manager_state(
                conn,
                manager_id=manager_id,
                balance=as_int(pick(record, "balance")),
                team_value=as_int(pick(record, "team_value")),
                points=as_int(pick(record, "points")),
                position=position,
            )
            rows += 1
        return rows

    # -- snapshot ----------------------------------------------------------

    def snapshot(self, conn: sqlite3.Connection) -> int:
        parsers = {
            "players": self.parse_players,
            "squad": self.parse_squad,
            "market": self.parse_market,
            "standings": self.parse_standings,
            "teams": self.parse_squad,  # mismo formato, plantillas de rivales
        }

        if not self.endpoints:
            raise NotConfiguredError(
                "No hay endpoints de Mister configurados. Ejecuta primero:\n"
                "  fh mister har <fichero.har>"
            )

        self.login()

        league_id = None
        if settings.mister_league_id:
            league_id = repo.upsert_league(
                conn,
                provider=self.provider,
                external_id=settings.mister_league_id,
                name=f"Liga {settings.mister_league_id}",
                season=settings.season,
            )

        total = 0
        for key, endpoint in self.endpoints.items():
            try:
                payload = self.client.fetch(
                    conn, endpoint, league_id=settings.mister_league_id or ""
                )
            except Exception as exc:  # un endpoint roto no debe tumbar el snapshot
                log.error("fallo capturando '%s': %s", key, exc)
                continue

            if payload is None:
                continue

            parser = parsers.get(key)
            if parser is None:
                log.info("'%s' guardado en crudo (sin parser todavia)", key)
                continue

            try:
                # Cada endpoint entra completo o no entra; el crudo ya esta fuera
                # de la transaccion, asi que un parser roto nunca pierde datos.
                with transaction(conn):
                    rows = parser(conn, payload, league_id)
            except Exception as exc:
                log.error("fallo parseando '%s' (crudo a salvo): %s", key, exc)
                continue

            log.info("%-10s -> %d filas", key, rows)
            total += rows

        return total
