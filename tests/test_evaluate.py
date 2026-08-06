"""Tests de guardar predicciones y medir el acierto.

Lo que se protege aqui es la honestidad del baremo. Es facil escribir una
evaluacion que salga bien: basta con dejar que el modelo vea lo que tenia que
adivinar, o con meter los ceros de quien no jugo. Los tests fijan que no.
"""

from __future__ import annotations

import json

import pytest

from fantasyhelper import evaluate, market
from fantasyhelper.storage import repository as repo

TEMPORADA = "2026-27"


@pytest.fixture
def liga(db):
    equipo = repo.upsert_team(db, name="Equipo", provider="mister", external_id="1")
    rival = repo.upsert_team(db, name="Rival", provider="mister", external_id="2")
    repo.upsert_fixture(db, season=TEMPORADA, matchday=1,
                        home_team_id=equipo, away_team_id=rival)
    return {"equipo": equipo, "rival": rival}


def _jugador(db, nombre, equipo, *, media=5.0, probabilidad=0.9):
    pid = repo.resolve_player(db, provider="mister", external_id=nombre,
                              name=nombre, team_id=equipo, position="DL")
    repo.record_season_stat(db, provider="mister", player_id=pid,
                            season="2025-26", points=int(media * 38),
                            avg_points=media, matches_played=38, team_id=equipo)
    repo.record_lineup_probability(db, player_id=pid, season=TEMPORADA,
                                   matchday=1, probability=probabilidad)
    return pid


# --- guardar ----------------------------------------------------------------


def test_se_guarda_lo_que_el_modelo_dice_hoy(db, liga):
    _jugador(db, "Uno", liga["equipo"])

    escritas = evaluate.record(db)
    assert escritas[evaluate.MODEL_POINTS] == 1

    fila = db.execute("SELECT * FROM prediction").fetchone()
    assert fila["model"] == "xpts"
    assert fila["matchday"] == 1
    assert fila["predicted"] > 0
    # Las piezas, para poder saber DONDE falla y no solo que falla.
    detalle = json.loads(fila["detail_json"])
    assert {"media_base", "probabilidad", "ajuste_rival", "ajuste_sede"} <= set(detalle)


def test_la_prediccion_guardada_lleva_dentro_la_probabilidad(db, liga):
    """Sin ella se guardaria 'cuanto haria SI juega', que no es comparable.

    El acta trae los puntos que hizo, no los que habria hecho de jugar.
    """
    titular = _jugador(db, "Titular", liga["equipo"], probabilidad=1.0)
    suplente = _jugador(db, "Suplente", liga["equipo"], probabilidad=0.2)

    evaluate.record(db)
    valores = {
        f["player_id"]: f["predicted"]
        for f in db.execute("SELECT player_id, predicted FROM prediction")
    }
    assert valores[titular] > valores[suplente] * 4


def test_la_captura_de_la_tarde_pisa_a_la_de_la_madrugada(db, liga):
    """De un dia interesa su ultima palabra, con los onces ya publicados."""
    _jugador(db, "Uno", liga["equipo"])
    evaluate.record(db)
    evaluate.record(db)

    assert db.execute("SELECT COUNT(*) FROM prediction").fetchone()[0] == 1


def test_sin_proximo_partido_no_se_predice_nada(db):
    """Un jugador cuyo equipo no tiene calendario no tiene jornada que predecir."""
    equipo = repo.upsert_team(db, name="Equipo", provider="mister", external_id="1")
    _jugador(db, "Uno", equipo)

    assert evaluate.record(db)[evaluate.MODEL_POINTS] == 0


# --- medir ------------------------------------------------------------------


def _prediccion(db, pid, *, jornada, valor, dia="2026-08-10"):
    repo.record_prediction(db, model=evaluate.MODEL_POINTS, player_id=pid,
                           season=TEMPORADA, matchday=jornada, predicted=valor,
                           made_on=dia)


def test_el_acierto_compara_prediccion_con_el_acta(db, liga):
    for indice in range(40):
        pid = _jugador(db, f"J{indice}", liga["equipo"])
        # Prediccion y realidad perfectamente alineadas.
        _prediccion(db, pid, jornada=1, valor=indice / 4)
        repo.record_player_points(db, provider="mister", player_id=pid,
                                  season=TEMPORADA, matchday=1, points=indice)

    score = evaluate.points_accuracy(db)[0]
    assert score.casos == 40
    assert score.correlacion == pytest.approx(1.0)
    assert score.fiable


