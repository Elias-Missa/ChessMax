"""Guess the Eval Duels — head-to-head evaluation-guessing game.

Two players are shown the *same* chess position and have 1 minute to guess
its engine evaluation (in centipawns / eval points); the closest guess wins.

Layering (mirrors ``server/guess_elo.py``):
* **Pure logic** — ``decide_winner``, ``guess_points``, ``bot_guess`` — unit-tested.
* **Data access** — ``eval_positions`` pool and ``eval_duels`` records.
* **Pool auto-seeding** — seeds from ``positions`` DB or built-in curated positions.
* **Matchmaking** — in-process waiting room that pairs two humans when both search,
  falling back to a bot opponent after 6 seconds so a duel is always available.
"""

from __future__ import annotations

import math
import random
import sqlite3
import threading
import time
from dataclasses import dataclass

EVAL_MIN = -1000  # -10.00 eval
EVAL_MAX = 1000   # +10.00 eval
DUEL_SECONDS = 60  # 1 minute countdown
BOT_WAIT_SECONDS = 6.0
WAIT_STALE_SECONDS = 20.0

SEED_POSITIONS = [
    ("4k3/p2p1p1p/1p1b1p2/8/8/1P1B1P2/P2P1P1P/4K3 w - - 0 1", 0, "Opposite-colored bishops draw"),
    ("2r2rk1/pb1n2pp/8/4Q3/8/7q/5bPP/7K w - - 0 1", 0, "Stalemate swindle setup"),
    ("4k3/8/8/4P3/4P3/4K3/8/8 w - - 0 1", 300, "Technical passed pawns (+3.00)"),
    ("2r3k1/5ppp/4p3/3pP3/3PN3/PR4P1/5P1P/6K1 w - - 0 1", 300, "Sharp tactical win (+3.00)"),
    ("8/8/4k3/4p3/4p3/8/8/4K3 w - - 0 1", -300, "Decided losing endgame (-3.00)"),
    ("r5k1/pp3ppp/8/4N3/8/q6P/PP3PP1/3R3K w - - 0 1", -300, "Swindle counterplay (-3.00)"),
    ("r1bq1rk1/pp3ppp/2n1pn2/2pp4/3P4/2PBPN2/PP1N1PPP/R2Q1RK1 w - - 0 1", 35, "Equal middlegame (+0.35)"),
    ("r1b1kb1r/pppp1ppp/5n2/4q3/8/4P3/PPP2PPP/RNBQKB1R w KQkq - 0 6", 120, "White development lead (+1.20)"),
    ("8/5pk1/4p1p1/3n3p/8/5P2/5KPP/8 w - - 0 40", -180, "Black knight endgame advantage (-1.80)"),
    ("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1", 25, "Standard opening (+0.25)"),
    ("3r2k1/p4ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", 900, "Back-rank mate threat (+9.00)"),
    ("r1bqk2r/pppp1ppp/2n2n2/4p3/1b2P3/2N2N2/PPPP1PPP/R1BQKB1R w KQkq - 4 5", 10, "Four Knights opening (+0.10)"),
]


# --------------------------------------------------------------------------- #
# Pure scoring                                                                  #
# --------------------------------------------------------------------------- #


def clamp_eval_guess(value: int | float) -> int:
    return max(EVAL_MIN, min(EVAL_MAX, int(round(value))))


def guess_points(true_eval_cp: int, guess_cp: int | None) -> int:
    """0–100 accuracy points for an eval guess; smooth decay with centipawn error."""
    if guess_cp is None:
        return 0
    delta = abs(int(true_eval_cp) - int(guess_cp))
    return round(100.0 * math.exp(-delta / 120.0))


def decide_winner(true_eval_cp: int, guess_a: int | None, guess_b: int | None) -> str:
    """Return ``"a"`` / ``"b"`` / ``"draw"`` — closest guess to ``true_eval_cp`` wins."""
    if guess_a is None and guess_b is None:
        return "draw"
    if guess_a is None:
        return "b"
    if guess_b is None:
        return "a"
    da, db = abs(true_eval_cp - guess_a), abs(true_eval_cp - guess_b)
    if da < db:
        return "a"
    if db < da:
        return "b"
    return "draw"


