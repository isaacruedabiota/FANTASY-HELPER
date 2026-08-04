"""Interfaz de linea de comandos: `fh <comando>`.

En Fase 0 la CLI es toda la interfaz que hay. La web llega en Fase 2, cuando ya
haya semanas de historico que enseñar; construirla antes seria decorar una base
de datos vacia.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from fantasyhelper import display, queries
from fantasyhelper.config import settings, setup_logging
from fantasyhelper.storage.db import connect, init_db

app = typer.Typer(help="FantasyHelper: asistente de decisiones para fantasy de futbol.", no_args_is_help=True)
mister_app = typer.Typer(help="Comandos del adapter de Mister.", no_args_is_help=True)
app.add_typer(mister_app, name="mister")

console = Console()
log = logging.getLogger(__name__)


@app.callback()
def _main() -> None:
    setup_logging()


@app.command()
def init() -> None:
    """Crea la base de datos y las carpetas de trabajo."""
    path = init_db()
    console.print(f"[green]Base de datos lista:[/green] {path}")
    console.print(f"Temporada configurada: [bold]{settings.season}[/bold]")
    if not settings.mister_configured:
        console.print(
            "\n[yellow]Falta configurar Mister.[/yellow] Copia .env.example a .env "
            "y despues ejecuta [bold]fh mister har <fichero.har>[/bold]."
        )


@app.command()
def estado() -> None:
    """Resumen del historico capturado: cuantos dias llevas y si falta alguno."""
    conn = connect()
    try:
        table = Table(title="Historico capturado", header_style="bold")
        table.add_column("Serie")
        table.add_column("Filas", justify="right")
        table.add_column("Dias", justify="right")
        table.add_column("Desde")
        table.add_column("Hasta")

        series = {
            "Valores de mercado": "player_value_snapshot",
            "Propiedad / clausulas": "ownership_snapshot",
            "Estado de rivales": "manager_snapshot",
            "Mercado diario": "market_listing_snapshot",
            "Probabilidad de once": "lineup_probability_snapshot",
        }
        for label, table_name in series.items():
            row = conn.execute(
                f"SELECT COUNT(*) AS n, COUNT(DISTINCT snapshot_date) AS d, "
                f"MIN(snapshot_date) AS a, MAX(snapshot_date) AS b FROM {table_name}"
            ).fetchone()
            table.add_row(
                label, str(row["n"]), str(row["d"]), row["a"] or "-", row["b"] or "-"
            )
        console.print(table)

        # Lo que alimenta a xPts no va por dias, sino por cobertura: de cuantos
        # jugadores sabemos algo y hasta que jornada llega el calendario.
        base = conn.execute(
            """
            SELECT (SELECT COUNT(DISTINCT player_id) FROM player_season_stat) AS con_historico,
                   (SELECT COUNT(*) FROM player) AS jugadores,
                   (SELECT COUNT(*) FROM fixture) AS partidos,
                   (SELECT MAX(matchday) FROM fixture) AS ultima_jornada,
                   (SELECT COUNT(*) FROM player_points) AS jornadas_jugadas
            """
        ).fetchone()
        console.print(
            f"\n[bold]Base de puntos esperados[/bold]\n"
            f"  Historico por temporada  {base['con_historico']} de {base['jugadores']} jugadores\n"
            f"  Calendario               {base['partidos']} partidos, "
            f"hasta la J{base['ultima_jornada'] or 0}\n"
            f"  Puntos por jornada       {base['jornadas_jugadas']} registros"
        )

        raw = conn.execute(
            "SELECT source, COUNT(*) AS n, SUM(LENGTH(content)) AS bytes FROM raw_payload "
            "GROUP BY source"
        ).fetchall()
        if raw:
            console.print("\n[bold]Almacen crudo[/bold]")
            for row in raw:
                console.print(
                    f"  {row['source']:<16} {row['n']:>5} respuestas  "
                    f"{(row['bytes'] or 0) / 1_048_576:.1f} MB"
                )

        runs = conn.execute(
            "SELECT job_name, started_at, status, rows_written, error FROM job_run "
            "ORDER BY started_at DESC LIMIT 5"
        ).fetchall()
        if runs:
            console.print("\n[bold]Ultimas ejecuciones[/bold]")
            for row in runs:
                color = "green" if row["status"] == "ok" else "red"
                console.print(
                    f"  [{color}]{row['status']:<6}[/{color}] {row['started_at']}  "
                    f"{row['rows_written'] or 0} filas"
                    + (f"  [red]{row['error']}[/red]" if row["error"] else "")
                )
        else:
            console.print("\n[yellow]Todavia no se ha ejecutado ninguna captura.[/yellow]")
    finally:
        conn.close()


@app.command()
def capturar(
    solo: str = typer.Option(
        None, help="Capturar una sola fuente: 'mister' o 'futbolfantasy'."
    ),
) -> None:
    """Ejecuta la captura diaria ahora mismo."""
    from fantasyhelper.jobs.snapshot import run_snapshot

    sources = (solo,) if solo else ("mister", "futbolfantasy")
    total = run_snapshot(sources=sources)
    console.print(f"[green]Captura terminada:[/green] {total} filas escritas.")


@app.command()
def planificador() -> None:
    """Arranca el planificador que captura automaticamente cada dia."""
    from fantasyhelper.jobs.scheduler import main as scheduler_main

    scheduler_main()


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

def _con_liga() -> tuple[object, object]:
    """Conexion y participante propio, o aborta con un mensaje util."""
    conn = connect()
    me = queries.my_manager(conn)
    if me is None:
        conn.close()
        console.print(
            "[red]No se sabe cual de los participantes eres tu.[/red]\n"
            "Ejecuta [bold]fh capturar --solo mister[/bold]: tu plantilla se "
            "identifica al leer /team."
        )
        raise typer.Exit(1)
    return conn, me


@app.command()
def plantilla(
    de: str = typer.Option(None, help="Ver la plantilla de un rival por su nombre."),
) -> None:
    """Tu plantilla: valor, cláusula, probabilidad de jugar y próximo rival."""
    conn, me = _con_liga()
    try:
        manager_id, titulo = me["id"], f"Plantilla de {me['name']}"
        if de:
            rival = conn.execute(
                "SELECT id, name FROM manager WHERE name LIKE ? LIMIT 1", (f"%{de}%",)
            ).fetchone()
            if rival is None:
                console.print(f"[red]No hay ningun participante que se parezca a '{de}'.[/red]")
                raise typer.Exit(1)
            manager_id, titulo = rival["id"], f"Plantilla de {rival['name']}"

        rows = queries.squad(conn, manager_id)
        if not rows:
            console.print("[yellow]Sin datos de plantilla todavia.[/yellow]")
            return

        table = display.player_table(titulo, extra=("Clausula",))
        for row in rows:
            table.add_row(*display.player_row(row, display.money(row["clause_value"])))
        console.print(table)

        total = sum(r["market_value"] or 0 for r in rows)
        clausulas = sum(r["clause_value"] or 0 for r in rows)
        console.print(
            f"\n  {len(rows)} jugadores · valor [bold]{display.money(total)}[/bold] · "
            f"blindarlos todos costaria {display.money(clausulas)}"
        )
    finally:
        conn.close()


@app.command()
def mercado() -> None:
    """El mercado de hoy, ordenado por probabilidad de ser titular."""
    conn = connect()
    try:
        rows = queries.market(conn)
        if not rows:
            console.print(
                "[yellow]No hay mercado capturado.[/yellow] Ejecuta [bold]fh capturar[/bold]."
            )
            return

        table = display.player_table("Mercado de hoy", extra=("Precio",))
        for row in rows:
            table.add_row(*display.player_row(row, display.money(row["asking_price"])))
        console.print(table)
    finally:
        conn.close()


def _por_coste_de_punto(filas: list[dict]) -> list[dict]:
    """Ordena por euros por punto esperado, dejando al final los que no lo tienen.

    Un jugador sin puntos esperados no es que sea mala compra: es que no
    sabemos nada de el, normalmente por no tener historico en Primera. Ponerlo
    al final con su coste ajustado de siempre lo deja visible sin que compita
    de tu a tu con los que si tienen numeros.
    """
    return sorted(
        filas,
        key=lambda f: (f["coste_por_punto"] is None, f["coste_por_punto"] or 0),
    )


@app.command()
def clausulas(
    saldo: int = typer.Option(None, help="Filtrar por lo que te puedes permitir (en euros)."),
    minimo: float = typer.Option(0.6, help="Probabilidad minima de ser titular."),
    limite: int = typer.Option(15, help="Cuantas filas mostrar."),
) -> None:
    """Radar de cláusulas: a quién sale más a cuenta arrebatarle un jugador.

    Ordena por euros de cláusula por punto esperado. Cuando aún no hay puntos
    esperados de un jugador -un debutante, o antes de descargar el histórico-
    cae al criterio anterior, el coste ajustado por probabilidad y jerarquía.
    """
    from fantasyhelper import xpts as modelo

    conn, me = _con_liga()
    try:
        # Por defecto se filtra por lo que realmente puedes pagar: mostrar
        # objetivos fuera de tu alcance solo estorba.
        if saldo is None:
            saldo = queries.my_balance(conn)
            if saldo is not None:
                console.print(
                    f"[dim]Filtrando por tu saldo: {display.money(saldo)} € "
                    f"(usa --saldo para cambiarlo)[/dim]"
                )

        objetivos = queries.clause_targets(
            conn, manager_id=me["id"], budget=saldo, min_probability=minimo
        )
        if not objetivos:
            console.print("[yellow]Ningun objetivo cumple el filtro.[/yellow]")
        else:
            objetivos = _por_coste_de_punto(
                modelo.attach(conn, objetivos, cost_field="clause_value")
            )
            table = display.player_table(
                "Objetivos: lo que cuesta cada punto esperado",
                extra=("Clausula", "xPts", "€/pt", "Dueno"),
            )
            for row in objetivos[:limite]:
                table.add_row(*display.player_row(
                    row,
                    display.money(row["clause_value"], short=True),
                    display.points(row["xpts"]),
                    display.cost_per_point(row["coste_por_punto"]),
                    display.truncate(row["owner"], 10),
                ))
            console.print(table)

        riesgo = queries.clause_risk(conn, me["id"])
        if riesgo:
            riesgo = _por_coste_de_punto(
                modelo.attach(conn, riesgo, cost_field="clause_value")
            )
            table = display.player_table(
                "\nTuyos mas apetecibles: los primeros son los que hay que blindar",
                extra=("Clausula", "xPts", "€/pt"),
            )
            for row in riesgo[:8]:
                table.add_row(*display.player_row(
                    row, display.money(row["clause_value"], short=True),
                    display.points(row["xpts"]),
                    display.cost_per_point(row["coste_por_punto"]),
                ))
            console.print(table)
    finally:
        conn.close()


@app.command()
def xpts(
    limite: int = typer.Option(20, help="Cuantas filas mostrar."),
    posicion: str = typer.Option(None, help="Filtrar por PT, DF, MC o DL."),
    maximo: int = typer.Option(None, help="Valor maximo del jugador, en euros."),
    minimo: float = typer.Option(0.0, help="Probabilidad minima de ser titular."),
    libres: bool = typer.Option(False, "--libres", help="Solo jugadores sin dueno."),
    orden: str = typer.Option(
        "coste", help="'coste' = euros por punto esperado; 'puntos' = mas puntos."
    ),
) -> None:
    """Puntos esperados por jornada y cuánto cuesta cada uno.

    Ordenado por euros por punto esperado, que es la comparación que de verdad
    decide una compra: da igual que uno sume más si cuesta el triple.

    Sin filtro de probabilidad la cabeza de la lista se llena de suplentes
    baratos, y no es un error: a la larga rinden muchos puntos por euro. Para
    decidir una alineación concreta conviene subir --minimo.
    """
    from fantasyhelper import xpts as modelo

    conn = connect()
    try:
        filas = modelo.attach(conn, queries.all_players(conn), cost_field="market_value")

        filas = [f for f in filas if f["xpts"]]
        if minimo:
            filas = [f for f in filas if modelo.playing_probability(f) >= minimo]
        if posicion:
            filas = [f for f in filas if f["position"] == posicion.upper()]
        if maximo is not None:
            filas = [f for f in filas if (f["market_value"] or 0) <= maximo]
        if libres:
            filas = [f for f in filas if not f["owner"]]

        if not filas:
            console.print(
                "[yellow]Sin datos suficientes.[/yellow] Hace falta el historico "
                "por temporada: [bold]fh mister historico[/bold]."
            )
            return

        if orden == "puntos":
            filas.sort(key=lambda f: -f["xpts"])
        else:
            filas.sort(key=lambda f: f["coste_por_punto"] or float("inf"))

        table = display.player_table(
            "Puntos esperados en la proxima jornada",
            extra=("Media", "xPts", "€/pt", "Dueno"),
        )
        for fila in filas[:limite]:
            table.add_row(*display.player_row(
                fila,
                display.points(fila.get("media_base")),
                display.points(fila["xpts"]),
                display.cost_per_point(fila["coste_por_punto"]),
                display.truncate(fila["owner"], 10) if fila["owner"] else "libre",
            ))
        console.print(table)
        console.print(
            "\n[dim]xPts = probabilidad × media esperada × rival × sede. "
            "La media mezcla el historico con lo que lleve esta temporada.[/dim]"
        )
    finally:
        conn.close()


@app.command()
def valor(
    limite: int = typer.Option(12, help="Cuantas filas por tabla."),
    minimo: int = typer.Option(None, help="Valor minimo del jugador, en euros."),
    mios: bool = typer.Option(False, "--mios", help="Solo tus jugadores."),
) -> None:
    """Quién va a subir y quién va a bajar de valor la próxima semana.

    En Mister la revalorización es beneficio limpio, así que esto es dinero que
    no depende de puntos ni de alineaciones.
    """
    from fantasyhelper import market

    conn = connect()
    try:
        modelo = market.calibrate(conn)
        if not modelo.useful:
            console.print(
                "[yellow]La señal es demasiado débil para fiarse.[/yellow] "
                "Hace falta mas historico: [bold]fh mister historico[/bold]."
            )
            return

        deriva = market.band_drift(conn)
        tabla = Table(title="Como se comporta cada tramo de precio",
                      header_style="bold", title_justify="left")
        for columna in ("Tramo", "Movimiento 7d", "Volatilidad", "Fiabilidad", "Muestras"):
            tabla.add_column(columna, justify="right" if columna != "Tramo" else "left")
        for _, _, nombre in market.PRICE_BANDS:
            banda = modelo.bands.get(nombre)
            if banda is None:
                continue
            tabla.add_row(
                nombre,
                display.percent(deriva.get(nombre)),
                f"{banda.volatility:.1%}",
                f"{banda.correlation:+.2f}" if banda.useful else "[red]no fiable[/red]",
                f"{banda.samples:,}".replace(",", "."),
            )
        console.print(tabla)

        filas = market.attach(conn, queries.all_players(conn), model=modelo)
        filas = [f for f in filas if f.get("ventaja") is not None]
        if minimo:
            filas = [f for f in filas if (f["market_value"] or 0) >= minimo]
        if mios:
            me = queries.my_manager(conn)
            filas = [f for f in filas if me and f["owner"] == me["name"]]
        if not filas:
            console.print("[yellow]Sin datos suficientes.[/yellow]")
            return

        filas.sort(key=lambda f: -f["euros_ventaja"])
        for titulo, seleccion in (
            ("\nVan a subir mas que los de su precio", filas[:limite]),
            ("\nVan a quedarse atras: si tienes alguno, es el momento de venderlo",
             list(reversed(filas))[:limite]),
        ):
            table = display.player_table(
                titulo, extra=("Tramo", "7d %", "vs tramo", "€", "Dueno")
            )
            for fila in seleccion:
                table.add_row(*display.player_row(
                    fila,
                    fila["tramo"],
                    display.percent(fila["cambio_reciente"]),
                    display.percent(fila["ventaja"]),
                    display.delta(fila["euros_ventaja"]),
                    display.truncate(fila["owner"], 10) if fila["owner"] else "libre",
                ))
            console.print(table)

        console.print(
            "\n[dim]'vs tramo' es lo que se espera que suba POR ENCIMA de los de su "
            "mismo precio, que es lo unico que se ha medido. Que suba el tramo "
            "entero da igual para decidir: entonces sube tambien lo que ya tienes."
            "[/dim]"
        )
    finally:
        conn.close()


@app.command()
def chollos(
    minimo: float = typer.Option(0.7, help="Probabilidad minima de ser titular."),
    limite: int = typer.Option(20, help="Cuantas filas mostrar."),
) -> None:
    """Jugadores libres y baratos que además van a jugar."""
    conn = connect()
    try:
        rows = queries.free_agents(conn, min_probability=minimo, limit=limite)
        if not rows:
            console.print("[yellow]Ningun jugador libre cumple el filtro.[/yellow]")
            return
        table = display.player_table(f"Libres con probabilidad >= {minimo:.0%}")
        for row in rows:
            table.add_row(*display.player_row(row))
        console.print(table)
    finally:
        conn.close()


@app.command()
def jugador(
    nombre: str = typer.Argument(..., help="Nombre o parte del nombre."),
    dias: int = typer.Option(90, help="Dias de historico de valor a dibujar."),
) -> None:
    """Ficha de un jugador, con la evolución de su valor de mercado."""
    conn = connect()
    try:
        rows = queries.find_players(conn, nombre)
        if not rows:
            console.print(f"[yellow]Ningun jugador coincide con '{nombre}'.[/yellow]")
            return

        if len(rows) > 1:
            table = display.player_table(f"{len(rows)} coincidencias", extra=("Dueno",))
            for row in rows:
                table.add_row(*display.player_row(row, row["owner"] or "libre"))
            console.print(table)
            console.print("\n[dim]Afina el nombre para ver la ficha completa.[/dim]")
            return

        row = rows[0]
        console.print(f"\n[bold]{row['name']}[/bold]  {row['position'] or '?'} · "
                      f"{row['team'] or '?'}")
        console.print(f"  Valor        {display.money(row['market_value'])} "
                      f"{display.delta(row['delta_1d'])}")
        console.print(f"  Probabilidad {display.probability(row['probability'])} "
                      f"{display.status(row['status'])}")
        console.print(f"  Jerarquia    {row['jerarquia'] if row['jerarquia'] is not None else '-'}")
        console.print(f"  Proximo      {display.opponent(row)}")
        console.print(f"  Dueno        {row['owner'] or 'libre'}"
                      + (f" · clausula {display.money(row['clause_value'])}"
                         if row["clause_value"] else ""))

        history = queries.value_history(conn, row["id"], days=dias)
        if len(history) > 1:
            valores = [h["market_value"] for h in history]
            cambio = valores[-1] - valores[0]
            console.print(
                f"\n  Valor ultimos {len(valores)} dias  "
                f"{display.sparkline(valores)}  {display.delta(cambio)}"
            )
            console.print(
                f"  [dim]{history[0]['snapshot_date']}  "
                f"min {display.money(min(valores), short=True)} / "
                f"max {display.money(max(valores), short=True)}  "
                f"{history[-1]['snapshot_date']}[/dim]"
            )
    finally:
        conn.close()


@app.command()
def liga() -> None:
    """Clasificación de tu liga."""
    conn = connect()
    try:
        rows = queries.standings(conn)
        if not rows:
            console.print("[yellow]Sin clasificacion capturada.[/yellow]")
            return
        table = Table(title="Clasificacion", header_style="bold", title_justify="left")
        table.add_column("#", justify="right")
        table.add_column("Participante")
        table.add_column("Puntos", justify="right")
        table.add_column("Jugadores", justify="right")
        table.add_column("Valor plantilla", justify="right")
        for row in rows:
            nombre = f"[bold]{row['name']}[/bold]" if row["is_me"] else row["name"]
            table.add_row(
                str(row["position"] or "-"), nombre, str(row["points"] or 0),
                str(row["squad_size"] or 0), display.money(row["team_value"]),
            )
        console.print(table)
    finally:
        conn.close()


@app.command()
def movimientos(
    tipo: str = typer.Option(None, help="Filtrar por tipo, p.ej. 'market' o 'join'."),
    limite: int = typer.Option(25, help="Cuantos mostrar."),
    tipos: bool = typer.Option(False, "--tipos", help="Solo el resumen por tipo."),
) -> None:
    """Movimientos de la liga: fichajes, ventas, cláusulas y altas.

    Se guarda toda tarjeta del feed aunque no sepamos aún qué representa, para
    no perder movimientos mientras se descubren los tipos.
    """
    import json as _json

    conn = connect()
    try:
        resumen = queries.feed_kinds(conn)
        if not resumen:
            console.print(
                "[yellow]Sin movimientos capturados.[/yellow] "
                "Ejecuta [bold]fh capturar --solo mister[/bold]."
            )
            return

        if tipos:
            table = Table(title="Tipos de movimiento vistos", header_style="bold",
                          title_justify="left")
            table.add_column("Tipo")
            table.add_column("Nº", justify="right")
            table.add_column("Desde")
            table.add_column("Hasta")
            for row in resumen:
                table.add_row(row["kind"] or "?", str(row["n"]),
                              (row["desde"] or "")[:10], (row["hasta"] or "")[:10])
            console.print(table)
            return

        table = Table(title="Movimientos", header_style="bold", title_justify="left")
        table.add_column("Visto", no_wrap=True)
        table.add_column("Tipo", no_wrap=True)
        table.add_column("Qué")
        table.add_column("Importes", justify="right", no_wrap=True)
        for row in queries.feed_events(conn, kind=tipo, limit=limite):
            importes = _json.loads(row["amounts"] or "[]")
            table.add_row(
                (row["first_seen"] or "")[:10],
                (row["kind"] or "?").removeprefix("card-"),
                display.truncate(row["summary"].replace(" | ", " "), 60),
                " ".join(display.money(i, short=True) for i in importes[:3]) or "",
            )
        console.print(table)
    finally:
        conn.close()


@app.command()
def bonificaciones(
    aplicar: bool = typer.Option(
        False, "--aplicar", help="Guardar la configuración conocida de LA LIGA 26/27."
    ),
) -> None:
    """Bonificaciones de la liga: qué se cobra por cada concepto."""
    from fantasyhelper import bonuses
    from fantasyhelper.storage.db import transaction

    conn = connect()
    try:
        if aplicar:
            with transaction(conn):
                bonuses.save_rules(conn, bonuses.LA_LIGA_2627)
            console.print("[green]Bonificaciones guardadas.[/green]")

        reglas = bonuses.load_rules(conn)
        if not reglas.per_point and not reglas.by_matchday_rank:
            console.print(
                "[yellow]No hay bonificaciones configuradas.[/yellow] "
                "Ejecuta [bold]fh bonificaciones --aplicar[/bold]."
            )
            return

        console.print("[bold]Por jornada[/bold]")
        console.print(f"  Por punto              {display.money(reglas.per_point)} €")
        console.print(f"  Por jugador en el once {display.money(reglas.per_ideal_xi_player)} €")
        console.print(f"  Por acierto de quiniela {display.money(reglas.per_quiniela_hit)} €")
        if reglas.per_goal:
            console.print(f"  Por gol                {display.money(reglas.per_goal)} €")
        if reglas.fixed_per_matchday:
            console.print(f"  Fija                   {display.money(reglas.fixed_per_matchday)} €")

        if reglas.by_matchday_rank:
            console.print("\n[bold]Por puesto en la jornada[/bold]")
            for puesto, importe in sorted(reglas.by_matchday_rank.items()):
                console.print(f"  {puesto:>2}º  {display.money(importe)} €")
    finally:
        conn.close()


@app.command()
def baseline() -> None:
    """Congela el punto de partida de la liga para poder estimar saldos.

    Ejecútalo tras crear o reiniciar la liga, con una captura recién hecha y
    antes de que nadie fiche: guarda el valor de cada plantilla en ese instante,
    que es el ancla de la que cuelga todo el cálculo de saldos.
    """
    from fantasyhelper.storage.db import transaction

    conn = connect()
    try:
        anterior = queries.baseline_at(conn)
        with transaction(conn):
            n = queries.freeze_baseline(conn)
        nuevo = queries.baseline_at(conn)
    finally:
        conn.close()

    if not n:
        console.print(
            "[red]No hay plantillas capturadas.[/red] "
            "Ejecuta antes [bold]fh capturar --solo mister[/bold]."
        )
        raise typer.Exit(1)

    if anterior:
        console.print(f"[yellow]Se reemplaza el punto de partida anterior[/yellow] ({anterior}).")
    console.print(f"[green]Punto de partida congelado:[/green] {n} participantes, {nuevo}.")


@app.command()
def saldos() -> None:
    """Saldo estimado de cada rival.

    Mister solo publica el tuyo. El de los demás se reconstruye a partir de la
    regla con la que arranca la liga (50M menos el valor de los 15 jugadores
    iniciales) descontando lo que hayan gastado en subir cláusulas.
    """
    conn = connect()
    try:
        filas = queries.estimated_balances(conn)
        if not filas:
            console.print(
                "[yellow]Sin datos suficientes.[/yellow] Hace falta una captura de "
                "las plantillas: [bold]fh capturar --solo mister[/bold]."
            )
            return

        table = Table(title="Saldo estimado", header_style="bold", title_justify="left")
        table.add_column("Participante")
        table.add_column("Plantilla", justify="right")
        table.add_column("Clausulas", justify="right")
        table.add_column("Saldo estimado", justify="right")
        table.add_column("Saldo real", justify="right")
        table.add_column("Desvio", justify="right")
        for row in filas:
            nombre = f"[bold]{row['name']}[/bold]" if row["is_me"] else row["name"]
            real = desvio = "-"
            # Solo se conoce el saldo propio, y es el unico contraste posible.
            if row["saldo_real"] is not None:
                diferencia = row["saldo_estimado"] - row["saldo_real"]
                color = "green" if abs(diferencia) < 100_000 else "red"
                real = display.money(row["saldo_real"])
                desvio = f"[{color}]{display.delta(diferencia) or '0'}[/{color}]"
            table.add_row(
                nombre,
                display.money(row["valor_plantilla"]),
                display.money(-int(row["gasto_clausulas"]) or None),
                display.money(int(row["saldo_estimado"])),
                real,
                desvio,
            )
        console.print(table)
        console.print(
            "\n[dim]saldo = 50M − plantilla − clausulas. Comprar o vender a precio de "
            "mercado no altera la suma: solo mueve dinero entre caja y plantilla.[/dim]"
        )
    finally:
        conn.close()


@app.command()
def reconciliar() -> None:
    """Unifica equipos y jugadores entre fuentes. Se ejecuta ya al capturar."""
    from fantasyhelper.reconcile import reconcile
    from fantasyhelper.storage.db import transaction

    conn = connect()
    try:
        with transaction(conn):
            report = reconcile(conn)
    finally:
        conn.close()

    console.print(f"  equipos fusionados : {report.teams_merged}")
    console.print(f"  jugadores enlazados: {report.players_linked}")
    console.print(f"  siguen sin cruzar  : {report.still_unmatched}")
    if report.players_linked:
        console.print("\nRevisa los enlaces deducidos con [bold]fh dudas[/bold].")


@app.command()
def dudas() -> None:
    """Jugadores cuyo cruce entre fuentes no es seguro y conviene revisar."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT p.name, a.provider, a.external_name, a.confidence "
            "FROM player_alias a JOIN player p ON p.id = a.player_id "
            "WHERE a.confidence < 1.0 ORDER BY a.confidence"
        ).fetchall()
        if not rows:
            console.print("[green]Ningun cruce dudoso.[/green]")
            return
        table = Table(title="Cruces a revisar", header_style="bold")
        table.add_column("Jugador canonico")
        table.add_column("Fuente")
        table.add_column("Nombre en la fuente")
        table.add_column("Confianza", justify="right")
        for row in rows:
            table.add_row(
                row["name"], row["provider"], row["external_name"] or "-",
                f"{row['confidence']:.2f}",
            )
        console.print(table)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mister
