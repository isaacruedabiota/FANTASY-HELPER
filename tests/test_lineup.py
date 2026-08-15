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
        "team_id": EQUIPO,
        "media_base": puntos,
        "xpts": puntos,
        "puntos_jornada": puntos,
        "matchday": 1,
        "market_value": 1_000_000,
        "probability": 1.0,
        "status": "ok",
    }
    fila.update(extra)
    return fila


#: Equipo por defecto de las filas de prueba, para poder darle un plan.
EQUIPO = 1


def plan(*, factor: float = 1.0, aplazado: bool = False) -> dict:
    """Lo que devuelve `matchday_outlook` para un equipo."""
    return {"opponent": "Rival", "opponent_id": None, "is_home": None,
            "factor": factor, "aplazado": aplazado}


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


def test_el_que_no_tiene_rival_en_la_jornada_no_puntua():
    """Sin rival asignado en la jornada no hay partido, y no hay puntos."""
    filas = [jugador("Sin rival", "DL", 9.0, team_id=7),
             jugador("Juega", "DL", 3.0, team_id=EQUIPO)]
    lineup.for_matchday(filas, 1, outlook={EQUIPO: plan()})

    assert filas[0]["puntos_jornada"] == 0
    assert filas[0]["motivo"] == "no juega la J1"
    assert filas[1]["puntos_jornada"] == 3.0
    assert filas[1]["motivo"] is None


def test_el_aplazado_puntua_igual_pero_mas_tarde():
    """El caso real: Athletic, Betis, Valencia, Madrid y Real Sociedad en la J1.

    Tienen rival asignado y juegan la jornada; lo que no tienen es fecha. Darles
    un cero era tan equivocado como alinear a quien no juega.
    """
    filas = [jugador("Aplazado", "DL", 10.0)]
    lineup.for_matchday(filas, 1, outlook={EQUIPO: plan(aplazado=True)})

    assert filas[0]["puntos_jornada"] == pytest.approx(10.0 * lineup.POSTPONED_DISCOUNT)
    assert filas[0]["puntos_jornada"] > 0, "puntua, no es una baja"
    assert filas[0]["motivo"] == "juega más tarde"
    assert filas[0]["aplazado"]


def test_un_buen_aplazado_sigue_ganandole_el_sitio_a_un_titular_mediocre():
    """El descuento es suave a proposito: sentarlo tambien seria una decision."""
    filas = plantilla(4, 4, 2)
    filas.append(jugador("Crack aplazado", "DL", 9.0, team_id=9))
    lineup.for_matchday(
        filas, 1, outlook={EQUIPO: plan(), 9: plan(aplazado=True)}
    )

    once = lineup.best_xi(filas)
    assert "Crack aplazado" in [f["name"] for f in once.titulares]


def test_sin_rejilla_de_calendario_no_se_descarta_a_nadie():
    """Antes de tener calendario, callarse es mejor que sentar a media plantilla."""
    filas = [jugador("Uno", "DL", 4.0, xpts=4.0)]
    lineup.for_matchday(filas, 1, outlook={})
    assert filas[0]["puntos_jornada"] == 4.0
    assert filas[0]["motivo"] is None


# --- los que no tienen historico --------------------------------------------


def test_la_referencia_de_cada_puesto_es_la_mediana():
    """Sobre la media BASE: `xpts_si_juega` ya lleva el ajuste del rival dentro
    y quien llama vuelve a aplicarlo, asi que se contaria dos veces."""
    referencia = lineup.position_baseline({
        1: {"position": "DL", "media_base": 1.0},
        2: {"position": "DL", "media_base": 4.0},
        3: {"position": "DL", "media_base": 40.0},
        4: {"position": "PT", "media_base": 2.0},
    })
    assert referencia["DL"] == 4.0, "la mediana, que no se la lleva el crack"
    assert referencia["PT"] == 2.0


def test_un_desconocido_vale_lo_que_uno_cualquiera_de_su_puesto():
    filas = [jugador("Debut", "DL", None, media_base=None, probability=0.5)]
    lineup.for_matchday(filas, 1, baseline={"DL": 4.0}, outlook={EQUIPO: plan()})

    assert filas[0]["puntos_jornada"] == pytest.approx(2.0)
    assert filas[0]["motivo"] == "sin datos"


def test_un_lesionado_no_le_gana_el_sitio_a_un_desconocido():
    """De uno sabemos que no juega; del otro no sabemos nada, que es distinto."""
    filas = [
        jugador("Lesionado", "DL", 8.0, probability=0.0, status="lesionado"),
        jugador("Debut", "DL", None, media_base=None, probability=0.6),
    ]
    lineup.for_matchday(filas, 1, baseline={"DL": 4.0}, outlook={EQUIPO: plan()})

    assert filas[0]["puntos_jornada"] == 0
    assert filas[0]["motivo"] == "lesionado"
    assert filas[1]["puntos_jornada"] > 0


