"""Registro de endpoints de Mister.

Mister no tiene API JSON. Su web app hace POST a estas rutas con la cabecera
`X-Requested-With: XMLHttpRequest` y recibe fragmentos de HTML renderizado.
Las rutas estan confirmadas leyendo el trafico real del navegador (HAR).

Se pueden sobreescribir desde `data/mister_endpoints.json` sin tocar codigo:
el dia que Mister cambie una ruta a mitad de temporada, se edita el JSON.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from fantasyhelper.config import settings

log = logging.getLogger(__name__)

ENDPOINTS_PATH = settings.data_dir / "mister_endpoints.json"


@dataclass
class Endpoint:
    key: str
    path: str
    method: str = "POST"
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def resolve(self, **values: Any) -> tuple[str, dict[str, Any]]:
        """Sustituye placeholders tipo {league_id} en la ruta y en los parametros."""
        path = self.path.format(**values)
        params = {
            k: (v.format(**values) if isinstance(v, str) else v)
            for k, v in self.params.items()
        }
        return path, params


#: Rutas confirmadas contra el trafico real de la web app.
DEFAULT_ENDPOINTS: dict[str, Endpoint] = {
    # Catalogo completo de jugadores con su valor de mercado. Pagina de 50 en 50.
    "search": Endpoint("search", "/search", note="catalogo de jugadores"),
    # Mercado del dia de tu liga.
    "market": Endpoint("market", "/market", note="mercado diario"),
    # Tu plantilla.
    "squad": Endpoint("squad", "/team", note="plantilla propia"),
    # Clasificacion: rivales, puntos y valor de sus plantillas.
    "standings": Endpoint("standings", "/standings", note="clasificacion de la liga"),
    # Movimientos de la liga: fichajes, ventas, clausulazos y altas.
    "feed": Endpoint("feed", "/feed", note="movimientos de la liga"),
}

REQUIRED_KEYS = tuple(DEFAULT_ENDPOINTS)


def load_endpoints() -> dict[str, Endpoint]:
    """Endpoints por defecto, con lo que haya en el JSON sobreescribiendo encima."""
    endpoints = dict(DEFAULT_ENDPOINTS)

    if ENDPOINTS_PATH.exists():
        data = json.loads(ENDPOINTS_PATH.read_text(encoding="utf-8"))
        for key, spec in data.get("endpoints", {}).items():
            endpoints[key] = Endpoint(key=key, **spec)
            log.debug("endpoint '%s' sobreescrito desde %s", key, ENDPOINTS_PATH.name)

    return endpoints


def save_endpoints(endpoints: dict[str, Endpoint], candidates: list[dict] | None = None) -> None:
    """Escribe el registro, conservando los candidatos detectados en el HAR."""
    ENDPOINTS_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if ENDPOINTS_PATH.exists():
        existing = json.loads(ENDPOINTS_PATH.read_text(encoding="utf-8"))

    payload = {
        "endpoints": {
            key: {k: v for k, v in asdict(ep).items() if k != "key"}
            for key, ep in endpoints.items()
        },
        "candidates": candidates if candidates is not None else existing.get("candidates", []),
    }
    ENDPOINTS_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("endpoints guardados en %s", ENDPOINTS_PATH)


def missing_keys() -> list[str]:
    configured = load_endpoints()
    return [k for k in REQUIRED_KEYS if k not in configured]
