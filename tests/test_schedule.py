"""Tests del calendario: la rejilla de rivales y su efecto sobre el modelo.

Lo que se prueba aqui no es un detalle de formato. La ficha de un jugador lleva
el rival de las quince jornadas siguientes, y de ahi sale poder mirar mas alla
del domingo. Pero atribuir ese calendario al equipo equivocado corrompe la
rejilla entera, y eso ya paso una vez.
"""

from __future__ import annotations

import json

import pytest

from fantasyhelper import xpts
from fantasyhelper.adapters.mister.parsers import parse_schedule, real_team_id
from fantasyhelper.storage import repository as repo

CDN = "https://cdn-mister.mundodeportivo.com/file/cdn-common/teams"


def ficha(team_id, rivales, *, nombre="Jugador"):
    """Una ficha de Mister con lo justo: su equipo y el rival de cada jornada."""
    return {
        "data": {
            "player": {"id": 1, "name": nombre,
                       "team": {"id": team_id, "name": f"Equipo {team_id}"}},
            "points": [
                {"id": 1000 + j, "number": j, "rivalLogoUrl": f"{CDN}/{rival}.png?version="}
                for j, rival in enumerate(rivales, start=1)
            ],
        }
    }


# --- el parser --------------------------------------------------------------


def test_se_extrae_el_rival_de_cada_jornada():
    equipo, partidos = parse_schedule(ficha(14, [17, 48, 3]))
    assert equipo == "14"
    assert [(p.matchday, p.opponent_external_id) for p in partidos] == [
        (1, "17"), (2, "48"), (3, "3")
    ]


def test_una_jornada_sin_escudo_no_inventa_rival():
    """Sin escudo no se sabe contra quien juega, y eso no es un rival cualquiera."""
    payload = ficha(14, [17, 48])
    payload["data"]["points"][0]["rivalLogoUrl"] = None
    _, partidos = parse_schedule(payload)
    assert [p.matchday for p in partidos] == [2]


def test_el_equipo_cero_de_mister_no_es_un_equipo():
    """Mister le pone el equipo 0 -que llama 'void'- a quien no tiene club.

    Tomarlo por un equipo real creo un 'mister-team-0' que la reconciliacion
    acabo fundiendo con el Real Madrid, y desde entonces cualquier jugador sin
    club era del Real Madrid.
    """
    assert real_team_id(0) is None
    assert real_team_id("0") is None
    assert real_team_id(14) == "14"


# --- el guardado ------------------------------------------------------------


@pytest.fixture
def liga(db):
    equipos = {
        externo: repo.upsert_team(db, name=nombre, provider="mister",
                                  external_id=str(externo))
        for externo, nombre in ((14, "Rayo"), (17, "Sevilla"), (48, "Alaves"),
                                (13, "Malaga"))
    }
    return equipos


def _adapter():
    from fantasyhelper.adapters.mister.adapter import MisterAdapter

    return MisterAdapter.__new__(MisterAdapter)  # sin cliente HTTP


def test_el_calendario_se_guarda_bajo_el_equipo_de_la_ficha(db, liga):
    adapter = _adapter()
    adapter.provider = "mister"
    jugador = repo.resolve_player(db, provider="mister", external_id="1",
                                  name="Uno", team_id=liga[14])

    adapter._store_schedule(db, ficha(14, [17, 48]), jugador)

    filas = db.execute(
        "SELECT matchday, team_id, opponent_id FROM team_schedule ORDER BY matchday"
    ).fetchall()
    assert [(f["matchday"], f["team_id"], f["opponent_id"]) for f in filas] == [
        (1, liga[14], liga[17]), (2, liga[14], liga[48])
    ]


def test_un_traspasado_no_le_pega_su_calendario_al_equipo_anterior(db, liga):
    """El fallo que corrompio dieciseis jornadas del Alaves.

    Moussa Diarra se fue del Alaves al Malaga y su `team_id` seguia en Alaves.
    Como el calendario se atribuia al equipo del JUGADOR, la ficha del Malaga
    escribia el calendario del Malaga encima del del Alaves, que aparecia
    jugando contra rivales que no eran los suyos.
    """
    adapter = _adapter()
    adapter.provider = "mister"
    # En la base sigue en el Alaves; su ficha ya dice Malaga.
    diarra = repo.resolve_player(db, provider="mister", external_id="9",
                                 name="Diarra", team_id=liga[48])

    adapter._store_schedule(db, ficha(13, [17, 14]), diarra)

    duenos = {f["team_id"] for f in db.execute("SELECT team_id FROM team_schedule")}
    assert duenos == {liga[13]}, "el calendario es del Malaga, no del Alaves"


def test_la_sede_no_se_borra_cuando_llega_solo_el_rival(db, liga):
    """El calendario da el rival pero no donde se juega; el partido si.

    Se capturan por separado, asi que sin cuidado la siguiente lectura del
    calendario borraria la sede que ya se sabia.
    """
    repo.record_schedule(db, season="2026-27", matchday=1, team_id=liga[14],
                         opponent_id=liga[17], is_home=True)
    repo.record_schedule(db, season="2026-27", matchday=1, team_id=liga[14],
                         opponent_id=liga[17])

    fila = db.execute("SELECT is_home FROM team_schedule").fetchone()
    assert fila["is_home"] == 1


# --- el modelo --------------------------------------------------------------


def _calendario(db, equipos, jornadas):
    for jornada, (equipo, rival) in jornadas.items():
        repo.record_schedule(db, season="2026-27", matchday=jornada,
                             team_id=equipos[equipo], opponent_id=equipos[rival])


