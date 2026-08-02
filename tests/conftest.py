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


@pytest.fixture
def team_html() -> str:
    """Pagina real de equipo de FutbolFantasy, congelada como fixture (gzip, 75 KB).

    Sirve para separar dos fallos distintos: si el parser falla contra la web
    real pero pasa contra este fixture, es que FutbolFantasy cambio el HTML;
    si falla contra el fixture, lo hemos roto nosotros.
    """
    with gzip.open(FIXTURES / "ff_equipo.html.gz", "rt", encoding="utf-8") as handle:
        return handle.read()
