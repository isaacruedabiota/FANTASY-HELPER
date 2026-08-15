"""Parseo de los fragmentos HTML de Mister.

Mister no tiene API JSON: su web app hace POST a /team, /market, /standings y
/search con la cabecera `X-Requested-With: XMLHttpRequest` y recibe fragmentos
de HTML ya renderizado. Asi que se parsea HTML, igual que con FutbolFantasy.

La estructura es consistente entre las tres paginas de jugadores:

    <div class="player-row">
      <a class="player" href="players/58954/kylian-mbappe">   <- id y slug
        <div class="player-position" data-position="4">       <- 1 PT .. 4 DL
        <div class="player-avatar" data-id_player="58954">
        <div class="name">K. Mbappé</div>
        <div class="underName">€ 24.641.000 <span class="value-arrow red">
        <div class="avg">0,0</div>
      </a>
      <button data-id_owner="..." data-popup="bid|sale" data-text="...">
    </div>

El parseo se aisla aqui para poder probarlo contra fragmentos reales guardados
en tests/fixtures sin necesidad de sesion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Tag

from fantasyhelper.utils.names import slugify, strip_accents

log = logging.getLogger(__name__)

#: data-position en Mister -> codigo canonico.
POSITION_MAP = {"1": "PT", "2": "DF", "3": "MC", "4": "DL"}

#: El equipo numero 0 de Mister no es un equipo: es su forma de decir "ninguno".
#: Lo llama literalmente 'void' y se lo pone a quien no tiene club, como Ter
#: Stegen en pretemporada. Tomarlo por un equipo real fue un fallo con cola: se
#: creo un 'mister-team-0' que la reconciliacion acabo fundiendo con el Real
#: Madrid, y a partir de ahi todo jugador sin club era del Real Madrid.
VOID_TEAM_ID = "0"

PLAYER_HREF_RE = re.compile(r"players/(\d+)/([\w\-]+)")
#: Mister pega emojis al nombre como distintivo ("A. Grimaldo💥").
EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff️]+"
)
USER_HREF_RE = re.compile(r"users/(\d+)/([\w\-]+)")
TEAM_LOGO_RE = re.compile(r"/teams/(\d+)\.png")
#: "15 jugadores · € 25.054.000"
SQUAD_INFO_RE = re.compile(r"(\d+)\s*jugadores.*?€\s*([\d.]+)", re.DOTALL)


@dataclass
class MisterPlayer:
    external_id: str
    slug: str
    name: str
    position: str | None = None
    team_external_id: str | None = None
    market_value: int | None = None
    value_direction: str | None = None  # up | down | flat
    average: float | None = None
    owner_id: str | None = None
    asking_price: int | None = None
    ends_at: str | None = None
    #: Lo que cuesta arrebatarselo a su dueno. Solo lo da el API JSON.
    clause_value: int | None = None
    #: Blindaje activo: mientras dure, la clausula no se puede pagar.
    clause_shield: int | None = None
    bought_at: str | None = None
    #: Nombre del dueno, cuando la fuente lo trae (el catalogo si).
    owner_name: str | None = None
    #: Nivel de clausula (0 = por defecto). Cada escalon por encima se ha pagado.
    clause_level: int | None = None
    #: Base sobre la que se calculan clausula y coste de subirla.
    clause_floor: int | None = None


@dataclass
class MisterManager:
    external_id: str
    slug: str
    name: str
    position: int | None = None
    points: int | None = None
    team_value: int | None = None
    squad_size: int | None = None
    #: Foto de perfil, si la ha subido. El fichero es un hash generado al
    #: subirla, asi que no se puede deducir del id: hay que leerla.
    avatar_url: str | None = None
    #: Color e inicial del circulo que Mister pinta cuando no hay foto.
    avatar_color: str | None = None
    avatar_initials: str | None = None


@dataclass
class SeasonStat:
    """Lo que rindio un jugador en una temporada pasada."""

    season: str                 # '2025-26'
    points: int
    avg_points: float
    #: Partidos que DISPUTO, que no es lo mismo que jornadas de la temporada.
    matches_played: int
    team_external_id: str | None = None


@dataclass
class MisterFixture:
    """Un partido del calendario, con sede y jornada."""

    external_id: str
    matchday: int
    home_external_id: str
    away_external_id: str
    kickoff_utc: str | None = None


def parse_money(text: str | None) -> int | None:
    """'6.831.000' -> 6831000. Formato espanol: el punto es separador de miles."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_decimal(text: str | None) -> float | None:
    """'0,0' -> 0.0. La coma es el separador decimal."""
    if not text:
        return None
    cleaned = text.strip().replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def real_team_id(external_id: object) -> str | None:
    """El id de equipo, salvo cuando significa 'sin equipo'."""
    valor = _str_or_none(external_id)
    return None if valor == VOID_TEAM_ID else valor


