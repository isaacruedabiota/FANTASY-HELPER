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
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "es-ES,es;q=0.9",
            },
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        self._load_session()

    # -- sesion ------------------------------------------------------------

    def _load_session(self) -> None:
        """Restaura cookies y token de la ejecucion anterior."""
        if self.session_path.exists():
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
            for name, value in data.get("cookies", {}).items():
                self._client.cookies.set(name, value)
            if token := data.get("token"):
                self._client.headers["Authorization"] = f"Bearer {token}"
            log.debug("sesion restaurada de %s", self.session_path)

        # El token de .env manda sobre lo guardado.
        if settings.mister_token:
            self._apply_token(settings.mister_token)

    def _apply_token(self, token: str) -> None:
        """Acepta tanto un bearer token como una cookie completa ('a=1; b=2')."""
        if "=" in token and ";" in token or token.count("=") > 1:
            for part in token.split(";"):
                if "=" in part:
                    name, _, value = part.strip().partition("=")
                    self._client.cookies.set(name, value)
        else:
            self._client.headers["Authorization"] = f"Bearer {token}"

    def save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        auth = self._client.headers.get("Authorization", "")
        payload = {
            "cookies": dict(self._client.cookies),
            "token": auth.removeprefix("Bearer ").strip() or None,
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
        inventar. El flujo soportado es: inicias sesion en el navegador, copias
        la cookie de sesion a MISTER_TOKEN en .env (o exportas un HAR) y el
        cliente la reutiliza.
        """
        if not (self._client.cookies or self._client.headers.get("Authorization")):
            raise NotConfiguredError(
                "No hay sesion de Mister.\n"
                "  1. Entra en https://mister.mundodeportivo.com con tu navegador\n"
                "  2. DevTools (F12) > pestana Red > recarga > click derecho > "
                "'Guardar todo como HAR'\n"
                "  3. Ejecuta: fh mister har <fichero.har>\n"
                "Eso rellena los endpoints y la cookie de sesion de una vez."
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
    ) -> Any:
        """Pide un endpoint, guarda el crudo y devuelve el JSON parseado.

        El orden importa: primero se persiste la respuesta, luego se parsea. Si
        el parseo revienta porque Mister cambio el formato, el dato del dia ya
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

        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            log.warning("respuesta no-JSON en %s (guardada en crudo)", endpoint.key)
            return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
