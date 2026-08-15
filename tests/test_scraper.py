"""Tests del parseo de FutbolFantasy contra una pagina real congelada."""

from __future__ import annotations

from fantasyhelper.adapters.futbolfantasy.scraper import (
    FutbolFantasyScraper,
    normalize_position,
)


def test_parsea_la_plantilla_completa(team_html):
    rows = FutbolFantasyScraper.parse_players(team_html, team_name="Valencia")
    assert len(rows) >= 20, "deberia extraer la plantilla entera"
    assert all(r.external_id.isdigit() for r in rows)
    assert all(r.slug for r in rows)


def test_valor_de_mister_exacto(team_html):
    """Caso verificado a mano contra el HTML: si esto cambia, cambio la fuente."""
    rows = FutbolFantasyScraper.parse_players(team_html)
    tarrega = next(r for r in rows if r.slug == "cesar-tarrega")

    assert tarrega.name == "César Tárrega"
    assert tarrega.probability == 0.80
    assert tarrega.position == "DF"
    assert tarrega.values["mister"] == (1_140_000, 33_000)


def test_todos_tienen_posicion(team_html):
    # Sin posicion no se puede optimizar la alineacion, asi que no puede faltar.
    rows = FutbolFantasyScraper.parse_players(team_html)
    sin_posicion = [r.name for r in rows if r.position is None]
    assert not sin_posicion, f"jugadores sin posicion: {sin_posicion}"


def test_estados_de_lesion_y_sancion(team_html):
    rows = FutbolFantasyScraper.parse_players(team_html)
    estados = {r.status for r in rows}
    assert estados <= {"ok", "duda", "lesionado", "tocado", "de_vuelta",
                       "sancionado", "no_disponible", "ausente"}
    assert "lesionado" in estados, "el fixture incluye lesionados conocidos"


def test_los_tres_niveles_de_lesion_salen_separados(team_html):
    """`data-lesion` es la GRAVEDAD, no un si/no, y esto estuvo al reves.

    La condicion era `lesion > 0`, asi que el rojo -que vale 0 y es el unico que
    de verdad no juega- pasaba por sano, y el verde -que si juega- se descartaba
    como baja. Los tres casos estan verificados a mano contra el bloque "Estado
    fisico de la plantilla" del propio fixture:

        Sergi Canós      gravedad-0  "Rotura de lig. cruzado anterior"
        Alberto Marí     gravedad-1  "Duda para la jornada"
        Rubén Iranzo     gravedad-2  "Disponible para la jornada"
    """
    por_slug = {r.slug: r for r in FutbolFantasyScraper.parse_players(team_html)}

    assert por_slug["sergi-canos"].status == "lesionado"
    assert por_slug["alberto-mari"].status == "tocado"
    assert por_slug["ruben-iranzo"].status == "de_vuelta"


def test_probabilidad_normalizada_entre_0_y_1(team_html):
    rows = FutbolFantasyScraper.parse_players(team_html)
    for row in rows:
        if row.probability is not None:
            assert 0.0 <= row.probability <= 1.0


def test_normalize_position_acepta_variantes():
    # La fuente alterna estos tres nombres para la misma posicion.
    assert normalize_position("Medio") == "MC"
    assert normalize_position("Mediocampista") == "MC"
    assert normalize_position("Centrocampista") == "MC"
    assert normalize_position("Portero") == "PT"
    assert normalize_position("Defensa") == "DF"
    assert normalize_position("Delantero") == "DL"
    assert normalize_position(None) is None
    assert normalize_position("Entrenador") is None