def _value_direction(node: Tag) -> str | None:
    arrow = node.select_one(".value-arrow")
    if arrow is None:
        return None
    classes = arrow.get("class") or []
    if "green" in classes:
        return "up"
    if "red" in classes:
        return "down"
    return "flat"


def _first_attr(row: Tag, *names: str) -> str | None:
    """Busca un atributo en la fila o en cualquiera de sus descendientes.

    Mister reparte los datos entre el enlace, la tarjeta y los botones segun la
    pagina, asi que se busca en todo el bloque en vez de fijar una ruta.
    """
    for name in names:
        if row.has_attr(name):
            return row[name]
        node = row.select_one(f"[{name}]")
        if node is not None:
            return node[name]
    return None


def parse_players(html: bytes | str) -> list[MisterPlayer]:
    """Extrae los jugadores de /team, /market o /search."""
    soup = BeautifulSoup(html, "lxml")
    players: list[MisterPlayer] = []

    for row in soup.select(".player-row"):
        link = row.select_one("a.player[href]")
        if link is None:
            continue
        match = PLAYER_HREF_RE.search(link["href"])
        if not match:
            continue

        external_id, slug = match.group(1), match.group(2)

        name_node = row.select_one(".info .name")
        name = name_node.get_text(strip=True) if name_node else slug.replace("-", " ")
        name = EMOJI_RE.sub("", name).strip()

        position = None
        if pos_node := row.select_one(".player-position[data-position]"):
            position = POSITION_MAP.get(str(pos_node["data-position"]).strip())

        # El valor va suelto dentro de .underName, entre el simbolo € y la flecha.
        market_value = None
        if under := row.select_one(".underName"):
            market_value = parse_money(under.get_text(" ", strip=True))

        team_external_id = None
        logo = row.select_one(".icons img.team-logo[src]")
        if logo and (logo_match := TEAM_LOGO_RE.search(logo["src"])):
            team_external_id = real_team_id(logo_match.group(1))

        average = None
        if avg := row.select_one(".avg"):
            average = parse_decimal(avg.get_text(strip=True))

        # data-id_owner = "0" significa que el jugador es libre, no del usuario 0.
        owner_id = _first_attr(row, "data-id_owner", "data-owner")
        if owner_id in ("0", ""):
            owner_id = None

        players.append(
            MisterPlayer(
                external_id=external_id,
                slug=slug,
                name=name,
                position=position,
                team_external_id=team_external_id,
                market_value=market_value,
                value_direction=_value_direction(row),
                average=average,
                owner_id=owner_id,
                asking_price=parse_money(_first_attr(row, "data-price")),
                ends_at=_first_attr(row, "data-ends"),
            )
        )
    return players


@dataclass
class MisterOwner:
    """Dueno de un jugador segun el catalogo."""

    external_id: str
    name: str | None = None


@dataclass
class MisterSquad:
    """Plantilla completa de un participante, con sus clausulas."""

    manager: MisterManager
    players: list[MisterPlayer]
    league_external_id: str | None = None


