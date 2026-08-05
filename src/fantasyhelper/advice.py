"""Que hacer hoy: puntos y revalorizacion sumados en una sola moneda.

Hasta aqui habia dos respuestas sueltas y en unidades distintas. `xpts` dice
cuantos puntos se esperan de un jugador y `market` cuanto se va a revalorizar,
pero no habia forma de comparar "2,8 puntos" con "+640.000 €".

El cambio lo pone la propia liga: paga 75.000 € por punto. Con eso un punto ES
una cantidad de dinero, y las dos cosas se suman:

    rendimiento semanal = xPts x euros_por_punto + revalorizacion esperada

Y entonces se ve algo que por separado no se veia: un suplente barato que se esta
revalorizando puede generar mas dinero a la semana que un titular caro estancado.

LO QUE ESTA CONVERSION NO RECOGE

Solo entra el pago por punto, que es lo unico atribuible a un jugador concreto.
La liga paga ademas 250.000 € por jugador en el once ideal y 50.000 € por acierto
de quiniela, que no dependen de un jugador en particular, y una escala por puesto
en la jornada que en esta liga va al REVES: el primero cobra 200.000 y el ultimo
1.400.000, para igualar la competicion.

Ese ultimo detalle importa mas de lo que parece. Sumar puntos te sube en la
jornada y por tanto te REDUCE ese ingreso. Con diez participantes, pasar de
ultimo a primero cuesta 1,2M de bonificacion, que a 75.000 € el punto son
dieciseis puntos. Asi que la ventaja real de una gran jornada es menor de lo que
sale aqui, aunque sigue siendo ventaja: quedar primero suele requerir bastante
mas de dieciseis puntos de diferencia.

TAMPOCO ES UN OPTIMIZADOR

Ordena jugadores por lo que rinden; no resuelve que combinacion comprar con un
presupuesto dado, que es un problema distinto y va en la fase siguiente.
"""

from __future__ import annotations

import sqlite3

from fantasyhelper import market, queries, xpts
from fantasyhelper.bonuses import BonusRules, load_rules


def weekly_euros(
    conn: sqlite3.Connection,
    rows: list,
    *,
    rules: BonusRules | None = None,
    model: market.MomentumModel | None = None,
    points: dict[int, dict] | None = None,
    values: dict[int, dict] | None = None,
) -> list[dict]:
    """Anade a cada jugador lo que genera en una semana, en euros.

    Se devuelven tambien los dos sumandos por separado, porque tienen fiabilidad
    muy distinta: los puntos salen de un historico de temporadas y la
    revalorizacion de una correlacion medida a siete dias. Ver una cifra sin sus
    dos mitades invita a fiarse de ella mas de lo que toca.

    `points` y `values` permiten pasar los dos modelos ya calculados. Sin eso,
    enriquecer cuatro listas recorre el historico cuatro veces para llegar a los
    mismos numeros.
    """
    reglas = rules if rules is not None else load_rules(conn)
    euros_por_punto = reglas.per_point

    filas = xpts.attach(conn, rows, cost_field="market_value", predictions=points)
    filas = market.attach(conn, filas, model=model, predictions=values)

    for fila in filas:
        puntos = fila.get("xpts")
        revalorizacion = fila.get("euros_ventaja")
        fila["euros_por_puntos"] = (
            round(puntos * euros_por_punto) if puntos and euros_por_punto else None
        )
        # None y no cero cuando falta una mitad: no es que genere cero, es que no
        # lo sabemos, y sumarlo como cero hundiria al jugador en el ranking.
        if fila["euros_por_puntos"] is None and revalorizacion is None:
            fila["rendimiento_semanal"] = None
        else:
            fila["rendimiento_semanal"] = (
                (fila["euros_por_puntos"] or 0) + (revalorizacion or 0)
            )

        # Lo mismo pero a tres jornadas vista, que es el plazo de un fichaje.
        # Se recalcula entero en vez de escalar el anterior porque la mitad de
        # revalorizacion no depende del calendario y no hay que tocarla.
        calendario = fila.get("xpts_calendario")
        fila["rendimiento_calendario"] = (
            None
            if fila["rendimiento_semanal"] is None
            else (round(calendario * euros_por_punto) if calendario else 0)
            + (revalorizacion or 0)
        )

    return filas


def _rendimiento(fila: dict) -> float:
    return fila["rendimiento_semanal"] if fila["rendimiento_semanal"] is not None else 0


