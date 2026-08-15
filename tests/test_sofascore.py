"""Tests del adaptador de SofaScore."""

from __future__ import annotations

import pytest

from fantasyhelper.adapters.sofascore.ratings import (
    points_to_average,
    rating_to_points,
)
from fantasyhelper.adapters.sofascore.scraper import (
    Candidate,
    SeasonRating,
    combine,
    normalize_season,
    pick,
)

# --- la tabla, punto por punto ----------------------------------------------


@pytest.mark.parametrize(
    ("nota", "puntos"),
    [
        # El tramo regular, tal y como esta dictado.
        (7.0, 5), (7.1, 5),
        (7.2, 6), (7.3, 6),
        (7.4, 7), (7.5, 7),
        (7.6, 8), (7.7, 8),
        (7.8, 9), (7.9, 9),
        # Y los tramos anchos de arriba.
        (8.0, 10), (8.3, 10), (8.5, 10),
        (8.6, 11), (9.0, 11), (9.4, 11),
        (9.5, 12), (10.0, 12),
    ],
)
def test_la_tabla_dictada(nota, puntos):
    assert rating_to_points(nota) == puntos


@pytest.mark.parametrize(
    ("nota", "puntos"),
    [(6.9, 4), (6.8, 4), (6.7, 3), (6.6, 3), (6.1, 0), (6.0, 0), (5.9, -1)],
)
def test_por_debajo_del_siete_se_prolonga_la_escala(nota, puntos):
    """Suposicion nuestra, no dato: la tabla dictada empieza en 7,0."""
    assert rating_to_points(nota) == puntos


def test_no_se_desplazan_los_tramos_por_los_decimales_binarios():
    """(7.2 - 7.0) / 0.2 vale 0,99999... en binario y truncaria a 5.

    El fallo seria invisible: 5 tambien es un numero razonable para un 7,2.
    """
    assert rating_to_points(7.2) == 6
    assert rating_to_points(7.4) == 7
    assert rating_to_points(7.6) == 8


def test_la_nota_se_redondea_a_una_decima():
    """Las medias de temporada vienen con cuatro cifras y no significan tanto."""
    assert rating_to_points(7.1875) == rating_to_points(7.2) == 6
    assert rating_to_points(7.1499) == 5


def test_sin_nota_no_hay_puntos():
    assert rating_to_points(None) is None
    assert points_to_average([]) is None


def test_se_convierte_partido_a_partido_y_luego_se_promedia():
    """La tabla es escalonada: promediar notas y convertir una vez no es igual.

    Dos partidos de 7,1 y 7,3 son 5 y 6 puntos -media 5,5-. Promediando las
    notas sale 7,2, que convertido daria 6. La diferencia es medio punto por
    jornada, que en una temporada son veinte.
    """
    assert points_to_average([7.1, 7.3]) == pytest.approx(5.5)
    assert rating_to_points((7.1 + 7.3) / 2) == 6


# --- el emparejado, que es lo que puede salir mal en silencio ---------------


ANTONY = [
    Candidate("958380", "Antony", "Real Betis"),
    Candidate("1105971", "Antony Alves", "Portland Timbers"),
    Candidate("1129151", "Antony Papadopoulos", "Dagenham & Redbridge"),
    Candidate("133709", "Antony Silva", None),
]


def test_el_equipo_desempata_entre_homonimos():
    """Buscar 'Antony' devuelve dieciocho personas y solo una juega en el Betis."""
    assert pick(ANTONY, name="Antony", team="Betis").external_id == "958380"


def test_sin_equipo_vale_el_nombre_exacto_si_es_unico():
    assert pick(ANTONY, name="Antony", team=None).external_id == "958380"


def test_ante_la_duda_no_se_empareja():
    """Colgarle a un jugador el historial de otro no se ve y se propaga."""
    gemelos = [
        Candidate("1", "Juan García", "Getafe"),
        Candidate("2", "Juan García", "Getafe"),
    ]
    assert pick(gemelos, name="Juan García", team="Getafe") is None
    assert pick(gemelos, name="Juan García", team=None) is None


def test_el_equipo_acota_pero_el_nombre_confirma():
    """Con el equipo a secas bastaba un unico jugador del Levante, llamarase
    como se llamase. El nombre tiene que encajar tambien."""
    assert pick(
        [Candidate("9", "Otro Nombre", "Levante")], name="Antony", team="Levante"
    ) is None


def test_se_acepta_el_nombre_abreviado_de_mister():
    """Mister escribe 'A. Sivera' y SofaScore 'Antonio Sivera Salva'."""
    candidatos = [Candidate("5", "Antonio Sivera Salva", "Deportivo Alavés")]
    assert pick(candidatos, name="A. Sivera", team="Alavés").external_id == "5"


def test_sin_candidatos_no_hay_nada():
    assert pick([], name="Antony", team="Betis") is None


# --- juntar competiciones ---------------------------------------------------


def test_las_competiciones_se_promedian_por_partidos():
    """Una eliminatoria de tres partidos no puede pesar como una liga de treinta."""
    notas = [
        SeasonRating("2024-25", "Premier League", 7.4, 30),   # 7 puntos
        SeasonRating("2024-25", "Europa League", 8.2, 3),     # 10 puntos
    ]
    media, partidos = combine(notas)["2024-25"]
    assert partidos == 33
    assert media == pytest.approx((7 * 30 + 10 * 3) / 33)
    assert 7 < media < 7.3, "la copa mueve poco, que es lo que tiene que pasar"


def test_cada_temporada_va_por_su_cuenta():
    notas = [
        SeasonRating("2024-25", "Premier League", 7.4, 30),
        SeasonRating("2023-24", "Premier League", 7.0, 30),
    ]
    juntas = combine(notas)
    assert juntas["2024-25"][0] == 7.0
    assert juntas["2023-24"][0] == 5.0


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [("25/26", "2025-26"), ("24/25", "2024-25"), ("2026", None),
     ("", None), (None, None)],
)
def test_la_temporada_se_normaliza_como_en_el_resto(crudo, esperado):
    assert normalize_season(crudo) == esperado