# --- los tres niveles de lesion ---------------------------------------------


def test_solo_la_lesion_roja_es_una_baja():
    """La roja no juega; la naranja y la verde si, y tratarlas igual era el fallo."""
    filas = [
        jugador("Rojo", "DL", 5.0, status="lesionado", probability=0.0),
        jugador("Naranja", "DL", 5.0, status="tocado", probability=0.5),
        jugador("Verde", "DL", 5.0, status="de_vuelta", probability=0.8),
    ]
    lineup.for_matchday(filas, 1, outlook={EQUIPO: plan()})

    rojo, naranja, verde = filas
    assert rojo["puntos_jornada"] == 0
    assert naranja["puntos_jornada"] > 0
    assert verde["puntos_jornada"] > naranja["puntos_jornada"]


def test_el_que_vuelve_de_lesion_juega_menos_minutos():
    """Su probabilidad dice si sera titular; esto, cuanto durara en el campo."""
    sano = jugador("Sano", "DL", 5.0, probability=0.8)
    volviendo = jugador("Vuelve", "DL", 5.0, probability=0.8, status="de_vuelta")
    lineup.for_matchday([sano, volviendo], 1, outlook={EQUIPO: plan()})

    assert volviendo["puntos_jornada"] == pytest.approx(
        sano["puntos_jornada"] * xpts.RETURNING_MINUTES
    )


def test_se_avisa_de_quien_vuelve_de_lesion():
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Vuelve", "DL", 3.0, status="de_vuelta"))
    lineup.for_matchday(filas, 1, outlook={EQUIPO: plan()})

    avisos = " ".join(lineup.best_xi(filas).avisos)
    assert "Vuelven de lesión" in avisos
    assert "Vuelve" in avisos


def test_el_rival_que_se_ensena_es_el_de_la_jornada():
    """A un aplazado se le pintaba el rival de su proximo partido con fecha."""
    fila = jugador("Aplazado", "DL", 3.0, opponent="Espanyol", is_home=False)
    lineup.for_matchday([fila], 1, outlook={EQUIPO: plan(aplazado=True)})

    assert fila["rival_jornada"] == "Rival"
    assert fila["opponent"] == "Rival", "el de la jornada, no el del proximo"


def test_se_avisa_del_partido_aplazado():
    filas = plantilla(4, 4, 1)
    filas.append(jugador("Aplazado", "DL", 3.0, team_id=9))
    lineup.for_matchday(
        filas, 1, outlook={EQUIPO: plan(), 9: plan(aplazado=True)}
    )

    avisos = " ".join(lineup.best_xi(filas).avisos)
    assert "aplazado" in avisos
    assert "Puntúan igual" in avisos


# --- el ex-equipo -----------------------------------------------------------


def test_se_marca_a_quien_juega_contra_su_exequipo():
    fila = jugador("Vuelve a casa", "DL", 3.0)
    lineup.for_matchday(
        [fila], 1,
        outlook={EQUIPO: dict(plan(), opponent_id=77, opponent="Su ex")},
        former={fila["id"]: {77: "Su ex"}},
    )
    assert fila["exequipo"]


def test_el_exequipo_no_toca_los_puntos():
    """Es un dato, no un ajuste: no hay ni un partido suyo contra ellos."""
    con = jugador("Con ex", "DL", 4.0)
    sin = jugador("Sin ex", "DL", 4.0)
    lineup.for_matchday(
        [con, sin], 1,
        outlook={EQUIPO: dict(plan(), opponent_id=77, opponent="Su ex")},
        former={con["id"]: {77: "Su ex"}},
    )
    assert con["puntos_jornada"] == sin["puntos_jornada"]
    assert not sin["exequipo"]


def test_otro_rival_no_es_su_exequipo():
    fila = jugador("Uno", "DL", 3.0)
    lineup.for_matchday(
        [fila], 1,
        outlook={EQUIPO: dict(plan(), opponent_id=5, opponent="Otro")},
        former={fila["id"]: {77: "Su ex"}},
    )
    assert not fila["exequipo"]


