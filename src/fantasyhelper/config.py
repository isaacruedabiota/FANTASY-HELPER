"""Configuracion global, leida de .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


#: Minimo permitido para la captura periodica. Cada pasada son unas 45
#: peticiones entre Mister y FutbolFantasy; por debajo de cinco minutos se
#: encadenarian sin haber terminado la anterior y el ritmo contra Mister seria
#: dificil de justificar.
MIN_CAPTURE_MINUTES = 5


@dataclass(frozen=True)
class Settings:
    season: str
    db_path: Path
    data_dir: Path
    log_level: str

    mister_token: str | None
    mister_xauth: str | None
    mister_league_id: str | None

    #: Cada cuantos minutos capturar, o None para las dos capturas diarias de
    #: siempre. Es frescura y no mas historico: los snapshots son una fila por
    #: dia, asi que capturar mas a menudo reescribe la misma fila.
    capture_every_minutes: int | None

    @property
    def mister_configured(self) -> bool:
        # x-auth es tan obligatorio como la cookie: con una sola no autentica.
        return bool(self.mister_token and self.mister_xauth)


def _env(key: str) -> str | None:
    value = os.getenv(key, "").strip()
    return value or None


def _capture_interval() -> int | None:
    """Minutos entre capturas, validado.

    Un valor ilegible no puede tumbar el planificador ni, peor, colarse como un
    intervalo absurdo: se avisa y se sigue con las capturas diarias.
    """
    crudo = _env("FH_CAPTURE_EVERY_MINUTES")
    if crudo is None:
        return None
    try:
        minutos = int(crudo)
    except ValueError:
        logging.getLogger(__name__).warning(
            "FH_CAPTURE_EVERY_MINUTES=%r no es un numero; se ignora", crudo
        )
        return None
    if minutos < MIN_CAPTURE_MINUTES:
        logging.getLogger(__name__).warning(
            "FH_CAPTURE_EVERY_MINUTES=%d es demasiado seguido; se sube a %d",
            minutos, MIN_CAPTURE_MINUTES,
        )
        return MIN_CAPTURE_MINUTES
    return minutos


def load_settings() -> Settings:
    data_dir = PROJECT_ROOT / "data"
    db_path = Path(_env("FH_DB_PATH") or data_dir / "fantasyhelper.db")
    return Settings(
        season=_env("FH_SEASON") or "2026-27",
        db_path=db_path,
        data_dir=data_dir,
        log_level=_env("FH_LOG_LEVEL") or "INFO",
        mister_token=_env("MISTER_TOKEN"),
        mister_xauth=_env("MISTER_XAUTH"),
        mister_league_id=_env("MISTER_LEAGUE_ID"),
        capture_every_minutes=_capture_interval(),
    )


settings = load_settings()


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx registra cada peticion a INFO y ensucia la salida del job.
    logging.getLogger("httpx").setLevel(logging.WARNING)
