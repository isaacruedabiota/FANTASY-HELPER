# FantasyHelper

Asistente de decisiones para juegos fantasy de fútbol. Uso personal, alojado en local.
Empieza por **Mister**, con **FutbolFantasy** como fuente de enriquecimiento, y está
diseñado para conectar más juegos después sin reescribir el análisis.

## La idea central

El histórico de valores de mercado, cláusulas y probabilidades de alineación **no se
puede reconstruir a posteriori**. O se captura cada día, o se pierde. Por eso lo primero
que se ha construido no es el análisis sino el recolector: cuando en octubre quieras
predecir subidas de valor, tendrás dos meses de datos con los que hacerlo.

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 0 | Modelo canónico, captura diaria, FutbolFantasy | **funcionando** |
| 0b | Adapter de Mister (endpoints vía HAR) | esqueleto, pendiente de sesión |
| 1 | Consultas de mercado y plantilla por CLI | pendiente |
| 2 | Puntos esperados + once óptimo + radar de cláusulas + web | pendiente |
| 3 | Modelo de valor de mercado entrenado con el histórico propio | pendiente |
| 4 | Segundo adapter (Biwenger / LaLiga Fantasy) | pendiente |

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

copy .env.example .env     # y rellena tus datos

fh init                    # crea la base de datos
fh capturar --solo futbolfantasy
fh estado
```

## Uso diario

```bash
fh capturar          # captura de hoy (Mister + FutbolFantasy)
fh estado            # cuántos días de histórico llevas y si falta alguno
fh dudas             # jugadores cuyo cruce entre fuentes no es seguro
fh planificador      # deja la captura automática corriendo
```

Para que capture solo, sin tener una terminal abierta, registra `fh planificador` como
tarea programada de Windows. Si el ordenador estuvo apagado a la hora prevista, el job
se ejecuta igualmente al arrancar (hay 6 horas de margen), así no se pierde el día.

## Conectar Mister

Mister no publica API, así que los endpoints se descubren leyendo el tráfico real del
navegador en vez de adivinarlos:

1. Entra en tu liga en <https://mister.mundodeportivo.com>.
2. Abre DevTools (F12) → pestaña **Red**, recarga y navega por Mercado y Clasificación.
3. Click derecho → **Guardar todo como HAR**.
4. `fh mister har fichero.har`

Eso detecta los endpoints, propone a qué corresponde cada uno, y extrae tu ID de liga y
la cookie de sesión. Revisa `data/mister_endpoints.json` (incluye una muestra de cada
respuesta para confirmar cuál es cuál) y copia la cookie a `MISTER_TOKEN` en `.env`.

> Un HAR contiene tus cookies de sesión: equivale a una contraseña. Se procesa en local
> y `data/` está en `.gitignore`, pero no lo compartas.

**No hace falta para empezar.** FutbolFantasy ya publica los valores de mercado de Mister
y su variación diaria, así que el histórico de precios se captura sin sesión. Mister solo
es necesario para lo privado de tu liga: tu plantilla, las de los rivales, cláusulas y saldos.

## Arquitectura

```
Mister ──────┐
Biwenger ────┼──> Adapter ──> MODELO CANÓNICO ──> features ──> decisión ──> UI
LaLiga F. ───┘                (player, team, fixture,
                               ownership, value, points)
FutbolFantasy ──> enriquecimiento (probabilidad, lesiones, dificultad)
```

Dos reglas que sostienen todo lo demás:

1. **Los adapters no saben de análisis y el análisis no sabe de qué juego viene el dato.**
   Cada fuente traduce a las mismas tablas; las tablas `*_alias` hacen de crosswalk entre
   los identificadores de cada fuente. Añadir un fantasy nuevo es escribir un adapter.
2. **El crudo se guarda antes de parsear, siempre.** Si mañana cambia el formato de una
   fuente o se nos ocurre una métrica nueva, se reprocesa el histórico entero desde
   `raw_payload` en vez de haberlo perdido. Se guarda comprimido (~2 MB/día en vez de 21).

### Estructura

```
src/fantasyhelper/
  config.py                 configuración desde .env
  storage/
    schema.sql              esquema canónico comentado
    db.py                   conexión, migraciones, transacciones
    repository.py           escrituras y crosswalk entre fuentes
    raw.py                  almacén crudo comprimido
  adapters/
    base.py                 contrato que cumple cada juego fantasy
    mister/                 cliente, endpoints vía HAR, mapeo a canónico
    futbolfantasy/          scraper de probabilidad, estado y valores
  jobs/
    snapshot.py             captura diaria
    scheduler.py            planificación (03:30 y 19:00)
  cli.py
tests/
  fixtures/                 página real congelada, para detectar cambios en la fuente
```

## Qué se captura hoy

De las 20 páginas de equipo de FutbolFantasy, por jugador y día:

- Probabilidad de ser titular y estado (lesionado, sancionado, duda, ausente).
- Valor de mercado y variación diaria **en 6 juegos fantasy**, Mister incluido.
- Jerarquía en el equipo, rival, dificultad del rival (1-5) y si juega en casa.
- Lanzador de penaltis, faltas y córners.

## Scraping responsable

El `robots.txt` de FutbolFantasy permite el rastreo completo. Aun así: una petición cada
2 segundos, caché de 6 horas, User-Agent identificable y todo el crudo guardado para no
volver a pedir lo mismo dos veces. Con Mister se usa tu propia sesión, 1,2 s entre
peticiones y reintentos con backoff. Además de ser lo correcto, es lo que evita que te
bloqueen a mitad de temporada.

Si algún día esto se publica, cada usuario tendría que usar sus propias credenciales, y
habría que revisar los términos de uso de ambos servicios antes de exponerlo a terceros.

## Tests

```bash
pytest
```

Los tests del scraper corren contra una página real congelada en `tests/fixtures/`. Eso
separa dos fallos que se parecen mucho: si el parser falla contra la web real pero pasa
contra el fixture, es que FutbolFantasy cambió el HTML; si falla contra el fixture, lo
hemos roto nosotros.
