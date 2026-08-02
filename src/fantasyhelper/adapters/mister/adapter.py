"""Adapter de Mister: del HTML de Mister al modelo canonico.

Reparto de responsabilidades:
    client.py   habla HTTP y guarda el crudo
    parsers.py  convierte HTML en dataclasses, sin tocar la base de datos
    adapter.py  (esto) traduce esas dataclasses al modelo canonico

Que aporta Mister sobre lo que ya da FutbolFantasy: lo privado de tu liga.
Quien tiene a quien, el mercado de tu liga y la clasificacion de tus rivales.
Los valores de mercado los da tambien, y sirven para contrastar (se ha visto
una diferencia de 4.000 € en Mbappe entre ambas fuentes el mismo dia).
"""

from __future__ import annotations

import logging
import sqlite3

from fantasyhelper.adapters.mister.client import MisterClient
from fantasyhelper.adapters.mister.endpoints import load_endpoints
from fantasyhelper.adapters.mister.parsers import (
    MisterPlayer,
    parse_players,
    parse_standings,
)
from fantasyhelper.config import settings
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import transaction

log = logging.getLogger(__name__)

PROVIDER = "mister"


class MisterAdapter:
    """Implementa el protocolo FantasyAdapter."""

    provider = PROVIDER

    def __init__(self, client: MisterClient | None = None) -> None:
        self.client = client or MisterClient()
        self.endpoints = load_endpoints()

    def login(self) -> None:
        self.client.login()

    # -- traduccion al modelo canonico -------------------------------------

    def _player_id(self, conn: sqlite3.Connection, player: MisterPlayer) -> int:
        team_id = None
        if player.team_external_id:
            # Mister identifica los equipos por numero, sin darnos el nombre en
            # estas paginas. Se registra con el id como nombre provisional; el
            # crosswalk lo unifica con el nombre real que si da FutbolFantasy.
            team_id = repo.upsert_team(
                conn,
                name=f"mister-team-{player.team_external_id}",
                provider=self.provider,
                external_id=player.team_external_id,
            )
        return repo.resolve_player(
            conn,
            provider=self.provider,
            external_id=player.external_id,
            name=player.name,
            team_id=team_id,
            position=player.position,
        )

    def _store_values(
        self, conn: sqlite3.Connection, players: list[MisterPlayer]
    ) -> int:
        rows = 0
        for player in players:
            if player.market_value is None:
                continue
            player_id = self._player_id(conn, player)
            repo.record_player_value(
                conn,
                provider=self.provider,
                source=self.provider,
                player_id=player_id,
                market_value=player.market_value,
            )
            rows += 1
        return rows

    def _store_ownership(
        self, conn: sqlite3.Connection, players: list[MisterPlayer], league_id: int
    ) -> int:
        rows = 0
        for player in players:
            player_id = self._player_id(conn, player)
            manager_id = None
            if player.owner_id:
                manager_id = repo.upsert_manager(
                    conn, league_id=league_id, external_id=player.owner_id,
                    name=player.owner_id,
                )
            repo.record_ownership(
                conn, league_id=league_id, player_id=player_id, manager_id=manager_id
            )
            if player.market_value is not None:
                repo.record_player_value(
                    conn, provider=self.provider, source=self.provider,
                    player_id=player_id, market_value=player.market_value,
                )
            rows += 1
        return rows

    def _store_market(
        self, conn: sqlite3.Connection, players: list[MisterPlayer], league_id: int
    ) -> int:
        rows = 0
        for player in players:
            player_id = self._player_id(conn, player)
            seller_id = None
            if player.owner_id:
                seller_id = repo.upsert_manager(
                    conn, league_id=league_id, external_id=player.owner_id,
                    name=player.owner_id,
                )
            repo.record_market_listing(
                conn,
                league_id=league_id,
                player_id=player_id,
                seller_id=seller_id,
                # En el mercado, si no hay precio de venta explicito el precio
                # de salida es el valor de mercado.
                asking_price=player.asking_price or player.market_value,
                expires_at=player.ends_at,
            )
            rows += 1
        return rows

    def _store_standings(
        self, conn: sqlite3.Connection, html: bytes, league_id: int
    ) -> int:
        rows = 0
        for manager in parse_standings(html):
            manager_id = repo.upsert_manager(
                conn, league_id=league_id, external_id=manager.external_id,
                name=manager.name,
            )
            repo.record_manager_state(
                conn,
                manager_id=manager_id,
                team_value=manager.team_value,
                points=manager.points,
                position=manager.position,
            )
            rows += 1
        return rows

    # -- snapshot ----------------------------------------------------------

    def snapshot(self, conn: sqlite3.Connection) -> int:
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
        else:
            log.warning(
                "MISTER_LEAGUE_ID sin configurar: se capturaran valores de mercado "
                "pero no plantillas, mercado ni clasificacion."
            )

        total = 0
        for key, endpoint in self.endpoints.items():
            # Todo salvo el catalogo depende de saber en que liga estamos.
            if key != "search" and league_id is None:
                continue

            try:
                html = self.client.fetch(conn, endpoint)
            except Exception as exc:
                log.error("fallo capturando '%s': %s", key, exc)
                continue

            try:
                # El crudo ya esta guardado fuera de la transaccion, asi que un
                # parser roto nunca pierde el dato del dia.
                with transaction(conn):
                    rows = self._parse_into_db(conn, key, html, league_id)
            except Exception as exc:
                log.error("fallo parseando '%s' (crudo a salvo): %s", key, exc)
                continue

            log.info("%-10s -> %d filas", key, rows)
            total += rows

        return total

    def _parse_into_db(
        self, conn: sqlite3.Connection, key: str, html: bytes, league_id: int | None
    ) -> int:
        if key == "search":
            return self._store_values(conn, parse_players(html))
        if key == "squad":
            return self._store_ownership(conn, parse_players(html), league_id)
        if key == "market":
            return self._store_market(conn, parse_players(html), league_id)
        if key == "standings":
            return self._store_standings(conn, html, league_id)
        log.info("'%s' guardado en crudo (sin parser todavia)", key)
        return 0
