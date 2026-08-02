"""Analisis de ficheros HAR exportados desde las DevTools.

Mister no documenta su API, asi que en vez de adivinar rutas se lee el trafico
real del navegador. De un HAR se saca todo lo necesario de una vez:
    - que endpoints existen y que devuelven,
    - la cookie de sesion,
    - el id de tu liga.

Un HAR contiene tus cookies de sesion: es equivalente a una contrasena. Se
procesa en local y solo se guarda la cookie en data/ (que esta en .gitignore).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from fantasyhelper.adapters.mister.endpoints import Endpoint, save_endpoints

log = logging.getLogger(__name__)

MISTER_HOSTS = ("mister.mundodeportivo.com", "mundodeportivo.com")

# Pistas para adivinar que representa cada endpoint. Es solo una propuesta:
# la confirmacion final la das tu mirando el JSON de muestra.
KEY_HINTS: dict[str, tuple[str, ...]] = {
    "players": ("player", "jugador", "catalog", "squadplayers"),
    "market": ("market", "mercado", "offer", "puja", "bid"),
    "squad": ("squad", "plantilla", "lineup", "alineacion", "myteam"),
    "standings": ("standing", "ranking", "clasificacion", "classification", "leaderboard"),
    "teams": ("teams", "equipos", "community", "rivals", "users"),
}

# Cookies que no son de sesion y solo ensucian.
COOKIE_NOISE = re.compile(r"^(_ga|_gid|_fbp|_gcl|__gads|euconsent|OptanonConsent)", re.IGNORECASE)


@dataclass
class HarEntry:
    method: str
    url: str
    path: str
    params: dict[str, str]
    status: int
    content_type: str
    body: str | None
    guessed_key: str | None
    score: int

    @property
    def size(self) -> int:
        return len(self.body or "")

    def sample(self, limit: int = 400) -> str:
        if not self.body:
            return ""
        return self.body[:limit].replace("\n", " ")


def _decode_body(content: dict[str, Any]) -> str | None:
    text = content.get("text")
    if text is None:
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
    return text


def _guess_key(path: str, body: str | None) -> tuple[str | None, int]:
    """Propone a que clave del snapshot corresponde el endpoint, y su confianza."""
    haystack = path.lower()
    best: tuple[str | None, int] = (None, 0)

    for key, hints in KEY_HINTS.items():
        score = sum(10 for hint in hints if hint in haystack)
        # Un JSON grande suele ser el catalogo de jugadores, no un ping.
        if body and len(body) > 20_000 and key == "players":
            score += 5
        if score > best[1]:
            best = (key, score)
    return best


def parse_har(har_path: Path) -> tuple[list[HarEntry], dict[str, str], list[str]]:
    """Devuelve (endpoints candidatos, cookies de sesion, ids de liga detectados)."""
    raw = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
    entries: list[HarEntry] = []
    cookies: dict[str, str] = {}
    league_ids: set[str] = set()

    for item in raw.get("log", {}).get("entries", []):
        request = item.get("request", {})
        response = item.get("response", {})
        url = request.get("url", "")
        parsed = urlparse(url)

        if not any(parsed.netloc.endswith(host) for host in MISTER_HOSTS):
            continue

        for cookie in request.get("cookies", []):
            name = cookie.get("name", "")
            if name and not COOKIE_NOISE.match(name):
                cookies[name] = cookie.get("value", "")

        content = response.get("content", {})
        content_type = content.get("mimeType", "") or ""
        body = _decode_body(content)

        # Solo interesan respuestas de datos, no HTML, imagenes ni bundles JS.
        is_json = "json" in content_type.lower() or (body or "").lstrip()[:1] in ("{", "[")
        if not is_json:
            continue

        key, score = _guess_key(parsed.path, body)
        entries.append(
            HarEntry(
                method=request.get("method", "GET"),
                url=url,
                path=parsed.path,
                params=dict(parse_qsl(parsed.query)),
                status=response.get("status", 0),
                content_type=content_type,
                body=body,
                guessed_key=key,
                score=score,
            )
        )

        for segment in parsed.path.split("/"):
            if segment.isdigit() and len(segment) >= 4:
                league_ids.add(segment)

    # Deduplicar por (metodo, ruta) quedandose con la respuesta mas rica.
    unique: dict[tuple[str, str], HarEntry] = {}
    for entry in entries:
        existing = unique.get((entry.method, entry.path))
        if existing is None or entry.size > existing.size:
            unique[(entry.method, entry.path)] = entry

    ordered = sorted(unique.values(), key=lambda e: (-e.score, -e.size))
    return ordered, cookies, sorted(league_ids)


def write_candidates(entries: list[HarEntry]) -> dict[str, Endpoint]:
    """Guarda los candidatos y preconfigura los que tienen una propuesta clara."""
    endpoints: dict[str, Endpoint] = {}
    used: set[str] = set()

    for entry in entries:
        key = entry.guessed_key
        if key and key not in used and entry.score >= 10 and entry.status == 200:
            endpoints[key] = Endpoint(
                key=key,
                path=entry.path,
                method=entry.method,
                params=entry.params,
                note=f"autodetectado del HAR (confianza {entry.score})",
            )
            used.add(key)

    candidates = [
        {
            "method": e.method,
            "path": e.path,
            "params": e.params,
            "status": e.status,
            "bytes": e.size,
            "guess": e.guessed_key,
            "sample": e.sample(),
        }
        for e in entries
    ]
    save_endpoints(endpoints, candidates=candidates)
    return endpoints
