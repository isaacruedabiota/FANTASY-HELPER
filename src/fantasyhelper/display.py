"""Formato de salida en terminal.

Separado de las consultas para que la web de la Fase 2 pueda reutilizar el SQL
sin arrastrar nada de rich.
"""

from __future__ import annotations

import sqlite3

from rich.table import Table

#: Bloques de altura creciente para dibujar la serie de valor en una celda.
SPARK_CHARS = "▁▂▃▄▅▆▇█"

#: Colores por probabilidad de ser titular.
PROB_COLORS = ((0.8, "green"), (0.6, "yellow"), (0.0, "red"))

STATUS_LABELS = {
    "ok": "",
    "duda": "[yellow]duda[/yellow]",
    "lesionado": "[red]lesion[/red]",
    "sancionado": "[red]sancion[/red]",
    "no_disponible": "[red]no disp.[/red]",
    "ausente": "[yellow]ausente[/yellow]",
}


def money(value: float | None, *, short: bool = False) -> str:
    """Formatea euros al estilo espanol. En corto, '6,9M' en vez de '6.940.000'."""
    if value is None:
        return "-"
    value = int(value)
    if short:
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:,.1f}M".replace(".", ",")
        if abs(value) >= 1_000:
            return f"{value // 1_000}k"
        return str(value)
    return f"{value:,}".replace(",", ".")


def delta(value: int | None, *, show_zero: bool = False) -> str:
    """Variacion en euros. El cero se deja en blanco salvo que se pida verlo.

    En una columna de variaciones el blanco se lee bien, pero en una de totales
    se confunde con "no hay dato", que es otra cosa. De ahi el interruptor.
    """
    if value is None or (not value and not show_zero):
        return ""
    if not value:
        return "[dim]0[/dim]"
    color = "green" if value > 0 else "red"
    sign = "+" if value > 0 else ""
    return f"[{color}]{sign}{money(value, short=True)}[/{color}]"


def probability(value: float | None) -> str:
    if value is None:
        return "-"
    color = next(c for threshold, c in PROB_COLORS if value >= threshold)
    return f"[{color}]{value:.0%}[/{color}]"


def status(value: str | None) -> str:
    return STATUS_LABELS.get(value or "ok", value or "")


def difficulty(value: int | None) -> str:
    """Dificultad del rival: 1 facil, 5 dificil."""
    if value is None:
        return "-"
    color = "green" if value <= 2 else "yellow" if value == 3 else "red"
    return f"[{color}]{value}[/{color}]"


#: Anchos pensados para que quince jugadores quepan en un terminal de 80 columnas
#: sin que rich parta las filas en varias lineas, que las vuelve ilegibles.
NAME_WIDTH = 18
TEAM_WIDTH = 9


def column(row: sqlite3.Row, name: str, default: object = None) -> object:
    """Lee una columna que puede no estar en el SELECT. sqlite3.Row no tiene .get()."""
    return row[name] if name in set(row.keys()) else default


def opponent(row: sqlite3.Row) -> str:
    """Rival de la proxima jornada: '@SEV·4' es fuera y dificil.

    Compacto a proposito: en una tabla de quince jugadores el ancho manda.
    """
    name = column(row, "opponent")
    if not name:
        return "-"
    where = "" if column(row, "is_home") else "@"
    nivel = column(row, "opponent_difficulty")
    # El calendario da el rival aunque no sepamos su dificultad; en ese caso se
    # omite el sufijo en vez de escribir un guion que no dice nada.
    sufijo = f"·{difficulty(nivel)}" if nivel is not None else ""
    return f"{where}{truncate(name, TEAM_WIDTH)}{sufijo}"


def percent(value: float | None) -> str:
    """Variacion relativa, coloreada como el dinero: verde sube, rojo baja."""
    if value is None:
        return "-"
    color = "green" if value > 0 else "red" if value < 0 else "dim"
    return f"[{color}]{value:+.1%}[/{color}]"


def points(value: float | None) -> str:
    """Puntos esperados. Un decimal: mas precision seria fingir exactitud."""
    return "-" if value is None else f"{value:.1f}"


def cost_per_point(value: float | None) -> str:
    """Euros por punto esperado, en corto. Es la cifra que compara de verdad."""
    return "-" if value is None else money(value, short=True)


def truncate(text: str | None, width: int) -> str:
    if not text:
        return "-"
    return text if len(text) <= width else text[: width - 1] + "…"


def sparkline(values: list[int]) -> str:
    """Dibuja una serie numerica con caracteres de bloque."""
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(values)
    span = high - low
    return "".join(
        SPARK_CHARS[min(int((v - low) / span * len(SPARK_CHARS)), len(SPARK_CHARS) - 1)]
        for v in values
    )


def player_table(title: str, *, extra: tuple[str, ...] = ()) -> Table:
    """Tabla base de jugadores; `extra` anade columnas al final."""
    table = Table(title=title, header_style="bold", title_justify="left", pad_edge=False)
    table.add_column("Jugador", no_wrap=True)
    table.add_column("P", justify="center", no_wrap=True)
    table.add_column("Equipo", no_wrap=True)
    table.add_column("Valor", justify="right", no_wrap=True)
    table.add_column("7d", justify="right", no_wrap=True)
    table.add_column("Prob", justify="right", no_wrap=True)
    table.add_column("Estado", no_wrap=True)
    table.add_column("Rival", no_wrap=True)
    for name in extra:
        table.add_column(name, justify="right", no_wrap=True)
    return table


def player_row(row: sqlite3.Row, *extra: str) -> tuple[str, ...]:
    # El balon marca al lanzador de penaltis del equipo.
    penalties = "⚽" if column(row, "penalties") == 1 else ""
    return (
        truncate(row["name"], NAME_WIDTH) + penalties,
        row["position"] or "?",
        truncate(row["team"], TEAM_WIDTH),
        money(row["market_value"], short=True),
        delta(column(row, "change_7d") or row["delta_1d"]),
        probability(row["probability"]),
        status(row["status"]),
        opponent(row),
        *extra,
    )