def _con_calendario(fila: dict) -> float:
    """Rendimiento a varias jornadas, que es por lo que se ordena al fichar."""
    valor = fila.get("rendimiento_calendario")
    return valor if valor is not None else _rendimiento(fila)


#: Un rival no paga una clausula por un suplente por barata que sea, asi que
#: para el aviso de blindaje solo cuentan los que rinden por encima de la
#: mediana de la plantilla. Sin este filtro la lista la encabezaban jugadores de
#: 235.000 € que generan 91.000 € a la semana: baratisimos en proporcion, pero
#: que no le mejoran el equipo a nadie.
def _amenazados(filas: list[dict]) -> list[dict]:
    """Los tuyos que un rival querria, ordenados por lo rapido que se amortizan."""
    utiles = [f for f in filas if f["clause_value"] and _rendimiento(f) > 0]
    if not utiles:
        return []

    rendimientos = sorted(_rendimiento(f) for f in utiles)
    corte = rendimientos[len(rendimientos) // 2]
    candidatos = [f for f in utiles if _rendimiento(f) >= corte]

    for fila in candidatos:
        # Semanas que tardaria el ladron en recuperar lo que paga. Es la cuenta
        # que haria el, y por tanto la que dice quien corre peligro.
        fila["semanas_amortizacion"] = fila["clause_value"] / _rendimiento(fila)

    candidatos.sort(key=lambda f: f["semanas_amortizacion"])
    return candidatos


def briefing(
    conn: sqlite3.Connection,
    *,
    manager_id: int,
    budget: int | None = None,
    limit: int = 8,
    rules: BonusRules | None = None,
    model: market.MomentumModel | None = None,
    points: dict[int, dict] | None = None,
    values: dict[int, dict] | None = None,
) -> dict:
    """Las cuatro decisiones del dia, ya resueltas.

    El modelo de valor se calcula una sola vez y se reutiliza en las cuatro
    consultas: calibrarlo recorre todo el historico y hacerlo cuatro veces
    multiplicaria por cuatro la espera sin cambiar ni un numero. Se puede
    inyectar ya hecho, que es lo que hacen los tests para no depender de una
    calibracion sobre datos de prueba.
    """
    modelo = model or market.calibrate(conn)
    reglas = rules if rules is not None else load_rules(conn)

    # Los dos modelos, una sola vez. Antes cada una de las cuatro listas los
    # recalculaba por su cuenta y el resumen tardaba cinco veces mas de lo
    # necesario, cosa que en la Raspberry se nota y en una peticion web mas.
    puntos = points if points is not None else xpts.expected_points(conn)
    valores = values if values is not None else market.forecast(conn, model=modelo)

    def enriquecer(filas: list) -> list[dict]:
        return weekly_euros(
            conn, filas, rules=reglas, model=modelo, points=puntos, values=valores
        )

    mios = enriquecer(queries.squad(conn, manager_id))
    # Fuera los que no tienen ninguna de las dos mitades: no saber lo que rinde
    # un jugador no es motivo para recomendar venderlo.
    vendibles = [f for f in mios if f["rendimiento_semanal"] is not None]
    vendibles.sort(key=_rendimiento)

    # Solo lo que esta HOY en el mercado. Antes salia aqui cualquier jugador sin
    # dueno del catalogo entero, y la mayoria no se podian fichar: en Mister no
    # se compra a quien te apetece, se compra de la lista del dia, que son unas
    # decenas y cambia cada madrugada. Recomendar a los demas era ruido.
    disponibles = [
        fila for fila in enriquecer(queries.market(conn))
        # Los propios no se compran, y los del mercado se ordenan por lo que
        # cuestan de verdad -el precio pedido-, que en los de rival no es su
        # valor de mercado.
        if fila["seller_id"] != manager_id
        and fila["rendimiento_semanal"] is not None
        and (budget is None or (fila["asking_price"] or fila["market_value"] or 0) <= budget)
    ]
    disponibles.sort(key=_con_calendario, reverse=True)

    objetivos = enriquecer(
        queries.clause_targets(conn, manager_id=manager_id, budget=budget)
    )
    objetivos.sort(key=_con_calendario, reverse=True)

    riesgo = _amenazados(enriquecer(queries.clause_risk(conn, manager_id)))

    return {
        "euros_por_punto": reglas.per_point,
        "modelo": modelo,
        "vender": vendibles[:limit],
        "comprar": disponibles[:limit],
        "clausulas": objetivos[:limit],
        "blindar": riesgo[:limit],
    }
