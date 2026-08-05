# FantasyHelper

Asistente de decisiones para juegos fantasy de fútbol. Uso personal, alojado en local.
Empieza por **Mister**, con **FutbolFantasy** como fuente de enriquecimiento, y está
diseñado para conectar más juegos después sin reescribir el análisis.

## La idea central

Las cláusulas, la propiedad de cada jugador y las probabilidades de alineación **no se
pueden reconstruir a posteriori**: solo existen si se capturaron el día que ocurrieron.
Por eso lo primero que se construyó no fue el análisis sino el recolector.

Matiz importante que apareció al reversear Mister: el **valor de mercado sí es
recuperable**, porque Mister publica alrededor de un año de valores diarios por jugador
(`fh mister historico`). Eso permite entrenar la predicción de subidas y bajadas desde el
primer día, sin esperar a acumular histórico propio.

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 0 | Modelo canónico, captura diaria, FutbolFantasy | **funcionando** |
| 0b | Adapter de Mister: HTML + API JSON, con cláusulas | **funcionando** |
| 0c | Backfill del histórico de valores | **funcionando** |
| 1 | Consultas por CLI: plantilla, mercado, cláusulas, chollos, ficha | **funcionando** |
| 2a | Puntos esperados (xPts) y euros por punto | **funcionando** |
| 2b | Recomendación diaria: puntos y valor en la misma moneda | **funcionando** |
| 2c | Web de consulta, servida desde la Raspberry | **funcionando** |
| 2d | Once óptimo | pendiente |
| 3 | Modelo de valor de mercado entrenado con el histórico | **funcionando** |
| 4 | Segundo adapter (Biwenger / LaLiga Fantasy) | pendiente |

## El saldo de los rivales

Mister solo publica el saldo propio. El de los demás se reconstruye:

```
saldo = 50.000.000
      − valor de los 15 jugadores con los que arrancó
      − lo gastado en subir cláusulas
      ± compras, ventas y bonificaciones posteriores
```

El primer término se **congela** con `fh baseline` justo tras crear o reiniciar la liga,
antes de que nadie fiche. Congelar y no guardar solo la fecha es deliberado: los snapshots
del día en curso se reescriben en cada captura, así que una referencia a "el día X" iría
cambiando bajo los pies.

El segundo sale de que cada jugador lleva su nivel de cláusula: subir un escalón cuesta el
**20% del suelo**, con multiplicadores `[1,5 · 2 · 2,5 · 3 · 3,5 · 4]`. Se aplica el suelo
de hoy, y como los valores suben, el gasto sale algo sobreestimado (0,6% medido contra el
saldo propio); se afina solo según se acumule histórico del nivel diario.

El tercero se lee de las tarjetas `card-transfer` del feed, que traen origen, destino e
importe. Se guarda **toda** tarjeta con su HTML aunque no se reconozca el tipo: recién
reiniciada una liga solo hay altas y avisos, y los tipos de compra y venta van apareciendo
sobre la marcha. Cuando aparezca uno nuevo se reprocesa el histórico en vez de haberlo
perdido — la misma razón por la que existe `raw_payload`.

## Raspberry Pi

Instalación, servicio de systemd y copias de seguridad: [deploy/README.md](deploy/README.md).

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

### Consultas

```bash
fh hoy                   # qué comprar, vender, clausular y blindar
fh plantilla             # tu plantilla: valor, cláusula, probabilidad, rival
fh plantilla --de Skar   # la de un rival
fh mercado               # el mercado de hoy, por probabilidad de ser titular
fh clausulas             # radar de cláusulas + a quién te conviene blindar
fh clausulas --saldo 3000000
fh saldos                # saldo estimado de cada rival
fh movimientos           # fichajes, ventas y altas de la liga
fh movimientos --tipos   # qué tipos de movimiento se han visto ya
fh chollos               # libres y baratos que además van a jugar
fh xpts                  # puntos esperados y euros por punto esperado
fh xpts --minimo 0.7 --posicion DL
fh valor                 # quién va a subir y quién a bajar de valor
fh valor --mios          # solo los tuyos: a quién conviene vender ya
fh jugador pedri         # ficha con la evolución del valor
fh liga                  # clasificación
```

### Mantenimiento

```bash
fh capturar          # captura de hoy (Mister + FutbolFantasy)
fh estado            # cuántos días de histórico llevas y si falta alguno
fh dudas             # jugadores cuyo cruce entre fuentes no es seguro
fh reconciliar       # unifica equipos y jugadores entre fuentes (ya va en capturar)
fh planificador      # deja la captura automática corriendo
fh web               # la web, en http://pi-isaac.local:8129

fh mister historico  # ficha completa de cada jugador (valores, temporadas, calendario)
fh mister reprocesar # relee las fichas guardadas sin hacer ni una petición
```

