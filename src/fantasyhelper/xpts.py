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

EL CALENDARIO, APARTE

xPts mira una sola jornada, y para decidir un fichaje eso se queda corto: un
jugador se tiene tres o cuatro semanas, no un domingo. De ahi `xpts_calendario`,
que aplica la dificultad media de las proximas jornadas en vez de la de la
siguiente.

Va como cifra APARTE y no multiplicando a xPts, a proposito. xPts tiene que
seguir siendo lo que se espera de la jornada que viene, porque es lo unico que
se puede contrastar despues con los puntos que de verdad haga el jugador.
Mezclarle el calendario lo convertiria en una media de tres semanas que ya no
se puede comprobar contra nada.

Sobre lo que este modulo NO hace: no predice el valor de mercado ni el
resultado del partido, y no distingue por que un jugador rindio poco. Es una
media condicionada, no una prediccion de la jornada.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
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

#: Jugadores que hacen falta en una posicion para fiarse de la recta que
#: relaciona precio y rendimiento. Con menos se usa la mediana del puesto, que
#: es lo que habia antes.
MIN_FOR_VALUE_PRIOR = 20

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

#: Jornadas que se miran hacia delante para juzgar el calendario. Tres semanas
#: es el horizonte con el que se decide un fichaje: por menos no compensa el
#: coste de la operacion, y mas alla el once del rival ya no se parece al de
#: hoy. La ficha de Mister trae quince, asi que el limite es de criterio y no
#: de dato.
LOOKAHEAD_MATCHDAYS = 3

#: Jugadores con historico que hacen falta para fiarse de la fuerza de un
#: equipo. Por debajo se usa la mediana de la liga: un recien ascendido del que
#: solo conocemos a dos jugadores no es que sea flojo, es que no lo sabemos.
MIN_PLAYERS_FOR_STRENGTH = 8

#: Cuantos jugadores definen la fuerza de un equipo. Se toman los mejores, no
#: todos: la plantilla entera diluye la calidad con suplentes que no juegan.
SQUAD_CORE = 11


@dataclass
class ValuePrior:
    """Lo que cabe esperar de un jugador de un puesto sabiendo lo que cuesta.

    El precio es lo unico que sabemos de un fichaje recien llegado del
    extranjero, y no es poco: es el juicio agregado de miles de personas que
    SI han visto jugar a Antony o a Bernardo Silva. Ignorarlo y darles la
    mediana de su puesto los iguala con un suplente de 300.000 €.

    La recta va contra el logaritmo del valor porque el precio se reparte por
    ordenes de magnitud -de 200.000 a 15 millones- y en lineal los caros
    aplastarian el ajuste.
    """

    slope: float
    intercept: float
    samples: int
    #: Rango observado de medias en ese puesto. La recta no se extrapola fuera:
    #: un valor extremo daria una media que nadie ha hecho nunca.
    low: float
    high: float
    #: Error medio absoluto de la recta y el de usar la mediana, para poder
    #: decir en pantalla si compensa.
    error: float = 0.0
    error_median: float = 0.0

    def estimate(self, value: int | None) -> float | None:
        if not value or value <= 0:
            return None
        media = self.intercept + self.slope * math.log10(value)
        return max(self.low, min(self.high, media))


@dataclass
class Averages:
    """Media base de cada jugador y de donde ha salido."""

    values: dict[int, float]
    #: Los que no tienen ni un partido registrado y van estimados por su precio.
    estimated: set[int]
    priors: dict[str | None, ValuePrior]


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


def current_matchday(conn: sqlite3.Connection) -> int | None:
    """La jornada que se juega ahora: la primera sin terminar del calendario.

    Es la MINIMA de las pendientes y no la del proximo partido de cada equipo,
    porque esas dos cosas no coinciden para todos. Con el Mundial hay seis
    equipos que descansan la primera jornada: su proximo partido es de la
    segunda, y aun asi la jornada en juego es la primera. La diferencia decide
    quien puntua este fin de semana y quien no.
    """
    fila = conn.execute(
        "SELECT MIN(matchday) AS j FROM fixture "
        "WHERE season = ? AND status != 'finished'",
        (settings.season,),
    ).fetchone()
    return fila["j"] if fila else None


