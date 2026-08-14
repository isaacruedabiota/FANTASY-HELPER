"""Web de consulta, servida desde la Raspberry.

Es una capa de presentacion y nada mas: todo el SQL sigue en `queries.py` y todo
el calculo en `xpts`, `market` y `advice`, que es lo que se decidio desde el
principio para no acabar con dos versiones de la verdad. Si una cifra sale
distinta en la web y en la CLI, es un fallo.

Pensada primero para el movil, que es donde se mira el mercado de verdad. De ahi
que las listas sean tarjetas y no tablas: una tabla de doce columnas en una
pantalla de cinco pulgadas no se lee.

Sin autenticacion, a proposito: escucha solo en la red de casa. El dia que salga
a internet por el tunel hara falta una contrasena, y el sitio de ponerla es un
middleware aqui.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fantasyhelper import advice, display, lineup, market, queries, xpts
from fantasyhelper.bonuses import load_rules
from fantasyhelper.config import settings
from fantasyhelper.storage.db import connect
from fantasyhelper.web import charts
from fantasyhelper.web.cache import CACHE, momentum_model, photo_ids, predictions
from fantasyhelper.web.tasks import RUNNER

log = logging.getLogger(__name__)

AQUI = Path(__file__).parent
app = FastAPI(title="FantasyHelper", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
templates = Jinja2Templates(directory=AQUI / "templates")

# Los mismos formateadores que la CLI, para que un numero se escriba igual en los
# dos sitios.
templates.env.filters["money"] = display.money
# En una tarjeta de movil "9,9M" cabe y "9.876.000" no. La cifra exacta se
# reserva para las tablas y para la ficha del jugador.
templates.env.filters["moneyk"] = lambda v: display.money(v, short=True)
templates.env.filters["percent"] = lambda v: "-" if v is None else f"{v:+.1%}"
templates.env.filters["points"] = display.points
templates.env.globals["settings"] = settings


def _ultima_captura(conn: sqlite3.Connection) -> str | None:
    """Cuando se leyo Mister por ultima vez.

    Se mira la fuente Mister y no la fecha maxima a secas: la captura de las
    19:00 solo refresca los onces de FutbolFantasy, asi que la fecha global
    diria "hoy" con los valores y las clausulas de la madrugada.
    """
    fila = conn.execute(
        "SELECT MAX(captured_at) AS t FROM player_value_snapshot "
        "WHERE provider = 'mister' AND source = 'mister'"
    ).fetchone()
    return fila["t"] if fila else None


def _pagina_con_estado(request: Request, pagina: str, conn, **contexto):
    """Toda pagina lleva en la cabecera cuando se capturo por ultima vez.

    Y los mapas de fotos, porque cualquier lista de jugadores los necesita y
    pasarlos ruta por ruta era garantizar olvidarse en una.

    El segundo parametro se llama `pagina` y no `plantilla` por una razon
    practica: hay rutas que pasan una plantilla de futbol en el contexto, y con
    ese nombre chocaba con el de la plantilla HTML.
    """
    contexto["ultima_captura"] = _ultima_captura(conn)
    contexto["captura"] = RUNNER.state().as_dict()
    jugadores, equipos = photo_ids(conn)
    contexto.setdefault("fotos_jugador", jugadores)
    contexto.setdefault("fotos_equipo", equipos)
    return templates.TemplateResponse(request, pagina, contexto)


def _conn() -> sqlite3.Connection:
    return connect()


def _me(conn: sqlite3.Connection):
    me = queries.my_manager(conn)
    if me is None:
        raise HTTPException(
            503, "Todavia no se sabe quien eres. Ejecuta 'fh capturar --solo mister'."
        )
    return me


def _tope_de_gasto(
    conn: sqlite3.Connection,
    cartera,
    *,
    manager_id: int,
    valor_plantilla: int | None = None,
) -> int | None:
    """Hasta donde se puede comprometer hoy. Una sola definicion para toda la web.

    No es el saldo disponible: es el futuro -ya descontadas las pujas lanzadas-
    mas el cuarto de plantilla que la liga deja deber. Coincide al euro con el
    `maxDebt` que publica Mister, y por eso ese numero se usa tal cual cuando
    esta: sumarle el saldo seria contarlo dos veces.

    `valor_plantilla` se acepta ya calculado porque quien lo tiene a mano se
    ahorra volver a leer la plantilla, y solo hace falta en el caso raro de que
    Mister no publique `maxDebt`.
    """
    if cartera is None:
        return None
    if cartera["max_debt"]:
        return cartera["max_debt"]
    if valor_plantilla is None:
        valor_plantilla = sum(
            fila["market_value"] or 0 for fila in queries.squad(conn, manager_id)
        )
    return queries.spendable(
        cartera["balance"], cartera["future_balance"], valor_plantilla
    )



@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    """Lo que hay que decidir hoy."""
    conn = _conn()
    try:
        me = _me(conn)
        cartera = queries.my_wallet(conn)
        saldo = cartera["balance"] if cartera else None
        puntos, valores = predictions(conn)

        plantilla = advice.weekly_euros(
            conn, queries.squad(conn, me["id"]), points=puntos, values=valores
        )
        valor_plantilla = sum(f["market_value"] or 0 for f in plantilla)

        tope = _tope_de_gasto(
            conn, cartera, manager_id=me["id"], valor_plantilla=valor_plantilla
        )

        datos = advice.briefing(
            conn, manager_id=me["id"], budget=tope, limit=6,
            model=momentum_model(conn), points=puntos, values=valores,
        )
        return _pagina_con_estado(
            request, "hoy.html", conn,
            titulo="Hoy",
            me=me,
            cartera=cartera,
            saldo=saldo,
            tope=tope,
            valor_plantilla=valor_plantilla,
            jugadores=len(plantilla),
            datos=datos,
        )
    finally:
        conn.close()


@app.get("/once", response_class=HTMLResponse)
def once(request: Request):
    """A quien alinear esta jornada, y que fichaje mejoraria el once.

    Es la unica pantalla que NO habla en euros por semana. Alinear no mueve la
    revalorizacion -un jugador sube de valor igual desde el banquillo- asi que
    lo unico que se decide aqui son los puntos de la jornada. El dinero vuelve
    en la segunda mitad, que es otra pregunta: que se puede comprar hoy que
    entre en ese once.
    """
    conn = _conn()
    try:
        me = _me(conn)
        cartera = queries.my_wallet(conn)
        puntos, valores = predictions(conn)
        datos = lineup.recommend(
            conn,
            manager_id=me["id"],
            budget=_tope_de_gasto(conn, cartera, manager_id=me["id"]),
            model=momentum_model(conn),
            points=puntos,
            values=valores,
        )
        return _pagina_con_estado(
            request, "once.html", conn,
            titulo="El once",
            me=me,
            # Mister cuenta al portero en la formacion ('1-3-5-2'); aqui no.
            # Sin normalizar, la pantalla pedia cambiar la formacion incluso
            # cuando ya era la puesta.
            formacion_actual=lineup.normalize_formation(me["formation"]),
            nombres_linea=lineup.LINE_NAMES,
            **datos,
        )
    finally:
        conn.close()


@app.get("/plantilla", response_class=HTMLResponse)
def plantilla(request: Request, de: str | None = None):
    conn = _conn()
    try:
        me = _me(conn)
        manager = me
        if de:
            fila = conn.execute(
                "SELECT id, name FROM manager WHERE name = ? LIMIT 1", (de,)
            ).fetchone()
            if fila is None:
                raise HTTPException(404, f"No hay ningun participante llamado {de}.")
            manager = fila

        puntos, valores = predictions(conn)
        filas = advice.weekly_euros(
            conn, queries.squad(conn, manager["id"]), points=puntos, values=valores
        )
        filas.sort(key=lambda f: f["rendimiento_semanal"] or -1, reverse=True)

        # La suma de la ventaja esperada, no de "lo que va a subir": el modelo
        # predice cuanto se aparta cada jugador de los de su precio, y el
        # movimiento del tramo entero se deja fuera a proposito.
        previsto = sum(f["euros_ventaja"] for f in filas if f.get("euros_ventaja"))
        return _pagina_con_estado(
            request, "plantilla.html", conn,
            titulo=f"Plantilla de {manager['name']}",
            filas=filas,
            manager=manager,
            participantes=queries.standings(conn),
            total=sum(f["market_value"] or 0 for f in filas),
            diario=queries.squad_daily_change(conn).get(manager["id"]),
            previsto=previsto,
        )
    finally:
        conn.close()


@app.get("/mercado", response_class=HTMLResponse)
def mercado(request: Request, mios: int = 0):
    """El mercado del dia, sin los propios.

    Los tuyos se ocultan por defecto porque en esa lista no hay nada que
    decidir: ya son tuyos. Quedan a un clic por si se quiere ver a que precio
    han salido.
    """
    conn = _conn()
    try:
        me = queries.my_manager(conn)
        puntos, valores = predictions(conn)
        todos = advice.weekly_euros(
            conn, queries.market(conn), points=puntos, values=valores
        )
        soy_yo = (lambda f: me is not None and f["seller_id"] == me["id"])
        propios = [f for f in todos if soy_yo(f)]
        filas = todos if mios else [f for f in todos if not soy_yo(f)]
        filas.sort(key=lambda f: f["rendimiento_semanal"] or -1, reverse=True)
        return _pagina_con_estado(
            request, "mercado.html", conn,
            titulo="Mercado de hoy",
            filas=filas,
            propios=len(propios),
            mostrando_mios=bool(mios),
            libres=sum(1 for f in filas if not f["seller_id"]),
            vacio="No hay mercado capturado todavia.")
    finally:
        conn.close()


@app.get("/valor", response_class=HTMLResponse)
def valor(request: Request):
    """El modelo de mercado y quien se sale de su tramo."""
    conn = _conn()
    try:
        modelo = momentum_model(conn)
        _, valores = predictions(conn)
        filas = [
            f for f in market.attach(
                conn, queries.all_players(conn), predictions=valores)
            if f.get("ventaja") is not None
        ]
        filas.sort(key=lambda f: -f["euros_ventaja"])
        return _pagina_con_estado(
            request, "valor.html", conn,
            titulo="Valor de mercado",
            modelo=modelo,
            deriva=market.band_drift(conn),
            bandas=market.PRICE_BANDS,
            suben=filas[:12],
            bajan=list(reversed(filas))[:12],
        )
    finally:
        conn.close()


@app.get("/liga", response_class=HTMLResponse)
def liga(request: Request):
    conn = _conn()
    try:
        return _pagina_con_estado(
            request, "liga.html", conn,
            titulo="La liga",
            clasificacion=queries.standings(conn),
            saldos=queries.estimated_balances(conn),
            # El error medido sobre el propio saldo, que es el unico contraste
            # posible. Va a la pantalla porque una estimacion sin su error al
            # lado invita a creersela mas de lo que toca.
            error_estimacion=queries.estimation_error(conn),
            reglas=load_rules(conn),
            # Todos, sin tope. El feed crece unas pocas tarjetas al dia y
            # cortarlo escondia justo lo que se busca al mirarlo.
            movimientos=queries.movements(conn),
        )
    finally:
        conn.close()


@app.get("/jugador/{player_id}", response_class=HTMLResponse)
def jugador(request: Request, player_id: int, dias: int = 365):
    conn = _conn()
    try:
        fila = next(
            (f for f in queries.all_players(conn) if f["id"] == player_id), None
        )
        if fila is None:
            raise HTTPException(404, "Ese jugador no esta en la base de datos.")

        puntos, valores = predictions(conn)
        enriquecida = advice.weekly_euros(
            conn, [fila], points=puntos, values=valores
        )[0]
        historico = queries.value_history(conn, player_id, days=dias)
        temporadas = conn.execute(
            "SELECT season, points, avg_points, matches_played FROM player_season_stat "
            "WHERE player_id = ? ORDER BY season DESC",
            (player_id,),
        ).fetchall()

        return _pagina_con_estado(
            request, "jugador.html", conn,
            titulo=fila["name"],
            jugador=enriquecida,
            temporadas=temporadas,
            grafico=charts.value_chart(historico),
            historico=historico,
            dias=dias,
        )
    finally:
        conn.close()


# --- actualizar los datos ---------------------------------------------------


@app.post("/api/capturar")
def api_capturar(solo: str | None = None):
    """Arranca una captura y devuelve enseguida, sin esperar a que termine.

    Es POST y no GET a proposito: un GET lo dispara cualquier cosa que precargue
    enlaces -el navegador, un lector de RSS, el propio movil-, y esto sale a la
    red contra Mister. Una accion con efectos no puede colgar de un enlace.
    """
    fuentes = (solo,) if solo else ("mister", "futbolfantasy")
    arrancada, motivo = RUNNER.start(fuentes)
    return {"arrancada": arrancada, "motivo": motivo, **RUNNER.state().as_dict()}


@app.get("/api/capturar")
def api_capturar_estado():
    """Como va la captura. La pagina lo consulta cada pocos segundos."""
    conn = _conn()
    try:
        return {**RUNNER.state().as_dict(), "ultima_captura": _ultima_captura(conn)}
    finally:
        conn.close()


# --- API JSON ---------------------------------------------------------------
# Las mismas respuestas sin plantilla. Existen para lo que venga despues: un bot
# que avise por la manana, o una extension que meta el dato dentro de Mister.

@app.get("/api/hoy")
def api_hoy():
    conn = _conn()
    try:
        me = _me(conn)
        puntos, valores = predictions(conn)
        datos = advice.briefing(
            conn, manager_id=me["id"], budget=queries.my_balance(conn),
            model=momentum_model(conn), points=puntos, values=valores,
        )
        return {
            clave: [
                {k: v for k, v in fila.items() if k != "html"}
                for fila in datos[clave]
            ]
            for clave in ("comprar", "clausulas", "vender", "blindar")
        }
    finally:
        conn.close()


@app.get("/api/once")
def api_once():
    """El once recomendado y las mejoras, sin plantilla. Para el bot que venga."""
    conn = _conn()
    try:
        me = _me(conn)
        puntos, valores = predictions(conn)
        datos = lineup.recommend(
            conn,
            manager_id=me["id"],
            budget=_tope_de_gasto(conn, queries.my_wallet(conn), manager_id=me["id"]),
            model=momentum_model(conn),
            points=puntos,
            values=valores,
        )
        once = datos["once"]
        return {
            "jornada": datos["jornada"],
            "formacion": once.formacion,
            "puntos": once.puntos,
            "avisos": once.avisos,
            "titulares": [
                {clave: fila[clave] for clave in ("id", "name", "position", "team")}
                | {"puntos": fila["puntos_jornada"], "motivo": fila["motivo"]}
                for fila in once.titulares
            ],
            "mejoras": [
                {
                    "jugador": mejora["jugador"]["name"],
                    "tipo": mejora["tipo"],
                    "coste": mejora["coste"],
                    "gana": mejora["gana"],
                    "desplaza": [f["name"] for f in mejora["desplaza"]],
                }
                for mejora in datos["mejoras"]
            ],
        }
    finally:
        conn.close()


@app.get("/api/jugadores")
def api_jugadores():
    """Todos los jugadores con xPts y prediccion de valor, para uso externo."""
    conn = _conn()
    try:
        puntos, valores = predictions(conn)
        return advice.weekly_euros(
            conn, queries.all_players(conn), points=puntos, values=valores
        )
    finally:
        conn.close()


@app.get("/api/salud")
def api_salud():
    """Si los datos estan frescos y cuanto tardan los modelos."""
    conn = _conn()
    try:
        fila = conn.execute(
            "SELECT MAX(snapshot_date) AS ultima FROM player_value_snapshot"
        ).fetchone()
        return {
            "ultima_captura": fila["ultima"],
            "temporada": settings.season,
            "modelos": CACHE.stats(),
        }
    finally:
        conn.close()


@app.get("/api/xpts")
def api_xpts():
    conn = _conn()
    try:
        return xpts.expected_points(conn)
    finally:
        conn.close()
