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

import json
import logging
import sqlite3

from fantasyhelper import reconcile
from fantasyhelper.adapters.mister.client import MisterClient
from fantasyhelper.adapters.mister.endpoints import load_endpoints
from fantasyhelper.adapters.mister.parsers import (
    MisterPlayer,
    parse_feed,
    parse_matchday_points,
    parse_next_fixture,
    parse_player_search,
    parse_players,
    parse_schedule,
    parse_season_history,
    parse_standings,
    parse_team_names,
    parse_user_config,
    parse_user_squad,
    parse_value_history,
    real_team_id,
)
from fantasyhelper.config import settings
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import transaction
from fantasyhelper.storage.raw import iter_raw
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
            # Estas paginas dan el equipo por numero, sin nombre. Si ya sabemos
            # a que equipo corresponde ese numero -por la ficha del jugador, que
            # si trae el nombre, o por el crosswalk- se reutiliza.
            #
            # Reutilizarlo no es una optimizacion: crear aqui un equipo nuevo
            # llamado 'mister-team-9' reescribia el alias y deshacia en cada
            # captura la unificacion que hubiera hecho `fh reconciliar`.
            team_id = repo.team_id_for_alias(
                conn, provider=self.provider, external_id=player.team_external_id
            )
        if team_id is None and player.team_external_id:
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

    def store_player_card(
        self, conn: sqlite3.Connection, payload: dict, player_id: int
    ) -> dict[str, int]:
        """Vuelca todo lo que trae la ficha de un jugador.

        La ficha es con diferencia la respuesta mas rica de Mister y durante un
        tiempo solo se le saco el grafico de valores. Ademas de eso trae el
        rendimiento de hasta cinco temporadas, los puntos por jornada de la
        actual y el proximo partido con sede y hora, que es de donde sale el
        calendario. Se guarda todo de una vez porque la peticion ya esta hecha.
        """
        escrito = {
            "valores": 0, "temporadas": 0, "jornadas": 0,
            "partidos": 0, "calendario": 0,
        }

        # Lo primero, porque todo lo demas resuelve equipos por alias: aqui es
        # donde 'mister-team-9' pasa a llamarse Sevilla.
        nombres = parse_team_names(payload)
        equipo = ((payload.get("data") or {}).get("player") or {}).get("team") or {}
        # `real_team_id` y no el id a secas: el equipo 0 de Mister significa "sin
        # club". Enlazarlo era lo que creaba un alias '0' que luego la
        # reconciliacion fundia con un equipo real, y desde ahi cualquier
        # jugador sin club pasaba a ser de ese equipo.
        propio = real_team_id(equipo.get("id"))
        canonico = conn.execute(
            "SELECT team_id FROM player WHERE id = ?", (player_id,)
        ).fetchone()
        del_jugador = canonico["team_id"] if canonico else None
        self._resolve_team(conn, player_id, propio, del_jugador, nombres)

        for external_id, name in nombres.items():
            if external_id == propio:
                continue
            if repo.team_id_for_alias(
                conn, provider=self.provider, external_id=external_id
            ) is None:
                repo.upsert_team(
                    conn, name=name, provider=self.provider, external_id=external_id
                )

        for date, value in parse_value_history(payload):
            repo.record_player_value(
                conn, provider=self.provider, source=self.provider,
                player_id=player_id, market_value=value, snapshot_date=date,
            )
            escrito["valores"] += 1

        for stat in parse_season_history(payload):
            team_id = None
            if stat.team_external_id:
                team_id = repo.team_id_for_alias(
                    conn, provider=self.provider, external_id=stat.team_external_id
                )
            repo.record_season_stat(
                conn, provider=self.provider, player_id=player_id,
                season=stat.season, points=stat.points,
                avg_points=stat.avg_points, matches_played=stat.matches_played,
                team_id=team_id,
            )
            escrito["temporadas"] += 1

        for matchday, points in parse_matchday_points(payload):
            repo.record_player_points(
                conn, provider=self.provider, player_id=player_id,
                season=settings.season, matchday=matchday, points=points,
            )
            escrito["jornadas"] += 1

        if (fixture := parse_next_fixture(payload)) is not None:
            home = repo.team_id_for_alias(
                conn, provider=self.provider, external_id=fixture.home_external_id
            )
            away = repo.team_id_for_alias(
                conn, provider=self.provider, external_id=fixture.away_external_id
            )
            # Solo si ambos equipos ya estan en el crosswalk; si no, el partido
            # se recogera en la siguiente captura, cuando el catalogo los traiga.
            if home and away:
                repo.upsert_fixture(
                    conn, season=settings.season, matchday=fixture.matchday,
                    home_team_id=home, away_team_id=away,
                    kickoff_utc=fixture.kickoff_utc,
                )
                # El mismo partido, visto desde cada equipo. Aqui si se sabe la
                # sede, asi que rellena la que el calendario deja en blanco.
                for equipo_id, rival_id, en_casa in (
                    (home, away, True), (away, home, False)
                ):
                    repo.record_schedule(
                        conn, season=settings.season, matchday=fixture.matchday,
                        team_id=equipo_id, opponent_id=rival_id, is_home=en_casa,
                    )
                escrito["partidos"] += 1

        escrito["calendario"] += self._store_schedule(conn, payload, player_id)
        return escrito

    def _resolve_team(
        self,
        conn: sqlite3.Connection,
        player_id: int,
        propio: str | None,
        del_jugador: int | None,
        nombres: dict[str, str],
    ) -> None:
        """Casa el numero de equipo de la ficha con el equipo canonico.

        Hay dos formas de leer la misma ficha y no dan lo mismo:

          - el numero de equipo es nuevo -> lo que sabemos es el equipo del
            jugador, y sirve para ponerle nombre a ese numero. Es lo que
            convierte 'mister-team-9' en Sevilla.
          - el numero de equipo ya se conoce -> manda el, y lo que ha cambiado
            es el jugador: se ha traspasado.

        Confundirlas costo caro. Se repuntaba el alias al equipo del jugador
        SIEMPRE, asi que cuando Moussa Diarra se fue del Alaves al Malaga, el
        alias del Malaga paso a apuntar al Alaves, y con el se llevo el
        calendario entero: el Alaves aparecia jugando contra los rivales del
        Malaga en dieciseis jornadas seguidas.
        """
        if not propio:
            return

        conocido = repo.team_id_for_alias(
            conn, provider=self.provider, external_id=propio
        )

        if conocido is None:
            # Numero sin identificar: el jugador le pone nombre.
            if del_jugador:
                reconcile.link_team_alias(
                    conn, provider=self.provider,
                    external_id=propio, team_id=del_jugador,
                )
                conocido = del_jugador
        elif del_jugador and del_jugador != conocido:
            # `resolve_player` fija el equipo la primera vez y no lo pisa nunca,
            # asi que sin esto un traspaso no se entera nadie.
            log.info(
                "el jugador %s cambia de equipo: %s -> %s",
                player_id, del_jugador, conocido,
            )
            repo.set_player_team(conn, player_id=player_id, team_id=conocido)

        if conocido and nombres.get(propio):
            repo.rename_team(conn, team_id=conocido, name=nombres[propio])

    def _store_schedule(
        self, conn: sqlite3.Connection, payload: dict, player_id: int
    ) -> int:
        """Guarda el calendario de rivales del equipo de este jugador.

        Con una ficha por equipo basta para tener la rejilla entera, pero se
        graba en todas: son escrituras idempotentes y asi el calendario esta
        completo aunque un dia fallen algunas peticiones.
        """
        externo, partidos = parse_schedule(payload)
        if not partidos:
            return 0

        # El equipo lo manda la FICHA, no lo que la base de datos crea que es el
        # equipo del jugador. Esos rivales son los del equipo que la ficha dice,
        # por construccion.
        #
        # Fiarse del jugador estuvo mal y se noto: Moussa Diarra se habia ido
        # del Alaves al Malaga y su `team_id` seguia en Alaves, asi que el
        # calendario del Malaga se escribio encima del del Alaves y este quedo
        # jugando contra rivales que no eran los suyos.
        equipo = (
            repo.team_id_for_alias(conn, provider=self.provider, external_id=externo)
            if externo
            else None
        )
        if equipo is None:
            fila = conn.execute(
                "SELECT team_id FROM player WHERE id = ?", (player_id,)
            ).fetchone()
            equipo = fila["team_id"] if fila else None
        if equipo is None:
            return 0

        escritos = 0
        for partido in partidos:
            rival = repo.team_id_for_alias(
                conn, provider=self.provider,
                external_id=partido.opponent_external_id,
            )
            # Un equipo no juega contra si mismo: si el escudo del rival
            # coincide con el propio es que el alias aun apunta mal, y escribir
            # eso ensuciaria el calendario con partidos imposibles.
            if rival is None or rival == equipo:
                continue
            repo.record_schedule(
                conn, season=settings.season, matchday=partido.matchday,
                team_id=equipo, opponent_id=rival,
            )
            escritos += 1
        return escritos

    def reprocess_cards(self, conn: sqlite3.Connection) -> tuple[int, dict[str, int]]:
        """Relee las fichas ya descargadas y extrae lo que en su dia no se guardo.

        Esta es la razon de guardar el crudo. Las fichas se bajaron para sacar
        el grafico de valores, y traian ademas el rendimiento por temporada y
        el calendario; recuperarlo ahora no cuesta ni una peticion.
        """
        alias = {
            fila["external_id"]: fila["player_id"]
            for fila in conn.execute(
                "SELECT external_id, player_id FROM player_alias WHERE provider = 'mister'"
            )
        }

        procesadas = 0
        total = {
            "valores": 0, "temporadas": 0, "jornadas": 0,
            "partidos": 0, "calendario": 0,
        }
        for _, endpoint, content in iter_raw(
            conn, source=self.provider, endpoint_like="ajax/players/%"
        ):
            player_id = alias.get(endpoint.rsplit("/", 1)[-1])
            if player_id is None:
                continue
            try:
                payload = json.loads(content)
            except ValueError:
                log.warning("ficha ilegible en %s", endpoint)
                continue

            with transaction(conn):
                escrito = self.store_player_card(conn, payload, player_id)
            procesadas += 1
            for clave, valor in escrito.items():
                total[clave] += valor

        return procesadas, total

    def backfill_values(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int | None = None,
        skip_done: bool = True,
    ) -> tuple[int, dict[str, int]]:
        """Recorre la ficha de cada jugador y guarda todo lo que trae.

        Una peticion por jugador, asi que es lento. Es reanudable: por defecto
        salta a los que ya tienen historico, de modo que si se corta a la mitad
        basta con volver a lanzarlo.

        Devuelve (jugadores procesados, filas escritas por concepto).
        """
        # Solo se puede pedir la ficha de los jugadores de los que conocemos su
        # id en Mister, que son los que han aparecido en catalogo, mercado o
        # alguna plantilla.
        sql = """
            SELECT a.external_id, a.player_id, p.name
            FROM player_alias a
            JOIN player p ON p.id = a.player_id
            WHERE a.provider = 'mister'
        """
        if skip_done:
            # "Ya esta hecho" = tiene valores de dias anteriores a hoy Y consta
            # su rendimiento por temporada. Lo segundo empezo a guardarse
            # despues, asi que sin ello los ya descargados quedarian fuera.
            sql += """
              AND NOT (
                EXISTS (
                  SELECT 1 FROM player_value_snapshot v
                  WHERE v.player_id = a.player_id AND v.provider = 'mister'
                    AND v.snapshot_date < date('now'))
                AND EXISTS (
                  SELECT 1 FROM player_season_stat s
                  WHERE s.player_id = a.player_id AND s.provider = 'mister')
              )
            """
        sql += " ORDER BY p.name"
        if limit:
            sql += f" LIMIT {int(limit)}"

        targets = conn.execute(sql).fetchall()
        log.info("ficha pendiente para %d jugadores", len(targets))

        processed = 0
        total = {
            "valores": 0, "temporadas": 0, "jornadas": 0,
            "partidos": 0, "calendario": 0,
        }
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

            with transaction(conn):
                escrito = self.store_player_card(conn, payload, target["player_id"])
            processed += 1
            for clave, valor in escrito.items():
                total[clave] += valor
            log.info(
                "  %-26s %d dias, %d temporadas",
                target["name"][:26], escrito["valores"], escrito["temporadas"],
            )

        return processed, total

    def _store_standings(
        self, conn: sqlite3.Connection, html: bytes, league_id: int
    ) -> int:
        rows = 0
        for manager in parse_standings(html):
            manager_id = repo.upsert_manager(
                conn, league_id=league_id, external_id=manager.external_id,
                name=manager.name,
            )
            repo.record_manager_avatar(
                conn, manager_id=manager_id, url=manager.avatar_url,
                color=manager.avatar_color, initials=manager.avatar_initials,
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
                conn,
                manager_id=manager_id,
                balance=self.user.balance,
                future_balance=self.user.future_balance,
                max_debt=self.user.max_debt,
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

    def _store_feed(
        self, conn: sqlite3.Connection, html: bytes, league_id: int | None
    ) -> int:
        nuevos = 0
        for event in parse_feed(html):
            if repo.record_feed_event(
                conn,
                league_id=league_id,
                external_id=event.external_id,
                kind=event.kind,
                summary=event.summary,
                html=event.html,
                relative_time=event.relative_time,
                player_ids=event.player_ids,
                user_ids=event.user_ids,
                amounts=event.amounts,
            ):
                nuevos += 1
                log.debug("movimiento nuevo [%s] %s", event.kind, event.summary[:80])
        return nuevos

    def _parse_into_db(
        self, conn: sqlite3.Connection, key: str, html: bytes, league_id: int | None
    ) -> int:
        if key == "feed":
            return self._store_feed(conn, html, league_id)
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