def test_se_avisa_del_exequipo_sin_prometer_nada():
    filas = plantilla(4, 4, 1)
    crack = jugador("Nostálgico", "DL", 3.0)
    filas.append(crack)
    lineup.for_matchday(
        filas, 1,
        outlook={EQUIPO: dict(plan(), opponent_id=77, opponent="Su ex")},
        former={crack["id"]: {77: "Su ex"}},
    )
    avisos = " ".join(lineup.best_xi(filas).avisos)
    assert "ex-equipo" in avisos
    assert "No mueve el cálculo" in avisos


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
    # Dos equipos con el partido de la J1 aplazado, como los del Mundial: tienen
    # rival asignado en la rejilla pero su proximo partido con fecha es de la J2.
    mundialista = repo.upsert_team(db, name="Mundialista", provider="mister",
                                   external_id="3")
    cuarto = repo.upsert_team(db, name="Cuarto", provider="mister", external_id="4")
    # Y uno que de verdad no juega la jornada: no sale en la rejilla.
    ausente = repo.upsert_team(db, name="Ausente", provider="mister",
                               external_id="5")
    repo.upsert_fixture(db, season="2026-27", matchday=1,
                        home_team_id=local, away_team_id=visitante)
    repo.upsert_fixture(db, season="2026-27", matchday=2,
                        home_team_id=mundialista, away_team_id=cuarto)

    # La rejilla que publica Mister: los cuatro tienen rival en la J1, incluidos
    # los dos cuyo partido no tiene fecha.
    for equipo, contrario, casa in ((local, visitante, 1), (visitante, local, 0),
                                    (mundialista, cuarto, None),
                                    (cuarto, mundialista, None)):
        repo.record_schedule(db, season="2026-27", matchday=1, team_id=equipo,
                             opponent_id=contrario, is_home=casa)

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

    return {"yo": yo, "rival": rival, "fichar": fichar, "local": local,
            "mundialista": mundialista, "ausente": ausente,
            "league_id": league_id}


def test_de_punta_a_punta_sale_un_once_completo(db, liga):
    datos = lineup.recommend(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    once = datos["once"]

    assert datos["jornada"] == 1
    assert len(once.titulares) == 11
    assert all(f["puntos_jornada"] > 0 for f in once.titulares)


def test_de_punta_a_punta_el_aplazado_juega_la_jornada(db, liga):
    """El caso real: su partido de la J1 aun no tiene fecha, pero se juega."""
    liga["fichar"]("Estrella", "DL", media=12.0, dueno=liga["yo"],
                   equipo=liga["mundialista"])

    datos = lineup.recommend(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    titulares = {f["name"]: f for f in datos["once"].titulares}

    assert "Estrella" in titulares, "puntua en la jornada, aunque sea mas tarde"
    assert titulares["Estrella"]["aplazado"]
    assert titulares["Estrella"]["motivo"] == "juega más tarde"
    assert any("aplazado" in aviso for aviso in datos["once"].avisos)


def test_de_punta_a_punta_no_se_alinea_a_quien_no_tiene_partido(db, liga):
    """Sin rival asignado en la rejilla no hay jornada que jugar."""
    liga["fichar"]("Fuera", "DL", media=12.0, dueno=liga["yo"],
                   equipo=liga["ausente"])

    datos = lineup.recommend(db, manager_id=liga["yo"], rules=REGLAS, model=MODELO)
    assert "Fuera" not in [f["name"] for f in datos["once"].titulares]

    banquillo = {f["name"]: f for f in datos["once"].suplentes}
    assert banquillo["Fuera"]["motivo"] == "no juega la J1"
    assert banquillo["Fuera"]["puntos_jornada"] == 0


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


def test_el_sistema_de_puntuacion_se_guarda_y_se_traduce(db, liga):
    repo.record_scoring_system(db, league_id=liga["league_id"], system="mix2")

    codigo, nombre, detalle = queries.scoring_system(db)
    assert codigo == "mix2"
    assert nombre == "Mixto 2"
    assert "SofaScore al 50%" in detalle


def test_un_cambio_de_sistema_se_denuncia(db, liga):
    """Cambiarlo hace que las medias de antes y las de despues no se puedan
    comparar, y el modelo las mezclaria sin enterarse."""
    assert repo.record_scoring_system(
        db, league_id=liga["league_id"], system="mix2") is None
    assert repo.record_scoring_system(
        db, league_id=liga["league_id"], system="mix2") is None, "sin cambio, callado"

    anterior = repo.record_scoring_system(
        db, league_id=liga["league_id"], system="mr")
    assert anterior == "mix2"
    assert queries.scoring_system(db)[0] == "mr"


def test_sin_sistema_conocido_no_se_inventa_uno(db, liga):
    codigo, nombre, _ = queries.scoring_system(db)
    assert codigo is None
    assert nombre == "desconocido"


def test_la_formacion_propia_se_guarda_y_se_lee(db, liga):
    repo.record_manager_formation(db, manager_id=liga["yo"], formation="4-4-2")
    assert queries.my_manager(db)["formation"] == "4-4-2"
    # Un None no borra la que hay: la captura que no la traiga no debe perderla.
    repo.record_manager_formation(db, manager_id=liga["yo"], formation=None)
    assert queries.my_manager(db)["formation"] == "4-4-2"
