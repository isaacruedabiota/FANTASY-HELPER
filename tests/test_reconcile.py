"""Tests de la reconciliacion entre fuentes."""

from __future__ import annotations

from fantasyhelper.reconcile import names_match, reconcile
from fantasyhelper.storage import repository as repo


def test_names_match_une_abreviatura_y_nombre_completo():
    assert names_match("A. Grimaldo", "Álex Grimaldo")
    assert names_match("F. Valverde", "Federico Valverde")
    assert names_match("T. Martínez", "Toni Martinez")
    assert names_match("Á. Núñez", "Álvaro Núñez")


def test_names_match_une_apodo_y_nombre_compuesto():
    assert names_match("Pedri", "Pedri González")
    assert names_match("Isco", "Isco Alarcón")


def test_names_match_no_une_jugadores_distintos():
    assert not names_match("Javi Guerra", "Javi Galán")
    # Mismo apellido y misma inicial, pero dos personas distintas: cuando los
    # dos nombres vienen completos no basta con la inicial.
    assert not names_match("Rafa Rodríguez", "Riki Rodríguez")
    assert not names_match("Pedri", "Pedro Porro")


def _poblar(db, *, equipo_ff: str, equipo_mister: str, jugadores: list[tuple[str, str]]):
    """Crea jugadores en ambas fuentes, con equipos aun sin unificar."""
    ff_team = repo.upsert_team(db, name=equipo_ff, provider="futbolfantasy",
                               external_id=equipo_ff)
    mister_team = repo.upsert_team(db, name=equipo_mister, provider="mister",
                                   external_id="2")
    for index, (nombre_ff, nombre_mister) in enumerate(jugadores):
        repo.resolve_player(db, provider="futbolfantasy", external_id=f"ff{index}",
                            name=nombre_ff, team_id=ff_team)
        repo.resolve_player(db, provider="mister", external_id=f"m{index}",
                            name=nombre_mister, team_id=mister_team)
    return ff_team, mister_team


def test_fusiona_equipos_y_enlaza_jugadores(db):
    # Tres jugadores con el mismo slug cruzan solos y sirven de evidencia para
    # deducir que 'mister-team-2' es el mismo equipo que 'atletico'.
    _poblar(
        db,
        equipo_ff="atletico",
        equipo_mister="mister-team-2",
        jugadores=[
            ("Antoine Griezmann", "Antoine Griezmann"),
            ("Jan Oblak", "Jan Oblak"),
            ("Koke Resurrección", "Koke Resurrección"),
            # Estos dos solo cruzan por apellido + inicial.
            ("Álex Baena", "A. Baena"),
            ("Julián Álvarez", "J. Álvarez"),
        ],
    )

    report = reconcile(db)

    assert report.teams_merged == 1
    assert report.players_linked == 2
    assert report.still_unmatched == 0

    # Ya no quedan jugadores de Mister sin su equivalente en FutbolFantasy.
    huerfanos = db.execute(
        """
        SELECT COUNT(*) n FROM player p
        JOIN player_alias a ON a.player_id = p.id AND a.provider = 'mister'
        WHERE NOT EXISTS (SELECT 1 FROM player_alias f
                          WHERE f.player_id = p.id AND f.provider = 'futbolfantasy')
        """
    ).fetchone()["n"]
    assert huerfanos == 0

    # Y solo queda un equipo, con los alias de las dos fuentes apuntando a el.
    assert db.execute("SELECT COUNT(*) n FROM team").fetchone()["n"] == 1
    providers = [
        r["provider"] for r in db.execute("SELECT provider FROM team_alias ORDER BY provider")
    ]
    assert providers == ["futbolfantasy", "mister"]


def test_los_enlaces_deducidos_quedan_marcados_para_revision(db):
    _poblar(
        db,
        equipo_ff="atletico",
        equipo_mister="mister-team-2",
        jugadores=[
            ("Antoine Griezmann", "Antoine Griezmann"),
            ("Jan Oblak", "Jan Oblak"),
            ("Álex Baena", "A. Baena"),
        ],
    )

    reconcile(db)

    dudosos = db.execute(
        "SELECT COUNT(*) n FROM player_alias WHERE confidence < 1.0"
    ).fetchone()["n"]
    assert dudosos >= 1, "un match por apellido no es una certeza y debe poder revisarse"


def test_no_enlaza_si_hay_ambiguedad(db):
    # Dos candidatos con el mismo apellido e inicial: no se puede decidir.
    _poblar(
        db,
        equipo_ff="atletico",
        equipo_mister="mister-team-2",
        jugadores=[
            ("Antoine Griezmann", "Antoine Griezmann"),
            ("Jan Oblak", "Jan Oblak"),
            ("Alberto Moreno", "X. Otro"),
            ("Antonio Moreno", "A. Moreno"),
        ],
    )

    report = reconcile(db)
    assert report.players_linked == 0
    assert report.still_unmatched == 2


def test_es_idempotente(db):
    _poblar(
        db,
        equipo_ff="atletico",
        equipo_mister="mister-team-2",
        jugadores=[
            ("Antoine Griezmann", "Antoine Griezmann"),
            ("Jan Oblak", "Jan Oblak"),
            ("Álex Baena", "A. Baena"),
        ],
    )

    primero = reconcile(db)
    segundo = reconcile(db)

    assert primero.players_linked == 1
    assert segundo.teams_merged == 0
    assert segundo.players_linked == 0