def test_se_miran_varias_jornadas_y_no_solo_la_siguiente(db, liga):
    _calendario(db, liga, {1: (14, 17), 2: (14, 48), 3: (14, 13), 4: (14, 17)})
    repo.upsert_fixture(db, season="2026-27", matchday=1,
                        home_team_id=liga[14], away_team_id=liga[17])

    proximas = xpts.upcoming(db)[liga[14]]
    assert [f["matchday"] for f in proximas] == [1, 2, 3], "tres, no una ni todas"


def test_quien_descansa_la_primera_jornada_empieza_a_contar_en_la_segunda(db, liga):
    """Los seis equipos con internacionales en semifinales del Mundial.

    Tienen rival asignado en la J1 y aun asi no juegan esa semana. Contarles esa
    jornada les pondria un partido que no van a disputar.
    """
    _calendario(db, liga, {1: (14, 17), 2: (14, 48), 3: (14, 13), 4: (14, 17)})
    # Su proximo partido es el de la J2: la J1 la descansa.
    repo.upsert_fixture(db, season="2026-27", matchday=2,
                        home_team_id=liga[14], away_team_id=liga[48])

    proximas = xpts.upcoming(db)[liga[14]]
    assert [f["matchday"] for f in proximas] == [2, 3, 4]


def test_sin_calendario_no_se_premia_ni_se_penaliza():
    """No saber contra quien juega no es motivo para bajarle la nota a nadie."""
    assert xpts.schedule_factor({1: 5.0, 2: 3.0}, None) == 1.0
    assert xpts.schedule_factor({1: 5.0, 2: 3.0}, []) == 1.0


def test_un_calendario_facil_puntua_mas_que_uno_dificil(db, liga):
    fuerza = {liga[17]: 8.0, liga[48]: 2.0, liga[13]: 2.0, liga[14]: 5.0}
    _calendario(db, liga, {1: (14, 48), 2: (14, 13)})
    facil = xpts.upcoming(db)[liga[14]]

    db.execute("DELETE FROM team_schedule")
    _calendario(db, liga, {1: (14, 17), 2: (14, 17)})
    dificil = xpts.upcoming(db)[liga[14]]

    assert xpts.schedule_factor(fuerza, facil) > 1.0
    assert xpts.schedule_factor(fuerza, dificil) < 1.0


def test_el_calendario_promedia_y_no_multiplica(db, liga):
    """Tres rivales duros no reducen a la mitad lo que rinde POR PARTIDO.

    Multiplicando tres factores de 0,8 saldria 0,51, que diria otra cosa muy
    distinta de lo que esto mide.
    """
    # Con la mediana en 3 y el rival en 9, el ajuste sin tope seria 0,5 y el
    # tope lo deja en 0,75. Hacen falta varios equipos para que la mediana no
    # caiga sobre el propio rival.
    fuerza = {liga[17]: 9.0, liga[14]: 3.0, liga[48]: 3.0, liga[13]: 3.0}
    _calendario(db, liga, {1: (14, 17), 2: (14, 17), 3: (14, 17)})

    factor = xpts.schedule_factor(fuerza, xpts.upcoming(db)[liga[14]])
    assert factor == pytest.approx(xpts.OPPONENT_CLAMP[0]), "el tope, no su cubo"


def test_la_sede_solo_se_aplica_donde_se_conoce(db, liga):
    """Media rejilla no tiene sede, y darla por 'fuera' penalizaria a medio mundo."""
    fuerza = {liga[17]: 5.0, liga[14]: 5.0}
    repo.record_schedule(db, season="2026-27", matchday=1, team_id=liga[14],
                         opponent_id=liga[17])
    sin_sede = xpts.schedule_factor(fuerza, xpts.upcoming(db)[liga[14]])

    repo.record_schedule(db, season="2026-27", matchday=1, team_id=liga[14],
                         opponent_id=liga[17], is_home=False)
    fuera = xpts.schedule_factor(fuerza, xpts.upcoming(db)[liga[14]])

    assert sin_sede == pytest.approx(1.0)
    assert fuera == pytest.approx(xpts.AWAY_PENALTY)


def test_el_calendario_no_se_mete_dentro_de_xpts(db, liga):
    """xPts es la jornada que viene y tiene que seguir siendolo.

    Es lo unico que luego se puede contrastar con los puntos que de verdad haga
    el jugador el domingo; mezclarle tres semanas lo dejaria sin comprobar.
    """
    jugador = repo.resolve_player(db, provider="mister", external_id="1",
                                  name="Uno", team_id=liga[14])
    repo.record_season_stat(db, provider="mister", player_id=jugador,
                            season="2025-26", points=190, avg_points=5.0,
                            matches_played=38, team_id=liga[14])
    _calendario(db, liga, {1: (14, 17), 2: (14, 17), 3: (14, 17)})
    repo.upsert_fixture(db, season="2026-27", matchday=1,
                        home_team_id=liga[14], away_team_id=liga[17])

    prediccion = xpts.expected_points(db)[jugador]
    assert prediccion["xpts_si_juega"] == pytest.approx(
        prediccion["media_base"]
        * prediccion["ajuste_rival"]
        * prediccion["ajuste_sede"]
    )
    assert "ajuste_calendario" in prediccion, "va aparte, pero va"


def test_el_json_de_una_ficha_real_se_parsea(db):
    """Contra la forma exacta que devuelve Mister, no contra una inventada."""
    payload = json.loads(
        '{"data": {"player": {"team": {"id": 14, "name": "Rayo Vallecano"}},'
        ' "points": [{"id": 3968, "number": 1, "rivalLogoUrl":'
        ' "https://cdn-mister.mundodeportivo.com/file/cdn-common/teams/17.png?version="}]}}'
    )
    equipo, partidos = parse_schedule(payload)
    assert equipo == "14"
    assert partidos[0].opponent_external_id == "17"