#: La configuracion del usuario va incrustada en la pagina completa como
#: `_FG_user = {...};`. No aparece en los fragmentos XHR, solo en la pagina.
FG_USER_RE = re.compile(r"_FG_user\s*=\s*(\{.*?\});", re.DOTALL)


@dataclass
class MisterUser:
    """Lo que Mister publica sobre el usuario de la sesion."""

    external_id: str
    name: str
    #: Saldo disponible ahora mismo. Mister no lo publica de los rivales.
    balance: int | None = None
    #: Saldo contando pujas y ventas pendientes.
    future_balance: int | None = None
    #: Hasta donde permite endeudarse la liga.
    max_debt: int | None = None
    league_external_id: str | None = None
    league_name: str | None = None
    formation: str | None = None
    #: Sistema de puntuacion de la liga: 'mix2', 'mr', 'as', 'marca'...
    scoring_system: str | None = None
    #: Otras ligas del usuario: {id: nombre}.
    other_leagues: dict[str, str] = field(default_factory=dict)


def parse_user_config(html: bytes | str) -> MisterUser | None:
    """Extrae `_FG_user` de la pagina completa: saldo, liga activa y demas ligas."""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    match = FG_USER_RE.search(text)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("_FG_user encontrado pero no es JSON valido")
        return None

    balance = data.get("balance") or {}
    if not isinstance(balance, dict):
        balance = {"current": balance}

    other: dict[str, str] = {}
    for key, value in (data.get("communities") or {}).items():
        if isinstance(value, dict) and value.get("name"):
            other[str(key)] = value["name"]

    return MisterUser(
        external_id=str(data.get("id_uc", "")),
        name=data.get("uc_name") or data.get("name") or "",
        balance=_as_int(balance.get("current")),
        future_balance=_as_int(balance.get("future")),
        max_debt=_as_int(balance.get("maxDebt")),
        league_external_id=_str_or_none(data.get("id_community")),
        league_name=data.get("community"),
        formation=data.get("formation"),
        # 'provider' en la configuracion del usuario es el sistema de
        # puntuacion de la liga activa, no el proveedor de datos.
        scoring_system=_str_or_none(data.get("provider")),
        other_leagues=other,
    )


def parse_json_player(entry: dict, *, default_owner: str | None = None) -> MisterPlayer | None:
    """Convierte un jugador del API JSON al modelo canonico.

    Sirve tanto para los de una plantilla (`team_now`) como para los del
    catalogo (`players`): Mister usa la misma forma en ambos sitios.
    """
    external_id = entry.get("id")
    name = entry.get("name")
    if external_id is None or not name:
        return None

    # 'clause' llega de dos formas segun el endpoint: un diccionario completo en
    # las plantillas y un entero pelado en el catalogo.
    raw_clause = entry.get("clause")
    clause = raw_clause if isinstance(raw_clause, dict) else {"value": raw_clause}
    market = entry.get("market") or {}
    owner = entry.get("owner") if isinstance(entry.get("owner"), dict) else {}

    return MisterPlayer(
        external_id=str(external_id),
        # El JSON no trae slug; se deriva del nombre completo, que aqui si viene
        # entero ("Eric Puerto" y no "E. Puerto" como en el HTML).
        slug=slugify(name),
        name=name,
        position=POSITION_MAP.get(str(entry.get("position"))),
        team_external_id=real_team_id(entry.get("id_team") or entry.get("team")),
        market_value=_as_int(entry.get("value")),
        owner_id=(
            _str_or_none(entry.get("id_uc"))
            or _str_or_none(owner.get("id"))
            or default_owner
        ),
        clause_value=_as_int(clause.get("value")),
        clause_shield=_as_int(entry.get("shield") or clause.get("shield")),
        bought_at=entry.get("created"),
        asking_price=_as_int(entry.get("price")) or _as_int(market.get("price")),
        owner_name=entry.get("uc_name") or owner.get("name"),
        clause_level=_as_int(entry.get("default") or clause.get("default")),
        clause_floor=_as_int(entry.get("floor") or clause.get("floor")),
    )


