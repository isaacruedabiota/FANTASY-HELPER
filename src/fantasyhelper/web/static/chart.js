/* Capa de hover del grafico de valor.
 *
 * El SVG lo dibuja el servidor; esto solo mueve la linea vertical, el punto y el
 * tooltip. El tooltip nunca es la unica via para leer una cifra: debajo hay una
 * tabla con los 366 valores, y el propio grafico marca los extremos.
 *
 * Se sigue el puntero por el eje X buscando el punto mas cercano, en vez de
 * exigir acertar encima de la linea: con un ano de datos la linea tiene dos
 * pixeles de ancho y en un movil seria imposible.
 *
 * El punto y el tooltip se colocan en PORCENTAJE sobre el contenedor, no en
 * coordenadas del SVG: el SVG se estira sin conservar proporciones y cualquier
 * cosa dibujada dentro saldria deformada.
 */
(function () {
  const plot = document.querySelector(".plot");
  const datos = document.getElementById("puntos-grafico");
  if (!plot || !datos) return;

  const puntos = JSON.parse(datos.textContent);
  if (!puntos.length) return;

  const tooltip = plot.querySelector(".tooltip");
  const marcador = plot.querySelector(".marker");
  const crosshair = plot.querySelector(".crosshair");
  const svg = plot.querySelector("svg.chart");
  const alto = svg.viewBox.baseVal.height;

  const euros = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 });

  function mover(evento) {
    const caja = svg.getBoundingClientRect();
    const clienteX = evento.touches ? evento.touches[0].clientX : evento.clientX;
    const fraccion = (clienteX - caja.left) / caja.width;

    let indice = Math.round(fraccion * (puntos.length - 1));
    indice = Math.max(0, Math.min(puntos.length - 1, indice));
    const p = puntos[indice];

    plot.classList.add("activo");
    crosshair.setAttribute("x1", p.x);
    crosshair.setAttribute("x2", p.x);
    crosshair.setAttribute("y1", 0);
    crosshair.setAttribute("y2", alto);

    marcador.style.left = p.left + "%";
    marcador.style.top = p.top + "%";

    tooltip.innerHTML =
      '<span class="fecha">' + p.fecha + "</span>" + euros.format(p.valor) + " €";
    tooltip.classList.add("visible");

    // El tooltip se mantiene dentro del recuadro en vez de salirse por los lados.
    const ancho = tooltip.offsetWidth;
    const x = (p.left / 100) * caja.width;
    tooltip.style.left =
      Math.min(Math.max(x - ancho / 2, 0), caja.width - ancho) + "px";
  }

  function salir() {
    plot.classList.remove("activo");
    tooltip.classList.remove("visible");
  }

  plot.addEventListener("pointermove", mover);
  plot.addEventListener("pointerdown", mover);
  plot.addEventListener("pointerleave", salir);
})();
