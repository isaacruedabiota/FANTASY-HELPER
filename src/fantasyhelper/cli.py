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