def parse_player_search(payload: dict) -> list[MisterPlayer]:
    """Extrae una pagina del catalogo de jugadores (`/ajax/sw/players` con offset)."""
    entries = ((payload.get("data") or {}).get("players")) or []
    return [player for entry in entries if (player := parse_json_player(entry))]


def parse_user_squad(payload: dict) -> MisterSquad:
    """Convierte la respuesta de /ajax/sw/users en plantilla + clausulas.

    Es la fuente del radar de clausulas: da, para cada jugador de un rival, lo
    que costaria arrebatarselo y si esta blindado. Ademas trae el nombre
    completo del jugador (el HTML solo da la abreviatura) y el id de la liga.
    """
    data = payload.get("data") or {}
    info = data.get("userInfo") or {}
    value = data.get("value") or {}

    manager = MisterManager(
        external_id=str(data.get("id", "")),
        slug=str(info.get("name", "")).lower().replace(" ", "-"),
        name=info.get("name") or str(data.get("id", "")),
        team_value=_as_int(value.get("value")),
    )

    players = [
        player
        for entry in data.get("team_now") or []
        if (player := parse_json_player(entry, default_owner=manager.external_id))
    ]

    return MisterSquad(
        manager=manager,
        players=players,
        league_external_id=_str_or_none(info.get("id_community")),
    )


#: Mister escribe las fechas en castellano y abreviadas ('24 sept 2025').
SPANISH_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
}
VALUE_DATE_RE = re.compile(r"(\d{1,2})\s+([a-záéíóú]+)\.?\s+(\d{4})", re.IGNORECASE)


def parse_spanish_date(text: str) -> str | None:
    """'24 sept 2025' -> '2025-09-24'. Devuelve None si no encaja."""
    match = VALUE_DATE_RE.search(strip_accents(text or "").lower())
    if not match:
        return None
    day, month_name, year = match.groups()
    month = SPANISH_MONTHS.get(month_name[:4]) or SPANISH_MONTHS.get(month_name[:3])
    if not month:
        log.debug("mes desconocido: %r", month_name)
        return None
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def parse_value_history(payload: dict) -> list[tuple[str, int]]:
    """Extrae el historico diario de valor de /ajax/sw/players.

    Mister publica en `values_chart` alrededor de un ano de valores diarios por
    jugador. Es la unica parte del historico que se puede recuperar hacia atras:
    clausulas, propiedad y probabilidades de once solo existen si se capturaron
    el dia que ocurrieron.
    """
    chart = ((payload.get("data") or {}).get("values_chart") or {})
    history: list[tuple[str, int]] = []

    for point in chart.get("points") or []:
        value = _as_int(point.get("value"))
        date = parse_spanish_date(str(point.get("date", "")))
        if value is not None and date:
            history.append((date, value))
    return history


#: '25/26' -> '2025-26'. Mister abrevia el ano a dos digitos en el historico
#: pero lo escribe entero en todas partes; se normaliza aqui para que la
#: temporada sea comparable con la que trae la configuracion.
def normalize_season(raw: str) -> str | None:
    match = re.fullmatch(r"(\d{2})/(\d{2})", raw.strip())
    if not match:
        return None
    return f"20{match.group(1)}-{match.group(2)}"


