"""Tests de las consultas de analisis."""

from __future__ import annotations

import pytest

from fantasyhelper import queries
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.db import today


@pytest.fixture
def liga(db):
    """Una liga minima: yo, un rival y jugadores con datos completos."""
    league_id = repo.upsert_league(
        db, provider="mister", external_id="1", name="Liga", season="2026-27"
    )
    yo = repo.upsert_manager(
        db, league_id=league_id, external_id="10", name="Glok", is_me=True
    )
    rival = repo.upsert_manager(db, league_id=league_id, external_id="20", name="Rival")
    equipo = repo.upsert_team(db, name="alaves", provider="mister", external_id="48")

    def jugador(nombre, *, valor, prob, status="ok", jerarquia=50, dueno=None, clausula=None):
        pid = repo.resolve_player(
            db, provider="mister", external_id=nombre, name=nombre,
            team_id=equipo, position="DF",
        )
        repo.record_player_value(
            db, provider="mister", player_id=pid, market_value=valor
        )
        repo.record_lineup_probability(
            db, player_id=pid, season="2026-27", matchday=1,
            probability=prob, status=status,
        )
        repo.record_player_context(
            db, player_id=pid, season="2026-27", matchday=1,
            jerarquia=jerarquia, opponent="GET", opponent_difficulty=3, is_home=True,
        )
        if dueno is not None:
            repo.record_ownership(
                db, league_id=league_id, player_id=pid, manager_id=dueno,
                clause_value=clausula,
            )
        return pid

    return {
        "league_id": league_id,
        "yo": yo,
        "rival": rival,
        "jugador": jugador,
    }


def test_me_identifica(db, liga):
    me = queries.my_manager(db)
    assert me is not None
    assert me["name"] == "Glok"


def test_plantilla_propia(db, liga):
    liga["jugador"]("Mio Uno", valor=1_000_000, prob=0.9, dueno=liga["yo"], clausula=1_500_000)
    liga["jugador"]("De Otro", valor=2_000_000, prob=0.9, dueno=liga["rival"], clausula=3_000_000)

    filas = queries.squad(db, liga["yo"])
    assert [f["name"] for f in filas] == ["Mio Uno"]
    assert filas[0]["clause_value"] == 1_500_000


def test_radar_excluye_los_mios_y_los_no_disponibles(db, liga):
    liga["jugador"]("Mio", valor=1_000_000, prob=0.9, dueno=liga["yo"], clausula=1_500_000)
    liga["jugador"]("Sano", valor=1_000_000, prob=0.9, dueno=liga["rival"], clausula=1_500_000)
    liga["jugador"](
        "Lesionado", valor=1_000_000, prob=0.9, status="lesionado",
        dueno=liga["rival"], clausula=1_000_000,
    )

    objetivos = queries.clause_targets(db, manager_id=liga["yo"])
    assert [o["name"] for o in objetivos] == ["Sano"]


def test_radar_ordena_por_coste_ajustado(db, liga):
    # Misma clausula, distinta probabilidad: gana el que seguro que juega.
    liga["jugador"]("Titular", valor=1_000_000, prob=0.9, dueno=liga["rival"], clausula=2_000_000)
    liga["jugador"]("Suplente", valor=1_000_000, prob=0.6, dueno=liga["rival"], clausula=2_000_000)

    objetivos = queries.clause_targets(db, manager_id=liga["yo"])
    assert [o["name"] for o in objetivos] == ["Titular", "Suplente"]
    assert objetivos[0]["adjusted_cost"] < objetivos[1]["adjusted_cost"]


def test_radar_respeta_el_presupuesto(db, liga):
    liga["jugador"]("Barato", valor=500_000, prob=0.9, dueno=liga["rival"], clausula=750_000)
    liga["jugador"]("Caro", valor=9_000_000, prob=0.9, dueno=liga["rival"], clausula=13_500_000)

    objetivos = queries.clause_targets(db, manager_id=liga["yo"], budget=1_000_000)
    assert [o["name"] for o in objetivos] == ["Barato"]


def test_riesgo_ignora_a_mis_lesionados(db, liga):
    """A un lesionado no te lo van a clausular, por barato que sea."""
    liga["jugador"](
        "Lesionado", valor=500_000, prob=0.9, status="lesionado",
        dueno=liga["yo"], clausula=750_000,
    )
    liga["jugador"]("Sano", valor=500_000, prob=0.9, dueno=liga["yo"], clausula=800_000)

    riesgo = queries.clause_risk(db, liga["yo"])
    assert [r["name"] for r in riesgo] == ["Sano"]