## Puntos esperados

```
xPts = P(juega) × media esperada × ajuste de rival × ajuste de sede
```

Con eso, el radar de cláusulas deja de ordenar por una heurística sin unidades y pasa a
ordenar por **euros por punto esperado**, que ya es una respuesta: Mbappé sale a 3,6M el
punto y Sivera a 1,5M.

La media esperada mezcla tres cosas, cada una con el peso que merece: las temporadas
pasadas (las recientes cuentan más), lo que lleve hecho esta temporada (que gana peso
jornada a jornada) y la media de su posición, hacia la que se le arrastra en proporción a
lo poco que sepamos de él. Ese último paso no es un adorno: sin él, un jugador que
disputó un solo partido y sacó 12 puntos aparecía con una media de 12, por delante de
cualquier crack.

Un aviso sobre los partidos jugados: Mister **no** los publica. Su `last_gameweek` es la
última jornada de la temporada, no las que jugó el jugador. Se deducen dividiendo puntos
entre media, que es exacto porque su media es por partido disputado.

Los lesionados y sancionados valen cero, no una probabilidad baja: no es que sea
improbable que jueguen, es que no pueden. Y a quien no tenemos dato de probabilidad se le
supone una baja (0,2) en vez de una media: no saber si juega no es lo mismo que jugar a
medias.

Para que capture solo, sin tener una terminal abierta, registra `fh planificador` como
tarea programada de Windows. Si el ordenador estuvo apagado a la hora prevista, el job
se ejecuta igualmente al arrancar (hay 6 horas de margen), así no se pierde el día.

## El valor de mercado

La revalorización es beneficio limpio, así que es dinero que no depende de puntos ni de
alineaciones. Con un año de valores diarios se puede comprobar si es predecible, y lo es:
**el cambio de los últimos siete días predice el de los siete siguientes**.

Pero solo comparando a cada jugador con los de su mismo rango de precio:

| Tramo | Volatilidad semanal | Correlación |
|---|---|---|
| <1M | 29,9% | +0,23 |
| 1-3M | 19,0% | +0,76 |
| 3-8M | 12,8% | +0,72 |
| 8-15M | 5,4% | +0,72 |
| >15M | 1,4% | +0,68 |

Metiendo a todos en el mismo saco la correlación se cae a +0,33, y no porque la señal sea
débil sino porque las escalas no son comparables: uno de 500k oscila un 30% en una semana
y uno de 24M un 1,4%. Al mezclarlos, la calibración la marcan los baratos y a los caros se
les aplica una vara que no es la suya.

Se vio en la primera tabla que salió: predecía que Mbappé, Pedri, Yamal y Vinicius iban a
caer un 6% cuando apenas se habían movido un 1%. Estaban por debajo de la «media del
mercado», que ese día era +21% porque los baratos estaban disparados en pretemporada.
Comparados con los de su tramo, que iban a −1,1%, no estaban cayendo en absoluto.

El modelo **se recalibra en cada ejecución** sobre el histórico guardado, en vez de llevar
constantes escritas a mano: si Mister cambia su algoritmo no nos vamos a enterar por
ningún otro medio.

Lo más probable es que esto funcione porque Mister reparte cada movimiento entre varios
días en vez de aplicarlo de golpe. Si es así no adivina nada: lee un ajuste que ya está en
marcha y no ha terminado. Sirve para decidir igual, pero no es lo mismo.

## Juntarlo todo: euros por semana

`fh xpts` da puntos y `fh valor` da euros, y no había forma de comparar «2,8 puntos» con
«+640.000 €». El cambio lo pone la propia liga: **paga 75.000 € por punto**. Con eso un
punto *es* una cantidad de dinero y las dos cosas se suman:

```
rendimiento semanal = xPts × euros_por_punto + revalorización esperada
```

Y entonces se ve algo que por separado no se veía: un suplente barato que se está
revalorizando puede generar más dinero a la semana que un titular caro estancado.

`fh hoy` usa esa moneda común para las cuatro decisiones: qué comprar de entre los libres
que caben en tu saldo, a quién sale a cuenta clausular, cuáles de los tuyos rinden menos y
cuáles corren peligro. El aviso de blindaje se ordena por **en cuántas semanas recuperaría
un rival lo que pagase**, que es la cuenta que haría él.

Dos avisos. Solo entra el pago por punto, que es lo único atribuible a un jugador concreto:
el once ideal y la quiniela no dependen de nadie en particular. Y la escala por puesto de
esta liga va al revés, así que **sumar puntos te reduce ese otro ingreso** — pasar de
último a primero cuesta 1,2M de bonificación, que a 75.000 € el punto son dieciséis puntos.

## La web

```bash
pip install -e '.[web]'
fh web                       # escucha en toda la red local, puerto 8129
```

