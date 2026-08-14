"""Tests del once de la jornada.

Lo que se comprueba aqui no es que el modelo acierte -eso lo mide `evaluate`-
sino que la eleccion respeta las reglas del juego: once jugadores, una formacion
legal, y nadie alineado que no vaya a jugar.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import count

import pytest

from fantasyhelper import lineup, market, queries, xpts
from fantasyhelper.bonuses import BonusRules
from fantasyhelper.storage import repository as repo

REGLAS = BonusRules(per_point=100_000)
MODELO = market.MomentumModel(bands={}, dates=0)

_ids = count(1)


def jugador(nombre: str, posicion: str, puntos: float | None, **extra) -> dict:
    """Una fila de jugador con lo justo para alinear.

    `xpts` y `puntos_jornada` van con el mismo valor para que la fila sirva
    tanto a `best_xi` -que lee la segunda- como a `for_matchday`, que recalcula
    la segunda a partir de la primera.
    """
    fila = {
        "id": next(_ids),
        "name": nombre,
        "position": posicion,
        "xpts": puntos,
        "puntos_jornada": puntos,
        "matchday": 1,
        "market_value": 1_000_000,
        "probability": 0.9,
        "status": "ok",
    }
    fila.update(extra)
    return fila


def plantilla(defensas=4, medios=4, delanteros=3, porteros=1) -> list[dict]:
    """Una plantilla generica en la que todos valen lo mismo."""
    cuantos = {"PT": porteros, "DF": defensas, "MC": medios, "DL": delanteros}
    return [
        jugador(f"{posicion}{i}", posicion, 3.0)
        for posicion, total in cuantos.items()
        for i in range(total)
    ]


# --- la formacion -----------------------------------------------------------


def test_el_once_son_once_y_la_formacion_es_legal():
    once = lineup.best_xi(plantilla(5, 5, 4))
    assert len(once.titulares) == 11
    defensas, medios, delanteros = (int(n) for n in once.formacion.split("-"))
    assert (defensas, medios, delanteros) in lineup.FORMATIONS
    assert sum(1 for f in once.titulares if f["position"] == "PT") == 1


def test_la_formacion_la_decide_donde_estan_los_puntos():
    """Con tres delanteros buenos y medios flojos hay que jugar con tres arriba."""
    filas = [jugador("Portero", "PT", 2.0)]
    filas += [jugador(f"DF{i}", "DF", 2.0) for i in range(5)]
    filas += [jugador(f"MC{i}", "MC", 1.0) for i in range(5)]
    filas += [jugador(f"DL{i}", "DL", 8.0) for i in range(3)]

    # 4-3-3 suma 37; 5-3-2 se queda en 31 porque deja fuera a un ocho para
    # meter a un dos.
    assert lineup.best_xi(filas).formacion == "4-3-3"


def test_no_se_inventa_una_formacion_que_mister_no_admite():
    """Con seis defensas buenisimos siguen sin caber mas de cinco."""
    filas = [jugador("Portero", "PT", 2.0)]
    filas += [jugador(f"DF{i}", "DF", 9.0) for i in range(6)]
    filas += [jugador(f"MC{i}", "MC", 1.0) for i in range(5)]
    filas += [jugador(f"DL{i}", "DL", 1.0) for i in range(3)]

    once = lineup.best_xi(filas)
    assert sum(1 for f in once.titulares if f["position"] == "DF") == 5


def test_una_linea_corta_descarta_las_formaciones_que_no_caben():
    """Con tres defensas no se puede jugar con cuatro, por buenos que sean."""
    once = lineup.best_xi(plantilla(defensas=3, medios=5, delanteros=3))
    assert once.formacion.startswith("3-")
    assert len(once.titulares) == 11


def test_sin_portero_no_hay_once_y_se_dice_que_falta():
    """'No se puede alinear' no sirve; 'te falta un portero' se arregla hoy."""
    once = lineup.best_xi(plantilla(porteros=0))
    assert not once.completo
    assert once.faltan == {"PT": 1}
    assert "1 portero" in once.avisos[0]
    # Los que hay no se pierden: van todos al banquillo.
    assert len(once.suplentes) == 11


def test_los_que_no_juegan_van_al_banquillo():
    filas = plantilla(5, 5, 3)
    once = lineup.best_xi(filas)
    assert len(once.suplentes) == len(filas) - 11
    ids = {f["id"] for f in once.titulares} | {f["id"] for f in once.suplentes}
    assert len(ids) == len(filas), "nadie puede estar en los dos sitios"


def test_el_empate_se_rompe_siempre_igual():
    """Dos ejecuciones identicas no pueden dar onces distintos."""
    filas = plantilla(5, 5, 3)
    primero = [f["id"] for f in lineup.best_xi(filas).titulares]
    segundo = [f["id"] for f in lineup.best_xi(list(reversed(filas))).titulares]
    assert primero == segundo


def test_a_igualdad_de_puntos_juega_el_mas_caro():
    """El valor de mercado es el juicio de miles de personas; el alfabeto no."""
    filas = plantilla(4, 4, 2)
    filas.append(jugador("Caro", "DL", 3.0, market_value=9_000_000))
    filas.append(jugador("Barato", "DL", 3.0, market_value=200_000))

    nombres = [f["name"] for f in lineup.best_xi(filas).titulares]
    assert "Caro" in nombres
    assert "Barato" not in nombres


# --- la jornada, que no es la misma para todos ------------------------------


def test_el_que_descansa_la_jornada_no_puntua():
    """Seis equipos no juegan la J1 por el Mundial y sus xPts son de la J2."""
    filas = [
        {"id": 1, "name": "Descansa", "position": "DL", "xpts": 9.0, "matchday": 2},
        {"id": 2, "name": "Juega", "position": "DL", "xpts": 3.0, "matchday": 1},
    ]
    lineup.for_matchday(filas, 1)

    assert filas[0]["puntos_jornada"] == 0
    assert filas[0]["motivo"] == "no juega la J1"
    assert filas[1]["puntos_jornada"] == 3.0
    assert filas[1]["motivo"] is None


def test_el_que_descansa_no_entra_en_el_once_por_bueno_que_sea():
    filas = plantilla(4, 4, 2)
    crack = jugador("Crack", "DL", None, xpts=20.0, matchday=2)
    filas.append(crack)
    lineup.for_matchday(filas, 1)

    once = lineup.best_xi(filas)
    assert "Crack" not in [f["name"] for f in once.titulares]


def test_sin_saber_la_jornada_no_se_descarta_a_nadie():
    """Antes de tener calendario, callarse es mejor que sentar a media plantilla."""
    filas = [{"id": 1, "name": "Uno", "position": "DL", "xpts": 4.0, "matchday": 7}]
    lineup.for_matchday(filas, None)
    assert filas[0]["puntos_jornada"] == 4.0


# --- los que no tienen historico --------------------------------------------


def test_la_referencia_de_cada_puesto_es_la_mediana():
    referencia = lineup.position_baseline({
        1: {"position": "DL", "xpts_si_juega": 1.0},
        2: {"position": "DL", "xpts_si_juega": 4.0},
        3: {"position": "DL", "xpts_si_juega": 40.0},
        4: {"position": "PT", "xpts_si_juega": 2.0},
    })
    assert referencia["DL"] == 4.0, "la mediana, que no se la lleva el crack"
    assert referencia["PT"] == 2.0


def test_un_desconocido_vale_lo_que_uno_cualquiera_de_su_puesto():
    filas = [{"id": 1, "name": "Debut", "position": "DL", "xpts": None,
              "probability": 0.5, "status": "ok"}]
    lineup.for_matchday(filas, 1, baseline={"DL": 4.0})

    assert filas[0]["puntos_jornada"] == pytest.approx(2.0)
    assert filas[0]["motivo"] == "sin datos"


def test_un_lesionado_no_le_gana_el_sitio_a_un_desconocido():
    """De uno sabemos que no juega; del otro no sabemos nada, que es distinto."""
    filas = [
        {"id": 1, "name": "Lesionado", "position": "DL", "xpts": 0.0,
         "probability": 0.0, "status": "lesionado"},
        {"id": 2, "name": "Debut", "position": "DL", "xpts": None,
         "probability": 0.6, "status": "ok"},
    ]
    lineup.for_matchday(filas, 1, baseline={"DL": 4.0})

    assert filas[0]["puntos_jornada"] == 0
    assert filas[0]["motivo"] == "lesionado"
    assert filas[1]["puntos_jornada"] > 0


# --- los avisos -------------------------------------------------------------


def test_se_avisa_de_quien_entra_sin_puntuar():
    """Con once justos hay que alinear al sancionado, pero hay que decirlo."""
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Sancionado", "DL", 0.0, status="sancionado"))

    once = lineup.best_xi(filas)
    assert "Sancionado" in [f["name"] for f in once.titulares], "no hay recambio"
    assert any("no puntúa" in aviso for aviso in once.avisos)


def test_se_avisa_de_los_titulares_dudosos():
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Duda", "DL", 1.0, probability=0.3))

    once = lineup.best_xi(filas)
    assert any("Duda (30%)" in aviso for aviso in once.avisos)


def test_sin_probabilidad_no_es_cero_por_ciento():
    """No hay dato de alineacion probable, que no es lo mismo que un 0%."""
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Sin once", "DL", 1.0, probability=None))

    avisos = " ".join(lineup.best_xi(filas).avisos)
    assert "Sin once" in avisos
    assert "Sin once (0%)" not in avisos, "nadie ha medido ese cero"
    assert "alineación probable" in avisos
    assert "20% de oficio" in avisos


def test_se_avisa_del_titular_sin_historial():
    """El aviso llego a quedarse mudo: su motivo ES 'sin datos' y se filtraba."""
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Debut", "DL", None, probability=0.9))
    lineup.for_matchday(filas, 1, baseline={"DL": 4.0, "DF": 3.0, "MC": 3.0,
                                            "PT": 3.0})

    avisos = " ".join(lineup.best_xi(filas).avisos)
    assert "media de su puesto" in avisos
    assert "Debut" in avisos


def test_no_se_avisa_del_historial_de_quien_no_va_a_jugar():
    """Que a un lesionado le falte historico no le importa a nadie."""
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Roto", "DL", 0.0, status="lesionado", sin_datos=True,
                         motivo="lesionado"))

    avisos = " ".join(lineup.best_xi(filas).avisos)
    assert "media de su puesto" not in avisos


@pytest.mark.parametrize(
    ("mister", "nuestra"),
    [("1-3-5-2", "3-5-2"), ("1-4-4-2", "4-4-2"), ("4-4-2", "4-4-2"),
     (None, None), ("", None)],
)
def test_la_formacion_de_mister_cuenta_al_portero(mister, nuestra):
    """'1-3-5-2' y '3-5-2' son la misma; sin esto la web pedia cambiarla siempre."""
    assert lineup.normalize_formation(mister) == nuestra


# --- que fichaje mejora el once ---------------------------------------------


def _mejoras(filas, candidatos, **kwargs):
    once = lineup.best_xi(filas)
    return lineup.improvements(
        filas, once, candidatos, kind="mercado", cost_field="asking_price", **kwargs
    )


def test_solo_sale_el_fichaje_que_de_verdad_entra_en_el_once():
    """Un cuarto delantero peor que los tres que tienes no mejora nada."""
    filas = plantilla(4, 4, 3)
    peor = jugador("Peor", "DL", 0.5, asking_price=1_000_000)
    mejor = jugador("Mejor", "DL", 9.0, asking_price=1_000_000)

    nombres = [m["jugador"]["name"] for m in _mejoras(filas, [peor, mejor])]
    assert nombres == ["Mejor"]


def test_la_mejora_se_mide_en_puntos_que_suma_al_once():
    filas = plantilla(4, 4, 3)  # todos a 3,0
    fichaje = jugador("Fichaje", "DL", 5.0, asking_price=1_000_000)

    mejora = _mejoras(filas, [fichaje])[0]
    assert mejora["gana"] == pytest.approx(2.0), "5,0 en lugar de un 3,0"
    assert [f["name"] for f in mejora["desplaza"]] != []


def test_lo_que_no_cabe_en_el_saldo_no_se_recomienda():
    filas = plantilla(4, 4, 3)
    caro = jugador("Caro", "DL", 9.0, asking_price=20_000_000)
    assert _mejoras(filas, [caro], budget=5_000_000) == []


def test_un_fichaje_puede_merecer_cambiar_de_formacion():
    """La mejora se mide rehaciendo el once, no comparando dentro de una linea."""
    filas = [jugador("Portero", "PT", 2.0)]
    filas += [jugador(f"DF{i}", "DF", 2.0) for i in range(4)]
    filas += [jugador(f"MC{i}", "MC", 3.0) for i in range(4)]
    filas += [jugador(f"DL{i}", "DL", 3.0) for i in range(2)]
    antes = lineup.best_xi(filas).formacion
    assert antes == "4-4-2", "con dos delanteros no hay otra"

    fichaje = jugador("Nueve", "DL", 9.0, asking_price=1_000_000)
    mejora = _mejoras(filas, [fichaje])[0]
    assert mejora["formacion"] != antes
    assert mejora["cambia_formacion"]


def test_el_coste_por_punto_es_por_punto_ANADIDO():
    filas = plantilla(4, 4, 3)
    fichaje = jugador("Fichaje", "DL", 5.0, asking_price=4_000_000)
    mejora = _mejoras(filas, [fichaje])[0]
    assert mejora["coste_por_punto"] == pytest.approx(4_000_000 / 2.0)


def test_no_se_recomienda_fichar_a_quien_ya_es_tuyo():
    filas = plantilla(4, 4, 3)
    suyo = dict(filas[-1], asking_price=1_000_000, puntos_jornada=99.0)
    assert _mejoras(filas, [suyo]) == []


def test_con_la_plantilla_corta_vale_el_que_tapa_el_hueco():
    """Justo cuando mas falta hace, esta lista salia vacia: sin once no hay
    puntos que sumar y todos los candidatos ganaban cero."""
    filas = plantilla(4, 4, 0)  # diez: falta un delantero
    once = lineup.best_xi(filas)
    assert not once.completo

    delantero = jugador("Nueve", "DL", 1.0, asking_price=1_000_000)
    portero = jugador("Otro portero", "PT", 9.0, asking_price=1_000_000)
    mejoras = lineup.improvements(
        filas, once, [portero, delantero], kind="mercado", cost_field="asking_price"
    )

    assert [m["jugador"]["name"] for m in mejoras] == ["Nueve"]
    assert mejoras[0]["tapa"] == 1
    assert mejoras[0]["coste_por_punto"] is None, "no hay puntos por los que dividir"


def test_el_mismo_jugador_por_dos_vias_sale_una_sola_y_por_lo_barato():
    comun = {"gana": 1.0, "tapa": 0, "formacion": "4-4-2", "cambia_formacion": False,
             "desplaza": [], "coste_por_punto": 0.0}
    unico = lineup._mas_barata([
        {"jugador": {"id": 7}, "tipo": "clausula", "coste": 9_000_000, **comun},
        {"jugador": {"id": 7}, "tipo": "mercado", "coste": 4_000_000, **comun},
    ])
    assert len(unico) == 1
    assert unico[0]["tipo"] == "mercado"


# --- de punta a punta contra la base de datos -------------------------------

INICIO = date(2026, 1, 1)
DIAS = 10


@pytest.fixture
def liga(db):
    """Una liga minima con plantilla propia, calendario y mercado."""
    league_id = repo.upsert_league(
        db, provider="mister", external_id="1", name="Liga", season="2026-27"
    )
    yo = repo.upsert_manager(db, league_id=league_id, external_id="10", name="Yo",
                             is_me=True)
    rival = repo.upsert_manager(db, league_id=league_id, external_id="20", name="Rival")
    local = repo.upsert_team(db, name="Local", provider="mister", external_id="1")
    visitante = repo.upsert_team(db, name="Visitante", provider="mister",
                                 external_id="2")
    # Un tercer equipo que descansa la primera jornada, como los del Mundial.
    mundialista = repo.upsert_team(db, name="Mundialista", provider="mister",
                                   external_id="3")
    cuarto = repo.upsert_team(db, name="Cuarto", provider="mister", external_id="4")
    repo.upsert_fixture(db, season="2026-27", matchday=1,
                        home_team_id=local, away_team_id=visitante)
    repo.upsert_fixture(db, season="2026-27", matchday=2,
                        home_team_id=mundialista, away_team_id=cuarto)

    def fichar(nombre, posicion, *, media=3.0, dueno=None, equipo=None,
               en_mercado=False, precio=None, valor=1_000_000):
        pid = repo.resolve_player(db, provider="mister", external_id=nombre,
                                  name=nombre, team_id=equipo or local,
                                  position=posicion)
        for indice in range(DIAS):
            repo.record_player_value(
                db, provider="mister", source="mister", player_id=pid,
                market_value=valor,
                snapshot_date=(INICIO + timedelta(days=indice)).isoformat(),
            )
        repo.record_season_stat(db, provider="mister", player_id=pid,
                                season="2025-26", points=int(media * 38),
                                avg_points=media, matches_played=38,
                                team_id=equipo or local)
        repo.record_lineup_probability(db, player_id=pid, season="2026-27",
                                       matchday=1, probability=1.0)
        if dueno is not None:
            repo.record_ownership(db, league_id=league_id, player_id=pid,
                                  manager_id=dueno, clause_value=2_000_000,
                                  clause_level=0, clause_floor=1_000_000)
        if en_mercado:
            repo.record_market_listing(db, league_id=league_id, player_id=pid,
                                       seller_id=dueno, asking_price=precio or valor)
        return pid

    for posicion, cuantos in (("PT", 1), ("DF", 4), ("MC", 4), ("DL", 3)):
        for i in range(cuantos):
            fichar(f"{posicion}{i}", posicion, dueno=yo)

    return {"yo": yo, "rival": rival, "fichar": fichar,
            "mundialista": mundialista, "league_id": league_id}


def test_de_punta_a_punta_sale_un_once_completo(db, liga):
    datos = lineup.recommend(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    once = datos["once"]

    assert datos["jornada"] == 1
    assert len(once.titulares) == 11
    assert all(f["puntos_jornada"] > 0 for f in once.titulares)


def test_de_punta_a_punta_no_se_alinea_a_quien_descansa(db, liga):
    """El caso real: el mejor jugador de la plantilla no juega la J1."""
    liga["fichar"]("Estrella", "DL", media=12.0, dueno=liga["yo"],
                   equipo=liga["mundialista"])

    datos = lineup.recommend(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    titulares = {f["name"]: f for f in datos["once"].titulares}
    assert "Estrella" not in titulares

    banquillo = {f["name"]: f for f in datos["once"].suplentes}
    assert banquillo["Estrella"]["motivo"] == "no juega la J1"
    assert banquillo["Estrella"]["xpts"] > 0, "puntuaria, pero no esta jornada"


def test_de_punta_a_punta_propone_el_fichaje_que_mejora_el_once(db, liga):
    liga["fichar"]("Crack", "DL", media=12.0, dueno=liga["rival"], en_mercado=True,
                   precio=1_500_000)
    liga["fichar"]("Malo", "DL", media=0.1, en_mercado=True, precio=1_500_000)

    datos = lineup.recommend(db, manager_id=liga["yo"], budget=2_000_000,
                             rules=REGLAS, model=MODELO)
    nombres = [m["jugador"]["name"] for m in datos["mejoras"]]
    assert nombres[0] == "Crack"
    assert "Malo" not in nombres


def test_lo_propio_puesto_a_la_venta_no_es_un_fichaje(db, liga):
    """Se ven en el mercado, pero ya son tuyos: no mejoran nada."""
    liga["fichar"]("Mio", "DL", media=12.0, dueno=liga["yo"], en_mercado=True)

    datos = lineup.recommend(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    assert "Mio" not in [m["jugador"]["name"] for m in datos["mejoras"]]


def test_la_jornada_en_juego_es_la_primera_sin_jugar(db, liga):
    assert xpts.current_matchday(db) == 1
    db.execute("UPDATE fixture SET status = 'finished' WHERE matchday = 1")
    assert xpts.current_matchday(db) == 2


def test_la_pantalla_del_once_se_pinta_entera(db, liga, monkeypatch, tmp_path):
    """Que el modelo funcione no basta: la plantilla tambien tiene que pintar."""
    pytest.importorskip("fastapi", reason="la web es opcional")
    from fastapi.testclient import TestClient

    from fantasyhelper.storage import db as db_module
    from fantasyhelper.web import app as modulo

    monkeypatch.setattr(
        modulo, "_conn", lambda: db_module.connect(tmp_path / "test.db")
    )
    respuesta = TestClient(modulo.app, raise_server_exceptions=False).get("/once")

    assert respuesta.status_code == 200
    assert "PT0" in respuesta.text, "el portero recomendado tiene que salir"
    assert "A quién poner" in respuesta.text


def test_la_formacion_propia_se_guarda_y_se_lee(db, liga):
    repo.record_manager_formation(db, manager_id=liga["yo"], formation="4-4-2")
    assert queries.my_manager(db)["formation"] == "4-4-2"
    # Un None no borra la que hay: la captura que no la traiga no debe perderla.
    repo.record_manager_formation(db, manager_id=liga["yo"], formation=None)
    assert queries.my_manager(db)["formation"] == "4-4-2"
