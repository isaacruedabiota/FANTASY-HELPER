"""Modelo de valor de mercado: quien va a subir y quien va a bajar.

En Mister la revalorizacion es beneficio limpio. Comprar a 5M y vender a 6M da un
millon que no depende de puntos ni de alineaciones, y es dinero reinvertible. Con
un ano de valores diarios guardados se puede mirar si eso es predecible.

LO QUE DICEN LOS DATOS

El cambio de los ultimos siete dias predice el de los siete siguientes. Pero solo
comparando a cada jugador con los de su MISMO RANGO DE PRECIO:

    tramo      volatilidad semanal   correlacion
    <1M              29,9%              +0,23
    1-3M             19,0%              +0,76
    3-8M             12,8%              +0,72
    8-15M             5,4%              +0,72
    >15M              1,4%              +0,68

Metiendo a todos en el mismo saco la correlacion se cae a +0,33, y no porque la
senal sea debil sino porque las escalas no son comparables: uno de 500k oscila un
30% en una semana y uno de 24M un 1,4%. Al mezclarlos, la calibracion la marcan
los baratos y a los caros se les aplica una vara que no es la suya.

Se vio en la primera tabla que salio de aqui: predecia que Mbappe, Pedri, Yamal y
Vinicius iban a caer un 6% cuando apenas se habian movido un 1%. Estaban por
debajo de la "media del mercado", que ese dia era +21% porque los baratos estaban
disparados en pretemporada. Comparados con los de su tramo, que iban a -1,1%, no
estaban cayendo en absoluto.

Los de menos de un millon son el tramo malo, y tiene sentido: con valores tan
bajos cualquier ajuste minimo es un porcentaje enorme.

POR QUE FUNCIONA, PROBABLEMENTE

Lo mas verosimil es que el algoritmo de Mister reparta cada movimiento entre
varios dias en vez de aplicarlo de golpe. Si es asi, esto no adivina nada: lee un
ajuste que ya esta en marcha y no ha terminado. Sirve para decidir igual, pero no
es lo mismo y conviene no confundirlo.

LO QUE NO HACE

No predice si va a subir el mercado, ni siquiera un tramo entero: predice cuanto
se apartara un jugador de los de su precio. Esa es la parte medida. El movimiento
del tramo se da aparte, como contexto, y no se proyecta hacia delante.

Aviso de sesgo: el historico solo cubre a los jugadores que siguen HOY en el
catalogo. Quien se fue de la liga no esta, asi que la muestra favorece un poco a
los que se quedaron.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

#: Dias que se miran hacia atras y hacia delante. Elegidos midiendo: de las
#: combinaciones probadas entre 3 y 30 dias, 7/7 es la que mas correlacion da.
LOOKBACK_DAYS = 7
HORIZON_DAYS = 7

#: Tramos de precio, con su etiqueta. La comparacion se hace siempre dentro del
#: tramo, que es lo unico que hace comparables a un jugador de 500k y a uno de
#: 20M. Los cortes son redondos a proposito: no hay nada magico en ellos, solo
#: que separan poblaciones con volatilidades muy distintas.
PRICE_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 1_000_000, "<1M"),
    (1_000_000, 3_000_000, "1-3M"),
    (3_000_000, 8_000_000, "3-8M"),
    (8_000_000, 15_000_000, "8-15M"),
    (15_000_000, 2**62, ">15M"),
)

#: Jugadores que hacen falta en un tramo y fecha para que su mediana signifique
#: algo. Con cuatro jugadores, la mediana del tramo es uno de ellos.
MIN_PLAYERS_PER_BAND = 15

#: Correlacion por debajo de la cual no merece la pena hacer caso a un tramo.
MIN_USEFUL_CORRELATION = 0.15


def band_of(value: int | None) -> str | None:
    if not value:
        return None
    return next(name for low, high, name in PRICE_BANDS if low <= value < high)


@dataclass
class BandModel:
    """La relacion medida entre pasado y futuro dentro de un tramo de precio."""

    band: str
    #: Que parte del movimiento reciente se repite en el siguiente periodo.
    slope: float
    #: Como de fiable es. Es la cifra que dice cuanto creerse lo demas.
    correlation: float
    #: Desviacion tipica del cambio semanal en el tramo. Explica por que hay que
    #: separarlos: va del 30% al 1,4%.
    volatility: float
    samples: int

    @property
    def useful(self) -> bool:
        return self.correlation >= MIN_USEFUL_CORRELATION and self.samples > 500


@dataclass
class MomentumModel:
    bands: dict[str, BandModel]
    dates: int

    @property
    def samples(self) -> int:
        return sum(b.samples for b in self.bands.values())

    @property
    def useful(self) -> bool:
        return any(b.useful for b in self.bands.values())

    def for_value(self, value: int | None) -> BandModel | None:
        nombre = band_of(value)
        modelo = self.bands.get(nombre) if nombre else None
        return modelo if modelo and modelo.useful else None


def _load_series(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """{player_id: {fecha: valor}}, que es como se necesita para restar fechas."""
    series: dict[int, dict[str, int]] = defaultdict(dict)
    for row in conn.execute(
        "SELECT player_id, snapshot_date, market_value FROM player_value_snapshot "
        "WHERE provider = 'mister' AND source = 'mister' AND market_value > 0"
    ):
        series[row["player_id"]][row["snapshot_date"]] = row["market_value"]
    return series


def _change(valores: dict[str, int], desde: str, dias: int) -> float | None:
    """Variacion relativa entre dos fechas, o None si falta alguna."""
    hasta = (date.fromisoformat(desde) + timedelta(days=dias)).isoformat()
    inicio = valores.get(desde)
    if not inicio or hasta not in valores:
        return None
    return (valores[hasta] - inicio) / inicio


def _fit(pares: list[tuple[float, float]]) -> tuple[float, float, float]:
    """(pendiente, correlacion, volatilidad) de una nube de puntos."""
    xs = [x for x, _ in pares]
    ys = [y for _, y in pares]
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    if not sx or not sy:
        return 0.0, 0.0, sx
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in pares) / len(pares)
    return cov / (sx * sx), cov / (sx * sy), sx


def calibrate(
    conn: sqlite3.Connection,
    *,
    lookback: int = LOOKBACK_DAYS,
    horizon: int = HORIZON_DAYS,
) -> MomentumModel:
    """Mide sobre el historico cuanto se repite el movimiento reciente, por tramo.

    Se calibra cada vez en vez de dejar constantes escritas a mano. Cuesta unos
    segundos y a cambio el modelo se corrige solo si Mister cambia su algoritmo,
    que es algo que no vamos a saber por ningun otro medio.
    """
    series = _load_series(conn)

    # tramo -> fecha -> [(cambio pasado, cambio futuro)]
    observaciones: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for valores in series.values():
        for fecha in valores:
            inicio = (date.fromisoformat(fecha) - timedelta(days=lookback)).isoformat()
            pasado = _change(valores, inicio, lookback)
            futuro = _change(valores, fecha, horizon)
            if pasado is None or futuro is None:
                continue
            nombre = band_of(valores[fecha])
            if nombre:
                observaciones[nombre][fecha].append((pasado, futuro))

    bandas: dict[str, BandModel] = {}
    fechas: set[str] = set()
    for nombre, por_fecha in observaciones.items():
        # A cada dia y tramo se le resta su MEDIANA, no su media: en pretemporada
        # unos pocos baratos disparados arrastran la media y dejan de representar
        # al resto.
        desviaciones: list[tuple[float, float]] = []
        for fecha, filas in por_fecha.items():
            if len(filas) < MIN_PLAYERS_PER_BAND:
                continue
            fechas.add(fecha)
            centro_pasado = statistics.median(p for p, _ in filas)
            centro_futuro = statistics.median(f for _, f in filas)
            desviaciones.extend(
                (p - centro_pasado, f - centro_futuro) for p, f in filas
            )

        if len(desviaciones) < 2:
            continue
        pendiente, correlacion, volatilidad = _fit(desviaciones)
        bandas[nombre] = BandModel(
            band=nombre, slope=pendiente, correlation=correlacion,
            volatility=volatilidad, samples=len(desviaciones),
        )

    return MomentumModel(bands=bandas, dates=len(fechas))


def band_drift(conn: sqlite3.Connection, *, lookback: int = LOOKBACK_DAYS) -> dict[str, float]:
    """Cuanto se ha movido cada tramo en los ultimos dias. Contexto, no prediccion."""
    series = _load_series(conn)
    ultima = conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM player_value_snapshot "
        "WHERE provider = 'mister' AND source = 'mister'"
    ).fetchone()
    if not ultima or not ultima["d"]:
        return {}

    hoy = ultima["d"]
    inicio = (date.fromisoformat(hoy) - timedelta(days=lookback)).isoformat()
    por_tramo: dict[str, list[float]] = defaultdict(list)
    for valores in series.values():
        cambio = _change(valores, inicio, lookback)
        nombre = band_of(valores.get(hoy))
        if cambio is not None and nombre:
            por_tramo[nombre].append(cambio)

    return {
        nombre: statistics.median(cambios)
        for nombre, cambios in por_tramo.items()
        if len(cambios) >= MIN_PLAYERS_PER_BAND
    }


def forecast(
    conn: sqlite3.Connection,
    *,
    model: MomentumModel | None = None,
    lookback: int = LOOKBACK_DAYS,
) -> dict[int, dict]:
    """Cuanto se apartara cada jugador de los de su precio la proxima semana.

        ventaja = pendiente_del_tramo x (lo suyo - la mediana de su tramo)

    Se devuelve la ventaja sobre el tramo y no un cambio absoluto porque es lo
    unico que se ha medido. Que suba el tramo entero o no es otra pregunta, y
    ademas da igual para decidir: si sube todo, sube tambien lo que ya tienes.
    """
    modelo = model or calibrate(conn)
    series = _load_series(conn)

    ultima = conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM player_value_snapshot "
        "WHERE provider = 'mister' AND source = 'mister'"
    ).fetchone()
    if not ultima or not ultima["d"]:
        return {}

    hoy = ultima["d"]
    inicio = (date.fromisoformat(hoy) - timedelta(days=lookback)).isoformat()

    recientes: dict[int, tuple[float, int, str]] = {}
    por_tramo: dict[str, list[float]] = defaultdict(list)
    for player_id, valores in series.items():
        cambio = _change(valores, inicio, lookback)
        nombre = band_of(valores.get(hoy))
        if cambio is None or not nombre:
            continue
        recientes[player_id] = (cambio, valores[hoy], nombre)
        por_tramo[nombre].append(cambio)

    centros = {
        nombre: statistics.median(cambios)
        for nombre, cambios in por_tramo.items()
        if len(cambios) >= MIN_PLAYERS_PER_BAND
    }

    resultado: dict[int, dict] = {}
    for player_id, (cambio, valor, nombre) in recientes.items():
        banda = modelo.for_value(valor)
        centro = centros.get(nombre)
        if banda is None or centro is None:
            continue
        ventaja = banda.slope * (cambio - centro)
        resultado[player_id] = {
            "cambio_reciente": cambio,
            "tramo": nombre,
            "centro_tramo": centro,
            "ventaja": ventaja,
            "euros_ventaja": round(valor * ventaja),
            "fiabilidad": banda.correlation,
            "desde": inicio,
        }
    return resultado


def attach(
    conn: sqlite3.Connection,
    rows: list,
    *,
    model: MomentumModel | None = None,
    predictions: dict[int, dict] | None = None,
) -> list[dict]:
    """Anade la prediccion de valor a filas de jugadores que ya traigan `id`.

    `predictions` evita recalcular. Importa: cada llamada a `forecast` recorre el
    historico entero, y una pantalla que muestre cuatro listas lo recorreria
    cuatro veces para obtener exactamente los mismos numeros.
    """
    prediccion = predictions if predictions is not None else forecast(conn, model=model)
    return [{**dict(fila), **prediccion.get(dict(fila)["id"], {})} for fila in rows]
