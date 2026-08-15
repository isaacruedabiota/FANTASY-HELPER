"""De la nota de SofaScore a puntos de Mister.

SofaScore puntua cada actuacion con una nota de 6 a 10 y Mister la convierte en
puntos por tramos. La tabla la dicto el usuario y esta transcrita tal cual:

    7,0 - 7,1   ->   5      cada dos decimas, un punto
    7,2 - 7,3   ->   6
    7,4 - 7,5   ->   7
    7,6 - 7,7   ->   8
    7,8 - 7,9   ->   9
    8,0 - 8,5   ->  10      aqui se rompe el paso regular
    8,6 - 9,4   ->  11
    9,5 - 10    ->  12      el maximo

Por arriba los tramos se ensanchan a proposito: un 8,5 y un 8,1 son la misma
actuacion excelente, y sin ese ensanchamiento un partido perfecto valdria
veinte puntos.

LO QUE NO DIJO LA TABLA

Que pasa por debajo de 7,0. Se prolonga el paso regular de dos decimas, que es
lo unico coherente con lo que si esta dicho: 6,8-6,9 son 4, 6,6-6,7 son 3, y asi
hasta que la nota se acaba. Queda anotado porque es una suposicion nuestra y no
un dato: si algun dia se ve un jugador con nota baja y puntos conocidos, se
comprueba y se corrige aqui.

CUIDADO CON LOS DECIMALES

La conversion trabaja en DECIMAS ENTERAS y no con la nota en coma flotante. En
binario, (7.2 - 7.0) / 0.2 vale 0,99999... y al truncar da 5 en vez de 6: el
tramo entero se desplaza. Es el fallo clasico de las tablas por tramos y aqui
seria invisible, porque 5 tambien es un numero razonable.
"""

from __future__ import annotations

#: Nota a partir de la cual empieza la escala regular, en decimas.
BASE_TENTHS = 70
#: Puntos que valen `BASE_TENTHS`.
BASE_POINTS = 5
#: Cuantas decimas hay que subir para ganar un punto.
STEP_TENTHS = 2

#: Los tramos anchos de arriba: (nota minima en decimas, puntos).
TOP_BANDS = ((95, 12), (86, 11), (80, 10))


def rating_to_points(rating: float | None) -> int | None:
    """Puntos de Mister que vale una nota de SofaScore.

    La nota se redondea a una decima antes de convertir, que es como la lee una
    persona y como la publica SofaScore por partido. Las medias de temporada
    vienen con mas precision (7,1875) y no tendria sentido tratar esa cuarta
    cifra como si significase algo.
    """
    if rating is None:
        return None

    decimas = round(float(rating) * 10)
    for minimo, puntos in TOP_BANDS:
        if decimas >= minimo:
            return puntos

    # // redondea hacia abajo tambien con negativos, que es justo lo que hace
    # falta para prolongar la escala por debajo del 7,0.
    return BASE_POINTS + (decimas - BASE_TENTHS) // STEP_TENTHS


def points_to_average(ratings: list[float]) -> float | None:
    """Media de puntos de una lista de notas, convertidas una a una.

    Convertir cada nota y despues promediar NO es lo mismo que promediar las
    notas y convertir una vez, porque la tabla es escalonada. Lo correcto es
    esto: cada partido dio los puntos de su tramo.
    """
    puntos = [p for r in ratings if (p := rating_to_points(r)) is not None]
    return sum(puntos) / len(puntos) if puntos else None