def parse_season_history(payload: dict) -> list[SeasonStat]:
    """Rendimiento por temporada, de la mas reciente a la mas antigua.

    Es lo unico del pasado que Mister entrega de golpe: hasta cinco temporadas
    con puntos totales, media por jornada y el equipo en el que estuvo. Sin esto
    no hay forma de estimar nada antes de que se juegue la primera jornada.
    """
    stats: list[SeasonStat] = []

    for entry in (payload.get("data") or {}).get("points_history") or []:
        season = normalize_season(str(entry.get("season", "")))
        points = _as_int(entry.get("points"))
        if season is None or points is None:
            continue

        # Los partidos disputados hay que deducirlos: Mister no los publica.
        #
        # 'last_gameweek' NO sirve, aunque lo parezca. Es la ultima jornada de
        # la temporada, no las que jugo el jugador: Iker Luque figuraba con 12
        # puntos, media 12,0 y last_gameweek 34, y lo que hizo fue jugar UN
        # partido. Tomandolo por 34 partidos, su media de 12 parecia solida y
        # se colaba en cabeza de cualquier ranking.
        #
        # La division si es exacta, porque la media que da Mister es por partido
        # disputado: 179 puntos con media 5,4242 son 33 partidos justos.
        avg = entry.get("avg")
        avg_points = float(avg) if isinstance(avg, (int, float)) else 0.0
        # Media cero no permite dividir. En la practica es quien no llego a
        # debutar, asi que cero partidos es tambien la lectura correcta.
        matches = round(points / avg_points) if avg_points else 0

        stats.append(
            SeasonStat(
                season=season,
                points=points,
                avg_points=avg_points,
                matches_played=matches,
                team_external_id=real_team_id(entry.get("id_team")),
            )
        )
    return stats


def parse_team_names(payload: dict) -> dict[str, str]:
    """{id de equipo en Mister: nombre real}, de lo que caiga en la ficha.

    Importa mas de lo que parece. Las paginas de jugadores solo dan el numero
    del equipo, asi que hasta ahora se registraban como 'mister-team-9' y hacia
    falta cruzarlos con FutbolFantasy para ponerles nombre. La ficha si trae el
    nombre, tanto del equipo del jugador como de los dos del proximo partido,
    de modo que el equipo queda identificado sin depender de la otra fuente.
    """
    data = payload.get("data") or {}
    names: dict[str, str] = {}

    team = (data.get("player") or {}).get("team") or {}
    if (team_id := real_team_id(team.get("id"))) and team.get("name"):
        names[team_id] = str(team["name"])

    for match in (data.get("next_match") or {}).values():
        if not isinstance(match, dict):
            continue
        for lado in ("home", "away"):
            side_id = real_team_id(match.get(f"id_{lado}"))
            if side_id and match.get(lado):
                names[side_id] = str(match[lado])

    return names


@dataclass
class ScheduledMatch:
    """Una jornada del calendario de un equipo: contra quien juega."""

    matchday: int
    opponent_external_id: str


def parse_schedule(payload: dict) -> tuple[str | None, list[ScheduledMatch]]:
    """Calendario del equipo del jugador: (id del equipo, rival por jornada).

    Es el hallazgo que hace posible mirar mas de una jornada hacia delante. La
    ficha lleva la lista de jornadas para pintar los puntos de cada una, y en
    cada entrada mete el ESCUDO del rival, del que se saca su id. Son quince
    jornadas por delante, y con veinte fichas -una por equipo- se tiene la
    rejilla entera del calendario sin una sola peticion de mas.

    Lo que no dice es la sede. Eso solo aparece en `next_match`, y por tanto se
    va sabiendo jornada a jornada.

    Cuidado con confundir esto con "cuando juega". Los seis equipos que
    descansan la primera jornada por el Mundial tienen su rival de J1 asignado
    igualmente: el partido existe, se juega mas tarde.
    """
    data = payload.get("data") or {}
    equipo = _str_or_none(((data.get("player") or {}).get("team") or {}).get("id"))

    partidos: list[ScheduledMatch] = []
    for entry in data.get("points") or []:
        numero = _as_int(entry.get("number"))
        escudo = TEAM_LOGO_RE.search(str(entry.get("rivalLogoUrl") or ""))
        if numero is not None and escudo:
            partidos.append(ScheduledMatch(numero, escudo.group(1)))

    return equipo, partidos