def test_probabilidad_desconocida_no_sube_en_el_ranking(db, liga):
    """Sin dato de probabilidad, el jugador debe quedar por debajo, no en medio."""
    liga["jugador"]("Conocido", valor=1_000_000, prob=0.9, dueno=liga["yo"], clausula=1_500_000)
    pid = repo.resolve_player(
        db, provider="mister", external_id="x", name="Desconocido", position="DF"
    )
    repo.record_player_value(db, provider="mister", player_id=pid, market_value=1_000_000)
    repo.record_ownership(
        db, league_id=liga["league_id"], player_id=pid,
        manager_id=liga["yo"], clause_value=1_500_000,
    )

    riesgo = queries.clause_risk(db, liga["yo"])
    assert [r["name"] for r in riesgo] == ["Conocido", "Desconocido"]


def test_libres_excluyen_a_los_que_tienen_dueno(db, liga):
    liga["jugador"]("Libre", valor=800_000, prob=0.9)
    liga["jugador"]("Con dueno", valor=800_000, prob=0.9, dueno=liga["rival"], clausula=1_200_000)

    libres = queries.free_agents(db)
    assert [f["name"] for f in libres] == ["Libre"]


def test_busqueda_encuentra_por_nombre_de_la_fuente(db, liga):
    pid = liga["jugador"]("Pedri Gonzalez", valor=20_000_000, prob=0.9)
    # Otra fuente lo llama de otra forma; buscar por ese nombre tambien vale.
    repo.resolve_player(
        db, provider="futbolfantasy", external_id="ff1", name="Pedri Gonzalez",
    )
    db.execute(
        "UPDATE player_alias SET external_name = 'Pedri' WHERE provider = 'futbolfantasy'"
    )

    assert [r["id"] for r in queries.find_players(db, "Pedri")] == [pid]


def test_historico_de_valor(db, liga):
    pid = liga["jugador"]("Con historia", valor=1_000_000, prob=0.9)
    repo.record_player_value(
        db, provider="mister", player_id=pid, market_value=900_000,
        snapshot_date="2026-07-01",
    )

    historia = queries.value_history(db, pid, days=365)
    assert [h["market_value"] for h in historia] == [900_000, 1_000_000]
    assert historia[-1]["snapshot_date"] == today()


def test_clasificacion_marca_quien_soy(db, liga):
    repo.record_manager_state(db, manager_id=liga["yo"], points=10, position=1)
    repo.record_manager_state(db, manager_id=liga["rival"], points=5, position=2)

    filas = queries.standings(db)
    assert [f["name"] for f in filas] == ["Glok", "Rival"]
    assert filas[0]["is_me"] == 1


# --- la ventana de `latest_value` -------------------------------------------

def test_el_ultimo_valor_no_se_pierde_por_la_ventana(db):
    """`latest_value` solo mira un mes atras, y eso no puede dejar a nadie fuera.

    La ventana existe por rendimiento: sin ella la consulta recorre el historico
    entero -mas de 130.000 filas- en cada peticion. Este test fija el contrato:
    quien tenga valor reciente tiene que aparecer, con historico viejo o sin el.
    """
    from fantasyhelper.queries import LATEST_VALUE_WINDOW_DAYS

    pid = repo.resolve_player(db, provider="mister", external_id="a", name="Uno")
    # Un ano de historico y el valor de hoy.
    for dia in range(0, 365, 7):
        repo.record_player_value(
            db, provider="mister", source="mister", player_id=pid,
            market_value=1_000_000 + dia,
            snapshot_date=f"2025-{1 + dia // 31:02d}-{1 + dia % 28:02d}",
        )
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=pid, market_value=5_000_000,
                             snapshot_date="2026-08-04")

    fila = next(f for f in queries.all_players(db) if f["id"] == pid)
    assert fila["market_value"] == 5_000_000, "debe ganar el mas reciente"
    assert LATEST_VALUE_WINDOW_DAYS >= 7, "una ventana corta se comeria a los recientes"


def test_quien_lleva_meses_sin_valor_queda_fuera(db):
    """Es el efecto buscado: quien ya no esta en el catalogo no es un jugador vivo."""
    vivo = repo.resolve_player(db, provider="mister", external_id="a", name="Vivo")
    ido = repo.resolve_player(db, provider="mister", external_id="b", name="Ido")
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=vivo, market_value=1_000_000,
                             snapshot_date="2026-08-04")
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=ido, market_value=2_000_000,
                             snapshot_date="2026-01-04")

    nombres = {f["name"] for f in queries.all_players(db)}
    assert "Vivo" in nombres
    assert "Ido" not in nombres