# ---------------------------------------------------------------------------

@mister_app.command("har")
def mister_har(
    fichero: Path = typer.Argument(..., exists=True, readable=True,
                                   help="HAR exportado desde DevTools."),
) -> None:
    """Descubre los endpoints de Mister a partir de un HAR del navegador.

    Como obtenerlo: entra en tu liga en el navegador, F12 > pestaña Red, recarga
    la pagina, navega por Mercado y Clasificacion, y luego click derecho >
    'Guardar todo como HAR'.
    """
    from fantasyhelper.adapters.mister.har import parse_har, write_candidates

    entries, cookies, league_ids = parse_har(fichero)
    if not entries:
        console.print(
            "[red]No se encontro trafico JSON de Mister en el HAR.[/red] "
            "Asegurate de haber navegado por la app con la pestaña Red abierta."
        )
        raise typer.Exit(1)

    endpoints = write_candidates(entries)

    table = Table(title=f"{len(entries)} endpoints encontrados", header_style="bold")
    table.add_column("Propuesta")
    table.add_column("Metodo")
    table.add_column("Ruta")
    table.add_column("Bytes", justify="right")
    for entry in entries[:25]:
        table.add_row(
            f"[green]{entry.guessed_key}[/green]" if entry.guessed_key else "-",
            entry.method, entry.path, str(entry.size),
        )
    console.print(table)

    console.print(f"\n[green]Preconfigurados:[/green] {', '.join(endpoints) or 'ninguno'}")
    if league_ids:
        console.print(f"[cyan]Posibles IDs de liga:[/cyan] {', '.join(league_ids)}")
        console.print("  -> ponlo en MISTER_LEAGUE_ID en tu .env")
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        console.print(
            f"\n[cyan]Cookie de sesion detectada[/cyan] ({len(cookies)} valores). "
            "Copiala a MISTER_TOKEN en .env:"
        )
        console.print(f"[dim]{cookie_header[:200]}{'...' if len(cookie_header) > 200 else ''}[/dim]")
    else:
        # Chrome ofrece dos exportaciones y la que censura cookies es la que
        # sale por defecto, asi que este caso es el habitual, no un error raro.
        console.print(
            "\n[yellow]El HAR no incluye cookies[/yellow] (exportacion saneada de Chrome). "
            "Sin sesion no se puede capturar tu liga. Para conseguirla:\n"
            "  DevTools > pestaña Red > click en una peticion a "
            "mister.mundodeportivo.com >\n"
            "  Cabeceras > Request Headers > copia el valor entero de [bold]Cookie[/bold]\n"
            "  y pegalo en [bold]MISTER_TOKEN[/bold] en tu .env"
        )
    console.print(
        "\nRevisa [bold]data/mister_endpoints.json[/bold]: la seccion 'candidates' "
        "incluye una muestra de cada respuesta para confirmar cual es cual."
    )


