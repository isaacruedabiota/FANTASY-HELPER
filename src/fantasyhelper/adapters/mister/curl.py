"""Extraccion de la sesion desde un comando cURL copiado del navegador.

Buscar la cabecera Cookie a mano en las DevTools es incomodo y cambia de sitio
segun la version y el idioma del navegador. En cambio "Copiar como cURL" esta
en el menu contextual de cualquier peticion, en todos los navegadores basados
en Chromium, y el comando resultante incluye la cookie completa.

Se admiten las tres variantes que genera Chromium:
    bash   -H 'Cookie: a=1; b=2'
    cmd    -H "Cookie: a=1; b=2"
    also   -b 'a=1; b=2'
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: -H 'Cookie: ...' / -H "Cookie: ..." (bash o cmd), sin distinguir mayusculas.
COOKIE_HEADER_RE = re.compile(
    r"""-H\s+(['"])\s*cookie\s*:\s*(?P<value>.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
#: -b 'a=1; b=2', la forma corta.
COOKIE_FLAG_RE = re.compile(r"""-b\s+(['"])(?P<value>.*?)\1""", re.IGNORECASE | re.DOTALL)

#: Cookies de analitica y publicidad que no sirven para autenticar.
NOISE_RE = re.compile(
    r"^(_ga|_gid|_gat|_fbp|_fbc|_gcl|__gads|__gpi|_pk_|euconsent|OptanonConsent|"
    r"OptanonAlertBoxClosed|cto_|panoramaId|permutive)",
    re.IGNORECASE,
)


def _clean(value: str) -> str:
    """Deshace los saltos de linea y escapes que mete cada shell."""
    value = value.replace("^\n", "").replace("\\\n", "").replace("`\n", "")
    value = value.replace("^%", "%").replace('\\"', '"').replace("^", "")
    return " ".join(value.split())


def extract_cookies(curl_command: str) -> dict[str, str]:
    """Devuelve las cookies de un comando cURL, sin las de analitica."""
    match = COOKIE_HEADER_RE.search(curl_command) or COOKIE_FLAG_RE.search(curl_command)
    if not match:
        return {}

    cookies: dict[str, str] = {}
    for part in _clean(match.group("value")).split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and not NOISE_RE.match(name):
            cookies[name] = value
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def update_env_token(env_path, token: str) -> bool:
    """Escribe MISTER_TOKEN en el .env conservando el resto del fichero.

    Devuelve True si se reemplazo una linea existente, False si se anadio.
    """
    line = f"MISTER_TOKEN={token}"

    if not env_path.exists():
        env_path.write_text(line + "\n", encoding="utf-8")
        return False

    lines = env_path.read_text(encoding="utf-8").splitlines()
    for index, existing in enumerate(lines):
        if existing.strip().startswith("MISTER_TOKEN="):
            lines[index] = line
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    lines.append(line)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return False