def upcoming(
    conn: sqlite3.Connection, *, matchdays: int = LOOKAHEAD_MATCHDAYS
) -> dict[int, list[sqlite3.Row]]:
    """Las proximas jornadas de cada equipo: {team_id: [filas, la primera antes]}.

    Distinto de `next_fixtures`, que solo da la siguiente. Un fichaje se
    mantiene tres o cuatro semanas, asi que el calendario de las siguientes
    jornadas pesa tanto como el del domingo, y hasta ahora no se miraba.

    El punto de partida es la jornada de su PROXIMO partido y no la jornada en
    curso de la liga, porque no son la misma para todos: los seis equipos que
    descansan la primera por el Mundial tienen rival asignado en ella y aun asi
    empiezan en la segunda. Contarles la J1 les pondria un partido que no van a
    jugar.
    """
    desde = {
        fila["team_id"]: fila["matchday"] for fila in next_fixtures(conn).values()
    }
    # Sin ningun proximo partido conocido -pretemporada cerrada, o el dia que
    # falle la captura- se arranca de la primera jornada que haya en la rejilla.
    primera = conn.execute(
        "SELECT MIN(matchday) AS j FROM team_schedule WHERE season = ?",
        (settings.season,),
    ).fetchone()
    por_defecto = (primera["j"] if primera else None) or 1

    calendario: dict[int, list[sqlite3.Row]] = {}
    for fila in conn.execute(
        """
        SELECT s.team_id, s.matchday, s.opponent_id, s.is_home, t.name AS opponent
        FROM team_schedule s
        LEFT JOIN team t ON t.id = s.opponent_id
        WHERE s.season = ?
        ORDER BY s.team_id, s.matchday
        """,
        (settings.season,),
    ):
        inicio = desde.get(fila["team_id"], por_defecto)
        if inicio <= fila["matchday"] < inicio + matchdays:
            calendario.setdefault(fila["team_id"], []).append(fila)

    return calendario


def schedule_factor(
    strength: dict[int, float], fixtures: list[sqlite3.Row] | None
) -> float:
    """Lo bueno o malo que es un tramo de calendario, como factor sobre la media.

    Es la media de los ajustes de rival de cada jornada, con la sede aplicada
    solo donde se sabe. Media y no producto: multiplicar tres factores de 0,8
    daria 0,51, y enfrentarse a tres equipos duros no reduce a la mitad lo que
    rinde un jugador POR PARTIDO, que es lo que mide esto.

    Sin calendario devuelve 1.0, que es lo honesto: no saber contra quien juega
    no es motivo para penalizarlo ni para premiarlo.
    """
    if not fixtures:
        return 1.0

    factores = []
    for partido in fixtures:
        factor = opponent_factor(strength, partido["opponent_id"])
        if partido["is_home"] is not None:
            factor *= HOME_ADVANTAGE if partido["is_home"] else AWAY_PENALTY
        factores.append(factor)

    return sum(factores) / len(factores)


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


_VALUES_SQL = """
    SELECT player_id, market_value FROM (
        SELECT player_id, market_value,
               ROW_NUMBER() OVER (
                   PARTITION BY player_id ORDER BY snapshot_date DESC
               ) AS rn
        FROM player_value_snapshot
        WHERE provider = 'mister' AND source = 'mister' AND market_value > 0
    ) WHERE rn = 1
"""


