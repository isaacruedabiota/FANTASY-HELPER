"""Puntos esperados por jornada (xPts).

Hasta ahora el radar de clausulas ordenaba por "coste ajustado", que era la
clausula dividida entre la probabilidad de jugar y la jerarquia. Servia para
ordenar, pero no significaba nada: no se podia decir si 4,2 era caro o barato.

xPts convierte eso en una unidad con sentido -puntos- y por tanto permite la
pregunta que de verdad importa: cuantos euros cuesta cada punto esperado.

    xPts = P(juega) x media_base x ajuste_rival x ajuste_sede

Cada factor sale de un sitio distinto y con una fiabilidad distinta:

  P(juega)      probabilidad de once de FutbolFantasy. Es un dato, no una
                estimacion nuestra. Si el jugador no esta disponible es 0.
  media_base    puntos por jornada que cabe esperar del jugador. Mezcla su
                historico con lo que lleve hecho esta temporada, dando cada vez
                mas peso a lo segundo (ver `blended_average`).
  ajuste_rival  la fuerza del rival, normalizada. Es lo mas discutible del
                modelo y esta acotado a +-25% a proposito.
  ajuste_sede   local o visitante. Un prior fijo hasta que haya datos propios.

Sobre lo que este modulo NO hace: no predice el valor de mercado ni el
resultado del partido, y no distingue por que un jugador rindio poco. Es una
media condicionada, no una prediccion de la jornada.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fantasyhelper.config import settings

#: Cuanto pesa cada temporada anterior respecto a la siguiente. Con 0.55, la
#: ultima temporada vale casi el doble que la anterior y cuatro anos atras ya
#: no mueve la aguja. El futbol de hace tres temporadas dice poco del de ahora.
SEASON_DECAY = 0.55

#: Jornadas de una temporada completa de Liga. Una temporada de 6 partidos
#: (lesion, fichaje en enero) no puede pesar como una de 38, aunque la media
#: sea la misma: la media de 6 partidos es mucho mas ruidosa.
FULL_SEASON = 38

#: Jornadas de la temporada en curso que hacen falta para que lo de este ano
#: pese tanto como todo el historico. Es la fuerza del prior: con 6, en la
#: jornada 6 ya van al 50%, y en la 20 el historico casi no cuenta.
#: Un numero bajo reacciona rapido pero se cree cualquier racha de dos partidos.
CURRENT_SEASON_PRIOR = 6.0

#: Jornadas de credito que se le dan de oficio a la media de su posicion. Quien
#: tenga menos evidencia que esto se parece mas a un jugador cualquiera de su
#: puesto que a lo que digan sus cuatro partidos.
PRIOR_EVIDENCE = 10.0

#: Evidencia minima para que un jugador sirva de referencia al calcular la
#: media de su posicion. Con menos, la referencia la marcarian precisamente los
#: casos raros que se quieren corregir.
MIN_EVIDENCE_FOR_PRIOR = FULL_SEASON

#: Cuanto mueve la fuerza del rival al resultado esperado. Un 0.25 significa
#: que enfrentarse al mejor equipo de la liga descuenta como mucho un 25%.
#: Es un prior, no una medicion: no tenemos datos de puntos concedidos, solo
#: la calidad del rival como aproximacion.
OPPONENT_WEIGHT = 0.25
OPPONENT_CLAMP = (0.75, 1.25)

#: Ventaja de jugar en casa. El consenso en futbol ronda el 5-10% y aqui se
#: queda en el extremo bajo porque parte de esa ventaja ya esta recogida en la
#: media del jugador. Se refinara con los desgloses casa/fuera que Mister
#: publica en la ficha, hoy vacios porque no se ha jugado nada.
HOME_ADVANTAGE = 1.05
AWAY_PENALTY = 0.95

#: Jugadores con historico que hacen falta para fiarse de la fuerza de un
#: equipo. Por debajo se usa la mediana de la liga: un recien ascendido del que
#: solo conocemos a dos jugadores no es que sea flojo, es que no lo sabemos.
MIN_PLAYERS_FOR_STRENGTH = 8

#: Cuantos jugadores definen la fuerza de un equipo. Se toman los mejores, no
#: todos: la plantilla entera diluye la calidad con suplentes que no juegan.
SQUAD_CORE = 11


@dataclass
class Adjustments:
    """Los factores que se aplican a la media base, ya resueltos."""

    opponent: float = 1.0
    venue: float = 1.0

    @property
    def combined(self) -> float:
        return self.opponent * self.venue


def blended_average(
    history: list[tuple[str, float, int]],
    *,
    current_points: int | None = None,
    current_matchdays: int = 0,
    prior: float | None = None,
) -> float | None:
    """Puntos por jornada esperados de un jugador.

    `history` son las temporadas pasadas como (temporada, media, partidos
    jugados), de la mas reciente a la mas antigua.

    Tres mezclas encadenadas. Primero las temporadas pasadas entre si, con menos
    peso segun se alejan y segun menos partidos tengan. Despues esa media
    historica con lo que lleve hecho esta temporada, que va ganando peso jornada
    a jornada: en la 1 no dice casi nada, en la 20 lo dice casi todo. Y por
    ultimo, si se pasa un `prior`, el resultado se acerca a el en proporcion a
    lo POCO que sepamos del jugador.

    Ese ultimo paso no sobra. Sin el, alguien que jugo un solo partido y saco 12
    puntos aparecia con una media de 12, por delante de cualquier crack: el peso
    por fiabilidad solo compara temporadas entre si, y con una unica temporada
    no compara nada. Se vio en la primera tabla que salio.

    Devuelve None si no hay ni historico ni temporada en curso, que es el caso
    de un debutante. None no es cero: significa que no lo sabemos, y quien
    llama decide que hacer con ello.
    """
    media, evidencia = _weighted_history(history)

    if current_matchdays and current_points is not None:
        actual = current_points / current_matchdays
        if media is None:
            media, evidencia = actual, float(current_matchdays)
        else:
            media = (
                current_matchdays * actual + CURRENT_SEASON_PRIOR * media
            ) / (current_matchdays + CURRENT_SEASON_PRIOR)
            evidencia += current_matchdays

    if media is None:
        return None
    if prior is None:
        return media
    return (evidencia * media + PRIOR_EVIDENCE * prior) / (evidencia + PRIOR_EVIDENCE)


def _weighted_history(
    history: list[tuple[str, float, int]],
) -> tuple[float | None, float]:
    """Media de las temporadas pasadas y cuantos partidos la respaldan."""
    numerador = denominador = evidencia = 0.0
    for indice, (_, avg, matches) in enumerate(history):
        if avg is None:
            continue
        jugados = min(matches or 0, FULL_SEASON)
        peso = (SEASON_DECAY**indice) * (jugados / FULL_SEASON)
        numerador += peso * avg
        denominador += peso
        # La evidencia se descuenta igual que el peso: lo de hace tres anos
        # cuenta, pero no como si fuera de ayer.
        evidencia += (SEASON_DECAY**indice) * jugados

    return (numerador / denominador if denominador else None), evidencia


def team_strength(conn: sqlite3.Connection) -> dict[int, float]:
    """Calidad de cada equipo, medida en puntos por jornada de su once.

    Se usa la media de los SQUAD_CORE mejores jugadores de la plantilla ACTUAL
    segun su ultima temporada registrada. Dos decisiones que importan:

    - plantilla actual, rendimiento pasado: un equipo que ha vendido a su
      goleador es hoy mas debil, aunque los puntos del goleador sigan en la
      base de datos atribuidos a ese club.
    - los mejores y no todos: la plantilla entera diluye la calidad con
      suplentes, y quien te hace dano son los que juegan.
    """
    filas = conn.execute(
        """
        WITH ultima AS (
            SELECT s.player_id, s.avg_points,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.player_id ORDER BY s.season DESC
                   ) AS rn
            FROM player_season_stat s
            WHERE s.avg_points IS NOT NULL AND s.matches_played > 0
        ),
        ranking AS (
            SELECT p.team_id, u.avg_points,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.team_id ORDER BY u.avg_points DESC
                   ) AS puesto
            FROM player p
            JOIN ultima u ON u.player_id = p.id AND u.rn = 1
            WHERE p.team_id IS NOT NULL
        )
        SELECT team_id, AVG(avg_points) AS fuerza, COUNT(*) AS jugadores
        FROM ranking
        WHERE puesto <= ?
        GROUP BY team_id
        """,
        (SQUAD_CORE,),
    ).fetchall()

    fiables = {
        f["team_id"]: f["fuerza"]
        for f in filas
        if f["jugadores"] >= MIN_PLAYERS_FOR_STRENGTH
    }
    if not fiables:
        return {}

    # A los equipos de los que sabemos poco se les da la fuerza mediana en vez
    # de la suya: no tener datos no es ser malo.
    ordenadas = sorted(fiables.values())
    mediana = ordenadas[len(ordenadas) // 2]
    return {f["team_id"]: fiables.get(f["team_id"], mediana) for f in filas}


def opponent_factor(strength: dict[int, float], opponent_id: int | None) -> float:
    """Cuanto penaliza enfrentarse a un rival, entre 0.75 y 1.25."""
    if opponent_id is None or not strength:
        return 1.0
    ordenadas = sorted(strength.values())
    mediana = ordenadas[len(ordenadas) // 2]
    if not mediana:
        return 1.0

    rival = strength.get(opponent_id, mediana)
    factor = 1.0 - OPPONENT_WEIGHT * (rival / mediana - 1.0)
    return max(OPPONENT_CLAMP[0], min(OPPONENT_CLAMP[1], factor))


def next_fixtures(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """Proximo partido de cada equipo: {team_id: fila con rival, sede y jornada}.

    El calendario se rellena solo: cada ficha de jugador trae su proximo
    partido, asi que una captura diaria basta para tener siempre la jornada
    siguiente completa. Las posteriores aun no se conocen.
    """
    filas = conn.execute(
        """
        WITH proximos AS (
            SELECT matchday, kickoff_utc, home_team_id AS team_id,
                   away_team_id AS opponent_id, 1 AS is_home
            FROM fixture
            WHERE season = ? AND status != 'finished'
            UNION ALL
            SELECT matchday, kickoff_utc, away_team_id, home_team_id, 0
            FROM fixture
            WHERE season = ? AND status != 'finished'
        ),
        ordenados AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY team_id ORDER BY matchday, kickoff_utc
            ) AS rn
            FROM proximos
        )
        SELECT o.team_id, o.opponent_id, o.is_home, o.matchday, o.kickoff_utc,
               t.name AS opponent
        FROM ordenados o
        LEFT JOIN team t ON t.id = o.opponent_id
        WHERE o.rn = 1
        """,
        (settings.season, settings.season),
    ).fetchall()
    return {fila["team_id"]: fila for fila in filas}


#: Historico por jugador junto con lo que lleva hecho esta temporada.
#: Se saca de una vez para todos y se mezcla en Python: el calculo tiene
#: suficientes ramas como para que en SQL fuese ilegible.
_HISTORY_SQL = """
    SELECT player_id, season, avg_points, matches_played
    FROM player_season_stat
    WHERE provider = 'mister' AND season != ?
    ORDER BY player_id, season DESC
