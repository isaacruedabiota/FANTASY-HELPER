-- Esquema canonico de FantasyHelper.
--
-- Principios de diseno:
--   1. Las entidades de futbol (equipo, jugador, partido) son UNICAS y agnosticas
--      del fantasy. Cada proveedor (mister, futbolfantasy, biwenger...) se conecta
--      a ellas mediante las tablas *_alias. Eso es lo que permite cruzar la
--      probabilidad de once de FutbolFantasy con el valor de mercado de Mister.
--   2. Todo lo que cambia en el tiempo va en tablas *_snapshot con snapshot_date.
--      Una fila por dia y entidad (UNIQUE), para que el job sea idempotente.
--      Este historico NO se puede recuperar hacia atras: es el activo del proyecto.
--   3. Nada se borra. raw_payload guarda la respuesta original por si hay que
--      recalcular features que hoy no se nos han ocurrido.
--
-- Fechas: TEXT en ISO-8601 UTC ('2026-08-15T03:00:00Z'). Ordenable como string.
-- Dinero: INTEGER en euros. Nada de floats para dinero.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------------
-- Entidades canonicas de futbol
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS team (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,   -- 'real-madrid'
    name        TEXT NOT NULL,          -- 'Real Madrid'
    short_name  TEXT,                   -- 'RMA'
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS player (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE, -- 'vinicius-junior'
    name          TEXT NOT NULL,
    team_id       INTEGER REFERENCES team(id),
    position      TEXT,                 -- PT | DF | MC | DL  (normalizado)
    birth_date    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_player_team ON player(team_id);

-- Crosswalk: como llama cada proveedor a cada jugador.
-- Sin esto es imposible cruzar fuentes; con esto, anadir un fantasy nuevo
-- es solo rellenar alias.
CREATE TABLE IF NOT EXISTS player_alias (
    id            INTEGER PRIMARY KEY,
    player_id     INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,        -- 'mister' | 'futbolfantasy' | ...
    external_id   TEXT NOT NULL,
    external_name TEXT,
    -- Equipo SEGUN ESTA FUENTE. Imprescindible tenerlo por fuente y no solo en
    -- player.team_id: comparar el equipo que declara cada una para los mismos
    -- jugadores es lo que permite deducir que el equipo 2 de Mister y el
    -- 'atletico' de FutbolFantasy son el mismo.
    team_id       INTEGER REFERENCES team(id),
    confidence    REAL NOT NULL DEFAULT 1.0,  -- <1.0 si el match fue difuso y hay que revisarlo
    UNIQUE (provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_player_alias_player ON player_alias(player_id);

CREATE TABLE IF NOT EXISTS team_alias (
    id            INTEGER PRIMARY KEY,
    team_id       INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    external_name TEXT,
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS fixture (
    id            INTEGER PRIMARY KEY,
    season        TEXT NOT NULL,        -- '2026-27'
    matchday      INTEGER NOT NULL,
    home_team_id  INTEGER NOT NULL REFERENCES team(id),
    away_team_id  INTEGER NOT NULL REFERENCES team(id),
    kickoff_utc   TEXT,
    status        TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | live | finished | postponed
    home_goals    INTEGER,
    away_goals    INTEGER,
    UNIQUE (season, matchday, home_team_id, away_team_id)
);
CREATE INDEX IF NOT EXISTS idx_fixture_matchday ON fixture(season, matchday);


-- ---------------------------------------------------------------------------
-- Capa de cuenta / liga / manager (especifica del fantasy)
-- ---------------------------------------------------------------------------

-- Preparado desde ya para multiusuario aunque de momento solo haya una cuenta:
-- cambiar esto mas adelante obligaria a rehacer media base de datos.
CREATE TABLE IF NOT EXISTS account (
    id           INTEGER PRIMARY KEY,
    provider     TEXT NOT NULL,
    label        TEXT NOT NULL,
    external_id  TEXT,
    -- Credenciales cifradas. En Fase 0 se leen de .env y esto queda a NULL.
    secret_enc   BLOB,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (provider, label)
);

CREATE TABLE IF NOT EXISTS league (
    id           INTEGER PRIMARY KEY,
    provider     TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    name         TEXT NOT NULL,
    season       TEXT,
    account_id   INTEGER REFERENCES account(id),
    -- Dia desde el que cuentan las cuentas: la primera captura tras crear o
    -- reiniciar la liga, cuando todos tienen el presupuesto de salida. Es el
    -- ancla para estimar el saldo de los rivales, que Mister no publica.
    baseline_date TEXT,
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS manager (
    id           INTEGER PRIMARY KEY,
    league_id    INTEGER NOT NULL REFERENCES league(id) ON DELETE CASCADE,
    external_id  TEXT NOT NULL,
    name         TEXT NOT NULL,
    is_me        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (league_id, external_id)
);


-- ---------------------------------------------------------------------------
-- Series temporales: EL ACTIVO. Una fila por dia y entidad.
-- ---------------------------------------------------------------------------

-- Valor de mercado del jugador segun el proveedor.
--
-- 'provider' es de QUIEN es el valor (mister, biwenger...) y 'source' es de
-- DONDE lo hemos sacado. No son lo mismo: FutbolFantasy publica los valores de
-- Mister, asi que se puede tener provider='mister' con source='futbolfantasy'.
-- Separarlos permite contrastar ambas fuentes cuando haya sesion de Mister.
CREATE TABLE IF NOT EXISTS player_value_snapshot (
    id             INTEGER PRIMARY KEY,
    snapshot_date  TEXT NOT NULL,       -- 'YYYY-MM-DD'
    captured_at    TEXT NOT NULL,
    provider       TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'mister',
    player_id      INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    market_value   INTEGER NOT NULL,    -- euros
    delta_1d       INTEGER,             -- variacion que reporta el proveedor
    UNIQUE (snapshot_date, provider, source, player_id)
);
CREATE INDEX IF NOT EXISTS idx_value_player_date
    ON player_value_snapshot(player_id, snapshot_date);

-- Quien tiene a quien, y a que precio de clausula. La base del radar de clausulas.
CREATE TABLE IF NOT EXISTS ownership_snapshot (
    id                 INTEGER PRIMARY KEY,
    snapshot_date      TEXT NOT NULL,
    captured_at        TEXT NOT NULL,
    league_id          INTEGER NOT NULL REFERENCES league(id) ON DELETE CASCADE,
    player_id          INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    manager_id         INTEGER REFERENCES manager(id),  -- NULL = libre / del sistema
    clause_value       INTEGER,         -- lo que cuesta arrebatarlo
    clause_locked_until TEXT,           -- blindaje temporal, si aplica
    buy_price          INTEGER,         -- lo que pago su dueno, si se conoce
    -- Nivel de clausula: 0 = por defecto (x1.5), y cada escalon multiplica mas
    -- (x2, x2.5 ... x4). Subir un escalon cuesta el 20% del suelo, asi que de
    -- aqui sale cuanto ha gastado un rival en blindar su plantilla.
    clause_level       INTEGER,
    clause_floor       INTEGER,         -- base sobre la que se calcula todo
    UNIQUE (snapshot_date, league_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_ownership_manager
    ON ownership_snapshot(league_id, manager_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_ownership_player
    ON ownership_snapshot(player_id, snapshot_date);

-- Saldo y estado de cada rival: sin esto no se puede estimar quien puede clausularte.
CREATE TABLE IF NOT EXISTS manager_snapshot (
    id             INTEGER PRIMARY KEY,
    snapshot_date  TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    manager_id     INTEGER NOT NULL REFERENCES manager(id) ON DELETE CASCADE,
    balance        INTEGER,             -- saldo disponible
    team_value     INTEGER,
    points         INTEGER,
    position       INTEGER,             -- puesto en la clasificacion
    UNIQUE (snapshot_date, manager_id)
);

-- Mercado diario: que hay a la venta y a que precio.
CREATE TABLE IF NOT EXISTS market_listing_snapshot (
    id              INTEGER PRIMARY KEY,
    snapshot_date   TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    league_id       INTEGER NOT NULL REFERENCES league(id) ON DELETE CASCADE,
    player_id       INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    seller_id       INTEGER REFERENCES manager(id),  -- NULL = jugador libre del sistema
    asking_price    INTEGER,
    expires_at      TEXT,
    UNIQUE (snapshot_date, league_id, player_id)
);

-- Probabilidad de ser titular (FutbolFantasy). Tambien historico: sirve para
-- medir cuanto acierta la fuente y calibrar el modelo de xPts.
CREATE TABLE IF NOT EXISTS lineup_probability_snapshot (
    id             INTEGER PRIMARY KEY,
    snapshot_date  TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    provider       TEXT NOT NULL DEFAULT 'futbolfantasy',
    player_id      INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    season         TEXT NOT NULL,
    matchday       INTEGER NOT NULL,
    probability    REAL,                -- 0.0 - 1.0
    status         TEXT,                -- ok | duda | lesionado | sancionado | descartado
    note           TEXT,
    UNIQUE (snapshot_date, provider, player_id, matchday)
);
CREATE INDEX IF NOT EXISTS idx_lineup_prob_md
    ON lineup_probability_snapshot(season, matchday, player_id);

-- Contexto de la jornada para cada jugador (FutbolFantasy).
-- Es la materia prima del modelo de puntos esperados: no basta con saber que un
-- jugador es bueno, hay que saber contra quien juega, si es titular indiscutible
-- y si lanza los penaltis.
CREATE TABLE IF NOT EXISTS player_context_snapshot (
    id                  INTEGER PRIMARY KEY,
    snapshot_date       TEXT NOT NULL,
    captured_at         TEXT NOT NULL,
    player_id           INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    season              TEXT NOT NULL,
    matchday            INTEGER NOT NULL,
    jerarquia           INTEGER,        -- importancia en el equipo (0-100)
    form                REAL,           -- tendencia reciente segun la fuente
    opponent            TEXT,           -- abreviatura del rival
    opponent_difficulty INTEGER,        -- 1 (facil) a 5 (dificil)
    is_home             INTEGER,
    penalties           INTEGER,        -- orden de lanzador (1 = primero); NULL si no lanza
    free_kicks          INTEGER,
    corners             INTEGER,
    UNIQUE (snapshot_date, player_id, matchday)
);
CREATE INDEX IF NOT EXISTS idx_context_md
    ON player_context_snapshot(season, matchday, player_id);


-- ---------------------------------------------------------------------------
-- Rendimiento (hechos consolidados, no snapshots)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS player_match_stat (
    id            INTEGER PRIMARY KEY,
    player_id     INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    fixture_id    INTEGER NOT NULL REFERENCES fixture(id) ON DELETE CASCADE,
    minutes       INTEGER,
    started       INTEGER,
    goals         INTEGER,
    assists       INTEGER,
    yellow_cards  INTEGER,
    red_cards     INTEGER,
    xg            REAL,
    xa            REAL,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (player_id, fixture_id)
);

-- Puntos fantasy. Dependen del proveedor: el mismo partido puntua distinto
-- en Mister que en Biwenger, por eso 'provider' forma parte de la clave.
CREATE TABLE IF NOT EXISTS player_points (
    id             INTEGER PRIMARY KEY,
    provider       TEXT NOT NULL,
    player_id      INTEGER NOT NULL REFERENCES player(id) ON DELETE CASCADE,
    season         TEXT NOT NULL,
    matchday       INTEGER NOT NULL,
    fixture_id     INTEGER REFERENCES fixture(id),
    points         INTEGER,
    breakdown_json TEXT,                -- desglose original, por si acaso
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (provider, player_id, season, matchday)
);
CREATE INDEX IF NOT EXISTS idx_points_player ON player_points(player_id, season, matchday);


-- Punto de partida congelado de cada participante.
--
-- No basta con recordar la FECHA del reinicio y mirar el snapshot de ese dia:
-- los snapshots del dia en curso se sobreescriben en cada captura, asi que la
-- "plantilla inicial" iria cambiando bajo los pies y el saldo estimado se
-- desviaria solo. Aqui se guarda el valor ya calculado, y no se toca mas.
CREATE TABLE IF NOT EXISTS manager_baseline (
    id           INTEGER PRIMARY KEY,
    league_id    INTEGER NOT NULL REFERENCES league(id) ON DELETE CASCADE,
    manager_id   INTEGER NOT NULL REFERENCES manager(id) ON DELETE CASCADE,
    baseline_at  TEXT NOT NULL,        -- instante exacto, no solo el dia
    squad_value  INTEGER NOT NULL,     -- valor de su plantilla en ese instante
    clause_spend INTEGER NOT NULL DEFAULT 0,
    UNIQUE (league_id, manager_id)
);


-- Movimientos de la liga (el "feed" de Mister): fichajes, ventas, clausulazos,
-- altas y avisos del administrador.
--
-- Se guarda TODA tarjeta, se reconozca su tipo o no, con su HTML original. Los
-- tipos se van descubriendo segun ocurren -recien reiniciada la liga solo hay
-- altas-, y cuando aparezca uno nuevo se reprocesa el historico en vez de
-- haberlo perdido. Es la misma razon por la que existe raw_payload.
CREATE TABLE IF NOT EXISTS feed_event (
    id             INTEGER PRIMARY KEY,
    external_id    TEXT NOT NULL,      -- 'feed-951299082', o un hash si no trae id
    league_id      INTEGER REFERENCES league(id) ON DELETE CASCADE,
    first_seen     TEXT NOT NULL,      -- cuando lo vimos por primera vez
    snapshot_date  TEXT NOT NULL,
    kind           TEXT,               -- 'card-join', 'card-market_unified'...
    relative_time  TEXT,               -- '17h', tal cual lo muestra Mister
    summary        TEXT,               -- texto plano de la tarjeta
    player_ids     TEXT,               -- JSON: ids externos de jugadores citados
    user_ids       TEXT,               -- JSON: ids externos de participantes
    amounts        TEXT,               -- JSON: cifras en euros detectadas
    html           BLOB,               -- la tarjeta entera, para reprocesar
    UNIQUE (external_id)
);
CREATE INDEX IF NOT EXISTS idx_feed_fecha ON feed_event(snapshot_date, kind);


-- ---------------------------------------------------------------------------
-- Almacen crudo
-- ---------------------------------------------------------------------------

-- Toda respuesta HTTP tal cual llego. Si manana cambiamos como parseamos algo,
-- se reprocesa el historico entero desde aqui en vez de haberlo perdido.
CREATE TABLE IF NOT EXISTS raw_payload (
    id            INTEGER PRIMARY KEY,
    captured_at   TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    source        TEXT NOT NULL,        -- 'mister' | 'futbolfantasy'
    endpoint      TEXT NOT NULL,
    params_json   TEXT,
    status_code   INTEGER,
    content_type  TEXT,
    content       BLOB NOT NULL,
    -- 'gzip' o 'identity'. El HTML de FutbolFantasy ocupa ~21 MB/dia sin
    -- comprimir (unos 6 GB por temporada) y baja a la decima parte con gzip.
    content_encoding TEXT NOT NULL DEFAULT 'identity',
    sha256        TEXT NOT NULL,       -- del contenido ORIGINAL, no del comprimido
    UNIQUE (snapshot_date, source, endpoint, sha256)
);
CREATE INDEX IF NOT EXISTS idx_raw_lookup ON raw_payload(source, endpoint, captured_at);

-- Registro de ejecuciones del job, para saber si un dia se perdio la captura.
CREATE TABLE IF NOT EXISTS job_run (
    id            INTEGER PRIMARY KEY,
    job_name      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,        -- running | ok | error
    rows_written  INTEGER DEFAULT 0,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_run_name ON job_run(job_name, started_at);
