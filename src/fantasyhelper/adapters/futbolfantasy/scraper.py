"""Scraper de FutbolFantasy: probabilidad de once, estado y valores de mercado.

Resulta ser mucho mas util de lo previsto. Las paginas de equipo
(/laliga/equipos/<slug>) traen, por jugador, un bloque con todo en atributos
`data-*` ya normalizados:

    data-nombre            slug estable del jugador
    data-probabilidad      probabilidad de ser titular
    data-valor-mister      valor de mercado en Mister, en euros
    data-valor-diff-mister variacion del dia
    data-jerarquia         importancia en el equipo
    data-rival_dif_index   dificultad del rival (1 facil - 5 dificil)
    data-lesion / data-sancionado / data-apercibido
    data-bpp / data-bpfd / data-bpc   lanzador de penaltis / faltas / corners

Consecuencia practica: el historico de valores de Mister se puede capturar sin
tener sesion de Mister. La sesion solo hace falta para lo privado de tu liga
(tu plantilla, las de los rivales, clausulas y saldos).

Se lee de los data-* y NO del texto de las columnas: el orden de columnas del
HTML no coincide con el de la cabecera, asi que parsear por posicion daria
valores cruzados.

Uso responsable: su robots.txt permite el rastreo completo, aun asi se va a
2 segundos por peticion, con cache de 6 horas y User-Agent identificable.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Self

import httpx
from bs4 import BeautifulSoup, Tag

from fantasyhelper.config import settings
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import transaction
from fantasyhelper.storage.raw import load_raw_fresh, save_raw

log = logging.getLogger(__name__)

PROVIDER = "futbolfantasy"
BASE_URL = "https://www.futbolfantasy.com"
HUB_PATH = "/laliga/posibles-alineaciones"
TEAM_PATH = "/laliga/equipos/{slug}"
USER_AGENT = "FantasyHelper/0.1 (uso personal; contacto: isaacru04@gmail.com)"
MIN_INTERVAL = 2.0
CACHE_HOURS = 6

PLAYER_ID_RE = re.compile(r"jugador_(\d+)")
#: 1000 en los campos de balon parado significa "no es lanzador".
NO_SET_PIECE = 1000

#: Valores de mercado publicados por FutbolFantasy -> nombre de proveedor canonico.
VALUE_ATTRS = {
    "mister": "mister",
    "biwenger": "biwenger",
    "comunio": "comunio",
    "laliga-fantasy": "laliga_fantasy",
    "fantasy-marca": "fantasy_marca",
    "futmondo": "futmondo",
}

#: Se compara por prefijo porque la fuente alterna "Medio", "Mediocampista"
#: y "Centrocampista" para la misma posicion.
POSITION_PREFIXES = (
    ("porter", "PT"),
    ("defen", "DF"),
    ("medio", "MC"),
    ("centro", "MC"),
    ("delan", "DL"),
)


def normalize_position(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().lower()
    for prefix, code in POSITION_PREFIXES:
        if text.startswith(prefix):
            return code
    log.debug("posicion desconocida: %r", raw)
    return None


@dataclass
class PlayerRow:
    """Una fila de jugador ya normalizada."""

    external_id: str
    slug: str
    name: str
    team_name: str | None
    position: str | None
    probability: float | None
    status: str
    jerarquia: int | None = None
    form: float | None = None
    opponent: str | None = None
    opponent_difficulty: int | None = None
    is_home: bool | None = None
    penalties: int | None = None
    free_kicks: int | None = None
    corners: int | None = None
    values: dict[str, tuple[int, int | None]] = field(default_factory=dict)


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip().replace("%", "")
    if not text or text in {"-", "?"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _set_piece(value: str | None) -> int | None:
    """Orden de lanzamiento. FutbolFantasy usa 1000 para 'no lanza'."""
    number = _int(value)
    if number is None or number >= NO_SET_PIECE:
        return None
    return number


#: `data-lesion` no es un si/no: es la GRAVEDAD, y coincide al detalle con la
#: clase `gravedad-N` con la que FutbolFantasy pinta el semaforo de su bloque
#: "Estado fisico de la plantilla". Verificado cruzando las dos cosas jugador a
#: jugador en la pagina de equipo:
#:
#:   -1  sano, sin parte medico
#:    0  rojo    "Baja hasta diciembre"        rotura de cruzado, de menisco
#:    1  naranja "Duda para la jornada"        se esta recuperando
#:    2  verde   "Disponible para la jornada"  vuelve, pero acaba de volver
#:
#: Esto estuvo AL REVES y era grave: la condicion era `lesion > 0`, asi que el
#: rojo -el unico que de verdad no juega- se colaba como sano, y el verde -que
#: si juega- se descartaba como baja.
#:
#: Los nombres se eligen para que se lean solos en una etiqueta de movil, sin
#: tabla de traduccion por medio.
INJURY_STATUS = {0: "lesionado", 1: "tocado", 2: "de_vuelta"}


def _status(attrs: dict[str, str], probability: float | None) -> str:
    """Estado del jugador, de mas grave a menos.

    Se calcula a partir de las banderas y no del texto, que cambia de idioma
    y de maquetado. `data-lesion` vale -1 cuando el jugador esta sano.
    """
    if _int(attrs.get("data-sancionado")):
        return "sancionado"
    lesion = _int(attrs.get("data-lesion"))
    if lesion in INJURY_STATUS:
        return INJURY_STATUS[lesion]
    if _int(attrs.get("data-nodisponible")):
        return "no_disponible"
    if _int(attrs.get("data-vacaciones")) or _int(attrs.get("data-internacional")):
        return "ausente"
    if probability is not None and probability < 0.5:
        return "duda"
    return "ok"


class FutbolFantasyScraper:
    provider = PROVIDER

    def __init__(self) -> None:
        self._last_request = 0.0
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    # -- red ---------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def fetch_html(
        self, conn: sqlite3.Connection, path: str, endpoint: str, *, use_cache: bool = True
    ) -> bytes:
        if use_cache:
            cached = load_raw_fresh(
                conn, source=self.provider, endpoint=endpoint, max_age_hours=CACHE_HOURS
            )
            if cached is not None:
                log.debug("cache: %s", endpoint)
                return cached

        self._throttle()
        response = self._client.get(path)
        response.raise_for_status()
        save_raw(
            conn,
            source=self.provider,
            endpoint=endpoint,
            content=response.content,
            params={"path": path},
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type"),
        )
        return response.content

    # -- parseo ------------------------------------------------------------

    @staticmethod
    def parse_team_slugs(html: bytes | str) -> list[str]:
        """Extrae los slugs de equipo de la pagina indice."""
        soup = BeautifulSoup(html, "lxml")
        slugs: list[str] = []
        for link in soup.select('a[href*="/laliga/equipos/"]'):
            slug = link["href"].rstrip("/").rsplit("/", 1)[-1]
            if slug and slug not in slugs:
                slugs.append(slug)
        return slugs

    @staticmethod
    def parse_players(html: bytes | str, team_name: str | None = None) -> list[PlayerRow]:
        """Extrae las filas de jugador de una pagina de equipo."""
        soup = BeautifulSoup(html, "lxml")
        rows: list[PlayerRow] = []

        # La posicion no esta en la fila de la lista, sino en la camiseta del
        # campo. Se indexa por id de jugador para cruzarla despues.
        positions: dict[str, str] = {}
        for shirt in soup.select(".camiseta-wrapper"):
            match = PLAYER_ID_RE.search(" ".join(shirt.get("class") or []))
            raw = shirt.get("data-posicionmister") or shirt.get("data-posicion")
            if match and raw:
                positions[match.group(1)] = raw

        for node in soup.select(".jugador.tipo_lista"):
            if not isinstance(node, Tag) or "tipo_lista_header" in (node.get("class") or []):
                continue

            classes = " ".join(node.get("class") or [])
            match = PLAYER_ID_RE.search(classes)
            slug = node.get("data-nombre")
            if not match or not slug:
                continue

            attrs = {k: v for k, v in node.attrs.items() if isinstance(v, str)}

            name_node = node.select_one(".nombre")
            name = name_node.get_text(" ", strip=True) if name_node else slug.replace("-", " ")

            probability = None
            if (raw_prob := _int(attrs.get("data-probabilidad"))) is not None:
                probability = min(raw_prob, 100) / 100.0

            values: dict[str, tuple[int, int | None]] = {}
            for suffix, provider in VALUE_ATTRS.items():
                value = _int(attrs.get(f"data-valor-{suffix}"))
                if value:
                    values[provider] = (value, _int(attrs.get(f"data-valor-diff-{suffix}")))

            position = normalize_position(positions.get(match.group(1)))

            difficulty = _int(attrs.get("data-rival_dif_index"))
            locvis = attrs.get("data-locvis") or ""

            rows.append(
                PlayerRow(
                    external_id=match.group(1),
                    slug=slug,
                    name=name,
                    team_name=team_name or attrs.get("data-equipo"),
                    position=position,
                    probability=probability,
                    status=_status(attrs, probability),
                    jerarquia=_int(attrs.get("data-jerarquia")),
                    form=_int(attrs.get("data-forma_value")),
                    opponent=attrs.get("data-rival"),
                    opponent_difficulty=difficulty,
                    is_home="🏠" in locvis if locvis else None,
                    penalties=_set_piece(attrs.get("data-bpp")),
                    free_kicks=_set_piece(attrs.get("data-bpfd")),
                    corners=_set_piece(attrs.get("data-bpc")),
                    values=values,
                )
            )
        return rows

    # -- escritura ---------------------------------------------------------

    def _store(
        self, conn: sqlite3.Connection, rows: list[PlayerRow], matchday: int
    ) -> int:
        """Escribe un equipo entero como una unidad.

        La transaccion cubre solo el guardado (sin HTTP dentro), asi cada equipo
        entra completo o no entra, y un fallo en el equipo 19 no toca los 18 ya
        confirmados.
        """
        with transaction(conn):
            return self._store_rows(conn, rows, matchday)

    def _store_rows(
        self, conn: sqlite3.Connection, rows: list[PlayerRow], matchday: int
    ) -> int:
        written = 0
        for row in rows:
            team_id = None
            if row.team_name:
                team_id = repo.upsert_team(
                    conn, name=row.team_name, provider=self.provider, external_id=row.team_name
                )

            # El slug se deriva del nombre visible y NO de data-nombre: el slug
            # que genera FutbolFantasy se come las iniciales acentuadas
            # ('Álex Baena' -> 'lex-baena'), lo que impide cruzarlo con Mister.
            player_id = repo.resolve_player(
                conn,
                provider=self.provider,
                external_id=row.external_id,
                name=row.name,
                team_id=team_id,
                position=row.position,
            )

            if row.probability is not None:
                repo.record_lineup_probability(
                    conn,
                    player_id=player_id,
                    season=settings.season,
                    matchday=matchday,
                    probability=row.probability,
                    status=row.status,
                )

            repo.record_player_context(
                conn,
                player_id=player_id,
                season=settings.season,
                matchday=matchday,
                jerarquia=row.jerarquia,
                form=row.form,
                opponent=row.opponent,
                opponent_difficulty=row.opponent_difficulty,
                is_home=row.is_home,
                penalties=row.penalties,
                free_kicks=row.free_kicks,
                corners=row.corners,
            )

            for provider, (value, delta) in row.values.items():
                repo.record_player_value(
                    conn,
                    provider=provider,
                    source=self.provider,
                    player_id=player_id,
                    market_value=value,
                    delta_1d=delta,
                )
            written += 1
        return written

    def snapshot(self, conn: sqlite3.Connection, matchday: int) -> int:
        """Recorre las 20 paginas de equipo y guarda todo lo del dia."""
        hub = self.fetch_html(conn, HUB_PATH, "hub-alineaciones")
        slugs = self.parse_team_slugs(hub)
        if not slugs:
            log.error("no se encontraron equipos en el indice; revisa parse_team_slugs()")
            return 0

        log.info("%d equipos a capturar", len(slugs))
        total = 0
        for slug in slugs:
            try:
                html = self.fetch_html(
                    conn, TEAM_PATH.format(slug=slug), f"equipo/{slug}"
                )
                # El slug de la URL como nombre de equipo: es estable y legible,
                # mejor que la abreviatura de tres letras que trae el HTML.
                rows = self.parse_players(html, team_name=slug)
            except Exception as exc:
                # Un equipo que falle no debe abortar la captura de los otros 19.
                log.error("equipo '%s': %s", slug, exc)
                continue

            if not rows:
                log.warning("equipo '%s': 0 jugadores (crudo guardado, revisar selectores)", slug)
                continue

            total += self._store(conn, rows, matchday)
            log.info("  %-20s %d jugadores", slug, len(rows))
        return total

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
