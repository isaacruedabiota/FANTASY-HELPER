"""Acceso a SQLite. Sin ORM: el esquema esta en schema.sql y se aplica tal cual."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fantasyhelper.config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    """Timestamp ISO-8601 UTC, el formato que usa toda la base de datos."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    """Fecha de snapshot, 'YYYY-MM-DD' en UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; las tx son explicitas
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


#: Columnas anadidas despues de la version inicial del esquema.
#: CREATE TABLE IF NOT EXISTS no toca las tablas que ya existen, asi que las
#: columnas nuevas hay que anadirlas aparte para no perder el historico ya
#: capturado (que es justo lo que no se puede recuperar).
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("raw_payload", "content_encoding", "TEXT NOT NULL DEFAULT 'identity'"),
    ("player_alias", "team_id", "INTEGER REFERENCES team(id)"),
    ("ownership_snapshot", "clause_level", "INTEGER"),
    ("ownership_snapshot", "clause_floor", "INTEGER"),
    ("league", "baseline_date", "TEXT"),
    ("league", "bonus_rules", "TEXT"),
    ("manager_snapshot", "future_balance", "INTEGER"),
    ("manager_snapshot", "max_debt", "INTEGER"),
    ("manager", "avatar_url", "TEXT"),
    ("manager", "avatar_color", "TEXT"),
    ("manager", "avatar_initials", "TEXT"),
    ("manager", "formation", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _COLUMN_MIGRATIONS:
        exists = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone()
        if not exists:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: Path | None = None) -> Path:
    """Crea o actualiza el esquema. Idempotente: se puede llamar siempre."""
    path = db_path or settings.db_path
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
    finally:
        conn.close()
    return path


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Agrupa escrituras: o entra todo el snapshot, o no entra nada."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