# --- la subida diaria y el historico ----------------------------------------


def _valores(db, pid, pares):
    for fecha, valor in pares:
        repo.record_player_value(db, provider="mister", source="mister",
                                 player_id=pid, market_value=valor,
                                 snapshot_date=fecha)


def test_el_historico_dice_cuanto_subio_cada_dia(db):
    pid = repo.resolve_player(db, provider="mister", external_id="a", name="Uno")
    _valores(db, pid, [("2026-08-01", 1_000_000), ("2026-08-02", 1_100_000),
                       ("2026-08-03", 1_050_000)])

    filas = queries.value_history(db, pid, days=30)
    assert [f["delta"] for f in filas] == [None, 100_000, -50_000]
    assert filas[0]["delta"] is None, "el primero no tiene contra que compararse"


def test_un_hueco_en_la_captura_se_declara_en_vez_de_disimularse(db):
    """Si un dia fallo la captura, la diferencia abarca dos dias.

    Presentarla como variacion diaria seria mentir, asi que viaja tambien el
    hueco y la pantalla lo escribe.
    """
    pid = repo.resolve_player(db, provider="mister", external_id="a", name="Uno")
    _valores(db, pid, [("2026-08-01", 1_000_000), ("2026-08-04", 1_300_000)])

    filas = queries.value_history(db, pid, days=30)
    assert filas[-1]["delta"] == 300_000
    assert filas[-1]["dias"] == 3


def test_manda_el_valor_leido_de_mister_cuando_hay_dos(db):
    """El mismo dia puede traer el valor de Mister y el de FutbolFantasy."""
    pid = repo.resolve_player(db, provider="mister", external_id="a", name="Uno")
    repo.record_player_value(db, provider="mister", source="futbolfantasy",
                             player_id=pid, market_value=9_000_000,
                             snapshot_date="2026-08-01")
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=pid, market_value=1_000_000,
                             snapshot_date="2026-08-01")

    filas = queries.value_history(db, pid, days=30)
    assert filas[0]["market_value"] == 1_000_000


def test_la_subida_diaria_de_una_plantilla_ignora_las_compras(db):
    """Es "lo que han subido MIS jugadores", no "cuanto ha cambiado el total".

    Sin esto, fichar a alguien apareceria como una revalorizacion enorme y
    venderlo como un desplome, que es justo lo contrario de lo que se mira.
    """
    liga = repo.upsert_league(db, provider="mister", external_id="1", name="L")
    yo = repo.upsert_manager(db, league_id=liga, external_id="10", name="Yo", is_me=True)

    viejo = repo.resolve_player(db, provider="mister", external_id="a", name="Viejo")
    nuevo = repo.resolve_player(db, provider="mister", external_id="b", name="Nuevo")
    _valores(db, viejo, [("2026-08-01", 1_000_000), ("2026-08-02", 1_200_000)])
    # El fichado de hoy no tiene valor de ayer, asi que no puede sumar nada.
    _valores(db, nuevo, [("2026-08-02", 8_000_000)])
    # La fecha de la propiedad la pone `record_ownership` (hoy) y es
    # independiente de las fechas de los valores: lo que compara la consulta son
    # los dos ultimos dias CON VALORES.
    for pid in (viejo, nuevo):
        repo.record_ownership(db, league_id=liga, player_id=pid, manager_id=yo)

    cambio = queries.squad_daily_change(db)[yo]
    assert cambio["delta"] == 200_000, "solo el que estaba en las dos fechas"
    assert cambio["dias"] == 1


def test_sin_dos_dias_de_datos_no_hay_subida_diaria(db):
    """El primer dia de vida de la base no se puede comparar con nada."""
    liga = repo.upsert_league(db, provider="mister", external_id="1", name="L")
    yo = repo.upsert_manager(db, league_id=liga, external_id="10", name="Yo")
    pid = repo.resolve_player(db, provider="mister", external_id="a", name="Uno")
    _valores(db, pid, [("2026-08-02", 1_000_000)])
    repo.record_ownership(db, league_id=liga, player_id=pid, manager_id=yo)

    assert queries.squad_daily_change(db) == {}