def _matchday_numbers(payload: dict) -> dict[str, int]:
    """{id de jornada -> numero}. El calendario numera, el partido solo referencia."""
    numbers: dict[str, int] = {}
    for entry in (payload.get("data") or {}).get("points") or []:
        number = _as_int(entry.get("number"))
        if entry.get("id") is not None and number is not None:
            numbers[str(entry["id"])] = number
    return numbers


def parse_next_fixture(payload: dict) -> MisterFixture | None:
    """Proximo partido del jugador, con local, visitante y hora.

    Es la unica parte del calendario que llega completa: el resto de jornadas
    solo dicen contra quien se juega, no donde. Como se captura a diario, el
    calendario se va rellenando jornada a jornada por si solo.
    """
    matches = (payload.get("data") or {}).get("next_match") or {}
    if not isinstance(matches, dict) or not matches:
        return None
    match = next(iter(matches.values()))
    if not isinstance(match, dict):
        return None

    home = _str_or_none(match.get("id_home"))
    away = _str_or_none(match.get("id_away"))
    external_id = _str_or_none(match.get("id_match"))
    matchday = _matchday_numbers(payload).get(str(match.get("id_gameweek")))
    if not (home and away and external_id and matchday):
        return None

    kickoff = None
    timestamp = _as_int((match.get("date") or {}).get("ts"))
    if timestamp:
        kickoff = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return MisterFixture(
        external_id=external_id,
        matchday=matchday,
        home_external_id=home,
        away_external_id=away,
        kickoff_utc=kickoff,
    )


def parse_matchday_points(payload: dict) -> list[tuple[int, int]]:
    """Puntos por jornada de la temporada en curso: [(jornada, puntos)].

    Vacio mientras no se haya jugado nada. Segun avance la temporada esto va
    sustituyendo al historico por temporada como base de la estimacion, porque
    dice lo que rinde el jugador AHORA y no hace dos anos.
    """
    rows: list[tuple[int, int]] = []
    for entry in (payload.get("data") or {}).get("points") or []:
        number = _as_int(entry.get("number"))
        points = _as_int((entry.get("points") or {}).get("points"))
        if number is not None and points is not None:
            rows.append((number, points))
    return rows


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return parse_money(str(value))


def _str_or_none(value: object) -> str | None:
    return None if value in (None, "", 0) else str(value)


@dataclass
class FeedEvent:
    """Una tarjeta del feed, con lo poco que se puede extraer sin saber su tipo."""

    external_id: str
    kind: str | None
    summary: str
    html: str
    relative_time: str | None = None
    player_ids: list[str] = field(default_factory=list)
    user_ids: list[str] = field(default_factory=list)
    amounts: list[int] = field(default_factory=list)


#: '17h', '3d', 'ahora'... Mister muestra el tiempo en relativo, no una fecha.
RELATIVE_TIME_RE = re.compile(r"^\s*(ahora|\d+\s*(?:s|min|m|h|d|sem)\.?)\s*$", re.IGNORECASE)
#: Cifras en euros dentro del texto de una tarjeta.
AMOUNT_RE = re.compile(r"€\s*([\d.]{4,})|([\d.]{7,})")
#: Las tarjetas rotativas no traen un id estable, asi que se identifican por su
#: contenido para no reescribirlas cada dia ni duplicarlas.
UNSTABLE_IDS = {"feed-0", "feed-", ""}