def base_averages(conn: sqlite3.Connection) -> Averages:
    """Media base de cada jugador, y de donde sale.

    Dos pasadas. En la primera se calcula la media de cada jugador y cuanta
    evidencia la respalda; con eso se saca lo que cabe esperar de un jugador de
    su puesto y su precio, usando solo a los que tienen recorrido. En la
    segunda, cada jugador se acerca a esa referencia en proporcion a lo POCO que
    sepamos de el.

    La referencia va por posicion porque las escalas no son comparables: un
    portero puntua de otra manera que un delantero, y algunos hasta en negativo.

    Y dentro de cada posicion va por PRECIO y no por la mediana del puesto. La
    mediana trata igual a Antony, que vale 15 millones, y a un suplente de
    300.000 €, cuando lo unico que sabemos de los dos es justamente cuanto
    valen. Medido sobre los 289 jugadores con 30 partidos o mas, el error medio
    de la referencia baja de 0,68 a 0,51 puntos -un 25% menos-, y en los medios
    un 32%.

    Los que no tienen ni un partido registrado -140 en el catalogo actual, casi
    todos fichajes llegados de otras ligas- ya no se quedan fuera: entran con la
    estimacion por precio y marcados en `estimated`, para que la pantalla pueda
    decir de donde sale su numero.
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
    valores = {
        fila["player_id"]: fila["market_value"]
        for fila in conn.execute(_VALUES_SQL)
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

    priors = _value_priors(crudas, posiciones, valores)

    def referencia(player_id: int) -> float | None:
        prior = priors.get(posiciones.get(player_id))
        return prior.estimate(valores.get(player_id)) if prior else None

    medias = {
        player_id: blended_average(
            history.get(player_id, []),
            current_points=current.get(player_id, (None, 0))[0],
            current_matchdays=current.get(player_id, (None, 0))[1],
            prior=referencia(player_id),
        )
        for player_id in crudas
    }

    # Los que no tienen ni un partido: su media ES la referencia de su precio.
    estimated: set[int] = set()
    for player_id in valores:
        if player_id in medias:
            continue
        estimado = referencia(player_id)
        if estimado is not None:
            medias[player_id] = estimado
            estimated.add(player_id)

    return Averages(values=medias, estimated=estimated, priors=priors)


def _value_priors(
    crudas: dict[int, tuple[float, float]],
    posiciones: dict[int, str | None],
    valores: dict[int, int],
) -> dict[str | None, ValuePrior]:
    """Recta precio -> media de cada posicion, sobre los que tienen recorrido.

    Se ajusta solo con los jugadores de los que sabemos bastante: si entrasen
    los de cuatro partidos, la recta la marcarian precisamente los casos raros
    que se quieren corregir.

    Cuando en una posicion no hay jugadores suficientes se devuelve una recta
    plana con la mediana, que es exactamente lo que habia antes. Asi el peor
    caso del cambio es no empeorar nada.
    """
    por_posicion: dict[str | None, list[tuple[float, float]]] = {}
    for player_id, (media, evidencia) in crudas.items():
        if evidencia < MIN_EVIDENCE_FOR_PRIOR:
            continue
        valor = valores.get(player_id)
        por_posicion.setdefault(posiciones.get(player_id), []).append(
            (math.log10(valor) if valor else 0.0, media)
        )

    priors: dict[str | None, ValuePrior] = {}
    for posicion, muestras in por_posicion.items():
        medias = [m for _, m in muestras]
        mediana = sorted(medias)[len(medias) // 2]
        plana = ValuePrior(
            slope=0.0, intercept=mediana, samples=len(muestras),
            low=min(medias), high=max(medias),
            error_median=statistics.mean(abs(m - mediana) for m in medias),
        )
        plana.error = plana.error_median

        utiles = [(x, m) for x, m in muestras if x]
        if len(utiles) >= MIN_FOR_VALUE_PRIOR and len({x for x, _ in utiles}) > 1:
            recta = statistics.linear_regression(
                [x for x, _ in utiles], [m for _, m in utiles]
            )
            ajustada = ValuePrior(
                slope=recta.slope, intercept=recta.intercept, samples=len(utiles),
                low=min(medias), high=max(medias),
                error=statistics.mean(
                    abs(m - (recta.intercept + recta.slope * x)) for x, m in utiles
                ),
                error_median=plana.error_median,
            )
            # Solo si de verdad mejora. Una recta que acierta menos que la
            # mediana es ruido con pinta de modelo.
            priors[posicion] = ajustada if ajustada.error < plana.error else plana
        else:
            priors[posicion] = plana

    return priors


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
    resumen = base_averages(conn)
    medias = resumen.values
    fuerza = team_strength(conn)
    calendario = next_fixtures(conn)
    proximas = upcoming(conn)

    # La posicion viaja con la prediccion para que quien alinee pueda sacar la
    # referencia de cada puesto sin volver a la base de datos. Un debutante no
    # tiene media propia, y compararlo con la de su puesto es lo mas honesto
    # que se puede hacer con el.
    plantillas = {
        fila["id"]: (fila["team_id"], fila["position"])
        for fila in conn.execute("SELECT id, team_id, position FROM player")
    }

    resultado: dict[int, dict] = {}
    for player_id, media in medias.items():
        equipo, posicion = plantillas.get(player_id, (None, None))
        partido = calendario.get(equipo)
        ajustes = Adjustments()
        if partido is not None:
            ajustes.opponent = opponent_factor(fuerza, partido["opponent_id"])
            ajustes.venue = HOME_ADVANTAGE if partido["is_home"] else AWAY_PENALTY

        esperado = media * ajustes.combined
        probabilidad = (probabilities or {}).get(player_id)

        # El calendario se da APARTE y no multiplicando a xPts. xPts es lo que
        # se espera de la proxima jornada y tiene que seguir siendolo: mezclarle
        # las tres siguientes lo convertiria en una media de tres semanas que ya
        # no se puede contrastar con los puntos que haga el domingo.
        siguientes = proximas.get(equipo) or []
        resultado[player_id] = {
            "media_base": media,
            "position": posicion,
            # Sin un solo partido registrado: su media sale de lo que cuesta.
            # Viaja hasta la pantalla porque no es lo mismo un 4,3 medido en
            # cinco temporadas que un 4,3 deducido de un precio.
            "sin_historial": player_id in resumen.estimated,
            "ajuste_rival": ajustes.opponent,
            "ajuste_sede": ajustes.venue,
            "matchday": partido["matchday"] if partido is not None else None,
            "opponent": partido["opponent"] if partido is not None else None,
            "is_home": bool(partido["is_home"]) if partido is not None else None,
            "xpts_si_juega": esperado,
            "xpts": esperado * probabilidad if probabilidad is not None else None,
            "ajuste_calendario": schedule_factor(fuerza, siguientes),
            "proximas": [
                {
                    "matchday": f["matchday"],
                    "opponent": f["opponent"],
                    "opponent_id": f["opponent_id"],
                    "is_home": None if f["is_home"] is None else bool(f["is_home"]),
                    "factor": opponent_factor(fuerza, f["opponent_id"]),
                }
                for f in siguientes
            ],
        }
    return resultado


def playing_probability(row: dict) -> float:
    """Probabilidad de que un jugador dispute la jornada.

    Un lesionado o sancionado es un cero, no una probabilidad baja: no es que
    sea improbable que juegue, es que no puede. Y de quien no sabemos nada se
    supone poco, porque no tener noticias de un jugador suele significar que no
    es titular.

    Ojo con lo que cuenta como lesionado: solo la baja de verdad -la roja de
    FutbolFantasy-. El que se esta recuperando o acaba de volver SI juega, y su
    probabilidad ya viene rebajada en la fuente; ponerle un cero era descartar a
    gente disponible.
    """
    from fantasyhelper.queries import UNAVAILABLE, UNKNOWN_PROBABILITY

    if (row.get("status") or "ok") in UNAVAILABLE:
        return 0.0
    probabilidad = row.get("probability")
    return UNKNOWN_PROBABILITY if probabilidad is None else float(probabilidad)


#: Lo que rinde un jugador que vuelve de lesion respecto a lo suyo.
#:
#: La probabilidad de la fuente responde a "sera titular", que es otra pregunta:
#: aunque salga de inicio, el que acaba de reaparecer juega media hora y se va.
#: Son dos descuentos distintos y por eso se multiplican en vez de solaparse.
#:
#: Es un prior, no una medicion: no tenemos minutos jugados por jugador. Se
#: queda en el lado prudente -un 15%- y se revisara cuando haya con que.
RETURNING_MINUTES = 0.85


def minutes_factor(row: dict) -> float:
    """Descuento por volver de lesion. 1.0 para todos los demas."""
    from fantasyhelper.queries import RETURNING

    return RETURNING_MINUTES if (row.get("status") or "") in RETURNING else 1.0


def attach(
    conn: sqlite3.Connection,
    rows: list,
    *,
    cost_field: str = "market_value",
    predictions: dict[int, dict] | None = None,
) -> list[dict]:
    """Anade xPts y euros por punto esperado a filas de jugadores.

    `cost_field` es lo que se paga en ese contexto: la clausula en el radar, el
    precio pedido en el mercado, el valor de mercado en el resto. Es la razon de
    ser de todo esto: 3,1 puntos esperados no dicen nada por si solos, pero
    1,4M por punto frente a 4,8M por punto ya es una decision.

    `predictions` evita recalcular el modelo cuando se enriquecen varias listas
    seguidas, que es lo que hace cualquier pantalla de resumen.
    """
    modelo = predictions if predictions is not None else expected_points(conn)

    enriquecidas: list[dict] = []
    for fila in rows:
        datos = dict(fila)
        prediccion = modelo.get(datos["id"])
        probabilidad = playing_probability(datos)
        minutos = minutes_factor(datos)

        xpts = None
        if prediccion is not None:
            xpts = prediccion["xpts_si_juega"] * probabilidad * minutos
            # `matchday` es la jornada del PROXIMO partido de su equipo, que no
            # tiene por que ser la que se juega ahora: quien descansa por el
            # Mundial la tiene una mas alta. Alinear mira esa diferencia.
            datos.update(
                {clave: prediccion[clave]
                 for clave in ("media_base", "matchday", "sin_historial",
                               "ajuste_rival", "ajuste_sede",
                               "ajuste_calendario", "proximas")}
            )
            # Lo que rendiria por jornada durante las proximas semanas, en vez
            # de solo la que viene. Es lo que importa al fichar: un jugador se
            # tiene varias jornadas, no una.
            datos["xpts_calendario"] = (
                prediccion["media_base"]
                * prediccion["ajuste_calendario"]
                * probabilidad
                * minutos
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
