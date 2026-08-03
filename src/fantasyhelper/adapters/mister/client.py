"""Cliente HTTP de Mister.

Scraping responsable, por dos motivos: es lo correcto y es lo que evita que te
bloqueen la cuenta en mitad de la temporada.
  - Un intervalo minimo entre peticiones (MIN_INTERVAL).
  - User-Agent identificable, sin fingir ser un navegador.
  - La sesion se reutiliza entre ejecuciones: un login al dia, no uno por peticion.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Self

import httpx

from fantasyhelper.adapters.base import AdapterError, NotConfiguredError
from fantasyhelper.adapters.mister.endpoints import Endpoint, load_endpoints
from fantasyhelper.config import settings
from fantasyhelper.storage.raw import save_raw

log = logging.getLogger(__name__)

BASE_URL = "https://mister.mundodeportivo.com"
SESSION_PATH = settings.data_dir / "mister_session.json"
USER_AGENT = "FantasyHelper/0.1 (uso personal; contacto: isaacru04@gmail.com)"
MIN_INTERVAL = 1.2  # segundos entre peticiones
MAX_RETRIES = 3


class MisterClient:
    provider = "mister"

    def __init__(self, session_path: Path | None = None) -> None:
        self.session_path = session_path or SESSION_PATH
        self.endpoints = load_endpoints()
        self._last_request = 0.0
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "es-ES,es;q=0.9",
                # Estas dos son las que hacen que Mister devuelva el fragmento
                # de HTML en vez de la pagina entera.
                "X-Requested-With": "XMLHttpRequest",
                "Partial-Request": "true",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/feed",
            },
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        self._load_session()

    # -- sesion ------------------------------------------------------------

    def _load_session(self) -> None:
        """Restaura cookies y cabeceras de autenticacion de la ejecucion anterior."""
        if self.session_path.exists():
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
            for cookie in data.get("cookies", []):
                self._client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
            for name, value in data.get("headers", {}).items():
                self._client.headers[name] = value
            log.debug("sesion restaurada de %s", self.session_path)

        # Lo que haya en .env manda sobre lo guardado.
        if settings.mister_token:
            self.apply_token(settings.mister_token)
        if settings.mister_xauth:
            self.apply_xauth(settings.mister_xauth)

    def apply_token(self, token: str) -> None:
        """Carga una cookie completa con el formato 'a=1; b=2'."""
        for part in token.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                self._client.cookies.set(name, value)

    def apply_xauth(self, xauth: str) -> None:
        """Cabecera x-auth, obligatoria: sin ella Mister rechaza la peticion."""
        self._client.headers["X-Auth"] = xauth

    def save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        # Se recorre el jar en vez de hacer dict(cookies): Mister emite la misma
        # cookie ('token') para varios dominios y dict() revienta con duplicados.
        payload = {
            "cookies": [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                }
                for cookie in self._client.cookies.jar
            ],
            "headers": {
                name: value
                for name, value in self._client.headers.items()
                if name.lower() == "x-auth"
            },
        }
        self.session_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Contiene credenciales de sesion: que no lo lea nadie mas.
        try:
            self.session_path.chmod(0o600)
        except OSError:
            pass

    def login(self) -> None:
        """Comprueba que hay sesion utilizable.

        Mister no expone un endpoint de login documentado, y adivinarlo seria
        inventar. El flujo soportado es copiar la sesion del navegador con
        `fh mister sesion`, que extrae cookie y x-auth de un comando cURL.
        """
        if not self._client.cookies or not self._client.headers.get("X-Auth"):
            raise NotConfiguredError(
                "Falta la sesion de Mister (cookie y/o cabecera x-auth).\n"
                "  1. Entra en https://mister.mundodeportivo.com con tu navegador\n"
                "  2. DevTools (F12) > pestana Red > recarga\n"
                "  3. Click derecho en una peticion > Copiar > Copiar como cURL\n"
                "  4. Ejecuta: fh mister sesion"
            )
        self.save_session()

    # -- peticiones --------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Peticion con throttling y reintentos con backoff ante 429/5xx."""
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                response = self._client.request(method, path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("error de red en %s (intento %d): %s", path, attempt, exc)
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    # Respetar Retry-After si el servidor lo indica.
                    wait = float(response.headers.get("Retry-After", 2**attempt))
                    log.warning("HTTP %s en %s, esperando %.0fs", response.status_code, path, wait)
                    last_error = AdapterError(f"HTTP {response.status_code} en {path}")
                    time.sleep(wait)
                    continue
                if response.status_code in (401, 403):
                    raise NotConfiguredError(
                        f"Mister devuelve {response.status_code} en {path}: la sesion ha "
                        "caducado. Vuelve a copiar la cookie a MISTER_TOKEN en .env."
                    )
                return response

            time.sleep(2**attempt)

        raise AdapterError(f"No se pudo completar {method} {path}") from last_error

    def fetch(
        self,
        conn: sqlite3.Connection,
        endpoint: Endpoint,
        **values: Any,
    ) -> bytes:
        """Pide un endpoint, guarda el crudo y devuelve el HTML.

        El orden importa: primero se persiste la respuesta, luego se parsea. Si
        el parseo revienta porque Mister cambio el maquetado, el dato del dia ya
        esta a salvo y se puede reprocesar.
        """
        path, params = endpoint.resolve(**values)
        response = self.request(path, method=endpoint.method, params=params)

        save_raw(
            conn,
            source=self.provider,
            endpoint=endpoint.key,
            content=response.content,
            params={"path": path, **params},
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type"),
        )

        # Si la sesion caduca, Mister responde 200 con la pantalla de login en
        # vez de un error, asi que hay que detectarlo por el contenido.
        body = response.content
        if b'id="partial-content"' not in body and b"player-row" not in body:
            if b"login" in body.lower()[:4000]:
                raise NotConfiguredError(
                    f"Mister devolvio la pantalla de login en {path}: la sesion ha "
                    "caducado. Copia de nuevo la cookie a MISTER_TOKEN en .env."
                )
            log.warning("respuesta inesperada en %s (guardada en crudo)", endpoint.key)
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
