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

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

#: data-position en Mister -> codigo canonico.
POSITION_MAP = {"1": "PT", "2": "DF", "3": "MC", "4": "DL"}

PLAYER_HREF_RE = re.compile(r"players/(\d+)/([\w\-]+)")
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


@dataclass
class MisterManager:
    external_id: str
    slug: str
    name: str
    position: int | None = None
    points: int | None = None
    team_value: int | None = None
    squad_size: int | None = None


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
            team_external_id = logo_match.group(1)

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

        managers.append(
            MisterManager(
                external_id=match.group(1),
                slug=match.group(2),
                name=name_node.get_text(strip=True) if name_node else match.group(2),
                position=int(position_node.get_text(strip=True)) if position_node else None,
                points=points,
                team_value=team_value,
                squad_size=squad_size,
            )
        )
    return managers
