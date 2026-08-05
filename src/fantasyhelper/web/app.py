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

from fantasyhelper import advice, display, market, queries, xpts
from fantasyhelper.bonuses import load_rules
from fantasyhelper.config import settings
from fantasyhelper.storage.db import connect
from fantasyhelper.web import charts
from fantasyhelper.web.cache import CACHE, momentum_model, predictions
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


def _pagina_con_estado(request: Request, plantilla: str, conn, **contexto):
    """Toda pagina lleva en la cabecera cuando se capturo por ultima vez."""
    contexto["ultima_captura"] = _ultima_captura(conn)
    contexto["captura"] = RUNNER.state().as_dict()
    return templates.TemplateResponse(request, plantilla, contexto)


def _conn() -> sqlite3.Connection:
    return connect()


def _me(conn: sqlite3.Connection):
    me = queries.my_manager(conn)
    if me is None:
        raise HTTPException(
            503, "Todavia no se sabe quien eres. Ejecuta 'fh capturar --solo mister'."
        )
    return me





@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    """Lo que hay que decidir hoy."""
    conn = _conn()
    try:
        me = _me(conn)
        saldo = queries.my_balance(conn)
        puntos, valores = predictions(conn)
        datos = advice.briefing(
            conn, manager_id=me["id"], budget=saldo, limit=6,
            model=momentum_model(conn), points=puntos, values=valores,
        )
        plantilla = queries.squad(conn, me["id"])
        return _pagina_con_estado(
            request, "hoy.html", conn,
            titulo="Hoy",
            me=me,
            saldo=saldo,
            valor_plantilla=sum(f["market_value"] or 0 for f in plantilla),
            jugadores=len(plantilla),
            datos=datos,
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
        return _pagina_con_estado(
            request, "plantilla.html", conn,
            titulo=f"Plantilla de {manager['name']}",
            filas=filas,
            manager=manager,
            participantes=queries.standings(conn),
            total=sum(f["market_value"] or 0 for f in filas),
        )
    finally:
        conn.close()


@app.get("/mercado", response_class=HTMLResponse)
def mercado(request: Request):
    conn = _conn()
    try:
        puntos, valores = predictions(conn)
        filas = advice.weekly_euros(
            conn, queries.market(conn), points=puntos, values=valores
        )
        filas.sort(key=lambda f: f["rendimiento_semanal"] or -1, reverse=True)
        return _pagina_con_estado(
            request, "lista.html", conn, titulo="Mercado de hoy", filas=filas,
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
            reglas=load_rules(conn),
            movimientos=queries.feed_events(conn, limit=25),
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
