"""Bonificaciones de la liga.

Mister no publica estas reglas por ninguna via legible: estan en la pantalla de
configuracion de la comunidad y hay que transcribirlas. Se guardan por liga en
la base de datos para que el dia que cambien no haya que tocar codigo.

Importan para el saldo: cada jornada, las bonificaciones ingresan dinero a todos
los participantes, y sin contarlas el saldo estimado de los rivales se queda
corto jornada tras jornada.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class BonusRules:
    """Que paga la liga por cada concepto. Importes en euros."""

    #: Por cada punto que sume el equipo en la jornada.
    per_point: int = 0
    #: Por puesto en la clasificacion de la jornada: {puesto: importe}.
    by_matchday_rank: dict[int, int] = field(default_factory=dict)
    #: Por cada jugador propio que entre en el once ideal de la jornada.
    per_ideal_xi_player: int = 0
    #: Por gol anotado por un jugador propio.
    per_goal: int = 0
    #: Cantidad fija que cobra todo el mundo cada jornada.
    fixed_per_matchday: int = 0
    #: Por acierto en la quiniela.
    per_quiniela_hit: int = 0

    def matchday_total(
        self,
        *,
        points: int = 0,
        rank: int | None = None,
        ideal_xi_players: int = 0,
        goals: int = 0,
        quiniela_hits: int = 0,
    ) -> int:
        """Lo que ingresa un participante en una jornada."""
        total = self.fixed_per_matchday
        total += self.per_point * max(points, 0)
        total += self.per_ideal_xi_player * ideal_xi_players
        total += self.per_goal * goals
        total += self.per_quiniela_hit * quiniela_hits
        if rank is not None:
            total += self.by_matchday_rank.get(rank, 0)
        return total

    def to_json(self) -> str:
        return json.dumps(
            {
                "per_point": self.per_point,
                "by_matchday_rank": {str(k): v for k, v in self.by_matchday_rank.items()},
                "per_ideal_xi_player": self.per_ideal_xi_player,
                "per_goal": self.per_goal,
                "fixed_per_matchday": self.fixed_per_matchday,
                "per_quiniela_hit": self.per_quiniela_hit,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> BonusRules:
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(
            per_point=data.get("per_point", 0),
            by_matchday_rank={
                int(k): v for k, v in (data.get("by_matchday_rank") or {}).items()
            },
            per_ideal_xi_player=data.get("per_ideal_xi_player", 0),
            per_goal=data.get("per_goal", 0),
            fixed_per_matchday=data.get("fixed_per_matchday", 0),
            per_quiniela_hit=data.get("per_quiniela_hit", 0),
        )


#: Configuracion de LA LIGA 26/27, transcrita de la pantalla de ajustes.
#:
#: Ojo con la escala por clasificacion: el primero cobra menos que el ultimo.
#: Es lo que muestra la configuracion, aunque vaya al reves de lo que uno
#: esperaria de un premio; si algun dia no cuadran los saldos, es el primer
#: sitio donde mirar.
LA_LIGA_2627 = BonusRules(
    per_point=75_000,
    by_matchday_rank={
        1: 200_000,
        2: 300_000,
        3: 400_000,
        4: 500_000,
        5: 600_000,
        6: 700_000,
        7: 800_000,
        8: 1_000_000,
        9: 1_200_000,
        10: 1_400_000,
    },
    per_ideal_xi_player=250_000,
    per_goal=0,          # desactivado en la liga
    fixed_per_matchday=0,  # desactivado en la liga
    per_quiniela_hit=50_000,
)


def load_rules(conn: sqlite3.Connection, league_id: int | None = None) -> BonusRules:
    """Lee las bonificaciones configuradas para una liga."""
    if league_id is None:
        row = conn.execute(
            "SELECT bonus_rules FROM league WHERE bonus_rules IS NOT NULL LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT bonus_rules FROM league WHERE id = ?", (league_id,)
        ).fetchone()
    return BonusRules.from_json(row["bonus_rules"] if row else None)


def save_rules(conn: sqlite3.Connection, rules: BonusRules, league_id: int | None = None) -> int:
    if league_id is None:
        return conn.execute(
            "UPDATE league SET bonus_rules = ?", (rules.to_json(),)
        ).rowcount
    return conn.execute(
        "UPDATE league SET bonus_rules = ? WHERE id = ?", (rules.to_json(), league_id)
    ).rowcount