def parse_feed(html: bytes | str) -> list[FeedEvent]:
    """Extrae los movimientos de la liga del feed.

    A proposito NO interpreta cada tipo de tarjeta: guarda todas con su HTML
    para poder reprocesarlas cuando se sepa que forma tiene cada movimiento.
    Recien reiniciada una liga solo aparecen altas y avisos del administrador,
    asi que los tipos de compra y venta se descubren sobre la marcha.
    """
    soup = BeautifulSoup(html, "lxml")
    events: list[FeedEvent] = []

    for card in soup.select("[class*=card-]"):
        classes = card.get("class") or []
        kind = next(
            (c for c in classes if c.startswith("card-") and c != "card-wrapper"), None
        )
        if kind is None:
            continue

        text = card.get_text(" | ", strip=True)
        if not text:
            continue

        raw_id = str(card.get("id") or "")
        external_id = (
            raw_id
            if raw_id not in UNSTABLE_IDS
            else f"{kind}-{hashlib.sha1(text.encode()).hexdigest()[:16]}"
        )

        player_ids, user_ids = [], []
        for link in card.select("a[href]"):
            if match := PLAYER_HREF_RE.search(link["href"]):
                player_ids.append(match.group(1))
            elif match := USER_HREF_RE.search(link["href"]):
                user_ids.append(match.group(1))

        amounts = []
        for match in AMOUNT_RE.finditer(text):
            value = parse_money(match.group(1) or match.group(2))
            if value:
                amounts.append(value)

        relative = None
        for node in card.select(".date, .time, time, small"):
            candidate = node.get_text(strip=True)
            if RELATIVE_TIME_RE.match(candidate):
                relative = candidate
                break

        events.append(
            FeedEvent(
                external_id=external_id,
                kind=kind,
                summary=text[:500],
                html=str(card),
                relative_time=relative,
                player_ids=list(dict.fromkeys(player_ids)),
                user_ids=list(dict.fromkeys(user_ids)),
                amounts=amounts,
            )
        )
    return events


def parse_standings(html: bytes | str) -> list[MisterManager]:
    """Extrae la clasificacion de la liga: rivales, puntos y valor de plantilla."""
    soup = BeautifulSoup(html, "lxml")
    managers: list[MisterManager] = []

    for row in soup.select(".player-row"):
        link = row.select_one("a.user[href]")
        if link is None:
            continue
        match = USER_HREF_RE.search(link["href"])
        if not match:
            continue

        name_node = row.select_one(".info .name")
        position_node = row.select_one(".position")
        points_node = row.select_one(".points")

        # ".points" incluye el sufijo "Pts" en un <span> que hay que descartar.
        points = None
        if points_node is not None:
            own_text = "".join(
                child for child in points_node.children if isinstance(child, str)
            )
            points = parse_money(own_text)

        squad_size = team_value = None
        played = row.select_one(".played")
        if played and (info := SQUAD_INFO_RE.search(played.get_text(" ", strip=True))):
            squad_size = int(info.group(1))
            team_value = parse_money(info.group(2))

        url, color, initials = _avatar(row)

        managers.append(
            MisterManager(
                external_id=match.group(1),
                slug=match.group(2),
                name=name_node.get_text(strip=True) if name_node else match.group(2),
                position=int(position_node.get_text(strip=True)) if position_node else None,
                points=points,
                team_value=team_value,
                squad_size=squad_size,
                avatar_url=url,
                avatar_color=color,
                avatar_initials=initials,
            )
        )
    return managers


#: 'background-color: hsl(115 50 50);' del circulo de avatar.
AVATAR_COLOR_RE = re.compile(r"background-color:\s*([^;\"]+)")


def _avatar(row: Tag) -> tuple[str | None, str | None, str | None]:
    """(foto, color de fondo, inicial) del participante.

    Mister pinta siempre el circulo de color con la inicial y encima, si la hay,
    la foto con un `onerror` que la esconde. Se guardan las dos cosas: la foto
    para mostrarla y el circulo para quien no tenga.
    """
    node = row.select_one(".user-avatar")
    if node is None:
        return None, None, None

    imagen = node.select_one("img[src]")
    inicial = node.select_one("span")
    color = AVATAR_COLOR_RE.search(str(node.get("style") or ""))

    return (
        imagen["src"] if imagen else None,
        color.group(1).strip() if color else None,
        inicial.get_text(strip=True) if inicial else None,
    )