def test_el_feed_se_puede_pedir_entero(db):
    for n in range(40):
        repo.record_feed_event(db, league_id=None, external_id=f"f{n}",
                               kind="card-transfer", summary=f"movimiento {n}",
                               html="<div></div>")

    assert len(queries.feed_events(db, limit=None)) == 40
    assert len(queries.feed_events(db, limit=10)) == 10


def test_un_traspaso_del_feed_se_desmonta_en_sus_piezas(db):
    """El texto crudo de la tarjeta arrastra la basura de su maquetacion.

    'Javi Puado cambia de AaronLor a Mister 0 A M 765.450' lleva pegados los
    puntos del jugador y la inicial del avatar del participante.
    """
    repo.record_feed_event(
        db, league_id=None, external_id="f1", kind="card-transfer",
        summary="Javi Puado | cambia de | AaronLor | a | Mister | 0 | A | M | 765.450",
        html="<div></div>", amounts=[0, 765_450],
    )

    ev = queries.describe_event(queries.feed_events(db)[0])
    assert ev["jugador"] == "Javi Puado"
    assert ev["origen"] == "AaronLor"
    assert ev["destino"] == "Mister"
    assert ev["importe"] == 765_450


def test_una_tarjeta_que_no_es_un_traspaso_se_deja_como_esta(db):
    """Altas y avisos no tienen esa estructura, y no hay que inventarsela."""
    repo.record_feed_event(db, league_id=None, external_id="f2", kind="card-join",
                           summary="R Rida | se unió a tu liga | 2h",
                           html="<div></div>")

    ev = queries.describe_event(queries.feed_events(db)[0])
    assert ev["jugador"] is None
    assert ev["texto"] == "R Rida se unió a tu liga 2h"


def test_los_movimientos_no_traen_la_maquetacion_de_mister(db):
    """El feed mezcla movimientos con trozos de interfaz.

    'Comentar' es el pie de otra tarjeta, 'promo' es publicidad y
    'market_unified' es el widget del mercado, que ya tiene su propia pantalla.
    Se guardan igual -por si algun dia sirven- pero no se ensenan.
    """
    for kind, resumen in (
        ("card-transfer", "Uno | cambia de | A | a | B"),
        ("card-footer", "Comentar"),
        ("card-promo", "Juega por 76K Creditos"),
        ("card-market_unified", "0 | S. Comesana | 8.349.000"),
        ("card-player_transfer", "Ter Stegen | abandona la competicion | 0"),
        ("card-admin", "El administrador ha desactivado el capitan"),
    ):
        repo.record_feed_event(db, league_id=None, external_id=kind, kind=kind,
                               summary=resumen, html="<div></div>")

    tipos = {m["kind"] for m in queries.movements(db)}
    assert tipos == {"card-transfer", "card-player_transfer", "card-admin"}
    # Guardados siguen estando todos: el crudo no se toca.
    assert len(queries.feed_events(db, limit=None)) == 6


def test_un_nombre_de_equipo_bueno_no_se_degrada_a_su_slug(db):
    """FutbolFantasy identifica al equipo por la URL: 'rayo-vallecano'.

    Mister si publica 'Rayo Vallecano'. Sin esta guarda, la captura siguiente
    pisaba el nombre bueno y en pantalla volvia a salir 'RAYO-VALLECANO'.
    """
    equipo = repo.upsert_team(db, name="rayo-vallecano")
    repo.rename_team(db, team_id=equipo, name="Rayo Vallecano")
    assert db.execute("SELECT name FROM team WHERE id=?", (equipo,)).fetchone()[0] == (
        "Rayo Vallecano"
    )

    # La siguiente captura de FutbolFantasy vuelve a pasar el slug.
    repo.upsert_team(db, name="rayo-vallecano")

    assert db.execute("SELECT name FROM team WHERE id=?", (equipo,)).fetchone()[0] == (
        "Rayo Vallecano"
    )


# --- la deuda maxima --------------------------------------------------------


def test_lo_que_se_puede_gastar_reproduce_el_maxdebt_de_mister(db):
    """La formula esta deducida, asi que se ancla a las cifras reales.

    Son dos capturas propias en las que la plantilla del momento coincide con la
    del snapshot del dia. Cuadran al euro, y por eso se puede aplicar a los
    rivales, de los que Mister no publica esta cifra.
    """
    assert queries.spendable(11_503_400, -2_372_720, 37_104_000) == 6_903_280
    assert queries.spendable(21_873_420, 327_060, 27_563_000) == 7_217_810


