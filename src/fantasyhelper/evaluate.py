"""Guardar lo que el modelo predice y comprobar despues si acerto.

POR QUE HAY QUE GUARDARLO Y NO BASTA CON RECALCULAR

Una prediccion solo significa algo si se hizo ANTES. Recalcular en octubre lo
que el modelo habria dicho en agosto no vale: para entonces ya ha visto los
partidos que tenia que adivinar, y `base_averages` mezcla la temporada en curso
con el historico. Saldria un numero muy bueno y completamente falso.

Por eso esto se escribe en cada captura, cuesta lo que cuesta, y no se puede
recuperar hacia atras. Es la misma razon por la que existe `raw_payload`.

LO QUE SI OCURRIO NO SE GUARDA

Los puntos de cada jornada estan en `player_points` y los valores en
`player_value_snapshot`, los dos para siempre. Duplicarlos aqui seria crear dos
versiones de la verdad. El acierto sale de unir la prediccion con ellos.

QUE SIGNIFICA ACERTAR

Para los puntos, la correlacion entre lo predicho y lo real, y el error medio
absoluto en puntos. La correlacion dice si ordena bien -que es para lo que se
usa: elegir entre jugadores- y el error dice si la escala es creible.

Para el valor, la correlacion entre la ventaja predicha sobre el tramo y la
ventaja que realmente tuvo. Ojo: se compara contra la MEDIANA DE SU TRAMO ese
dia, no contra cero, porque es lo unico que el modelo dice predecir.

Un aviso sobre el baremo: con una sola jornada jugada cualquier cifra de estas
es ruido. Hacen falta varias antes de tocar nada del modelo.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from fantasyhelper import market, queries, xpts
from fantasyhelper.config import settings
from fantasyhelper.storage import repository as repo

log = logging.getLogger(__name__)

#: Nombres de los dos modelos dentro de `prediction`.
MODEL_POINTS = "xpts"
MODEL_VALUE = "valor"

#: Observaciones minimas para que un baremo signifique algo. Por debajo se
#: calcula igual pero se marca como no fiable: con veinte jugadores la
#: correlacion salta de 0,2 a 0,7 segun quien se lesione.
MIN_SAMPLES = 30


def record(
    conn: sqlite3.Connection,
    *,
    points: dict[int, dict] | None = None,
    values: dict[int, dict] | None = None,
) -> dict[str, int]:
    """Escribe la prediccion de hoy de los dos modelos.

    Se le pueden pasar ya calculados -la captura los tiene a mano- para no
    recorrer el historico otra vez.
    """
    puntos = points if points is not None else xpts.expected_points(conn)
    valores = values if values is not None else market.forecast(conn)

    # La probabilidad de once no esta dentro de `expected_points` cuando se
    # llama sin ella, asi que se aplica aqui: sin ella se estaria guardando
    # "cuanto haria SI juega", que es otra cosa distinta y no comparable con
    # los puntos que aparezcan en el acta.
    estados = {
        fila["id"]: dict(fila)
        for fila in conn.execute(
            f"WITH {queries.LATEST_PROB_CTE} "
            "SELECT p.id, lp.probability, lp.status FROM player p "
            "LEFT JOIN latest_prob lp ON lp.player_id = p.id AND lp.rn = 1"
        )
    }

    escritas = {MODEL_POINTS: 0, MODEL_VALUE: 0}

    for player_id, prediccion in puntos.items():
        if prediccion["matchday"] is None:
            continue  # sin proximo partido conocido no hay nada que predecir
        probabilidad = xpts.playing_probability(estados.get(player_id, {}))
        repo.record_prediction(
            conn,
            model=MODEL_POINTS,
            player_id=player_id,
            season=settings.season,
            matchday=prediccion["matchday"],
            predicted=prediccion["xpts_si_juega"] * probabilidad,
            detail={
                "media_base": round(prediccion["media_base"], 4),
                "probabilidad": round(probabilidad, 4),
                "ajuste_rival": round(prediccion["ajuste_rival"], 4),
                "ajuste_sede": round(prediccion["ajuste_sede"], 4),
                "rival": prediccion["opponent"],
                "en_casa": prediccion["is_home"],
            },
        )
        escritas[MODEL_POINTS] += 1

    for player_id, prediccion in valores.items():
        objetivo = (
            date.fromisoformat(prediccion["desde"])
            + timedelta(days=market.LOOKBACK_DAYS + market.HORIZON_DAYS)
        ).isoformat()
        repo.record_prediction(
            conn,
            model=MODEL_VALUE,
            player_id=player_id,
            season=settings.season,
            horizon_date=objetivo,
            predicted=prediccion["ventaja"],
            # El valor de partida, porque el de dentro de una semana ya no lo es
            # y sin el la prediccion no se puede pasar a euros despues.
            baseline=prediccion["euros_ventaja"] / prediccion["ventaja"]
            if prediccion["ventaja"]
            else None,
            detail={
                "tramo": prediccion["tramo"],
                "cambio_reciente": round(prediccion["cambio_reciente"], 5),
                "centro_tramo": round(prediccion["centro_tramo"], 5),
                "fiabilidad": round(prediccion["fiabilidad"], 4),
            },
        )
        escritas[MODEL_VALUE] += 1

    return escritas


# --- comprobar el acierto ---------------------------------------------------


@dataclass
class Score:
    """Como de bien funciono un modelo sobre un conjunto de casos."""

    etiqueta: str
    casos: int
    correlacion: float | None
    error_medio: float | None
    #: Media de lo predicho y de lo real, para ver si el modelo va sesgado
    #: entero hacia arriba o hacia abajo.
    media_predicha: float | None
    media_real: float | None

    @property
    def fiable(self) -> bool:
        return self.casos >= MIN_SAMPLES


def _score(etiqueta: str, pares: list[tuple[float, float]]) -> Score:
    if not pares:
        return Score(etiqueta, 0, None, None, None, None)

    predichos = [p for p, _ in pares]
    reales = [r for _, r in pares]
    error = sum(abs(p - r) for p, r in pares) / len(pares)

    correlacion = None
    sp, sr = statistics.pstdev(predichos), statistics.pstdev(reales)
    if sp and sr and len(pares) > 1:
        mp, mr = statistics.mean(predichos), statistics.mean(reales)
        cov = sum((p - mp) * (r - mr) for p, r in pares) / len(pares)
        correlacion = cov / (sp * sr)

    return Score(
        etiqueta, len(pares), correlacion, error,
        statistics.mean(predichos), statistics.mean(reales),
    )


#: La prediccion que cuenta es la ULTIMA de cada jugador para esa jornada: la
#: hecha con las alineaciones probables ya publicadas, que es la que se habria
#: mirado al decidir. Las anteriores se guardan igual y sirven para ver cuanto
#: mejora segun se acerca el partido.
_ULTIMA_POR_JORNADA = """
    SELECT p.player_id, p.matchday, p.predicted, p.made_on, p.detail_json,
           ROW_NUMBER() OVER (
               PARTITION BY p.player_id, p.matchday ORDER BY p.made_on DESC
           ) AS rn
    FROM prediction p
    WHERE p.model = ? AND p.season = ?