Pensada primero para el móvil, que es donde se mira el mercado de verdad: las listas son
tarjetas y no tablas, porque una tabla de doce columnas no se lee en una pantalla de cinco
pulgadas. **No lleva contraseña**, así que no la expongas a internet tal cual.

Es una capa de presentación y nada más: todo el SQL sigue en `queries.py` y todo el cálculo
en `xpts`, `market` y `advice`. Si una cifra sale distinta en la web y en la CLI, es un
fallo.

Los gráficos son SVG generado en el servidor, sin ninguna librería: la Raspberry no sirve
200 KB de JavaScript por visita y el gráfico se ve aunque el móvil vaya mal de red. Las
etiquetas de los ejes van en HTML **encima** del SVG y no dentro, porque el dibujo se
estira a lo ancho y el texto saldría aplastado — se vio en la primera captura de pantalla.

Hay también un API JSON (`/api/hoy`, `/api/jugadores`, `/api/salud`) pensada para lo que
venga después: un bot que avise por la mañana, o una extensión que meta el dato dentro del
propio Mister.

## Conectar Mister

Mister tiene dos interfaces, ninguna documentada. Las páginas devuelven **fragmentos de
HTML** ante un `POST` con la cabecera `X-Requested-With: XMLHttpRequest`, y los popups
usan un **API JSON interno** en `/ajax/sw/*` que es donde está lo verdaderamente valioso:

| Ruta | Formato | Contenido |
|---|---|---|
| `/search` | HTML | catálogo de jugadores con valor (50 por página) |
| `/team` | HTML | tu plantilla |
| `/market` | HTML | el mercado del día de tu liga |
| `/standings` | HTML | clasificación, puntos y valor de plantilla de cada rival |
| `/ajax/sw/users` | JSON | plantilla completa de un participante **con las cláusulas**, blindajes, fecha de fichaje y el `id_community` de la liga |
| `/ajax/sw/players` | JSON | con `id`: ficha del jugador (cláusula, puntos por temporada, próximo partido y **un año de valores diarios**). Con `offset`: el **catálogo completo**, de 50 en 50 |

Dos trampas del catálogo, por si hay que volver a tocarlo: los filtros se envían aplanados
al estilo de jQuery (`filters[position]=1`), y **no se pueden mandar a cero**: `value_to=0`
se interpreta como "hasta 0 €" y devuelve la lista vacía. Y el campo `clause` llega como
diccionario en las plantillas pero como entero pelado en el catálogo.

La página completa (sin las cabeceras de XHR) incluye `_FG_user`, que es la única vía al
**saldo** — y solo al propio: Mister no publica el de los rivales por ningún sitio.

Ninguna ruta lleva el identificador de liga: lo decide la sesión. Se detecta solo a partir
de `id_community`, que sí publica el API JSON — necesario si juegas más de una liga a la
vez, porque si no se mezclarían los participantes de ambas.

Las rutas ya vienen configuradas. Lo único que falta es la **sesión**:

1. Entra en tu liga en <https://mister.mundodeportivo.com>.
2. DevTools (F12) → pestaña **Red** → recarga (F5).
3. Click derecho sobre una petición a `mister.mundodeportivo.com` (por ejemplo
   `standings` o `team`) → **Copiar** → **Copiar como cURL**.
4. `fh mister sesion`

El comando lee el cURL del portapapeles, extrae la cookie descartando las de analítica,
la escribe en `.env` y **comprueba contra el servidor que funciona** antes de darla por
buena — una cookie mal copiada devuelve un 200 con la pantalla de login, así que guardarla
sin verificar no sirve de nada. Si prefieres pegar el cURL en un fichero:
`fh mister sesion fichero.txt`.

`fh mister har fichero.har` sigue sirviendo para redescubrir rutas si Mister las cambia,
pero ojo: la opción de Chrome *"Guardar todo como HAR"* que sale por defecto **censura las
cookies**, así que para la sesión hay que copiarla a mano como en el paso 2.

> Un HAR y una cookie de sesión equivalen a tu contraseña. Se procesan en local y `data/`
> está en `.gitignore`, pero no los compartas.

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

   El crosswalk es la pieza más delicada, porque cada fuente escribe los nombres a su
   manera (`A. Grimaldo` / `Álex Grimaldo`, `pedri` / `pedri-gonzalez`). Se resuelve en
   dos tiempos: primero por slug, y luego con una fase de reconciliación
   ([reconcile.py](src/fantasyhelper/reconcile.py)) que deduce la equivalencia de equipos
   a partir de los jugadores ya cruzados y reintenta el resto por apellido e inicial
   dentro del equipo. Los enlaces deducidos quedan marcados con confianza < 1 y se
   revisan con `fh dudas`.
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