def test_quien_no_jugo_no_entra_en_el_baremo(db, liga):
    """Meterlo como un cero mediria otra cosa: si acertamos QUIEN juega.

    Y ademas inflaria la correlacion, porque acertar los ceros es facil.
    """
    jugo = _jugador(db, "Jugo", liga["equipo"])
    no_jugo = _jugador(db, "No jugo", liga["equipo"])
    _prediccion(db, jugo, jornada=1, valor=5.0)
    _prediccion(db, no_jugo, jornada=1, valor=4.0)
    repo.record_player_points(db, provider="mister", player_id=jugo,
                              season=TEMPORADA, matchday=1, points=6)

    assert evaluate.points_accuracy(db)[0].casos == 1


def test_manda_la_ultima_prediccion_de_cada_jornada(db, liga):
    """La del dia del partido, con las alineaciones probables ya publicadas.

    Es la que se habria mirado al decidir; las anteriores se guardan para ver
    cuanto mejora segun se acerca, pero no son las que se juzgan.
    """
    pid = _jugador(db, "Uno", liga["equipo"])
    _prediccion(db, pid, jornada=1, valor=1.0, dia="2026-08-10")
    _prediccion(db, pid, jornada=1, valor=9.0, dia="2026-08-14")
    repo.record_player_points(db, provider="mister", player_id=pid,
                              season=TEMPORADA, matchday=1, points=9)

    score = evaluate.points_accuracy(db)[0]
    assert score.casos == 1
    assert score.media_predicha == pytest.approx(9.0)


def test_las_jornadas_se_miden_por_separado_y_en_total(db, liga):
    for jornada in (1, 2):
        for indice in range(5):
            pid = _jugador(db, f"J{jornada}-{indice}", liga["equipo"])
            _prediccion(db, pid, jornada=jornada, valor=indice)
            repo.record_player_points(db, provider="mister", player_id=pid,
                                      season=TEMPORADA, matchday=jornada,
                                      points=indice)

    etiquetas = [s.etiqueta for s in evaluate.points_accuracy(db)]
    assert etiquetas == ["J1", "J2", "total"]


def test_pocos_casos_se_marcan_como_no_fiables(db, liga):
    """Con veinte jugadores la correlacion salta de 0,2 a 0,7 segun quien caiga."""
    pid = _jugador(db, "Uno", liga["equipo"])
    _prediccion(db, pid, jornada=1, valor=5.0)
    repo.record_player_points(db, provider="mister", player_id=pid,
                              season=TEMPORADA, matchday=1, points=6)

    assert not evaluate.points_accuracy(db)[0].fiable


# --- el modelo de valor -----------------------------------------------------


def test_el_valor_se_juzga_contra_su_tramo_y_no_contra_cero(db, liga):
    """El modelo no dice 'va a subir un 2%', dice 'un 2% MAS que los de su precio'.

    Si sube el tramo entero eso no es merito suyo, y si baja no es culpa suya.
    Aqui todos suben un 10% y ninguno se aparta: el acierto tiene que ser nulo,
    no perfecto.
    """
    for indice in range(market.MIN_PLAYERS_PER_BAND + 5):
        pid = _jugador(db, f"J{indice}", liga["equipo"])
        for fecha, valor in (("2026-08-01", 5_000_000), ("2026-08-08", 5_500_000)):
            repo.record_player_value(db, provider="mister", source="mister",
                                     player_id=pid, market_value=valor,
                                     snapshot_date=fecha)
        repo.record_prediction(
            db, model=evaluate.MODEL_VALUE, player_id=pid, season=TEMPORADA,
            predicted=0.0, made_on="2026-08-01", horizon_date="2026-08-08",
            detail={"tramo": "3-8M"},
        )

    score = evaluate.value_accuracy(db)[0]
    assert score.casos == market.MIN_PLAYERS_PER_BAND + 5
    # Todos se movieron igual, asi que su ventaja real sobre el tramo es cero.
    assert score.media_real == pytest.approx(0.0, abs=1e-9)


def test_un_tramo_con_pocos_jugadores_no_puntua(db, liga):
    """Con cuatro jugadores la mediana del tramo es uno de ellos."""
    for indice in range(3):
        pid = _jugador(db, f"J{indice}", liga["equipo"])
        for fecha, valor in (("2026-08-01", 5_000_000), ("2026-08-08", 5_500_000)):
            repo.record_player_value(db, provider="mister", source="mister",
                                     player_id=pid, market_value=valor,
                                     snapshot_date=fecha)
        repo.record_prediction(
            db, model=evaluate.MODEL_VALUE, player_id=pid, season=TEMPORADA,
            predicted=0.02, made_on="2026-08-01", horizon_date="2026-08-08",
            detail={"tramo": "3-8M"},
        )

    assert evaluate.value_accuracy(db) == []


def test_la_cobertura_dice_si_esto_esta_corriendo(db, liga):
    _jugador(db, "Uno", liga["equipo"])
    evaluate.record(db)

    cobertura = {f["model"]: f for f in evaluate.coverage(db)}
    assert cobertura["xpts"]["filas"] == 1
    assert cobertura["xpts"]["dias"] == 1
