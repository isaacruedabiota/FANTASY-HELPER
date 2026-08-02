"""Registro de endpoints de Mister.

Mister no publica API. Los endpoints se descubren exportando un HAR desde las
DevTools del navegador (`fh mister har`) y se guardan en
`data/mister_endpoints.json`, no en el codigo: cuando Mister cambie una ruta a
mitad de temporada se arregla editando un JSON, sin tocar Python.

Claves que necesita el snapshot diario:
    squad       -> mi plantilla (valor, clausula, precio de compra)
    market      -> mercado del dia (jugadores y precios de salida)
    standings   -> clasificacion de la liga y saldo de cada rival
    teams       -> plantillas de los rivales (para el radar de clausulas)
    players     -> catalogo de jugadores con su valor de mercado
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from fantasyhelper.config import settings

log = logging.getLogger(__name__)

ENDPOINTS_PATH = settings.data_dir / "mister_endpoints.json"

#: Claves que el job de snapshot intentara capturar, en este orden.
REQUIRED_KEYS = ("players", "market", "squad", "standings", "teams")


@dataclass
class Endpoint:
    key: str
    path: str
    method: str = "GET"
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


def load_endpoints() -> dict[str, Endpoint]:
    """Lee los endpoints configurados. Devuelve {} si aun no se ha importado el HAR."""
    if not ENDPOINTS_PATH.exists():
        return {}
    data = json.loads(ENDPOINTS_PATH.read_text(encoding="utf-8"))
    return {
        key: Endpoint(key=key, **spec)
        for key, spec in data.get("endpoints", {}).items()
    }


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
