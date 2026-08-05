"""Tests del parseo de Mister contra fragmentos reales de la web app."""

from __future__ import annotations

from fantasyhelper.adapters.mister.parsers import (
    parse_decimal,
    parse_money,
    parse_players,
    parse_standings,
)


def test_parse_money_formato_espanol():
    assert parse_money("6.831.000") == 6_831_000
    assert parse_money("€ 24.641.000") == 24_641_000
    assert parse_money(None) is None
    assert parse_money("-") is None


def test_parse_decimal_con_coma():
    assert parse_decimal("0,0") == 0.0
    assert parse_decimal("7,5") == 7.5
    assert parse_decimal("-") is None


def test_plantilla_propia(mister_team_html):
    players = parse_players(mister_team_html)
    assert len(players) == 15, "la plantilla de Mister son 15 jugadores"

    sivera = next(p for p in players if p.slug == "antonio-sivera")
    assert sivera.external_id == "7893"
    assert sivera.name == "A. Sivera"
    assert sivera.position == "PT"
    assert sivera.market_value == 6_831_000
    assert sivera.value_direction == "up"
    assert sivera.owner_id == "10741821", "en mi plantilla todos tienen dueno"


def test_mercado_diario(mister_market_html):
    players = parse_players(mister_market_html)
    assert len(players) == 15

    uche = next(p for p in players if p.slug == "christantus-uche")
    assert uche.external_id == "58383"
    assert uche.position == "DL"
    assert uche.market_value == 8_165_000
    assert uche.owner_id is None, "data-id_owner=0 significa jugador libre"


def test_catalogo_de_busqueda(mister_search_html):
    players = parse_players(mister_search_html)
    assert len(players) == 50, "la busqueda pagina de 50 en 50"

    mbappe = next(p for p in players if p.slug == "kylian-mbappe")
    assert mbappe.external_id == "58954"
    assert mbappe.market_value == 24_641_000
    assert mbappe.value_direction == "down"
    assert mbappe.position == "DL"
    assert mbappe.team_external_id == "15"


def test_el_slug_del_enlace_es_el_nombre_completo(mister_team_html):
    """El slug es lo que permite cruzar con FutbolFantasy.

    Mister muestra "A. Sivera" pero enlaza a players/7893/antonio-sivera. Sin el
    slug, ese jugador no se encontraria nunca con el de la otra fuente.
    """
    players = parse_players(mister_team_html)
    sivera = next(p for p in players if p.external_id == "7893")
    assert sivera.slug == "antonio-sivera"
    assert sivera.name == "A. Sivera"


def test_los_emojis_no_ensucian_el_nombre(mister_team_html):
    players = parse_players(mister_team_html)
    assert any(p.slug == "alejandro-grimaldo" for p in players)
    for player in players:
        assert "💥" not in player.name


def test_todos_los_jugadores_tienen_valor_y_posicion(mister_search_html):
    players = parse_players(mister_search_html)
    assert all(p.market_value for p in players)
    assert all(p.position in {"PT", "DF", "MC", "DL"} for p in players)


def test_clasificacion(mister_standings_html):
    managers = parse_standings(mister_standings_html)
    assert len(managers) >= 2

    primero = managers[0]
    assert primero.position == 1
    assert primero.external_id == "10741727"
    assert primero.name == "La Pabloneta"
    assert primero.squad_size == 15
    assert primero.team_value == 25_054_000


def test_puntos_no_arrastran_el_sufijo_pts(mister_standings_html):
    # ".points" contiene "0 <span>Pts</span>": el sufijo no debe colarse en el numero.
    managers = parse_standings(mister_standings_html)
    for manager in managers:
        assert manager.points is None or manager.points < 100_000


# --- avatares de los participantes ------------------------------------------

#: Copiado tal cual de una respuesta de /standings. Mister pinta SIEMPRE el
#: circulo de color con la inicial y encima, si la hay, la foto con un `onerror`
#: que la esconde; por eso vienen las dos cosas y hay que quedarse con ambas.
STANDINGS_CON_AVATAR = """
<div class="player-row">
  <a class="btn btn-sw-link user" href="users/10741820/gorje44">
    <div class="position">1</div>
    <div class="user-avatar user-avatar--sm" style="background-color: hsl(115 50 50); ">
      <span>G</span>
      <img src="https://cdn-mister.mundodeportivo.com/file/cdn-mister/users/64c8.png"
           onerror="this.style.display='none'" loading="lazy">
    </div>
    <div class="info"><div class="name">Gorje44</div>
      <div class="played">15 jugadores · € 22.572.000</div></div>
    <div class="points">0<span>Pts</span></div>
  </a>
</div>
<div class="player-row">
  <a class="btn btn-sw-link user" href="users/10741821/sinfoto">
    <div class="position">2</div>
    <div class="user-avatar user-avatar--sm" style="background-color: hsl(80 50 50); ">
      <span>S</span>
    </div>
    <div class="info"><div class="name">SinFoto</div></div>
  </a>
</div>
"""


def test_se_lee_la_foto_del_participante():
    con_foto, _ = parse_standings(STANDINGS_CON_AVATAR)

    assert con_foto.avatar_url.endswith("/users/64c8.png")
    assert con_foto.avatar_color == "hsl(115 50 50)"
    assert con_foto.avatar_initials == "G"


def test_quien_no_tiene_foto_conserva_su_circulo_de_color():
    """Sin esto, la mitad de la liga se quedaria sin nada que mostrar."""
    _, sin_foto = parse_standings(STANDINGS_CON_AVATAR)

    assert sin_foto.avatar_url is None
    assert sin_foto.avatar_color == "hsl(80 50 50)"
    assert sin_foto.avatar_initials == "S"