def test_sin_pujas_lanzadas_se_usa_el_saldo_disponible(db):
    """De los rivales no se conoce el futuro; su saldo es lo mejor que hay."""
    assert queries.spendable(10_000_000, None, 40_000_000) == 20_000_000
    assert queries.spendable(None, None, 40_000_000) is None


def test_de_un_rival_solo_se_sabe_lo_que_puede_deber(db):
    """Su caja no se publica en ningun sitio, asi que no se inventa.

    Lo unico exacto de su capacidad de gasto es el cuarto de plantilla que la
    liga deja deber, porque el valor de la plantilla si lo publica Mister. Hubo
    una columna que sumaba ademas un saldo estimado y estaba mal: contra el
    unico saldo real que existe fallaba por once millones, y a un participante
    con la plantilla por encima de 50M le salia un tope NEGATIVO.
    """
    liga = repo.upsert_league(db, provider="mister", external_id="1", name="L")
    rival = repo.upsert_manager(db, league_id=liga, external_id="20", name="Rival")

    pid = repo.resolve_player(db, provider="mister", external_id="a", name="Uno")
    repo.record_player_value(db, provider="mister", source="mister",
                             player_id=pid, market_value=8_000_000)
    repo.record_ownership(db, league_id=liga, player_id=pid, manager_id=rival,
                          clause_level=0, clause_floor=8_000_000)

    fila = next(f for f in queries.estimated_balances(db) if f["id"] == rival)
    assert fila["deuda_por_plantilla"] == 2_000_000, "el 25% de su plantilla"
    assert fila["saldo_real"] is None, "de un rival no se publica"
    assert fila["deuda_real"] is None


def test_lo_que_puede_deber_nunca_sale_negativo(db):
    """Aunque su plantilla valga mas que el presupuesto de salida.

    Es lo que delataba la columna anterior: ByJonyX tenia 67,7M de plantilla
    -mas que los 50M con los que empieza todo el mundo- y le salia un tope de
    -765.250 €, que no significa nada.
    """
    liga = repo.upsert_league(db, provider="mister", external_id="1", name="L")
    rico = repo.upsert_manager(db, league_id=liga, external_id="20", name="Rico")

    for indice in range(4):
        pid = repo.resolve_player(db, provider="mister", external_id=f"p{indice}",
                                  name=f"Caro {indice}")
        repo.record_player_value(db, provider="mister", source="mister",
                                 player_id=pid, market_value=17_000_000)
        repo.record_ownership(db, league_id=liga, player_id=pid, manager_id=rico)

    fila = next(f for f in queries.estimated_balances(db) if f["id"] == rico)
    assert fila["valor_plantilla"] == 68_000_000
    assert fila["deuda_por_plantilla"] == 17_000_000
    assert fila["deuda_por_plantilla"] > 0


def test_quien_desaparece_no_se_queda_pegado_a_su_ultimo_dueno(db):
    """El fallo de las 87 propiedades fantasma.

    `latest_ownership` cogia la fila mas reciente DE CADA JUGADOR sin mirar de
    cuando era. Quien sale de la competicion deja de aparecer en el catalogo y
    por tanto no genera fila nueva, asi que se quedaba en la plantilla de su
    ultimo dueno para siempre: Horatiu Moldovan salio el 3 de agosto y seguia
    contando, inflando la plantilla, su valor y el radar de clausulas.
    """
    liga = repo.upsert_league(db, provider="mister", external_id="1", name="L")
    yo = repo.upsert_manager(db, league_id=liga, external_id="10", name="Yo", is_me=True)

    sigue = repo.resolve_player(db, provider="mister", external_id="a", name="Sigue")
    se_fue = repo.resolve_player(db, provider="mister", external_id="b", name="Se fue")
    for pid in (sigue, se_fue):
        repo.record_player_value(db, provider="mister", source="mister",
                                 player_id=pid, market_value=1_000_000)

    # Ayer los dos eran mios; hoy solo aparece uno.
    db.execute(
        "INSERT INTO ownership_snapshot (snapshot_date, captured_at, league_id,"
        " player_id, manager_id) VALUES ('2026-08-03', '2026-08-03T03:30:00Z', ?, ?, ?)",
        (liga, se_fue, yo),
    )
    repo.record_ownership(db, league_id=liga, player_id=sigue, manager_id=yo)

    nombres = {f["name"] for f in queries.squad(db, yo)}
    assert nombres == {"Sigue"}, "el que ya no aparece no es de nadie"
