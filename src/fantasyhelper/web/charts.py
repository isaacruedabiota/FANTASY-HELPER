"""Graficos en SVG generado aqui, sin ninguna libreria de dibujo.

Tres razones para hacerlo en el servidor en vez de mandar datos y una libreria:
la Raspberry no tiene que servir 200 KB de JavaScript por visita, el grafico se
ve aunque el movil vaya mal de red, y sale igual en cualquier navegador.

DONDE VA CADA COSA

El SVG lleva SOLO la geometria -linea, relleno, rejilla- y se estira con
`preserveAspectRatio="none"` para ocupar el ancho que haya. Las etiquetas van
aparte, en HTML colocado encima.

No es un capricho. La primera version metia el texto dentro del SVG, y como el
viewBox es de 720 de ancho y en un movil se dibuja en 380, el estirado no
uniforme aplastaba las letras: en la captura los "26,0M" del eje salian
ilegibles. El texto en HTML no se escala con el dibujo y se lee igual en
cualquier pantalla.

DECISIONES DE FORMA, QUE NO SON DE GUSTO

  - Serie unica: linea de 2px con relleno al 10%, sin leyenda. Una leyenda de un
    solo elemento repite el titulo y ocupa sitio.
  - Rejilla en linea de un pixel y solida. Discontinua se lee como "umbral" o
    "proyeccion" cuando no es mas que una rejilla.
  - Etiquetas solo en los extremos. Un numero sobre cada punto es ruido y con
    366 puntos, ademas, ilegible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Sistema de coordenadas interno. El ancho real lo pone el contenedor: el SVG se
#: estira, y por eso el texto no puede vivir dentro.
WIDTH = 720
HEIGHT = 220

PAD_TOP, PAD_BOTTOM = 14, 22


@dataclass
class Chart:
    """Un grafico listo para la plantilla: dibujo, etiquetas y puntos."""

    svg: str = ""
    #: Marcas del eje vertical, colocadas en porcentaje desde arriba.
    ticks: list[dict] = field(default_factory=list)
    #: Fechas de los extremos del eje horizontal.
    desde: str = ""
    hasta: str = ""
    #: Puntos para la capa de hover, en coordenadas del viewBox.
    points: list[dict] = field(default_factory=list)
    empty: bool = True


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Marcas del eje en numeros redondos, que es lo unico que se lee de un vistazo."""
    if high <= low:
        return [low]
    bruto = (high - low) / count
    magnitud = 10 ** (len(str(int(bruto))) - 1) if bruto >= 1 else 1
    paso = magnitud
    for multiplo in (1, 2, 2.5, 5, 10):
        paso = magnitud * multiplo
        if bruto <= paso:
            break
    marcas: list[float] = []
    valor = int(low / paso) * paso
    while valor <= high + paso / 2:
        if valor >= low - paso / 2:
            marcas.append(valor)
        valor += paso
    return marcas


def _money(value: float) -> str:
    """Etiqueta corta de eje: 6,9M o 450k. En un eje no cabe mas."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".", ",")
    if abs(value) >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


def value_chart(history: list[dict]) -> Chart:
    """Evolucion del valor de un jugador: una serie, forma de linea.

    `history` son filas con snapshot_date y market_value, en orden cronologico.
    """
    datos = [
        (fila["snapshot_date"], fila["market_value"])
        for fila in history
        if fila["market_value"] is not None
    ]
    if len(datos) < 2:
        return Chart()

    valores = [v for _, v in datos]
    minimo, maximo = min(valores), max(valores)
    # Aire arriba y abajo para que la linea no toque los bordes del recuadro.
    margen = (maximo - minimo) * 0.12 or maximo * 0.05 or 1
    suelo, techo = minimo - margen, maximo + margen
    alto = HEIGHT - PAD_TOP - PAD_BOTTOM

    def y_de(valor: float) -> float:
        return PAD_TOP + alto * (1 - (valor - suelo) / (techo - suelo))

    def x_de(indice: int) -> float:
        return WIDTH * indice / (len(datos) - 1)

    puntos = [(round(x_de(i), 2), round(y_de(v), 2)) for i, (_, v) in enumerate(datos)]
    linea = " ".join(f"{x},{y}" for x, y in puntos)
    area = f"0,{HEIGHT} {linea} {WIDTH},{HEIGHT}"

    partes = [
        (
            f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" '
            f'preserveAspectRatio="none" aria-hidden="true">'
        )
    ]

    marcas = []
    for marca in _nice_ticks(suelo, techo):
        y = y_de(marca)
        if not (PAD_TOP - 1 <= y <= PAD_TOP + alto + 1):
            continue
        partes.append(f'<line class="grid" x1="0" y1="{y:.1f}" x2="{WIDTH}" y2="{y:.1f}"/>')
        marcas.append({"label": _money(marca), "top": round(y / HEIGHT * 100, 3)})

    partes.append(f'<polygon class="area" points="{area}"/>')
    partes.append(f'<polyline class="line" points="{linea}"/>')

    # La capa de hover: linea vertical y punto que mueve el JavaScript. El punto
    # lleva anillo del color de la superficie para leerse sobre la rejilla.
    partes.append('<line class="crosshair" x1="-9" y1="0" x2="-9" y2="0"/>')
    partes.append('<circle class="hover-ring" cx="-99" cy="-99" r="7"/>')
    partes.append('<circle class="hover-dot" cx="-99" cy="-99" r="4.5"/>')
    partes.append("</svg>")

    return Chart(
        svg="".join(partes),
        ticks=marcas,
        desde=datos[0][0],
        hasta=datos[-1][0],
        points=[
            {
                "x": puntos[i][0],
                "y": puntos[i][1],
                "left": round(puntos[i][0] / WIDTH * 100, 4),
                "top": round(puntos[i][1] / HEIGHT * 100, 4),
                "fecha": fecha,
                "valor": valor,
            }
            for i, (fecha, valor) in enumerate(datos)
        ],
        empty=False,
    )
