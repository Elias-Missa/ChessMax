"""SQLite helpers for the FastAPI app.

The app is single-user. We keep the ``users`` table for ratings and attempt
history but auto-provision one row (the singleton) and resolve it server-side.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from pipeline.import_puzzles import ensure_positions_schema


DEFAULT_DB_PATH = Path("data") / "trainer.db"
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
"""


def default_db_path() -> Path:
    return Path(os.environ.get("CHESS_TRAINER_DB", DEFAULT_DB_PATH))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_app_schema(connection)
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