@mister_app.command("sesion")
def mister_sesion(
    fichero: Path = typer.Argument(
        None,
        help="Fichero con el comando cURL. Si se omite, se lee del portapapeles.",
    ),
) -> None:
    """Configura la sesion de Mister a partir de un cURL copiado del navegador.

    En Brave/Chrome: DevTools (F12) > pestaña Red > click derecho sobre una
    peticion a mister.mundodeportivo.com > Copiar > Copiar como cURL.
    Luego ejecuta este comando (sin argumentos si lo tienes en el portapapeles).
    """
    from fantasyhelper.adapters.mister.curl import extract_session, update_env_var
    from fantasyhelper.config import PROJECT_ROOT

    if fichero:
        texto = fichero.read_text(encoding="utf-8", errors="replace")
    else:
        texto = _leer_portapapeles()
        if not texto:
            console.print(
                "[red]No se pudo leer el portapapeles.[/red] Pega el cURL en un fichero "
                "y ejecuta: [bold]fh mister sesion fichero.txt[/bold]"
            )
            raise typer.Exit(1)

    sesion = extract_session(texto)
    if not sesion.is_usable():
        console.print(
            "[red]No se encontro ninguna cookie en el cURL.[/red]\n"
            "Asegurate de haber copiado con [bold]Copiar como cURL[/bold] una peticion "
            "a mister.mundodeportivo.com estando con la sesion iniciada."
        )
        raise typer.Exit(1)

    token = sesion.cookie_header
    xauth = sesion.headers.get("x-auth")

    env_path = PROJECT_ROOT / ".env"
    update_env_var(env_path, "MISTER_TOKEN", token)
    console.print(f"[green]{len(sesion.cookies)} cookies extraidas:[/green] "
                  f"{', '.join(sesion.cookies)}")

    if xauth:
        update_env_var(env_path, "MISTER_XAUTH", xauth)
        console.print("[green]Cabecera x-auth extraida.[/green]")
    else:
        console.print(
            "[yellow]No se encontro la cabecera x-auth.[/yellow] Mister la exige: "
            "copia el cURL de una peticion POST a /market, /team o /standings, "
            "no de una imagen ni de un fichero estatico."
        )
        raise typer.Exit(1)

    console.print("MISTER_TOKEN y MISTER_XAUTH escritos en .env")
    console.print("\nComprobando que la sesion funciona...")

    # Probar de verdad: una cookie mal copiada da un 200 con pantalla de login,
    # asi que no vale con guardarla y suponer.
    from fantasyhelper.adapters.mister.client import MisterClient
    from fantasyhelper.adapters.mister.endpoints import load_endpoints
    from fantasyhelper.adapters.mister.parsers import parse_standings

    conn = connect()
    try:
        client = MisterClient()
        # settings se leyo al importar, antes de escribir el .env: aplicamos lo
        # recien extraido en vez de confiar en lo que hay en memoria.
        client.apply_token(token)
        client.apply_xauth(xauth)
        html = client.fetch(conn, load_endpoints()["standings"])
        managers = parse_standings(html)
    except Exception as exc:
        console.print(f"[red]La sesion no funciona:[/red] {exc}")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if not managers:
        console.print(
            "[yellow]La peticion funciono pero no se leyo ninguna clasificacion.[/yellow] "
            "Revisa data/ para ver el HTML devuelto."
        )
        raise typer.Exit(1)

    client.save_session()
    console.print(f"[green]Sesion valida.[/green] Liga con {len(managers)} participantes:")
    for manager in managers[:5]:
        console.print(
            f"  {manager.position or '?':>2}. {manager.name:<24} "
            f"{manager.points or 0:>4} pts   plantilla {manager.team_value or 0:,} €".replace(
                ",", "."
            )
        )
    console.print("\nYa puedes ejecutar [bold]fh capturar[/bold].")


