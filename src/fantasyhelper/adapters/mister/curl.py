r"""Extraccion de la sesion desde un comando cURL copiado del navegador.

Buscar las cabeceras a mano en las DevTools es incomodo y cambia de sitio segun
la version y el idioma del navegador. En cambio "Copiar como cURL" esta en el
menu contextual de cualquier peticion, en todos los navegadores basados en
Chromium, y el comando resultante trae todo lo necesario.

Mister necesita DOS cosas para autenticar, no solo la cookie:
    - las cookies de sesion (PHPSESSID, token, authenticated...)
    - la cabecera `x-auth`, un hash por sesion

Con la cookie sola las peticiones fallan, asi que se extraen las dos.

La variante de Windows (cmd) escapa medio comando con acentos circunflejos:
    -b ^"g_state=^{^\^"i_l^\^":0^}; PHPSESSID=abc^"
Por eso se desescapa antes de buscar nada.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Contenido de un argumento entrecomillado, admitiendo comillas escapadas
#: dentro. Es imprescindible: la cookie g_state de Google lleva JSON con \" y
#: un `.*?` simple cortaria el valor en la primera comilla interna.
_QUOTED = r"""(?P<value>(?:\\.|(?!(?P=q)).)*)(?P=q)"""

#: -H 'cookie: ...' en cualquier combinacion de mayusculas y comillas.
COOKIE_HEADER_RE = re.compile(
    r"""-H\s+(?P<q>['"])\s*cookie\s*:\s*""" + _QUOTED, re.IGNORECASE | re.DOTALL
)
#: -b 'a=1; b=2', la forma corta que usa Chromium para las cookies.
COOKIE_FLAG_RE = re.compile(r"""-b\s+(?P<q>['"])""" + _QUOTED, re.IGNORECASE | re.DOTALL)

#: Cookies de analitica y consentimiento: no autentican y solo ensucian.
NOISE_RE = re.compile(
    r"^(_ga|_gid|_gat|_fbp|_fbc|_gcl|__gads|__gpi|_pk_|AMP_|euconsent|OptanonConsent|"
    r"OptanonAlertBoxClosed|cto_|panoramaId|permutive|uid_dm|g_state)",
    re.IGNORECASE,
)

#: Cabeceras que Mister necesita ademas de las cookies.
AUTH_HEADERS = ("x-auth",)


@dataclass
class CurlSession:
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())

    def is_usable(self) -> bool:
        return bool(self.cookies)


def unescape(command: str) -> str:
    """Deshace los escapes que mete cada shell en el comando copiado.

    cmd de Windows antepone ^ a casi todo (^" ^% ^{ ^\\) y lo usa tambien como
    continuacion de linea; bash usa \\ al final de linea. Tras esto queda un
    comando plano sobre el que buscar con expresiones regulares.
    """
    # Continuaciones de linea primero, en las tres variantes. Se absorbe tambien
    # el espacio de alrededor para no dejar huecos dobles.
    command = re.sub(r"[ \t]*[\^\\`]\r?\n\s*", " ", command)
    # Luego cualquier ^X pasa a ser X (cubre ^" ^% ^{ ^\ y ^^).
    command = re.sub(r"\^(.)", r"\1", command)
    return command


def _split_cookies(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    raw = raw.replace('\\"', '"')
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if sep and name and not NOISE_RE.match(name):
            cookies[name] = value.strip()
    return cookies


def extract_header(command: str, name: str) -> str | None:
    """Devuelve el valor de una cabecera -H concreta, ya desescapada."""
    pattern = re.compile(
        rf"""-H\s+(?P<q>['"])\s*{re.escape(name)}\s*:\s*""" + _QUOTED,
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(command)
    return match.group("value").strip() if match else None


def extract_session(curl_command: str) -> CurlSession:
    """Extrae cookies y cabeceras de autenticacion de un comando cURL."""
    command = unescape(curl_command)

    match = COOKIE_HEADER_RE.search(command) or COOKIE_FLAG_RE.search(command)
    cookies = _split_cookies(match.group("value")) if match else {}

    headers = {}
    for name in AUTH_HEADERS:
        if value := extract_header(command, name):
            headers[name] = value

    return CurlSession(cookies=cookies, headers=headers)


def update_env_var(env_path: Path, key: str, value: str) -> bool:
    """Escribe una variable en el .env conservando el resto del fichero.

    Devuelve True si reemplazo una linea existente, False si la anadio.
    """
    line = f"{key}={value}"

    if not env_path.exists():
        env_path.write_text(line + "\n", encoding="utf-8")
        return False

    lines = env_path.read_text(encoding="utf-8").splitlines()
    for index, existing in enumerate(lines):
        if existing.strip().startswith(f"{key}="):
            lines[index] = line
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    lines.append(line)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return False
