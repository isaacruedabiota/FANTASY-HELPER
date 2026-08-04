"""Tests del modelo de puntos esperados."""

from __future__ import annotations

import pytest

from fantasyhelper import queries, xpts
from fantasyhelper.storage import repository as repo

# --- media base -------------------------------------------------------------


def test_sin_datos_no_inventa():
    """None y no cero: un debutante no es un jugador que rinda cero."""
    assert xpts.blended_average([]) is None


def test_una_sola_temporada_completa_es_su_media():
    assert xpts.blended_average([("2025-26", 5.0, 38)]) == pytest.approx(5.0)


def test_la_temporada_reciente_pesa_mas_que_la_vieja():
    media = xpts.blended_average([("2025-26", 6.0, 38), ("2024-25", 2.0, 38)])
    assert 4.0 < media < 6.0, "debe caer entre las dos, escorada a la reciente"


def test_una_temporada_corta_pesa_menos_que_una_completa():
    """Una media de cuatro partidos es mucho mas ruidosa que una de treinta y ocho."""
    completa = xpts.blended_average([("2025-26", 8.0, 4), ("2024-25", 2.0, 38)])
    corta = xpts.blended_average([("2025-26", 8.0, 38), ("2024-25", 2.0, 38)])
    assert completa < corta


def test_la_temporada_en_curso_va_ganando_peso():
    """Dos partidos buenos no cambian el juicio; veinte si."""
    historia = [("2025-26", 3.0, 38)]
    pronto = xpts.blended_average(historia, current_points=20, current_matchdays=2)
    tarde = xpts.blended_average(historia, current_points=200, current_matchdays=20)

    assert pronto < tarde
    assert abs(pronto - 3.0) < abs(tarde - 3.0), "al principio manda el historico"
    assert tarde > 7.0, "con veinte jornadas manda lo de este ano"


def test_sin_historico_vale_lo_de_este_ano():
    assert xpts.blended_average(
        [], current_points=30, current_matchdays=5
    ) == pytest.approx(6.0)


# --- ajuste por rival -------------------------------------------------------


def test_un_rival_medio_no_ajusta_nada():
    fuerza = {1: 4.0, 2: 5.0, 3: 6.0}
    assert xpts.opponent_factor(fuerza, 2) == pytest.approx(1.0)


def test_un_rival_fuerte_penaliza_y_uno_flojo_beneficia():
    fuerza = {1: 2.0, 2: 5.0, 3: 9.0}
    assert xpts.opponent_factor(fuerza, 3) < 1.0
    assert xpts.opponent_factor(fuerza, 1) > 1.0


def test_el_ajuste_por_rival_esta_acotado():
    """Es un prior discutible, asi que no se le deja mover el resultado a su antojo.

    El tope de abajo si llega a tocarse -un rival puede ser muchas veces mejor
    que la media-, pero el de arriba no: por muy flojo que sea un equipo su
    fuerza no baja de cero, y eso deja el ajuste en +25% como mucho.
    """
    fuerza = {1: 0.0, 2: 5.0, 3: 500.0}
    minimo, maximo = xpts.OPPONENT_CLAMP

    assert xpts.opponent_factor(fuerza, 3) == pytest.approx(minimo)
    assert xpts.opponent_factor(fuerza, 1) == pytest.approx(maximo)
    for equipo in fuerza:
        assert minimo <= xpts.opponent_factor(fuerza, equipo) <= maximo


def test_un_rival_desconocido_no_ajusta():
    assert xpts.opponent_factor({1: 4.0, 2: 5.0}, None) == 1.0
    assert xpts.opponent_factor({}, 7) == 1.0


# --- probabilidad de jugar --------------------------------------------------


def test_un_lesionado_no_juega():
    """Cero, no una probabilidad baja: no es improbable, es imposible."""
    assert xpts.playing_probability({"status": "lesionado", "probability": 0.9}) == 0.0
    assert xpts.playing_probability({"status": "sancionado", "probability": 0.9}) == 0.0


def test_de_quien_no_se_sabe_nada_se_supone_poco():
    assert xpts.playing_probability({}) == queries.UNKNOWN_PROBABILITY


def test_se_respeta_la_probabilidad_conocida():
    assert xpts.playing_probability({"probability": 0.85, "status": "ok"}) == 0.85


# --- integracion sobre la base de datos -------------------------------------


