from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from fantasyhelper.storage.db import connect, init_db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    """Base de datos limpia en disco temporal."""
    path = tmp_path / "test.db"
    init_db(path)
    conn = connect(path)
    yield conn
    conn.close()


def _gz(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def mister_team_html() -> str:
    """Fragmento real de /team: mi plantilla."""
    return _gz("mister_team.html.gz")


@pytest.fixture
def mister_market_html() -> str:
    """Fragmento real de /market: el mercado del dia."""
    return _gz("mister_market.html.gz")


@pytest.fixture
def mister_search_html() -> str:
    """Fragmento real de /search: catalogo de jugadores."""
    return _gz("mister_search.html.gz")


@pytest.fixture
def mister_standings_html() -> str:
    """Fragmento real de /standings: clasificacion de la liga."""
    return _gz("mister_standings.html.gz")


@pytest.fixture
def team_html() -> str:
    """Pagina real de equipo de FutbolFantasy, congelada como fixture (gzip, 75 KB).

    Sirve para separar dos fallos distintos: si el parser falla contra la web
    real pero pasa contra este fixture, es que FutbolFantasy cambio el HTML;
    si falla contra el fixture, lo hemos roto nosotros.
    """
    with gzip.open(FIXTURES / "ff_equipo.html.gz", "rt", encoding="utf-8") as handle:
        return handle.read()