def _leer_portapapeles() -> str | None:
    """Lee el portapapeles de Windows sin dependencias externas."""
    import subprocess

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


@mister_app.command("historico")
def mister_historico(
    limite: int = typer.Option(None, help="Procesar solo N jugadores (para probar)."),
    rehacer: bool = typer.Option(
        False, "--rehacer", help="Reprocesar tambien los que ya estan al dia."
    ),
) -> None:
    """Descarga la ficha completa de cada jugador: valores, temporadas y calendario.

    Una peticion por jugador, asi que tarda. Es reanudable: se puede cortar con
    Ctrl+C y relanzar sin perder lo hecho.
    """
    from fantasyhelper.adapters.mister.adapter import MisterAdapter

    conn = connect()
    adapter = MisterAdapter()
    try:
        adapter.login()
        procesados, filas = adapter.backfill_values(
            conn, limit=limite, skip_done=not rehacer
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrumpido.[/yellow] Relanza el comando para continuar.")
        raise typer.Exit(130) from None
    finally:
        adapter.client.close()
        conn.close()

    console.print(f"\n[green]Fichas descargadas:[/green] {procesados} jugadores")
    for concepto, n in filas.items():
        console.print(f"  {concepto:12} {display.money(n)}")


@mister_app.command("reprocesar")
def mister_reprocesar() -> None:
    """Relee las fichas ya guardadas y extrae lo que en su dia no se guardo.

    No hace ni una peticion: trabaja sobre el crudo almacenado. Es lo que
    justifica guardarlo.
    """
    from fantasyhelper.adapters.mister.adapter import MisterAdapter

    conn = connect()
    try:
        procesadas, filas = MisterAdapter().reprocess_cards(conn)
    finally:
        conn.close()

    console.print(f"[green]Fichas releidas:[/green] {procesadas}")
    for concepto, n in filas.items():
        console.print(f"  {concepto:12} {display.money(n)}")


@mister_app.command("endpoints")
def mister_endpoints() -> None:
    """Muestra los endpoints de Mister configurados y los que faltan."""
    from fantasyhelper.adapters.mister.endpoints import REQUIRED_KEYS, load_endpoints

    configured = load_endpoints()
    table = Table(header_style="bold")
    table.add_column("Clave")
    table.add_column("Estado")
    table.add_column("Ruta")
    for key in REQUIRED_KEYS:
        endpoint = configured.get(key)
        table.add_row(
            key,
            "[green]ok[/green]" if endpoint else "[red]falta[/red]",
            endpoint.path if endpoint else "-",
        )
    console.print(table)


@mister_app.command("probar")
def mister_probar() -> None:
    """Comprueba que la sesion y los endpoints funcionan, sin escribir nada."""
    from fantasyhelper.adapters.mister.client import MisterClient

    conn = connect()
    try:
        with MisterClient() as client:
            client.login()
            console.print("[green]Sesion valida.[/green]")
            for key, endpoint in client.endpoints.items():
                try:
                    payload = client.fetch(
                        conn, endpoint, league_id=settings.mister_league_id or ""
                    )
                    size = len(payload) if isinstance(payload, (list, dict)) else 0
                    console.print(f"  [green]ok[/green]   {key:<12} {endpoint.path} ({size} items)")
                except Exception as exc:
                    console.print(f"  [red]fallo[/red] {key:<12} {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
