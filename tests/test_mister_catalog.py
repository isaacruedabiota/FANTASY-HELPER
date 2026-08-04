"""Tests del catalogo paginado y de la configuracion del usuario."""

from __future__ import annotations

import pytest

from fantasyhelper.adapters.mister.adapter import _search_form
from fantasyhelper.adapters.mister.parsers import (
    parse_json_player,
    parse_player_search,
    parse_user_config,
)

#: Forma real de un jugador del catalogo. Ojo: aqui 'clause' es un entero,
#: mientras que en una plantilla es un diccionario.
CATALOG_ENTRY = {
    "id": 58954,
    "name": "Kylian Mbappé",
    "position": 4,
    "id_team": 15,
    "value": 24_633_000,
    "prev_value": 24_633_000,
    "id_uc": None,
    "uc_name": None,
    "clause": 24_633_000,
    "shield": 0,
    "is_mine": 0,
}

PAGE_HTML = """
<html><script>
_FG_cfg = {"pag":"team"};
_FG_user = {"id":1985666,"name":"Isaac","uc_name":"Glok","id_uc":10741821,
 "id_community":1564937,"community":"LA LIGA 26/27","formation":"1-4-4-2",
 "balance":{"current":37853000,"future":37853000,"maxDebt":43407000}};
</script></html>
"""


def test_clause_entero_del_catalogo():
    """En el catalogo 'clause' es un numero pelado, no el diccionario habitual."""
    player = parse_json_player(CATALOG_ENTRY)
    assert player is not None
    assert player.clause_value == 24_633_000
    assert player.position == "DL"
    assert player.slug == "kylian-mbappe"


def test_clause_diccionario_de_plantilla():
    entry = {**CATALOG_ENTRY, "clause": {"value": 5_000_000, "shield": 1}}
    player = parse_json_player(entry)
    assert player.clause_value == 5_000_000


def test_dueno_y_su_nombre():
    entry = {**CATALOG_ENTRY, "id_uc": 10741820, "uc_name": "Gorje44"}
    player = parse_json_player(entry)
    assert player.owner_id == "10741820"
    assert player.owner_name == "Gorje44"


def test_jugador_libre_no_tiene_dueno():
    player = parse_json_player(CATALOG_ENTRY)
    assert player.owner_id is None


def test_parse_pagina_de_catalogo():
    payload = {"status": "ok", "data": {"players": [CATALOG_ENTRY, CATALOG_ENTRY]}}
    assert len(parse_player_search(payload)) == 2
    assert parse_player_search({"data": {}}) == []


def test_formulario_de_busqueda_aplana_los_filtros():
    """jQuery serializa {'filters': {...}} con corchetes; hay que imitarlo."""
    form = _search_form(offset=50, position=1)
    assert form["offset"] == 50
    assert form["filters[position]"] == 1
    # Los topes NO se envian por defecto: un 0 significa 'hasta 0 euros' y deja
    # la respuesta vacia, que es justo el fallo que costo encontrar.
    assert "filters[value_to]" not in form
    assert "filters[clause_to]" not in form


def test_formulario_rechaza_filtros_inventados():
    with pytest.raises(ValueError, match="filtro desconocido"):
        _search_form(offset=0, inventado=1)


def test_configuracion_del_usuario():
    user = parse_user_config(PAGE_HTML)
    assert user is not None
    assert user.external_id == "10741821"
    assert user.name == "Glok"
    assert user.balance == 37_853_000
    assert user.max_debt == 43_407_000
    assert user.league_external_id == "1564937"
    assert user.league_name == "LA LIGA 26/27"


def test_configuracion_ausente_no_revienta():
    assert parse_user_config("<html>sin datos</html>") is None
    assert parse_user_config("_FG_user = {esto no es json};") is None