def bot_guess(true_eval_cp: int, rng: random.Random, *, sigma: float = 120.0) -> int:
    """A competitive-but-beatable bot guess: Gaussian noise around the true eval,
    rounded to the nearest 10cp (0.10 eval) and clamped."""
    raw = rng.gauss(true_eval_cp, sigma)
    return clamp_eval_guess(int(round(raw / 10.0) * 10))


# --------------------------------------------------------------------------- #
# Data access & Pool management                                                #
# --------------------------------------------------------------------------- #


def pool_size(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM eval_positions").fetchone()[0])


def ensure_eval_positions_pool(conn: sqlite3.Connection) -> int:
    """Ensure ``eval_positions`` has entries, auto-seeding from ``positions`` or defaults."""
    count = pool_size(conn)
    if count >= 10:
        return count

    added = 0
    # 1. Try seeding from positions table if available
    try:
        rows = conn.execute(
            "SELECT fen, best_eval FROM positions WHERE best_eval IS NOT NULL LIMIT 40"
        ).fetchall()
        for r in rows:
            eval_cp = int(round(float(r["best_eval"])))
            eval_cp = max(EVAL_MIN, min(EVAL_MAX, eval_cp))
            conn.execute(
                "INSERT INTO eval_positions (fen, true_eval_cp, source) VALUES (?, ?, 'positions_db')",
                (r["fen"], eval_cp),
            )
            added += 1
    except Exception:
        pass

    # 2. Fallback to curated seed positions if still short
    if pool_size(conn) < 5:
        for fen, eval_cp, comment in SEED_POSITIONS:
            conn.execute(
                "INSERT INTO eval_positions (fen, true_eval_cp, source) VALUES (?, ?, ?)",
                (fen, int(eval_cp), f"curated_{comment}"),
            )
            added += 1

    conn.commit()
    return pool_size(conn)


def pick_random_position(
    conn: sqlite3.Connection, rng: random.Random | None = None
) -> sqlite3.Row | None:
    ensure_eval_positions_pool(conn)
    return conn.execute("SELECT * FROM eval_positions ORDER BY RANDOM() LIMIT 1").fetchone()


def create_duel(
    conn: sqlite3.Connection,
    pos: sqlite3.Row,
    *,
    player_a: int,
    player_b: int | None,
    is_bot: bool,
    now: float,
    rng: random.Random,
) -> int:
    guess_b = bot_guess(int(pos["true_eval_cp"]), rng) if is_bot else None
    cur = conn.execute(
        """
        INSERT INTO eval_duels
            (position_id, true_eval_cp, player_a, player_b, is_bot, guess_b, deadline_ts, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            int(pos["id"]),
            int(pos["true_eval_cp"]),
            int(player_a),
            player_b,
            1 if is_bot else 0,
            guess_b,
            int(now + DUEL_SECONDS),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_duel(conn: sqlite3.Connection, duel_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM eval_duels WHERE id = ?", (int(duel_id),)).fetchone()


def active_duel_for(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM eval_duels
        WHERE status = 'active' AND (player_a = ? OR player_b = ?)
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), int(user_id)),
    ).fetchone()


def side_of(duel: sqlite3.Row, user_id: int) -> str | None:
    if int(duel["player_a"]) == int(user_id):
        return "a"
    if duel["player_b"] is not None and int(duel["player_b"]) == int(user_id):
        return "b"
    return None


def submit_guess(
    conn: sqlite3.Connection, duel: sqlite3.Row, side: str, guess_cp: int, now: float
) -> sqlite3.Row:
    column = "guess_a" if side == "a" else "guess_b"
    conn.execute(
        f"UPDATE eval_duels SET {column} = ? WHERE id = ?",
        (clamp_eval_guess(guess_cp), int(duel["id"])),
    )
    conn.commit()
    return maybe_resolve(conn, get_duel(conn, int(duel["id"])), now)


def maybe_resolve(conn: sqlite3.Connection, duel: sqlite3.Row, now: float) -> sqlite3.Row:
    if duel["status"] != "active":
        return duel
    both_in = duel["guess_a"] is not None and duel["guess_b"] is not None
    expired = now >= int(duel["deadline_ts"])
    if not (both_in or expired):
        return duel
    winner = decide_winner(int(duel["true_eval_cp"]), duel["guess_a"], duel["guess_b"])
    conn.execute(
        "UPDATE eval_duels SET status = 'done', winner = ? WHERE id = ?",
        (winner, int(duel["id"])),
    )
    conn.commit()
    return get_duel(conn, int(duel["id"]))


# --------------------------------------------------------------------------- #
# Matchmaking                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class _Waiter:
    since: float


_WAITING: dict[int, _Waiter] = {}
_WAITING_LOCK = threading.Lock()


def _clear_stale(now: float) -> None:
    for uid, waiter in list(_WAITING.items()):
        if now - waiter.since > WAIT_STALE_SECONDS:
            del _WAITING[uid]


def leave_queue(user_id: int) -> None:
    with _WAITING_LOCK:
        _WAITING.pop(int(user_id), None)


def find_or_create_match(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
) -> tuple[str, sqlite3.Row | None]:
    now = time.time() if now is None else now
    rng = rng or random.Random()
    user_id = int(user_id)

    existing = active_duel_for(conn, user_id)
    if existing is not None:
        return "matched", existing

    with _WAITING_LOCK:
        _clear_stale(now)
        for other_id, waiter in list(_WAITING.items()):
            if other_id != user_id:
                del _WAITING[other_id]
                _WAITING.pop(user_id, None)
                paired = other_id
                break
        else:
            waited = now - _WAITING.setdefault(user_id, _Waiter(now)).since
            paired = None

    pos = pick_random_position(conn, rng)
    if pos is None:
        return "searching", None

    if paired is not None:
        duel_id = create_duel(
            conn, pos, player_a=paired, player_b=user_id, is_bot=False, now=now, rng=rng
        )
        return "matched", get_duel(conn, duel_id)

    if waited >= BOT_WAIT_SECONDS:
        leave_queue(user_id)
        duel_id = create_duel(
            conn, pos, player_a=user_id, player_b=None, is_bot=True, now=now, rng=rng
        )
        return "matched", get_duel(conn, duel_id)

    return "searching", None


# --------------------------------------------------------------------------- #
# Serialization                                                                 #
# --------------------------------------------------------------------------- #


def duel_public(
    conn: sqlite3.Connection, duel: sqlite3.Row, user_id: int
) -> dict[str, object]:
    side = side_of(duel, user_id)
    pos = conn.execute(
        "SELECT fen FROM eval_positions WHERE id = ?", (int(duel["position_id"]),)
    ).fetchone()
    my_guess = duel["guess_a"] if side == "a" else duel["guess_b"]
    opp_guess = duel["guess_b"] if side == "a" else duel["guess_a"]
    done = duel["status"] == "done"
    payload: dict[str, object] = {
        "duel_id": int(duel["id"]),
        "you": side,
        "status": duel["status"],
        "deadline_ts": int(duel["deadline_ts"]),
        "is_bot": bool(duel["is_bot"]),
        "opponent": "Bot" if duel["is_bot"] else "Opponent",
        "fen": pos["fen"] if pos else "start",
        "your_guess": my_guess,
        "opponent_guessed": opp_guess is not None,
        "eval_min": EVAL_MIN,
        "eval_max": EVAL_MAX,
    }
    if done:
        winner_side = duel["winner"]
        outcome = "draw" if winner_side == "draw" else ("win" if winner_side == side else "loss")
        payload.update(
            {
                "true_eval_cp": int(duel["true_eval_cp"]),
                "opponent_guess": opp_guess,
                "winner": winner_side,
                "outcome": outcome,
                "your_points": guess_points(int(duel["true_eval_cp"]), my_guess),
                "opponent_points": guess_points(int(duel["true_eval_cp"]), opp_guess),
            }
        )
    return payload


__all__ = [
    "BOT_WAIT_SECONDS",
    "DUEL_SECONDS",
    "EVAL_MAX",
    "EVAL_MIN",
    "active_duel_for",
    "bot_guess",
    "clamp_eval_guess",
    "create_duel",
    "decide_winner",
    "duel_public",
    "ensure_eval_positions_pool",
    "find_or_create_match",
    "get_duel",
    "guess_points",
    "leave_queue",
    "maybe_resolve",
    "pick_random_position",
    "pool_size",
    "submit_guess",
]
