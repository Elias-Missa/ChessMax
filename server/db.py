"""SQLite helpers for the FastAPI app.

The app is single-user. We keep the ``users`` table for ratings and attempt
history but auto-provision one row (the singleton) and resolve it server-side.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from pipeline.import_puzzles import ensure_positions_schema


# Anchored to the repo root so the server works regardless of the CWD uvicorn
# was started from.
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = _REPO_ROOT / "data" / "trainer.db"
SINGLETON_USERNAME = "default"
VALID_OPENINGS: tuple[str, ...] = ("london", "caro-kann")

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    rating INTEGER DEFAULT 1500,
    selected_openings TEXT,
    chesscom_username TEXT,
    email TEXT UNIQUE,
    password_hash TEXT,
    password_salt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    user_move TEXT NOT NULL,
    eval_loss REAL NOT NULL,
    grade TEXT NOT NULL,
    user_rating_before INTEGER NOT NULL,
    user_rating_after INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id);

CREATE TABLE IF NOT EXISTS playouts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    maia_rating INTEGER NOT NULL,
    result TEXT NOT NULL,
    pgn TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT 'maia',
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_playouts_user ON playouts(user_id);

CREATE TABLE IF NOT EXISTS playout_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    maia_rating INTEGER NOT NULL,
    user_color TEXT NOT NULL,
    engine TEXT NOT NULL,
    fen TEXT NOT NULL,
    initial_fen TEXT NOT NULL,
    move_list TEXT NOT NULL DEFAULT '[]',
    eval_streak INTEGER NOT NULL DEFAULT 0,
    streak_losing_side TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_playout_sessions_user_status
    ON playout_sessions(user_id, status);

CREATE TABLE IF NOT EXISTS position_evals (
    position_id INTEGER PRIMARY KEY,
    eval_cp INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS hold_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    position_id INTEGER NOT NULL,
    user_color TEXT NOT NULL,
    maia_rating INTEGER NOT NULL,
    engine TEXT NOT NULL DEFAULT 'stockfish',
    fen TEXT NOT NULL,
    initial_fen TEXT NOT NULL,
    move_list TEXT NOT NULL DEFAULT '[]',
    target_moves INTEGER NOT NULL,
    threshold_cp INTEGER NOT NULL,
    baseline_eval_cp INTEGER NOT NULL,
    moves_survived INTEGER NOT NULL DEFAULT 0,
    min_eval_cp INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_hold_sessions_user_status
    ON hold_sessions(user_id, mode, status);

CREATE TABLE IF NOT EXISTS hold_results (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    position_id INTEGER NOT NULL,
    target_moves INTEGER NOT NULL,
    threshold_cp INTEGER NOT NULL,
    moves_survived INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    detail TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_hold_results_user_mode
    ON hold_results(user_id, mode, id);

CREATE TABLE IF NOT EXISTS guess_attempts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER,
    fen TEXT NOT NULL,
    guessed_eval_cp INTEGER NOT NULL,
    actual_eval_cp INTEGER NOT NULL,
    guessed_sharpness REAL NOT NULL,
    actual_sharpness REAL NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_guess_attempts_user ON guess_attempts(user_id, id);

CREATE TABLE IF NOT EXISTS forced_attempts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    user_line TEXT NOT NULL,
    expected_line TEXT NOT NULL,
    matched_plies INTEGER NOT NULL,
    total_plies INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS idx_forced_attempts_user ON forced_attempts(user_id, id);

-- "Your Mistakes": personalized puzzles mined from the user's own Chess.com games.
CREATE TABLE IF NOT EXISTS mistake_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chesscom_user TEXT NOT NULL,
    since_date TEXT NOT NULL,
    games_scanned INTEGER NOT NULL DEFAULT 0,
    games_eligible INTEGER NOT NULL DEFAULT 0,
    puzzles_created INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_mistake_runs_user ON mistake_runs(user_id, id);

CREATE TABLE IF NOT EXISTS mistake_puzzles (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    bucket TEXT NOT NULL,                    -- 'missed_win' | 'blunder'
    fen TEXT NOT NULL,
    side_to_move TEXT NOT NULL,
    user_color TEXT NOT NULL,
    best_move TEXT NOT NULL,                 -- UCI
    solution_moves TEXT,                     -- space-joined UCI PV
    user_actual_move TEXT NOT NULL,          -- UCI
    eval_before_cp INTEGER NOT NULL,         -- all evals user-POV
    eval_best_cp INTEGER NOT NULL,
    eval_played_cp INTEGER NOT NULL,
    second_best_gap_cp INTEGER,              -- Bucket A only
    volatility REAL,
    maia1900_p_solution REAL,                -- nullable (approx gate stores rank instead)
    maia_solution_rank INTEGER,
    maia_best_in_top3 INTEGER,
    clock_seconds REAL,
    ply_number INTEGER NOT NULL,
    game_url TEXT,
    game_id TEXT,                            -- end_time/url slug, dedupe key
    game_date TEXT,
    time_class TEXT,
    opponent TEXT,
    caption TEXT,
    solved INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (run_id) REFERENCES mistake_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_mistake_puzzles_user
    ON mistake_puzzles(user_id, solved, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mistake_puzzles_dedupe
    ON mistake_puzzles(user_id, fen, user_actual_move);

-- Accounts: login sessions (opaque token in an httpOnly cookie).
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Per-user saved analyzed games (the vol "Library", moved off browser IndexedDB).
CREATE TABLE IF NOT EXISTS vol_games (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    imported_at INTEGER,
    source_name TEXT,
    pgn TEXT,
    metadata_json TEXT,
    report_json TEXT,
    derived_stats_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_vol_games_user ON vol_games(user_id, imported_at);

-- Guess the Elo Duels: a pool of games generated at a hidden true rating.
CREATE TABLE IF NOT EXISTS elo_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    true_elo INTEGER NOT NULL,
    pgn TEXT NOT NULL,
    plies INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'maia2_selfplay',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_elo_games_elo ON elo_games(true_elo);

-- One head-to-head duel: two players guess the same game's rating; closest wins.
-- player_b is NULL for a bot opponent (bot_guess then holds its guess).
CREATE TABLE IF NOT EXISTS elo_duels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    true_elo INTEGER NOT NULL,
    player_a INTEGER NOT NULL,
    player_b INTEGER,
    is_bot INTEGER NOT NULL DEFAULT 0,
    guess_a INTEGER,
    guess_b INTEGER,
    deadline_ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    winner TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES elo_games(id),
    FOREIGN KEY (player_a) REFERENCES users(id),
    FOREIGN KEY (player_b) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_elo_duels_players ON elo_duels(player_a, player_b, status);

-- Guess the Eval Duels: positions with known engine eval (centipawns).
CREATE TABLE IF NOT EXISTS eval_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fen TEXT NOT NULL,
    true_eval_cp INTEGER NOT NULL,
    fen_san_history TEXT,
    source TEXT NOT NULL DEFAULT 'positions_db',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One head-to-head eval duel: two players guess the position's eval within 1 minute; closest wins.
CREATE TABLE IF NOT EXISTS eval_duels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    true_eval_cp INTEGER NOT NULL,
    player_a INTEGER NOT NULL,
    player_b INTEGER,
    is_bot INTEGER NOT NULL DEFAULT 0,
    guess_a INTEGER,
    guess_b INTEGER,
    deadline_ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    winner TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (position_id) REFERENCES eval_positions(id),
    FOREIGN KEY (player_a) REFERENCES users(id),
    FOREIGN KEY (player_b) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_eval_duels_players ON eval_duels(player_a, player_b, status);

-- Insights / Game Review persistence (normalized for aggregation).
CREATE TABLE IF NOT EXISTS games (
    game_id        TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    pgn            TEXT NOT NULL,
    white_name     TEXT,
    black_name     TEXT,
    white_rating   INTEGER,
    black_rating   INTEGER,
    result         TEXT,
    time_class     TEXT,
    time_control   TEXT,
    eco            TEXT,
    opening_name   TEXT,
    played_at      TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id          TEXT PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    game_id            TEXT NOT NULL REFERENCES games(game_id),
    user_color         TEXT NOT NULL,
    user_rating        INTEGER,
    depth_tier         TEXT NOT NULL,
    status             TEXT NOT NULL,
    progress           REAL DEFAULT 0,
    engine_version     TEXT,
    maia_version       TEXT,
    constants_version  TEXT,
    nodes              INTEGER,
    accuracy           REAL,
    fixable_loss       REAL,
    total_loss         REAL,
    loss_type          TEXT,
    detail_json        TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, game_id, depth_tier),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS review_moves (
    review_id      TEXT NOT NULL REFERENCES reviews(review_id) ON DELETE CASCADE,
    ply            INTEGER NOT NULL,
    san            TEXT NOT NULL,
    is_user_move   INTEGER NOT NULL,
    phase          TEXT NOT NULL,
    is_book        INTEGER DEFAULT 0,
    classification TEXT,
    win_prob       REAL,
    delta_w        REAL,
    volatility     REAL,
    findability    INTEGER,
    findability_personal REAL,
    r_find         INTEGER,
    time_spent     REAL,
    clock_remaining REAL,
    tactic_tags    TEXT,
    detail         TEXT,
    PRIMARY KEY (review_id, ply)
);
CREATE INDEX IF NOT EXISTS idx_review_moves_agg
    ON review_moves(review_id, is_user_move, phase, classification);

CREATE TABLE IF NOT EXISTS insight_runs (
    run_id          TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    chesscom_handle TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'chesscom',
    window_days     INTEGER NOT NULL,
    time_class      TEXT NOT NULL,
    games_analyzed  INTEGER DEFAULT 0,
    games_capped    INTEGER DEFAULT 0,
    status          TEXT NOT NULL,
    progress        REAL DEFAULT 0,
    -- Narration for the generation animation: which phase of the run is live,
    -- a human-readable line about it, and the denominator progress is against.
    stage           TEXT,
    stage_detail    TEXT,
    games_total     INTEGER DEFAULT 0,
    metrics         TEXT,
    detail          TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_insight_runs_user ON insight_runs(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS insight_run_games (
    run_id   TEXT NOT NULL REFERENCES insight_runs(run_id) ON DELETE CASCADE,
    game_id  TEXT NOT NULL REFERENCES games(game_id),
    PRIMARY KEY (run_id, game_id)
);

-- C.5: instructive positions flagged from an insights run for practice.
CREATE TABLE IF NOT EXISTS insight_flags (
    run_id       TEXT NOT NULL REFERENCES insight_runs(run_id) ON DELETE CASCADE,
    game_id      TEXT NOT NULL,
    review_id    TEXT NOT NULL,
    ply          INTEGER NOT NULL,
    fen          TEXT,
    side_to_move TEXT,
    move_uci     TEXT,
    best_uci     TEXT,
    san          TEXT,
    delta_w      REAL,
    findability  INTEGER,
    volatility   REAL,
    needs_full   INTEGER NOT NULL DEFAULT 0,
    puzzle_id    INTEGER,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, game_id, ply)
);
CREATE INDEX IF NOT EXISTS idx_insight_flags_run ON insight_flags(run_id, delta_w DESC);

-- Endgame Arena: mined endgame positions played out against Maia. The bucket
-- (drawn / winning / losing) is snapshotted at start because it decides both
-- the opponent's level and how the finished game is graded, and the live eval
-- moves as soon as the first move is played.
CREATE TABLE IF NOT EXISTS endgame_sessions (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    bucket         TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_ref     TEXT,
    user_color     TEXT NOT NULL,
    maia_rating    INTEGER NOT NULL,
    user_rating    INTEGER,
    start_eval_cp  INTEGER,
    initial_fen    TEXT NOT NULL,
    fen            TEXT NOT NULL,
    move_list      TEXT NOT NULL DEFAULT '[]',
    draw_offers    INTEGER NOT NULL DEFAULT 0,
    takebacks      INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'active',
    result         TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_endgame_sessions_user
    ON endgame_sessions(user_id, status);

CREATE TABLE IF NOT EXISTS endgame_results (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    session_id     INTEGER NOT NULL,
    bucket         TEXT NOT NULL,
    source         TEXT NOT NULL,
    maia_rating    INTEGER NOT NULL,
    user_color     TEXT NOT NULL,
    start_eval_cp  INTEGER,
    end_eval_cp    INTEGER,
    result         TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    passed         INTEGER NOT NULL,
    plies          INTEGER NOT NULL DEFAULT 0,
    takebacks      INTEGER NOT NULL DEFAULT 0,
    initial_fen    TEXT NOT NULL,
    pgn            TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (session_id) REFERENCES endgame_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_endgame_results_user
    ON endgame_results(user_id, bucket, id);

-- Dev tab: manual calibration labels over stored review positions. One row per
-- (user, review, ply); both label columns are nullable so volatility and
-- findability can be judged independently. The scores are snapshotted at
-- labelling time so a later refit can't silently reinterpret the verdict.
CREATE TABLE IF NOT EXISTS dev_labels (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    review_id          TEXT NOT NULL,
    ply                INTEGER NOT NULL,
    fen                TEXT,
    move_uci           TEXT,
    best_uci           TEXT,
    volatility         REAL,
    findability        INTEGER,
    volatility_label   TEXT,
    findability_label  TEXT,
    note               TEXT,
    constants_version  TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, review_id, ply),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_dev_labels_user ON dev_labels(user_id, updated_at DESC);

-- Daily check-in: one session per user per *local* day, five phase rows under
-- it. The rollups on the session row (phases_done / seconds_total) are
-- denormalized on purpose — the calendar reads a month of days at a time, and
-- recounting five phase rows per cell is a join nobody needs.
CREATE TABLE IF NOT EXISTS daily_sessions (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    day            TEXT NOT NULL,              -- the player's local YYYY-MM-DD
    phases_done    INTEGER NOT NULL DEFAULT 0,
    seconds_total  INTEGER NOT NULL DEFAULT 0,
    started_at     TIMESTAMP,
    completed_at   TIMESTAMP,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, day),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_daily_sessions_user ON daily_sessions(user_id, day);

CREATE TABLE IF NOT EXISTS daily_phase_runs (
    id             INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    phase          TEXT NOT NULL,              -- openings|puzzles|endgame|mistakes|review
    status         TEXT NOT NULL DEFAULT 'pending',
    seconds_spent  INTEGER NOT NULL DEFAULT 0,
    target_seconds INTEGER NOT NULL DEFAULT 300,
    payload        TEXT,                       -- phase-specific (the review's game)
    started_at     TIMESTAMP,
    completed_at   TIMESTAMP,
    UNIQUE (session_id, phase),
    FOREIGN KEY (session_id) REFERENCES daily_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_daily_phase_runs_user
    ON daily_phase_runs(user_id, phase, id);

-- Opening repertoire drill answers. Every attempt is kept, not just the latest:
-- the queue reads only the most recent one per card, but the history is what a
-- real spaced-repetition schedule would have to be fitted on.
CREATE TABLE IF NOT EXISTS repertoire_attempts (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    card_id      TEXT NOT NULL,
    fen          TEXT NOT NULL,
    expected_uci TEXT NOT NULL,
    played_uci   TEXT NOT NULL,
    correct      INTEGER NOT NULL,
    verdict      TEXT,
    timestamp    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_repertoire_attempts_card
    ON repertoire_attempts(user_id, card_id, id DESC);

-- ── Authored opening repertoire (the Chessbook-style builder) ──────────────
-- Distinct from `repertoire_attempts` above, which drills the repertoire MINED
-- from the player's own games. This one is CHOSEN: the player walks a board and
-- picks a move at each position, so the tree holds decisions that have not
-- happened in a real game yet. Both feed the same drill.
--
-- Identity is the position, not the path (`fen_key`: board/side/castling/ep, no
-- clocks), so two move orders reaching the same position land on one node and
-- transpositions merge for free. `path_uci` keeps ONE way of reaching it so the
-- UI can show how — it is display context, never identity.
CREATE TABLE IF NOT EXISTS opening_nodes (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    color        TEXT NOT NULL,
    fen_key      TEXT NOT NULL,
    fen          TEXT NOT NULL,
    ply          INTEGER NOT NULL,
    path_uci     TEXT NOT NULL DEFAULT '',
    opening_name TEXT,
    opening_eco  TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    UNIQUE (user_id, color, fen_key)
);
CREATE INDEX IF NOT EXISTS idx_opening_nodes_user
    ON opening_nodes(user_id, color, ply);

-- One chosen continuation from a node. `role` separates the user's own decision
-- ("mine") from an opponent reply we are covering ("theirs") — a repertoire is
-- only drilled on the former, but must branch on the latter to reach it.
CREATE TABLE IF NOT EXISTS opening_edges (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    node_id     INTEGER NOT NULL,
    uci         TEXT NOT NULL,
    san         TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'mine',
    note        TEXT,
    signals     TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (node_id) REFERENCES opening_nodes(id),
    UNIQUE (node_id, uci)
);
CREATE INDEX IF NOT EXISTS idx_opening_edges_node
    ON opening_edges(user_id, node_id);

-- Lichess explorer answers, cached so a position costs the network once. Keyed
-- by corpus + position + the query parameters that change the answer (rating
-- band and speeds), because the same FEN in the masters corpus and in a
-- 1400-1600 blitz corpus are different questions with different answers.
-- No user_id: the explorer's answer is a property of the position, not of who
-- asked, exactly like `position_cache`.
CREATE TABLE IF NOT EXISTS explorer_cache (
    database    TEXT NOT NULL,
    fen_key     TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (database, fen_key, params)
);

-- Shared Zobrist position cache (Insights.md B.3). ``nodes`` stores search
-- depth when the live path is depth-limited rather than node-limited.
CREATE TABLE IF NOT EXISTS position_cache (
    zobrist         TEXT NOT NULL,
    fen             TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    maia_version    TEXT NOT NULL DEFAULT '',
    nodes           INTEGER NOT NULL,
    features        TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (zobrist, engine_version, maia_version, nodes)
);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("CHESS_TRAINER_DB", DEFAULT_DB_PATH))


# DB paths whose schema/migrations have already been ensured this process.
# Running the full schema + migrations on every request-scoped connection was
# needless write traffic and a lock-contention source.
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    in_memory = path == Path(":memory:")
    if not in_memory:
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if in_memory:
        # Every :memory: connection is a brand-new database.
        ensure_app_schema(connection)
        return connection

    connection.execute("PRAGMA journal_mode = WAL")
    key = str(path.resolve())
    with _SCHEMA_LOCK:
        if key not in _SCHEMA_READY:
            ensure_app_schema(connection)
            _SCHEMA_READY.add(key)
    return connection


def ensure_app_schema(connection: sqlite3.Connection) -> None:
    ensure_positions_schema(connection)
    connection.executescript(APP_SCHEMA)
    _migrate_add_columns(connection)


def _migrate_add_columns(connection: sqlite3.Connection) -> None:
    """Add columns introduced after a table's first creation.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so columns
    added to a schema definition won't reach a DB that predates them (e.g. the
    shipped ``data/trainer.db``). Add them idempotently here.
    """

    existing = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    # SQLite can't ALTER ADD COLUMN with UNIQUE, so add plain columns and back the
    # email uniqueness with a separate index (NULLs stay distinct, so the legacy
    # null-email 'default' row is unaffected).
    for column in ("chesscom_username", "email", "password_hash", "password_salt"):
        if column not in existing:
            connection.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)"
    )
    insight_cols = {
        row["name"] for row in connection.execute("PRAGMA table_info(insight_runs)")
    }
    if insight_cols and "source" not in insight_cols:
        connection.execute(
            "ALTER TABLE insight_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'chesscom'"
        )
    if insight_cols:
        for column, decl in (
            ("stage", "TEXT"),
            ("stage_detail", "TEXT"),
            ("games_total", "INTEGER DEFAULT 0"),
        ):
            if column not in insight_cols:
                connection.execute(
                    f"ALTER TABLE insight_runs ADD COLUMN {column} {decl}"
                )
    connection.commit()


def get_singleton_user(connection: sqlite3.Connection) -> sqlite3.Row:
    """Return the one app user, creating it on first call."""

    row = connection.execute(
        "SELECT * FROM users WHERE username = ?",
        (SINGLETON_USERNAME,),
    ).fetchone()
    if row is not None:
        return row

    connection.execute(
        "INSERT INTO users (username, selected_openings) VALUES (?, ?)",
        (SINGLETON_USERNAME, "[]"),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM users WHERE username = ?",
        (SINGLETON_USERNAME,),
    ).fetchone()


def parse_openings(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [o for o in decoded if isinstance(o, str) and o in VALID_OPENINGS]


def serialize_openings(openings: list[str]) -> str:
    cleaned = [o for o in openings if o in VALID_OPENINGS]
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for o in cleaned:
        if o not in seen:
            ordered.append(o)
            seen.add(o)
    return json.dumps(ordered)
