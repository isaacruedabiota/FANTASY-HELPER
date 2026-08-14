"""El once de la jornada: a quien alinear y que fichaje lo mejoraria.

Las otras pantallas ordenan jugadores. Esta decide, que es otra cosa: hay que
elegir once de entre los quince, y elegir bien depende de una restriccion que
ninguna lista recoge -la formacion-. El mejor jugador disponible puede no caber
si ya hay cinco medios por delante de el.

POR QUE AQUI NO MANDA EL DINERO

En el resto de la web la moneda comun son los euros por semana, que suman los
puntos esperados y la revalorizacion. Para alinear eso seria un error: un
jugador se revaloriza este en el once o en el banquillo. La revalorizacion no
depende de a quien alineas, asi que meterla en la decision solo mueve el orden
sin cambiar el dinero que entra.

Lo unico que se juega al alinear son los PUNTOS de la jornada. Por eso aqui la
unidad es xPts y no euros, y por eso un jugador que se esta revalorizando puede
merecer sitio en el mercado y no en el once.

LA JORNADA, NO LA SEMANA QUE VIENE

xPts se calcula contra el proximo partido de cada equipo, que no es el mismo
para todos: con el Mundial hay seis equipos que descansan la primera jornada.
Sus jugadores tienen xPts -del partido de la jornada 2- y alinearlos este fin de
semana da cero. `for_matchday` es quien lo corrige, y es la diferencia entre una
recomendacion util y una que te sienta a media plantilla.

EL DINERO SI CUENTA PARA LA OTRA MITAD

La segunda parte -que fichaje mejoraria el once- si mira el saldo, porque ahi la
pregunta es distinta: no a quien alineo, sino que puedo comprar hoy que entre en
el once. La mejora se mide en puntos ganados por el ONCE, que es lo unico que
importa: fichar a un delantero de 6 puntos no sirve de nada si los tres que
tienes ya hacen 7.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from fantasyhelper import advice, market, queries, xpts
from fantasyhelper.bonuses import BonusRules

#: Las formaciones que admite Mister: portero fijo y diez de campo, con entre
#: tres y cinco defensas, entre tres y cinco medios y entre uno y tres
#: delanteros. Salen exactamente estas siete combinaciones.
FORMATIONS: tuple[tuple[int, int, int], ...] = (
    (3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 3, 2), (5, 4, 1),
)

#: Las cuatro lineas, de atras adelante. El orden es el de pintado.
LINES = ("PT", "DF", "MC", "DL")

LINE_NAMES = {"PT": "Portería", "DF": "Defensa", "MC": "Medio", "DL": "Delantera"}

#: Probabilidad por debajo de la cual un titular merece un aviso. No lo saca del
#: once -si es el mejor que hay, es el mejor que hay- pero si hay que decirlo:
#: el modelo ya ha descontado la probabilidad, y aun asi un 40% es una moneda al
#: aire y eso no se ve en la cifra final.
RISKY_PROBABILITY = 0.5


@dataclass
class Once:
    """Un once concreto, con lo que se espera de el y lo que falla."""

    formacion: str
    titulares: list[dict] = field(default_factory=list)
    suplentes: list[dict] = field(default_factory=list)
    puntos: float = 0.0
    avisos: list[str] = field(default_factory=list)
    #: Cuantos jugadores faltan en cada linea para poder alinear. Vacio si se
    #: puede formar un once legal.
    faltan: dict[str, int] = field(default_factory=dict)

    @property
    def completo(self) -> bool:
        return len(self.titulares) == 11

    def por_linea(self) -> list[tuple[str, list[dict]]]:
        """Los titulares agrupados de atras adelante, para pintarlos."""
        return [
            (linea, [f for f in self.titulares if f.get("position") == linea])
            for linea in LINES
        ]


def normalize_formation(formation: str | None) -> str | None:
    """La formacion de Mister en la misma forma que la nuestra.

    Mister la escribe contando al portero: '1-3-5-2' es lo que aqui se llama
    '3-5-2'. Sin esto, la pantalla avisaba de cambiar la formacion incluso
    cuando ya era la puesta, que es el peor tipo de aviso: el que se ignora.
    """
    partes = [parte for parte in (formation or "").split("-") if parte.strip()]
    if len(partes) == 4 and partes[0] == "1":
        partes = partes[1:]
    return "-".join(partes) or None


def position_baseline(predictions: dict[int, dict]) -> dict[str, float]:
    """Lo que rinde un jugador cualquiera de cada puesto.

    Sirve para los que no tienen historico. Sin esto, un debutante entra en la
    cuenta como un cero y queda por detras de un lesionado -que tambien vale
    cero, pero de este SI sabemos que no juega-. Poner en su lugar la mediana de
    su posicion no es adivinar su rendimiento; es decir "no sabemos nada de el,
    asi que se parece a un jugador cualquiera de su puesto", que es exactamente
    lo que sabemos.

    Mediana y no media: los delanteros estrella tiran de la media hacia arriba y
    la referencia dejaria de describir al jugador tipico.
    """
    por_puesto: dict[str, list[float]] = {}
    for datos in predictions.values():
        posicion, esperado = datos.get("position"), datos.get("xpts_si_juega")
        if posicion and esperado is not None:
            por_puesto.setdefault(posicion, []).append(esperado)

    return {
        posicion: sorted(valores)[len(valores) // 2]
        for posicion, valores in por_puesto.items()
        if valores
    }


def for_matchday(
    rows: list[dict],
    jornada: int | None,
    *,
    baseline: dict[str, float] | None = None,
) -> list[dict]:
    """Anade a cada fila lo que se espera de ella EN ESTA JORNADA.

    `xpts` responde a "cuanto hara en su proximo partido", que no es la misma
    pregunta. Un jugador del Athletic cuyo equipo descansa la primera jornada
    tiene 4,1 xPts y este domingo hace cero. Aqui se separan los dos casos y se
    escribe el motivo, porque un cero sin explicacion parece un fallo.

    Se escribe en las propias filas, como hace `advice.weekly_euros`: son dicts
    que ya viajan enriquecidos por media web.
    """
    referencia = baseline or {}
    for fila in rows:
        motivo = None
        puntos = fila.get("xpts")

        if puntos is None:
            # Ni historico ni temporada en curso. Se le da la referencia de su
            # puesto, descontada por lo probable que sea que juegue.
            tipico = referencia.get(fila.get("position"))
            puntos = (tipico or 0.0) * xpts.playing_probability(fila)
            motivo = "sin datos"
            fila["sin_datos"] = True

        # La jornada manda sobre todo lo demas: si su equipo no juega esta, da
        # igual lo bueno que sea.
        propia = fila.get("matchday")
        if jornada is not None and propia is not None and propia > jornada:
            puntos, motivo = 0.0, f"no juega la J{jornada}"

        estado = fila.get("status")
        if estado and estado in queries.UNAVAILABLE:
            motivo = estado.replace("_", " ")

        fila["puntos_jornada"] = puntos
        fila["motivo"] = motivo
    return rows


def _puntos(fila: dict) -> float:
    return fila.get("puntos_jornada") or 0.0


def _orden(fila: dict) -> tuple:
    """Criterio de eleccion dentro de una linea, y desempate estable.

    A igualdad de puntos esperados manda el mas caro: el valor de mercado es el
    juicio de miles de personas sobre un jugador y es mejor moneda de desempate
    que el orden alfabetico. El nombre queda de ultimo recurso para que el
    resultado no cambie entre dos ejecuciones identicas.
    """
    return (-_puntos(fila), -(fila.get("market_value") or 0), fila.get("name") or "")


def best_xi(players: list[dict]) -> Once:
    """El once que mas puntos espera, probando las siete formaciones.

    Dentro de una formacion la eleccion es trivial -los mejores de cada linea- y
    ademas exacta: las lineas no compiten entre si, asi que coger el mejor de
    cada una no puede dejar fuera una combinacion mejor. Lo unico que hay que
    buscar es la formacion, y son siete.

    Requiere que las filas traigan `puntos_jornada`, que pone `for_matchday`.
    """
    por_linea: dict[str, list[dict]] = {linea: [] for linea in LINES}
    for fila in players:
        if fila.get("position") in por_linea:
            por_linea[fila["position"]].append(fila)
    for filas in por_linea.values():
        filas.sort(key=_orden)

    mejor: Once | None = None
    for defensas, medios, delanteros in FORMATIONS:
        cupos = dict(zip(LINES, (1, defensas, medios, delanteros), strict=True))
        if any(len(por_linea[linea]) < cupo for linea, cupo in cupos.items()):
            continue

        titulares = [f for linea, cupo in cupos.items() for f in por_linea[linea][:cupo]]
        total = sum(_puntos(f) for f in titulares)
        if mejor is not None and total <= mejor.puntos:
            continue

        elegidos = {f["id"] for f in titulares}
        mejor = Once(
            formacion=f"{defensas}-{medios}-{delanteros}",
            titulares=titulares,
            suplentes=sorted(
                (f for f in players if f["id"] not in elegidos), key=_orden
            ),
            puntos=total,
        )

    if mejor is None:
        return _once_imposible(players, por_linea)

    mejor.avisos = _avisos(mejor)
    return mejor


#: Como se nombra a los que faltan en cada linea.
MISSING_NAMES = {
    "PT": ("portero", "porteros"),
    "DF": ("defensa", "defensas"),
    "MC": ("medio", "medios"),
    "DL": ("delantero", "delanteros"),
}


def _once_imposible(players: list[dict], por_linea: dict[str, list[dict]]) -> Once:
    """Cuando no cabe ninguna formacion, decir exactamente que hace falta.

    "No se puede alinear" no sirve de nada; "te falta un portero y un delantero"
    se arregla en el mercado esta misma tarde. Se busca la formacion mas
    BARATA de completar y se cuenta contra ella, que es el camino mas corto a
    tener un once legal.
    """
    faltan: dict[str, int] = {}
    for defensas, medios, delanteros in FORMATIONS:
        cupos = dict(zip(LINES, (1, defensas, medios, delanteros), strict=True))
        hueco = {
            linea: cupo - len(por_linea[linea])
            for linea, cupo in cupos.items()
            if len(por_linea[linea]) < cupo
        }
        if not faltan or sum(hueco.values()) < sum(faltan.values()):
            faltan = hueco

    detalle = ", ".join(
        f"{cuantos} {MISSING_NAMES[linea][0 if cuantos == 1 else 1]}"
        for linea, cuantos in faltan.items()
    )
    total = sum(faltan.values())
    return Once(
        formacion="–",
        suplentes=sorted(players, key=_orden),
        avisos=[(
            f"Con {len(players)} jugadores no se puede formar un once: "
            f"te falta{'n' if total > 1 else ''} {detalle}."
        )],
        faltan=faltan,
    )


def _avisos(once: Once) -> list[str]:
    """Lo que el numero final no cuenta de los once elegidos."""
    avisos: list[str] = []

    parados = [f for f in once.titulares if _puntos(f) <= 0]
    if parados:
        nombres = ", ".join(f["name"] for f in parados)
        avisos.append(
            f"{'Entra' if len(parados) == 1 else 'Entran'} en el once por falta de "
            f"recambio, pero no {'puntúa' if len(parados) == 1 else 'puntúan'} esta "
            f"jornada: {nombres}."
        )

    juegan = [f for f in once.titulares if _puntos(f) > 0 and not f.get("motivo")]

    dudosos = [
        f for f in juegan
        if f.get("probability") is not None and f["probability"] < RISKY_PROBABILITY
    ]
    if dudosos:
        nombres = ", ".join(
            f"{f['name']} ({f['probability'] * 100:.0f}%)" for f in dudosos
        )
        avisos.append(f"Menos de la mitad de probabilidad de ser titular: {nombres}.")

    # Sin probabilidad NO es cero: es que no hay alineacion probable publicada de
    # ese jugador, cosa que suele significar que no se cuenta con el. El modelo
    # le supone ese UNKNOWN_PROBABILITY, y decirlo es mas honesto que ensenar un
    # 0% que nadie ha medido.
    sin_once = [f for f in juegan if f.get("probability") is None]
    if sin_once:
        nombres = ", ".join(f["name"] for f in sin_once)
        avisos.append(
            "No aparecen en ninguna alineación probable, así que van con el "
            f"{queries.UNKNOWN_PROBABILITY:.0%} de oficio: {nombres}."
        )

    sin_datos = [f for f in juegan if f.get("sin_datos")]
    if sin_datos:
        nombres = ", ".join(f["name"] for f in sin_datos)
        avisos.append(
            "Sin historial del que tirar, así que van con la media de su puesto: "
            f"{nombres}."
        )

    return avisos


def improvements(
    squad: list[dict],
    once: Once,
    candidates: list[dict],
    *,
    kind: str,
    cost_field: str,
    budget: int | None = None,
) -> list[dict]:
    """Que candidatos harian mejor el once, y a costa de quien.

    La ganancia se mide rehaciendo el once entero con el candidato dentro, no
    comparandolo con el peor titular de su puesto. Es mas lento y es lo
    correcto: fichar un cuarto delantero bueno puede hacer que compense cambiar
    de 4-4-2 a 4-3-3, y esa mejora no aparece comparando dentro de una linea.

    Solo salen los que ENTRAN en el once. Un fichaje que se sienta en el
    banquillo podra ser un negocio -eso lo dice el mercado- pero no mejora la
    jornada, que es lo que se pregunta aqui.
    """
    ya_tuyos = {f["id"] for f in squad}
    mejoras: list[dict] = []

    for candidato in candidates:
        if candidato["id"] in ya_tuyos:
            continue
        coste = candidato.get(cost_field) or candidato.get("market_value")
        if not coste or (budget is not None and coste > budget):
            continue

        nuevo = best_xi([*squad, candidato])
        gana = nuevo.puntos - once.puntos
        entra = any(f["id"] == candidato["id"] for f in nuevo.titulares)

        # Con la plantilla corta no hay once del que hablar y la cuenta de
        # puntos da cero para todos. Lo que se busca entonces es otra cosa
        # -poder alinear- y un fichaje vale si tapa uno de los huecos, aunque
        # por si solo no complete el equipo. Sin esto, justo cuando mas falta
        # hace, esta lista salia vacia.
        tapa = sum(once.faltan.values()) - sum(nuevo.faltan.values())
        if not (entra and gana > 0) and tapa <= 0:
            continue

        dentro = {f["id"] for f in nuevo.titulares}
        mejoras.append({
            "jugador": candidato,
            "tipo": kind,
            "coste": coste,
            "gana": gana,
            "tapa": tapa,
            "formacion": nuevo.formacion,
            "cambia_formacion": nuevo.formacion != once.formacion,
            "desplaza": [f for f in once.titulares if f["id"] not in dentro],
            # Lo que cuesta cada punto que de verdad se suma al once. Es la cifra
            # que compara un delantero de 12M con uno de 3M: no cuantos puntos
            # hace, sino cuantos ANADE.
            "coste_por_punto": coste / gana if gana > 0 else None,
        })

    mejoras.sort(key=_utilidad)
    return mejoras


def _utilidad(mejora: dict) -> tuple:
    """Primero lo que hace posible el once, luego lo que le suma puntos.

    Con el equipo completo `tapa` es cero para todos y el orden se decide solo
    por los puntos, que es lo normal. Cuando falta gente, en cambio, todos los
    que tapan el mismo hueco empatan a cero puntos ganados, y ahi manda lo que
    rinde el candidato: si hay que fichar un medio, que sea el mejor medio que
    quepa en el saldo y no el mas barato.
    """
    return (
        -mejora["tapa"],
        -mejora["gana"],
        -(mejora["jugador"].get("puntos_jornada") or 0),
        mejora["coste"],
    )


def _mas_barata(mejoras: list[dict]) -> list[dict]:
    """Un mismo jugador puede llegar por dos vias; se queda la que salga mejor.

    Pasa a diario: un rival pone en el mercado a alguien cuya clausula tambien
    esta a tiro. Son la misma mejora del once por dos precios distintos, y
    ensenar las dos es ensenar dos veces la misma decision.
    """
    por_jugador: dict[int, dict] = {}
    for mejora in mejoras:
        clave = mejora["jugador"]["id"]
        anterior = por_jugador.get(clave)
        if anterior is None or mejora["coste"] < anterior["coste"]:
            por_jugador[clave] = mejora
    return list(por_jugador.values())


def recommend(
    conn: sqlite3.Connection,
    *,
    manager_id: int,
    budget: int | None = None,
    limit: int = 6,
    rules: BonusRules | None = None,
    model: market.MomentumModel | None = None,
    points: dict[int, dict] | None = None,
    values: dict[int, dict] | None = None,
) -> dict:
    """El once de la jornada y las compras que lo mejorarian.

    Los dos modelos se pueden inyectar ya calculados, como en `advice.briefing`:
    esta pantalla enriquece tres listas y recalcularlos en cada una multiplicaria
    por tres la espera en la Raspberry.
    """
    puntos = points if points is not None else xpts.expected_points(conn)
    valores = values if values is not None else market.forecast(conn, model=model)
    jornada = xpts.current_matchday(conn)
    referencia = position_baseline(puntos)

    def preparar(filas: list) -> list[dict]:
        enriquecidas = advice.weekly_euros(
            conn, filas, rules=rules, model=model, points=puntos, values=valores
        )
        return for_matchday(enriquecidas, jornada, baseline=referencia)

    plantilla = preparar(queries.squad(conn, manager_id))
    once = best_xi(plantilla)

    # Del mercado del dia salen los propios: ya son tuyos, ponerlos a la venta no
    # los hace fichables.
    mercado = [
        fila for fila in preparar(queries.market(conn))
        if fila["seller_id"] != manager_id
    ]
    objetivos = preparar(
        queries.clause_targets(conn, manager_id=manager_id, budget=budget)
    )

    mejoras = _mas_barata(
        improvements(plantilla, once, mercado, kind="mercado",
                     cost_field="asking_price", budget=budget)
        + improvements(plantilla, once, objetivos, kind="clausula",
                       cost_field="clause_value", budget=budget)
    )
    mejoras.sort(key=_utilidad)

    return {
        "jornada": jornada,
        "once": once,
        "plantilla": plantilla,
        "tope": budget,
        "mejoras": mejoras[:limit],
    }


def current_formation(conn: sqlite3.Connection) -> str | None:
    """La formacion que tengo puesta en Mister, en nuestra notacion."""
    fila = queries.my_manager(conn)
    return normalize_formation(fila["formation"]) if fila else None
