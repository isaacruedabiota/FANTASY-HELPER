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
    parse_player_search,
    parse_players,
    parse_standings,
    parse_user_config,
    parse_user_squad,
    parse_value_history,
)
from fantasyhelper.config import settings
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import transaction
from fantasyhelper.utils.names import slugify

log = logging.getLogger(__name__)

PROVIDER = "mister"

#: Cuantos jugadores devuelve cada pagina del catalogo.
SEARCH_PAGE_SIZE = 50

#: Filtros que admite la busqueda. Se envian solo si se piden expresamente:
#: mandarlos a cero NO significa "sin filtrar". Los topes (value_to, clause_to)
#: se interpretan literalmente, asi que un 0 quiere decir "hasta 0 euros" y
#: deja la respuesta vacia. Costo un rato descubrirlo.
SEARCH_FILTERS = (
    "position", "value_from", "value_to", "clause_from", "clause_to",
    "team", "injured", "favs", "owner", "benched", "stealable",
)


def _search_form(*, offset: int, **filters: int) -> dict[str, object]:
    """Formulario de busqueda, serializado como lo hace jQuery.

    jQuery convierte {'filters': {'position': 1}} en 'filters[position]=1', de
    ahi los corchetes en las claves.
    """
    form: dict[str, object] = {"offset": offset, "order": 0, "name": ""}
    for name, value in filters.items():
        if name not in SEARCH_FILTERS:
            raise ValueError(f"filtro desconocido: {name}")
        form[f"filters[{name}]"] = value
    return form


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
            # El slug del enlace es lo unico que permite cruzar con las otras
            # fuentes: el nombre visible viene abreviado ("A. Sivera").
            slug=player.slug,
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
        """Guarda la propiedad de los jugadores de /team, que es MI plantilla.

        Y por eso mismo sirve para identificarme: el dueno de esos jugadores soy
        yo. Es la unica pista que da Mister sobre cual de los participantes es
        el de la sesion.
        """
        rows = 0
        for player in players:
            player_id = self._player_id(conn, player)
            manager_id = None
            if player.owner_id:
                manager_id = repo.upsert_manager(
                    conn, league_id=league_id, external_id=player.owner_id,
                    name=player.owner_id, is_me=True,
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

    def capture_squads(
        self,
        conn: sqlite3.Connection,
        league_id: int,
        preloaded: dict | None = None,
    ) -> int:
        """Recorre las plantillas de todos los participantes via API JSON.

        Es la pieza que alimenta el radar de clausulas: de cada rival se obtiene
        que jugadores tiene, que cuesta arrebatarselos y si estan blindados.
        Una peticion por participante, no por jugador.

        Los participantes se leen de la base de datos, asi que si entra gente
        nueva en la liga aparece sola en la siguiente captura.
        """
        managers = conn.execute(
            "SELECT id, external_id, name FROM manager WHERE league_id = ?",
            (league_id,),
        ).fetchall()

        # La plantilla del primero ya se descargo al averiguar la liga.
        preloaded_id = None
        if preloaded:
            preloaded_id = str((preloaded.get("data") or {}).get("id", ""))

        rows = 0
        for manager in managers:
            if preloaded is not None and manager["external_id"] == preloaded_id:
                payload = preloaded
            else:
                payload = self.client.fetch_json(
                    conn,
                    "users",
                    endpoint_key=f"ajax/users/{manager['external_id']}",
                    id=manager["external_id"],
                    slug=slugify(manager["name"]),
                    comments=0,
                )
            if not payload:
                continue

            squad = parse_user_squad(payload)
            with transaction(conn):
                actuales: set[int] = set()
                for player in squad.players:
                    player_id = self._player_id(conn, player)
                    actuales.add(player_id)
                    repo.record_ownership(
                        conn,
                        league_id=league_id,
                        player_id=player_id,
                        manager_id=manager["id"],
                        clause_value=player.clause_value,
                        buy_price=player.asking_price,
                        clause_level=player.clause_level,
                        clause_floor=player.clause_floor,
                    )
                    if player.market_value is not None:
                        repo.record_player_value(
                            conn, provider=self.provider, source=self.provider,
                            player_id=player_id, market_value=player.market_value,
                        )
                    rows += 1

                # La plantilla que acaba de llegar es la verdad: lo que quede de
                # una captura anterior y ya no este, sobra.
                fantasmas = repo.prune_ownership(
                    conn, league_id=league_id, manager_id=manager["id"],
                    keep_player_ids=actuales,
                )
                if fantasmas:
                    log.info("    %d jugadores ya no estan en su plantilla", fantasmas)

                if squad.manager.team_value is not None:
                    repo.record_manager_state(
                        conn,
                        manager_id=manager["id"],
                        team_value=squad.manager.team_value,
                    )
            log.info("  plantilla de %-18s %d jugadores", manager["name"], len(squad.players))
        return rows

    def capture_catalog(
        self, conn: sqlite3.Connection, league_id: int, *, max_pages: int = 30
    ) -> int:
        """Recorre el catalogo completo de jugadores, pagina a pagina.

        La pagina /search solo muestra los 50 primeros; el catalogo entero se
        obtiene del mismo endpoint JSON que la ficha de jugador, pasandole
        `offset`. Ademas el JSON trae el nombre completo y la clausula, que el
        HTML no da.
        """
        total = 0
        for page in range(max_pages):
            offset = page * SEARCH_PAGE_SIZE
            payload = self.client.fetch_json(
                conn,
                "players",
                endpoint_key=f"ajax/catalogo/{offset:05d}",
                **_search_form(offset=offset),
            )
            if not payload:
                break

            players = parse_player_search(payload)
            if not players:
                break

            with transaction(conn):
                for player in players:
                    player_id = self._player_id(conn, player)
                    if player.market_value is not None:
                        repo.record_player_value(
                            conn, provider=self.provider, source=self.provider,
                            player_id=player_id, market_value=player.market_value,
                        )
                    # El catalogo tambien dice de quien es cada jugador, lo que
                    # completa las plantillas de participantes que aun no hemos
                    # recorrido uno a uno.
                    manager_id = None
                    if player.owner_id:
                        manager_id = repo.upsert_manager(
                            conn, league_id=league_id, external_id=player.owner_id,
                            name=player.owner_name or player.owner_id,
                        )
                    repo.record_ownership(
                        conn, league_id=league_id, player_id=player_id,
                        manager_id=manager_id,
                        # Para un jugador libre, Mister devuelve como clausula su
                        # propio valor; eso no es una clausula y confundiria el radar.
                        clause_value=player.clause_value if manager_id else None,
                    )
                    total += 1

            log.info("  catalogo %5d-%-5d %d jugadores", offset, offset + len(players), len(players))
            # Una pagina incompleta significa que ya no hay mas.
            if len(players) < SEARCH_PAGE_SIZE:
                break
        return total

    def backfill_values(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int | None = None,
        skip_done: bool = True,
    ) -> tuple[int, int]:
        """Recupera el historico de valor de mercado que publica Mister.

        Una peticion por jugador, asi que es lento y se ejecuta una sola vez.
        Es reanudable: por defecto salta a los que ya tienen historico, de modo
        que si se corta a la mitad basta con volver a lanzarlo.

        Devuelve (jugadores procesados, filas escritas).
        """
        # Solo se puede pedir el historico de jugadores de los que conocemos su
        # id en Mister, que son los que han aparecido en catalogo, mercado o
        # alguna plantilla.
        sql = """
            SELECT a.external_id, a.player_id, p.name
            FROM player_alias a
            JOIN player p ON p.id = a.player_id
            WHERE a.provider = 'mister'
        """
        if skip_done:
            # "Ya tiene historico" = tiene valores de dias anteriores a hoy.
            sql += """
              AND NOT EXISTS (
                SELECT 1 FROM player_value_snapshot v
                WHERE v.player_id = a.player_id AND v.provider = 'mister'
                  AND v.snapshot_date < date('now'))
            """
        sql += " ORDER BY p.name"
        if limit:
            sql += f" LIMIT {int(limit)}"

        targets = conn.execute(sql).fetchall()
        log.info("historico pendiente para %d jugadores", len(targets))

        processed = rows = 0
        for target in targets:
            payload = self.client.fetch_json(
                conn,
                "players",
                endpoint_key=f"ajax/players/{target['external_id']}",
                id=target["external_id"],
                slug=slugify(target["name"]),
                comments=0,
            )
            if not payload:
                continue

            history = parse_value_history(payload)
            with transaction(conn):
                for date, value in history:
                    repo.record_player_value(
                        conn,
                        provider=self.provider,
                        source=self.provider,
                        player_id=target["player_id"],
                        market_value=value,
                        snapshot_date=date,
                    )
            processed += 1
            rows += len(history)
            log.info("  %-26s %d dias de historico", target["name"][:26], len(history))

        return processed, rows

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

    def _resolve_league(
        self, conn: sqlite3.Connection, standings_html: bytes | None
    ) -> tuple[int, dict | None]:
        """Averigua a que liga pertenece lo que estamos capturando.

        Ninguna ruta de Mister lleva el id de liga: la sesion decide cual esta
        activa. Pero el API JSON si lo publica como `userInfo.id_community`, y
        eso importa porque un mismo usuario puede jugar varias ligas a la vez:
        sin distinguirlas se mezclarian participantes de unas y otras.

        Devuelve tambien el payload ya descargado del primer participante para
        no tener que volver a pedirlo.
        """
        external_id = settings.mister_league_id
        payload = None
        self.user = None

        # La pagina completa trae `_FG_user`: saldo, liga activa y su nombre.
        # Es la unica via al saldo, y solo al propio: Mister no publica el de
        # los rivales por ningun sitio.
        try:
            self.user = parse_user_config(self.client.fetch_full_page(conn))
        except Exception as exc:
            log.warning("no se pudo leer la configuracion del usuario: %s", exc)

        if self.user and self.user.league_external_id:
            external_id = self.user.league_external_id

        managers = parse_standings(standings_html) if standings_html else []
        if managers:
            payload = self.client.fetch_json(
                conn,
                "users",
                endpoint_key=f"ajax/users/{managers[0].external_id}",
                id=managers[0].external_id,
                slug=slugify(managers[0].name),
                comments=0,
            )
            if payload and (detected := parse_user_squad(payload).league_external_id):
                if external_id and external_id != detected:
                    log.warning(
                        "la liga activa en Mister (%s) no es la configurada en "
                        "MISTER_LEAGUE_ID (%s); se usa la activa",
                        detected, external_id,
                    )
                external_id = detected

        external_id = external_id or "default"
        league_id = repo.upsert_league(
            conn,
            provider=self.provider,
            external_id=external_id,
            name=(self.user.league_name if self.user else None) or f"Liga {external_id}",
            season=settings.season,
        )

        # El saldo propio va al mismo sitio que el del resto de participantes,
        # aunque de los demas quede siempre a NULL.
        if self.user and self.user.balance is not None:
            manager_id = repo.upsert_manager(
                conn, league_id=league_id, external_id=self.user.external_id,
                name=self.user.name, is_me=True,
            )
            repo.record_manager_state(
                conn, manager_id=manager_id, balance=self.user.balance
            )
            log.info(
                "soy %s en '%s', saldo %s €",
                self.user.name, self.user.league_name, f"{self.user.balance:,}".replace(",", "."),
            )

        return league_id, payload

    def snapshot(self, conn: sqlite3.Connection) -> int:
        self.login()

        # Primero se descarga todo, y solo despues se decide bajo que liga se
        # guarda: el id de liga solo se conoce tras consultar el API JSON.
        pages: dict[str, bytes] = {}
        for key, endpoint in self.endpoints.items():
            try:
                pages[key] = self.client.fetch(conn, endpoint)
            except Exception as exc:
                log.error("fallo capturando '%s': %s", key, exc)

        league_id, first_squad = self._resolve_league(conn, pages.get("standings"))

        total = 0
        for key, html in pages.items():
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

        # El catalogo completo via API JSON. La pagina /search solo da 50, y de
        # aqui salen ademas los duenos de todos los jugadores de la liga.
        try:
            total += self.capture_catalog(conn, league_id)
        except Exception as exc:
            log.error("fallo capturando el catalogo: %s", exc)

        # Las plantillas van al final, cuando el catalogo y la clasificacion ya
        # han registrado a todos los participantes.
        try:
            total += self.capture_squads(conn, league_id, preloaded=first_squad)
        except Exception as exc:
            log.error("fallo capturando plantillas: %s", exc)

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