"""

_CURRENT_SQL = """
    SELECT player_id, SUM(points) AS points, COUNT(*) AS matchdays
    FROM player_points
    WHERE provider = 'mister' AND season = ? AND points IS NOT NULL
    GROUP BY player_id
"""


def base_averages(conn: sqlite3.Connection) -> dict[int, float]:
    """Media base de todos los jugadores de los que se sabe algo.

    Dos pasadas. En la primera se calcula la media de cada jugador y cuanta
    evidencia la respalda; con eso se saca la media tipica de cada posicion,
    usando solo a los que tienen recorrido. En la segunda, cada jugador se
    acerca a la media de su puesto en proporcion a lo poco que sepamos de el.

    La referencia va por posicion y no por liga entera porque las escalas no
    son comparables: un portero puntua de otra manera que un delantero, y
    algunos hasta en negativo. Compararlos contra una media unica desplazaria a
    todos los porteros hacia arriba y a todos los delanteros hacia abajo.
    """
    history: dict[int, list[tuple[str, float, int]]] = {}
    for fila in conn.execute(_HISTORY_SQL, (settings.season,)):
        history.setdefault(fila["player_id"], []).append(
            (fila["season"], fila["avg_points"], fila["matches_played"] or 0)
        )

    current = {
        fila["player_id"]: (fila["points"], fila["matchdays"])
        for fila in conn.execute(_CURRENT_SQL, (settings.season,))
    }
    posiciones = {
        fila["id"]: fila["position"]
        for fila in conn.execute("SELECT id, position FROM player")
    }

    crudas: dict[int, tuple[float, float]] = {}
    for player_id in set(history) | set(current):
        puntos, jornadas = current.get(player_id, (None, 0))
        media = blended_average(
            history.get(player_id, []),
            current_points=puntos,
            current_matchdays=jornadas,
        )
        if media is None:
            continue
        _, evidencia = _weighted_history(history.get(player_id, []))
        crudas[player_id] = (media, evidencia + jornadas)

    referencia = _position_priors(crudas, posiciones)

    return {
        player_id: blended_average(
            history.get(player_id, []),
            current_points=current.get(player_id, (None, 0))[0],
            current_matchdays=current.get(player_id, (None, 0))[1],
            prior=referencia.get(posiciones.get(player_id)),
        )
        for player_id in crudas
    }


def _position_priors(
    crudas: dict[int, tuple[float, float]], posiciones: dict[int, str | None]
) -> dict[str | None, float]:
    """Media tipica de cada posicion, sobre los jugadores con recorrido."""
    por_posicion: dict[str | None, list[float]] = {}
    for player_id, (media, evidencia) in crudas.items():
        if evidencia >= MIN_EVIDENCE_FOR_PRIOR:
            por_posicion.setdefault(posiciones.get(player_id), []).append(media)

    return {
        posicion: sorted(medias)[len(medias) // 2]
        for posicion, medias in por_posicion.items()
        if medias
    }


def expected_points(
    conn: sqlite3.Connection,
    *,
    probabilities: dict[int, float] | None = None,
) -> dict[int, dict]:
    """xPts de cada jugador para su proxima jornada.

    `probabilities` permite inyectar la probabilidad de once ya resuelta; si no
    se pasa, se aplica solo la parte que depende del jugador y del calendario y
    quien llama multiplica por la probabilidad en su propia consulta. Esto es
    para que las consultas SQL, que ya saben de probabilidades y estados, no
    tengan que duplicar la logica.

    Devuelve {player_id: {xpts, media_base, ajuste_rival, ajuste_sede, ...}}.
    """
    medias = base_averages(conn)
    fuerza = team_strength(conn)
    calendario = next_fixtures(conn)

    equipos = {
        fila["id"]: fila["team_id"]
        for fila in conn.execute("SELECT id, team_id FROM player")
    }

    resultado: dict[int, dict] = {}
    for player_id, media in medias.items():
        partido = calendario.get(equipos.get(player_id))
        ajustes = Adjustments()
        if partido is not None:
            ajustes.opponent = opponent_factor(fuerza, partido["opponent_id"])
            ajustes.venue = HOME_ADVANTAGE if partido["is_home"] else AWAY_PENALTY

        esperado = media * ajustes.combined
        probabilidad = (probabilities or {}).get(player_id)
        resultado[player_id] = {
            "media_base": media,
            "ajuste_rival": ajustes.opponent,
            "ajuste_sede": ajustes.venue,
            "matchday": partido["matchday"] if partido is not None else None,
            "opponent": partido["opponent"] if partido is not None else None,
            "is_home": bool(partido["is_home"]) if partido is not None else None,
            "xpts_si_juega": esperado,
            "xpts": esperado * probabilidad if probabilidad is not None else None,
        }
    return resultado


def playing_probability(row: dict) -> float:
    """Probabilidad de que un jugador dispute la jornada.

    Un lesionado o sancionado es un cero, no una probabilidad baja: no es que
    sea improbable que juegue, es que no puede. Y de quien no sabemos nada se
    supone poco, porque no tener noticias de un jugador suele significar que no
    es titular.
    """
    from fantasyhelper.queries import UNAVAILABLE, UNKNOWN_PROBABILITY

    if (row.get("status") or "ok") in UNAVAILABLE:
        return 0.0
    probabilidad = row.get("probability")
    return UNKNOWN_PROBABILITY if probabilidad is None else float(probabilidad)


def attach(
    conn: sqlite3.Connection,
    rows: list,
    *,
    cost_field: str = "market_value",
) -> list[dict]:
    """Anade xPts y euros por punto esperado a filas de jugadores.

    `cost_field` es lo que se paga en ese contexto: la clausula en el radar, el
    precio pedido en el mercado, el valor de mercado en el resto. Es la razon de
    ser de todo esto: 3,1 puntos esperados no dicen nada por si solos, pero
    1,4M por punto frente a 4,8M por punto ya es una decision.
    """
    modelo = expected_points(conn)

    enriquecidas: list[dict] = []
    for fila in rows:
        datos = dict(fila)
        prediccion = modelo.get(datos["id"])
        probabilidad = playing_probability(datos)

        xpts = None
        if prediccion is not None:
            xpts = prediccion["xpts_si_juega"] * probabilidad
            datos.update(
                {clave: prediccion[clave]
                 for clave in ("media_base", "ajuste_rival", "ajuste_sede")}
            )
            # El rival del modelo sale del calendario propio y es mas fiable que
            # el que traiga la fila, pero solo se pisa si de verdad lo sabemos.
            if prediccion["opponent"]:
                datos["opponent"] = prediccion["opponent"]
                datos["is_home"] = prediccion["is_home"]

        coste = datos.get(cost_field)
        datos["xpts"] = xpts
        # Solo con puntos esperados POSITIVOS. Un portero con media negativa
        # -en Mister encajar resta- daba un coste por punto negativo, que al
        # ordenar de menor a mayor se colocaba el primero de la lista como si
        # fuese el mejor fichaje posible.
        datos["coste_por_punto"] = (
            coste / xpts if xpts and xpts > 0 and coste else None
        )
        enriquecidas.append(datos)

    return enriquecidas
