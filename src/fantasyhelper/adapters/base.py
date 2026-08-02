"""Contrato que cumple cada juego fantasy.

Todo lo especifico de un juego (autenticacion, endpoints, reglas de mercado,
sistema de puntos) vive detras de esta interfaz. El motor de decision solo ve
el modelo canonico, asi que anadir Biwenger o LaLiga Fantasy es escribir un
adapter nuevo, no tocar el analisis.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable


@runtime_checkable
class FantasyAdapter(Protocol):
    """Interfaz minima de un proveedor de fantasy."""

    provider: str

    def login(self) -> None:
        """Autentica y deja la sesion lista. Debe reutilizar sesion guardada si vale."""
        ...

    def snapshot(self, conn: sqlite3.Connection) -> int:
        """Captura el estado de hoy y lo escribe en la BD.

        Debe ser idempotente: ejecutarlo dos veces el mismo dia actualiza filas,
        no las duplica. Devuelve el numero de filas escritas.

        Obligacion de todo adapter: guardar el crudo con `save_raw` ANTES de
        parsear. Si el parseo falla, el dato no se pierde.
        """
        ...


class AdapterError(RuntimeError):
    """Fallo recuperable de un adapter (login caducado, endpoint cambiado...)."""


class NotConfiguredError(AdapterError):
    """Faltan credenciales o configuracion en .env."""
