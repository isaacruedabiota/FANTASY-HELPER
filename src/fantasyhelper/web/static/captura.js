/* El boton de actualizar y el reloj de antiguedad del dato.
 *
 * La captura tarda medio minuto, asi que el boton no espera: pide que empiece,
 * y a partir de ahi pregunta cada pocos segundos como va. Cuando termina,
 * recarga la pagina, que es la forma mas simple y honesta de que TODO lo que se
 * ve pase a ser lo nuevo: hay cuatro listas, varios modelos y una cache, y
 * refrescar solo unos trozos dejaria la pantalla mezclando datos de dos momentos.
 */
(function () {
  const boton = document.getElementById("btn-actualizar");
  const etiqueta = document.getElementById("antiguedad");
  if (!boton || !etiqueta) return;

  const INTERVALO = 3000;
  let sondeo = null;

  function antiguedad(iso) {
    if (!iso) return "sin capturar";
    // Las fechas se guardan en UTC sin sufijo de zona; sin la Z, el navegador
    // las interpretaria como hora local y saldrian dos horas de menos.
    const cuando = new Date(iso.endsWith("Z") ? iso : iso + "Z");
    const minutos = Math.round((Date.now() - cuando) / 60000);
    if (isNaN(minutos)) return "";
    if (minutos < 2) return "ahora mismo";
    if (minutos < 60) return "hace " + minutos + " min";
    const horas = Math.round(minutos / 60);
    if (horas < 24) return "hace " + horas + " h";
    return "hace " + Math.round(horas / 24) + " d";
  }

  function pinta(texto, ocupado) {
    etiqueta.textContent = texto;
    boton.disabled = !!ocupado;
    boton.textContent = ocupado ? "Actualizando…" : "Actualizar";
    etiqueta.classList.toggle("trabajando", !!ocupado);
  }

  function refrescaEtiqueta() {
    pinta(antiguedad(etiqueta.dataset.captura), false);
  }

  async function consulta() {
    let estado;
    try {
      estado = await (await fetch("/api/capturar")).json();
    } catch (e) {
      return; // sin red; se reintenta en el siguiente ciclo
    }

    if (estado.corriendo) {
      pinta("capturando… " + Math.round(estado.segundos) + "s", true);
      return;
    }

    clearInterval(sondeo);
    sondeo = null;
    if (estado.error) {
      pinta("ha fallado: " + estado.error, false);
      return;
    }
    // Recargar en vez de repintar: ver arriba.
    location.reload();
  }

  boton.addEventListener("click", async function () {
    pinta("arrancando…", true);
    let respuesta;
    try {
      respuesta = await (await fetch("/api/capturar", { method: "POST" })).json();
    } catch (e) {
      pinta("no se ha podido arrancar", false);
      return;
    }
    if (!respuesta.arrancada && !respuesta.corriendo) {
      pinta(respuesta.motivo, false);
      setTimeout(refrescaEtiqueta, 4000);
      return;
    }
    if (!sondeo) sondeo = setInterval(consulta, INTERVALO);
  });

  refrescaEtiqueta();

  // Si al cargar ya habia una captura en marcha -lanzada desde otra pestana o
  // desde el movil-, la pagina se engancha a ella en vez de ignorarla.
  const inicial = JSON.parse(
    document.getElementById("estado-captura").textContent || "{}"
  );
  if (inicial.corriendo) {
    pinta("capturando…", true);
    sondeo = setInterval(consulta, INTERVALO);
  }

  // El "hace X" se queda obsoleto si la pestana pasa la noche abierta.
  setInterval(function () {
    if (!sondeo) refrescaEtiqueta();
  }, 60000);
})();