"""


def points_accuracy(
    conn: sqlite3.Connection, *, matchday: int | None = None
) -> list[Score]:
    """Acierto del modelo de puntos, jornada a jornada y en total.

    Solo entran los jugadores de los que consta acta: si un jugador no aparece
    en `player_points` es que no jugo, y eso ya lo dice la probabilidad. Meterlo
    como un cero mediria otra cosa -si acertamos quien juega- y ademas inflaria
    la correlacion, porque acertar los ceros es facil.
    """
    condicion = "AND u.matchday = ?" if matchday is not None else ""
    params: list[object] = [MODEL_POINTS, settings.season, settings.season]
    if matchday is not None:
        params.append(matchday)

    filas = conn.execute(
        f"""
        WITH ultima AS ({_ULTIMA_POR_JORNADA})
        SELECT u.matchday, u.predicted, pp.points AS real
        FROM ultima u
        JOIN player_points pp
          ON pp.player_id = u.player_id AND pp.matchday = u.matchday
         AND pp.provider = 'mister' AND pp.season = ?
        WHERE u.rn = 1 AND pp.points IS NOT NULL {condicion}
        ORDER BY u.matchday
        """,
        params,
    ).fetchall()

    por_jornada: dict[int, list[tuple[float, float]]] = {}
    for fila in filas:
        por_jornada.setdefault(fila["matchday"], []).append(
            (fila["predicted"], float(fila["real"]))
        )

    scores = [_score(f"J{j}", pares) for j, pares in sorted(por_jornada.items())]
    if len(scores) > 1:
        todos = [par for pares in por_jornada.values() for par in pares]
        scores.append(_score("total", todos))
    return scores


def value_accuracy(conn: sqlite3.Connection) -> list[Score]:
    """Acierto del modelo de valor: ventaja predicha contra ventaja real.

    Contra la mediana de su tramo y no contra cero, porque el modelo no dice
    "va a subir un 2%": dice "va a subir un 2% MAS que los de su precio". Si
    sube todo el tramo eso no es un acierto suyo, y si baja todo no es un fallo.
    """
    filas = conn.execute(
        """
        SELECT p.player_id, p.made_on, p.horizon_date, p.predicted,
               p.detail_json, inicio.market_value AS valor_inicial,
               final.market_value AS valor_final
        FROM prediction p
        JOIN player_value_snapshot inicio
          ON inicio.player_id = p.player_id AND inicio.provider = 'mister'
         AND inicio.source = 'mister' AND inicio.snapshot_date = p.made_on
        JOIN player_value_snapshot final
          ON final.player_id = p.player_id AND final.provider = 'mister'
         AND final.source = 'mister' AND final.snapshot_date = p.horizon_date
        WHERE p.model = ? AND p.season = ?
        """,
        (MODEL_VALUE, settings.season),
    ).fetchall()

    # Se agrupa por dia y tramo para poder restar la mediana real de cada uno,
    # que es contra lo que se comparo al predecir.
    por_dia: dict[tuple[str, str], list[dict]] = {}
    for fila in filas:
        if not fila["valor_inicial"]:
            continue
        detalle = json.loads(fila["detail_json"] or "{}")
        tramo = detalle.get("tramo") or market.band_of(fila["valor_inicial"])
        real = (fila["valor_final"] - fila["valor_inicial"]) / fila["valor_inicial"]
        por_dia.setdefault((fila["made_on"], tramo), []).append(
            {"predicho": fila["predicted"], "real": real}
        )

    por_tramo: dict[str, list[tuple[float, float]]] = {}
    for (_, tramo), casos in por_dia.items():
        if len(casos) < market.MIN_PLAYERS_PER_BAND:
            continue
        centro = statistics.median(c["real"] for c in casos)
        por_tramo.setdefault(tramo, []).extend(
            (c["predicho"], c["real"] - centro) for c in casos
        )

    orden = [nombre for _, _, nombre in market.PRICE_BANDS]
    scores = [
        _score(tramo, por_tramo[tramo]) for tramo in orden if tramo in por_tramo
    ]
    if len(scores) > 1:
        todos = [par for pares in por_tramo.values() for par in pares]
        scores.append(_score("total", todos))
    return scores


def coverage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cuantas predicciones hay guardadas y de que dias. Para saber si esto corre."""
    return conn.execute(
        """
        SELECT model, COUNT(*) AS filas, COUNT(DISTINCT made_on) AS dias,
               MIN(made_on) AS desde, MAX(made_on) AS hasta
        FROM prediction WHERE season = ?
        GROUP BY model ORDER BY model
        """,
        (settings.season,),
    ).fetchall()
