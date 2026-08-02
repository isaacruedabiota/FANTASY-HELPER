"""Almacen de respuestas HTTP en crudo.

Regla del proyecto: nada se descarta. Si dentro de tres meses cambiamos como
calculamos una metrica, se reprocesa el historico entero desde aqui. Volver a
pedir los datos a la fuente no es una opcion: los valores de mercado de ayer
ya no existen en ninguna parte.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fantasyhelper.storage.db import today, utcnow

log = logging.getLogger(__name__)

#: Por debajo de este tamano comprimir no compensa (JSON pequenos de Mister).
COMPRESS_THRESHOLD = 4096


def _compress(blob: bytes) -> tuple[bytes, str]:
    if len(blob) < COMPRESS_THRESHOLD:
        return blob, "identity"
    return gzip.compress(blob, compresslevel=6), "gzip"


def _decompress(blob: bytes, encoding: str | None) -> bytes:
    return gzip.decompress(blob) if encoding == "gzip" else blob


def save_raw(
    conn: sqlite3.Connection,
    *,
    source: str,
    endpoint: str,
    content: bytes | str,
    params: dict[str, Any] | None = None,
    status_code: int | None = None,
    content_type: str | None = None,
) -> int | None:
    """Guarda un payload crudo. Devuelve el id, o None si ya estaba guardado hoy.

    El UNIQUE sobre (snapshot_date, source, endpoint, sha256) hace que reejecutar
    el job el mismo dia no duplique nada.
    """
    blob = content.encode("utf-8") if isinstance(content, str) else content
    # El sha256 es del contenido original: asi la deteccion de duplicados no
    # depende de si comprimimos ni del nivel de compresion.
    digest = hashlib.sha256(blob).hexdigest()
    stored, encoding = _compress(blob)

    cur = conn.execute(
        """
        INSERT INTO raw_payload
            (captured_at, snapshot_date, source, endpoint, params_json,
             status_code, content_type, content, content_encoding, sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, source, endpoint, sha256) DO NOTHING
        """,
        (
            utcnow(),
            today(),
            source,
            endpoint,
            json.dumps(params, ensure_ascii=False) if params else None,
            status_code,
            content_type,
            stored,
            encoding,
            digest,
        ),
    )
    if cur.rowcount == 0:
        log.debug("raw sin cambios: %s %s", source, endpoint)
        return None
    return cur.lastrowid


def load_raw(
    conn: sqlite3.Connection,
    *,
    source: str,
    endpoint: str,
    snapshot_date: str | None = None,
) -> bytes | None:
    """Recupera el payload mas reciente (o el de un dia concreto), ya descomprimido."""
    if snapshot_date:
        row = conn.execute(
            """
            SELECT content, content_encoding FROM raw_payload
            WHERE source = ? AND endpoint = ? AND snapshot_date = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (source, endpoint, snapshot_date),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT content, content_encoding FROM raw_payload
            WHERE source = ? AND endpoint = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (source, endpoint),
        ).fetchone()
    return _decompress(row["content"], row["content_encoding"]) if row else None


def load_raw_fresh(
    conn: sqlite3.Connection,
    *,
    source: str,
    endpoint: str,
    max_age_hours: float,
) -> bytes | None:
    """Payload guardado hace menos de `max_age_hours`, o None. Es la cache del scraper.

    Vive aqui y no en el scraper para que el detalle de si el contenido esta
    comprimido no se escape de la capa de almacenamiento.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT content, content_encoding FROM raw_payload "
        "WHERE source = ? AND endpoint = ? AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (source, endpoint, cutoff),
    ).fetchone()
    return _decompress(row["content"], row["content_encoding"]) if row else None


def iter_raw(
    conn: sqlite3.Connection, *, source: str, endpoint_like: str | None = None
) -> list[tuple[str, str, bytes]]:
    """Recorre el historico crudo para reprocesarlo. Devuelve (fecha, endpoint, contenido)."""
    sql = "SELECT snapshot_date, endpoint, content, content_encoding FROM raw_payload WHERE source = ?"
    params: list[Any] = [source]
    if endpoint_like:
        sql += " AND endpoint LIKE ?"
        params.append(endpoint_like)
    sql += " ORDER BY snapshot_date, endpoint"
    return [
        (r["snapshot_date"], r["endpoint"], _decompress(r["content"], r["content_encoding"]))
        for r in conn.execute(sql, params)
    ]