@pytest.fixture
def liga(db):
    """Dos equipos, un partido entre ellos y jugadores con historico."""
    casa = repo.upsert_team(db, name="Equipo Casa", provider="mister", external_id="1")
    fuera = repo.upsert_team(db, name="Equipo Fuera", provider="mister", external_id="2")
    repo.upsert_fixture(
        db, season="2026-27", matchday=1,
        home_team_id=casa, away_team_id=fuera, kickoff_utc="2026-08-15T17:00:00Z",
    )

    def fichar(equipo, nombre, media, valor, *, temporadas=1):
        pid = repo.resolve_player(
            db, provider="mister", external_id=nombre, name=nombre,
            team_id=equipo, position="DL",
        )
        repo.record_player_value(db, provider="mister", player_id=pid, market_value=valor)
        for indice in range(temporadas):
            repo.record_season_stat(
                db, provider="mister", player_id=pid,
                season=f"{2025 - indice}-{26 - indice}", points=int(media * 38),
                avg_points=media, matches_played=38, team_id=equipo,
            )
        return pid

    return {"casa": casa, "fuera": fuera, "fichar": fichar}


def test_jugar_en_casa_suma_y_fuera_resta(db, liga):
    local = liga["fichar"](liga["casa"], "Local", 5.0, 1_000_000)
    visitante = liga["fichar"](liga["fuera"], "Visitante", 5.0, 1_000_000)

    modelo = xpts.expected_points(db)
    assert modelo[local]["ajuste_sede"] == xpts.HOME_ADVANTAGE
    assert modelo[visitante]["ajuste_sede"] == xpts.AWAY_PENALTY
    assert modelo[local]["xpts_si_juega"] > modelo[visitante]["xpts_si_juega"]


def test_el_rival_y_la_jornada_salen_del_calendario(db, liga):
    local = liga["fichar"](liga["casa"], "Local", 5.0, 1_000_000)

    prediccion = xpts.expected_points(db)[local]
    assert prediccion["opponent"] == "Equipo Fuera"
    assert prediccion["matchday"] == 1
    assert prediccion["is_home"] is True


def test_sin_partido_no_hay_ajustes(db, liga):
    """Un jugador sin equipo o sin calendario conserva su media a secas."""
    huerfano = repo.resolve_player(
        db, provider="mister", external_id="x", name="Sin equipo", position="DL"
    )
    repo.record_season_stat(
        db, provider="mister", player_id=huerfano, season="2025-26",
        points=190, avg_points=5.0, matches_played=38,
    )

    prediccion = xpts.expected_points(db)[huerfano]
    assert prediccion["ajuste_rival"] == 1.0
    assert prediccion["ajuste_sede"] == 1.0
    assert prediccion["xpts_si_juega"] == pytest.approx(5.0)


def test_el_coste_por_punto_compara_precio_con_rendimiento(db, liga):
    """La razon de ser de todo esto: dos jugadores iguales, uno cuesta el doble."""
    liga["fichar"](liga["casa"], "Barato", 5.0, 1_000_000)
    liga["fichar"](liga["casa"], "Caro", 5.0, 2_000_000)

    filas = {
        f["name"]: f
        for f in xpts.attach(db, queries.all_players(db), cost_field="market_value")
    }
    assert filas["Caro"]["xpts"] == pytest.approx(filas["Barato"]["xpts"])
    assert filas["Caro"]["coste_por_punto"] == pytest.approx(
        2 * filas["Barato"]["coste_por_punto"]
    )


def test_un_lesionado_no_suma_puntos_esperados(db, liga):
    jugador = liga["fichar"](liga["casa"], "Tocado", 5.0, 1_000_000)
    repo.record_lineup_probability(
        db, player_id=jugador, season="2026-27", matchday=1,
        probability=0.9, status="lesionado",
    )

    fila = next(
        f for f in xpts.attach(db, queries.all_players(db)) if f["name"] == "Tocado"
    )
    assert fila["xpts"] == 0
    assert fila["coste_por_punto"] is None, "sin puntos esperados no hay precio por punto"


def test_un_debutante_no_estorba_al_ranking(db, liga):
    """Sin historico no hay estimacion, y eso no puede colarse como un cero barato."""
    repo.resolve_player(
        db, provider="mister", external_id="nuevo", name="Debutante",
        team_id=liga["casa"], position="DL",
    )
    pid = repo.resolve_player(
        db, provider="mister", external_id="nuevo", name="Debutante", position="DL"
    )
    repo.record_player_value(db, provider="mister", player_id=pid, market_value=500_000)

    fila = next(
        f for f in xpts.attach(db, queries.all_players(db)) if f["name"] == "Debutante"
    )
    assert fila["xpts"] is None
    assert fila["coste_por_punto"] is None


def test_la_fuerza_de_un_equipo_desconocido_es_la_mediana(db, liga):
    """No tener datos de un recien ascendido no es lo mismo que ser malo."""
    for indice in range(xpts.MIN_PLAYERS_FOR_STRENGTH):
        liga["fichar"](liga["casa"], f"Casa{indice}", 6.0, 1_000_000)
    liga["fichar"](liga["fuera"], "Solitario", 1.0, 1_000_000)

    fuerza = xpts.team_strength(db)
    assert fuerza[liga["casa"]] == pytest.approx(6.0)
    assert fuerza[liga["fuera"]] == pytest.approx(6.0), "sin evidencia, la mediana"
