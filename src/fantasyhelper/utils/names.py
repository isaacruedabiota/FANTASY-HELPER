"""Normalizacion de nombres.

El unico sitio del proyecto donde se decide que "Vinícius Jr." y "Vinicius Junior"
son el mismo jugador. Si el cruce entre fuentes falla, se arregla aqui.
"""

from __future__ import annotations

import re
import unicodedata

# Sufijos y prefijos que unas fuentes ponen y otras no.
_NOISE = re.compile(
    r"\b(jr|junior|senior|sr|hijo)\b",
    flags=re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def slugify(name: str) -> str:
    """'Vinícius Jr.' -> 'vinicius-jr'. Estable entre fuentes y usable como clave."""
    text = strip_accents(name).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def match_key(name: str) -> str:
    """Clave agresiva para cruzar fuentes: sin acentos, sin sufijos, sin espacios.

    Mas permisiva que slugify, por eso solo se usa para *proponer* un match,
    nunca como identificador.
    """
    text = strip_accents(name).lower()
    text = _NOISE.sub(" ", text)
    return re.sub(r"[^a-z0-9]", "", text)
