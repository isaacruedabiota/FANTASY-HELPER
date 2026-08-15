"""SofaScore: el pasado de los que nunca han jugado aqui.

Mister solo conoce LaLiga, asi que de los 140 jugadores llegados de otras ligas
no publica ni una temporada. Hasta ahora se les estimaba por lo que cuestan, que
es mejor que nada pero no distingue a un titular de la Serie A de un suplente de
la Premier con el mismo precio.

SofaScore si tiene ese pasado, y ademas es la fuente que usa el propio Mister
para su sistema estadistico -su configuracion lleva `apiProvider: sofascore`-,
asi que las notas de las que salen los puntos son literalmente las mismas.

LO QUE SE TRAE Y LO QUE NO

Se trae, por temporada y competicion, la nota media, los partidos y los minutos.
La conversion a puntos vive en `ratings.py` y es una tabla dictada, no una
estimacion.

NO se traen las notas partido a partido, que serian una peticion por partido. La
consecuencia esta medida y hay que tenerla presente: la tabla es escalonada y
comprime mucho por arriba -de 8,0 a 8,5 son todos 10 puntos-, asi que convertir
la MEDIA de la temporada da algo mas que convertir cada partido y promediar. El
sesgo es hacia arriba y afecta sobre todo a los jugadores irregulares. Se acota
solo: la estimacion se contrae despues hacia la referencia de su precio, que no
sabe nada de notas.

EL EMPAREJADO ES LO FRAGIL

Buscar por nombre devuelve homonimos -dieciocho "Antony"-. Solo se acepta un
candidato cuando su equipo actual coincide con el que ya sabemos, o cuando el
nombre normalizado encaja y no hay mas de uno. Ante la duda no se guarda nada:
un jugador con el historial de otro es mucho peor que un jugador sin historial.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass

import httpx

from fantasyhelper.adapters.sofascore.ratings import rating_to_points
from fantasyhelper.storage import repository as repo
from fantasyhelper.storage.raw import load_raw_fresh, save_raw
from fantasyhelper.utils.names import match_key, slugify

log = logging.getLogger(__name__)

PROVIDER = "sofascore"
BASE_URL = "https://api.sofascore.com/api/v1"

#: Segundos entre peticiones. Es una API no publicada y sin condiciones de uso
#: escritas, asi que se va despacio y se cachea todo lo que se puede.
MIN_INTERVAL = 1.5

#: Las temporadas cerradas no cambian nunca; la unica que se mueve es la actual.
#: Una semana de cache evita repetir cientos de peticiones al reejecutar.
CACHE_HOURS = 24 * 7

#: Partidos minimos en una temporada para que su nota signifique algo. Con dos
#: apariciones, una nota de 7,8 es ruido.
MIN_APPEARANCES = 5

#: '25/26' -> '2025-26'. Alguna competicion usa el ano suelto ('2026').
SEASON_RE = re.compile(r"^(\d{2})/(\d{2})$")


def normalize_season(year: str | None) -> str | None:
    match = SEASON_RE.match((year or "").strip())
    if match:
        return f"20{match.group(1)}-{match.group(2)}"
    return None


@dataclass
class Candidate:
    """Un jugador de la busqueda, con lo justo para saber si es el nuestro."""

    external_id: str
    name: str
    team: str | None


@dataclass
class SeasonRating:
    """Nota media de un jugador en una temporada y competicion."""

    season: str
    competition: str
    rating: float
    appearances: int
    minutes: int | None = None

    @property
    def points(self) -> int | None:
        return rating_to_points(self.rating)


class SofaScoreScraper:
    provider = PROVIDER

    def __init__(self) -> None:
        self._last_request = 0.0
        self._client = httpx.Client(
            base_url=BASE_URL,
            # La API responde 403 al User-Agent por defecto de httpx. No es una
            # barrera de autenticacion sino un filtro de bots genericos; se
            # manda uno de navegador y se va despacio.
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": "https://www.sofascore.com/",
            },
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    # -- red ---------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def fetch_json(
        self, conn: sqlite3.Connection, path: str, endpoint: str
    ) -> dict | None:
        cached = load_raw_fresh(
            conn, source=self.provider, endpoint=endpoint, max_age_hours=CACHE_HOURS
        )
        if cached is not None:
            return json.loads(cached)

        self._throttle()
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            log.warning("sofascore %s: %s", endpoint, exc)
            return None
        if response.status_code != 200:
            log.warning("sofascore %s: HTTP %s", endpoint, response.status_code)
            return None

        save_raw(
            conn,
            source=self.provider,
            endpoint=endpoint,
            content=response.content,
            params={"path": path},
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type"),
        )
        try:
            return response.json()
        except ValueError:
            log.warning("sofascore %s: la respuesta no es JSON", endpoint)
            return None

    # -- busqueda y emparejado ---------------------------------------------

    def search(self, conn: sqlite3.Connection, name: str) -> list[Candidate]:
        payload = self.fetch_json(
            conn, f"/search/all?q={httpx.QueryParams({'q': name})['q']}",
            f"search/{match_key(name)}",
        ) or {}
        candidatos = []
        for fila in payload.get("results") or []:
            if fila.get("type") != "player":
                continue
            entidad = fila.get("entity") or {}
            if entidad.get("id") is None:
                continue
            candidatos.append(Candidate(
                external_id=str(entidad["id"]),
                name=entidad.get("name") or "",
                team=(entidad.get("team") or {}).get("name"),
            ))
        return candidatos


def same_person(uno: str, otro: str) -> bool:
    """Si dos nombres pueden ser de la misma persona.

    No vale exigir igualdad: Mister abrevia ("A. Sivera") y SofaScore da el
    nombre completo ("Antonio Sivera Salva"). Se acepta cuando uno contiene al
    otro o cuando coincide el ultimo apellido, que es lo que ambos escriben
    siempre entero.

    No se puede usar `match_key` para los apellidos porque quita los espacios y
    deja 'antoniosiverasalva' de una pieza; se trocea con `slugify`, que si los
    conserva como guiones.
    """
    a, b = match_key(uno), match_key(otro)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True

    trozos_a = [t for t in slugify(uno).split("-") if len(t) > 2]
    trozos_b = [t for t in slugify(otro).split("-") if len(t) > 2]
    if not trozos_a or not trozos_b:
        return False

    # El ultimo trozo del nombre CORTO tiene que aparecer en el largo. No vale
    # comparar los dos ultimos: en Espana se llevan dos apellidos y Mister suele
    # dar solo el primero ('A. Sivera' frente a 'Antonio Sivera Salva'), asi que
    # exigir que coincida el ultimo los separaria siempre.
    corto, largo = sorted((trozos_a, trozos_b), key=len)
    return corto[-1] in largo


def pick(candidates: list[Candidate], *, name: str, team: str | None) -> Candidate | None:
    """El candidato que de verdad es nuestro jugador, o ninguno.

    El equipo ACOTA y el nombre CONFIRMA; hacen falta los dos. Con el equipo a
    secas bastaba con que hubiera un unico jugador del Levante en la respuesta
    para darlo por bueno, aunque se llamase de otra manera -lo encontro un test-.

    Devolver None es una respuesta valida y frecuente. Colgarle a un jugador el
    historial de otro es peor que dejarlo sin historial: el error no se ve, se
    propaga a los puntos esperados y desde ahi a las recomendaciones.
    """
    if not candidates:
        return None

    plausibles = [c for c in candidates if same_person(c.name, name)]
    if not plausibles:
        return None
    if len(plausibles) == 1:
        return plausibles[0]

    if team:
        clave = match_key(team)
        por_equipo = [
            c for c in plausibles
            if c.team and (clave in match_key(c.team) or match_key(c.team) in clave)
        ]
        if len(por_equipo) == 1:
            return por_equipo[0]
        plausibles = por_equipo or plausibles

    # Varios homonimos y el equipo no ha desempatado: solo si uno encaja exacto.
    exactos = [c for c in plausibles if match_key(c.name) == match_key(name)]
    return exactos[0] if len(exactos) == 1 else None


def season_ratings(
    scraper: SofaScoreScraper, conn: sqlite3.Connection, external_id: str
) -> list[SeasonRating]:
    """Notas por temporada y competicion de un jugador de SofaScore."""
    resumen = scraper.fetch_json(
        conn, f"/player/{external_id}/statistics/seasons",
        f"player/{external_id}/seasons",
    ) or {}

    notas: list[SeasonRating] = []
    for bloque in resumen.get("uniqueTournamentSeasons") or []:
        torneo = bloque.get("uniqueTournament") or {}
        torneo_id, torneo_nombre = torneo.get("id"), torneo.get("name")
        if torneo_id is None:
            continue
        for temporada in bloque.get("seasons") or []:
            season = normalize_season(temporada.get("year"))
            if season is None or temporada.get("id") is None:
                continue
            datos = scraper.fetch_json(
                conn,
                f"/player/{external_id}/unique-tournament/{torneo_id}"
                f"/season/{temporada['id']}/statistics/overall",
                f"player/{external_id}/t{torneo_id}/s{temporada['id']}",
            ) or {}
            stats = datos.get("statistics") or {}
            rating, partidos = stats.get("rating"), stats.get("appearances")
            if rating is None or not partidos or partidos < MIN_APPEARANCES:
                continue
            notas.append(SeasonRating(
                season=season,
                competition=torneo_nombre or str(torneo_id),
                rating=float(rating),
                appearances=int(partidos),
                minutes=stats.get("minutesPlayed"),
            ))
    return notas


def combine(notas: list[SeasonRating]) -> dict[str, tuple[float, int]]:
    """{temporada: (puntos por partido, partidos)} juntando competiciones.

    Un jugador puede sumar liga, copa y Europa en la misma temporada. Se
    promedian los puntos ponderando por partidos, que es lo que hace que una
    eliminatoria de tres partidos no pese como una liga de treinta.

    No se corrige por dificultad de la competicion, y conviene saberlo: un 7,2
    en la Eredivisie y un 7,2 en la Premier no valen lo mismo. Quien usa esto lo
    contrae despues hacia la referencia del precio, que si distingue.
    """
    por_temporada: dict[str, list[SeasonRating]] = {}
    for nota in notas:
        por_temporada.setdefault(nota.season, []).append(nota)

    resultado: dict[str, tuple[float, int]] = {}
    for season, grupo in por_temporada.items():
        partidos = sum(n.appearances for n in grupo)
        puntos = sum((n.points or 0) * n.appearances for n in grupo)
        if partidos:
            resultado[season] = (puntos / partidos, partidos)
    return resultado


#: Los que Mister no conoce de nada. Se piden por valor descendente porque si
#: hay que cortar la lista, mas vale conocer a Antony que al cuarto portero.
_SIN_HISTORIAL_SQL = """
    SELECT p.id, p.name, t.name AS team, v.market_value,
           a.external_id AS sofascore_id
    FROM player p
    LEFT JOIN team t ON t.id = p.team_id
    LEFT JOIN player_alias a ON a.player_id = p.id AND a.provider = 'sofascore'
    JOIN (SELECT player_id, market_value,
                 ROW_NUMBER() OVER (PARTITION BY player_id
                                    ORDER BY snapshot_date DESC) rn
          FROM player_value_snapshot
          WHERE provider = 'mister' AND source = 'mister') v
      ON v.player_id = p.id AND v.rn = 1
    WHERE NOT EXISTS (
        SELECT 1 FROM player_season_stat s
        WHERE s.player_id = p.id AND s.provider = 'mister'
          AND s.matches_played > 0)
    ORDER BY v.market_value DESC
