# Despliegue en Raspberry Pi

La captura diaria no necesita nada especial: es Python, SQLite y unas pocas peticiones
HTTP al día. Cualquier Pi con Raspberry Pi OS vale, y al estar siempre encendida no se
pierde ningún día — que es justo lo que importa, porque cláusulas, propiedad y
probabilidades de once no se pueden recuperar hacia atrás.

## Instalación

```bash
sudo apt update && sudo apt install -y python3-venv python3-dev git

git clone <tu-repo> ~/fantasyhelper
cd ~/fantasyhelper
python3 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env      # y rellena MISTER_TOKEN y MISTER_XAUTH
.venv/bin/fh init
```

Raspberry Pi OS Bookworm trae Python 3.11, que es justo el mínimo del proyecto.

> `lxml` puede tardar varios minutos en compilar en una Pi antigua. Si se atasca:
> `sudo apt install -y python3-lxml` y luego `pip install -e . --no-build-isolation`.

## La sesión

`fh mister sesion` lee del portapapeles, que en la Pi por SSH no existe. Copia el cURL
desde el navegador de tu ordenador a un fichero y pásaselo:

```bash
fh mister sesion sesion.txt
```

La sesión caduca cada cierto tiempo. Cuando pase, la captura empezará a fallar con un
aviso claro en el log y basta con repetir el paso. Merece la pena mirar `fh estado` de
vez en cuando: si una fuente lleva días sin capturar, ahí se ve.

## Servicio

```bash
sudo cp deploy/fantasyhelper.service /etc/systemd/system/fantasyhelper@.service
sudo systemctl daemon-reload
sudo systemctl enable --now fantasyhelper@$USER
```

La unidad es *templada* (`@`), así que toma tu usuario y espera el proyecto en
`/home/<usuario>/fantasyhelper`. Si lo pusiste en otro sitio, edita `WorkingDirectory`
y `ReadWritePaths`.

```bash
systemctl status fantasyhelper@$USER      # ¿está vivo?
journalctl -u fantasyhelper@$USER -f      # ver la captura en directo
```

El planificador captura a las 03:30 (cuando Mister ya ha actualizado los valores) y a las
19:00 solo los onces probables, que cambian por la tarde con las ruedas de prensa. Si la
Pi estuvo apagada a esa hora, el trabajo se ejecuta igualmente al arrancar dentro de un
margen de 6 horas, así que no se pierde el día.

### Capturar más a menudo

`FH_CAPTURE_EVERY_MINUTES=30` en `.env` cambia a una captura cada media hora, que
**sustituye** a las dos diarias. Reinicia el planificador después de tocarlo.

Merece la pena saber qué se compra y qué se paga:

|  | Dos diarias | Cada 30 min |
|---|---|---|
| Peticiones/día | ~65 | ~2.100 |
| Antigüedad del dato | hasta 10 h | hasta 30 min |
| Puntos de histórico/día | 1 | **1** |

La última fila es la que sorprende. Los snapshots son una fila por día y entidad —así
está la clave única en `schema.sql`—, de modo que capturar 48 veces reescribe 48 veces
las mismas filas. Se gana frescura, no resolución: para tener el valor de un jugador hora
a hora hay que cambiar el esquema, no el intervalo.

Y esas peticiones van con tu sesión de Mister. No se sabe dónde tienen el límite.

## Al actualizar el código: `fh init` y reinicia los DOS servicios

```bash
.venv/bin/fh init                          # aplica columnas nuevas, si las hay
sudo systemctl restart fantasyhelper@$USER fantasyhelper-web@$USER
```

`fh init` es idempotente y hay que ejecutarlo siempre: las columnas nuevas se añaden
ahí y no al conectar. Si el código espera una columna que la base no tiene, no falla
una pantalla — fallan todas, porque `my_manager` la lee en cada petición.

**Los dos, siempre.** El planificador es un proceso de días: importa el código al
arrancar y no vuelve a mirarlo. Reiniciar solo la web deja la captura nocturna corriendo
con la versión anterior, y el resultado es desconcertante porque la web se ve bien.

Ya pasó: se arregló que un nombre de equipo bueno no se degradara al slug de
FutbolFantasy, se reinició la web, y a las 03:30 la captura volvió a dejar
`RAYO-VALLECANO` en pantalla — con el arreglo ya en el disco desde hacía seis horas.

## El pasado de los fichajes de fuera

Mister solo conoce LaLiga, así que de un jugador llegado de otra liga no publica
ni una temporada. `fh sofascore` va a buscarlo:

```bash
.venv/bin/fh sofascore              # solo los que aún no tienen notas
.venv/bin/fh sofascore --limite 20  # una muestra, para probar
.venv/bin/fh sofascore --rehacer    # repasa también los ya emparejados
```

No va en la captura periódica a propósito: son ~9 peticiones por jugador y las
temporadas cerradas no cambian nunca. Con el catálogo entero tarda cerca de
media hora, así que conviene lanzarlo con `setsid ... &` y mirar el log después.
Se ejecuta cuando entran fichajes nuevos, no a diario.

## Consultar desde tu ordenador

La base de datos es un único fichero. Para mirar los datos sin entrar por SSH:

```bash
scp pi@raspberrypi.local:~/fantasyhelper/data/fantasyhelper.db .
```

O ejecuta las consultas directamente en la Pi (`fh clausulas`, `fh saldos`…): la salida
es texto y se ve igual de bien por SSH.

## Copia de seguridad

Lo único irreemplazable es `data/fantasyhelper.db`. Un volcado semanal a otra máquina:

```bash
sqlite3 ~/fantasyhelper/data/fantasyhelper.db ".backup '/tmp/fh-backup.db'"
```

Usa `.backup` y no `cp`: la base está en modo WAL y copiarla en caliente con `cp` puede
dejarte un fichero inconsistente.