"""


def backfill(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    scraper: SofaScoreScraper | None = None,
    only_missing: bool = True,
) -> dict[str, int]:
    """Rellena el pasado de los jugadores que Mister no conoce.

    `only_missing` salta a los que ya tienen notas guardadas, que es lo normal:
    las temporadas cerradas no cambian y repetir la busqueda de doscientos
    jugadores no aporta nada. Con `False` se rehace todo, que es lo que hay que
    hacer si cambia el emparejado.
    """
    scraper = scraper or SofaScoreScraper()
    filas = conn.execute(_SIN_HISTORIAL_SQL).fetchall()
    if limit:
        filas = filas[:limit]

    cuenta = {"mirados": 0, "emparejados": 0, "sin_encontrar": 0, "temporadas": 0}
    for fila in filas:
        ya_tiene = conn.execute(
            "SELECT 1 FROM player_season_stat WHERE player_id = ? "
            "AND provider = ? AND matches_played > 0",
            (fila["id"], PROVIDER),
        ).fetchone()
        if only_missing and ya_tiene:
            continue

        cuenta["mirados"] += 1
        external_id = fila["sofascore_id"]
        if external_id is None:
            elegido = pick(
                scraper.search(conn, fila["name"]),
                name=fila["name"], team=fila["team"],
            )
            if elegido is None:
                cuenta["sin_encontrar"] += 1
                log.info("sin emparejar en sofascore: %s (%s)",
                         fila["name"], fila["team"])
                continue
            external_id = elegido.external_id
            repo.link_player_alias(
                conn, player_id=fila["id"], provider=PROVIDER,
                external_id=external_id, external_name=elegido.name,
                # Se marca el emparejado por apellido para poder repasarlo:
                # es el que puede colar a un homonimo.
                confidence=(
                    1.0 if match_key(elegido.name) == match_key(fila["name"])
                    else 0.8
                ),
            )

        temporadas = combine(season_ratings(scraper, conn, external_id))
        if not temporadas:
            cuenta["sin_encontrar"] += 1
            continue

        cuenta["emparejados"] += 1
        for season, (media, partidos) in temporadas.items():
            repo.record_season_stat(
                conn, provider=PROVIDER, player_id=fila["id"], season=season,
                points=round(media * partidos), avg_points=media,
                matches_played=partidos,
            )
            cuenta["temporadas"] += 1

    return cuenta
