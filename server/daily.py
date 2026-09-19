"""Daily check-in: five timed phases, one session per day, one calendar.

The check-in is a **conductor, not a sixth trainer**. Four of its five phases
are modes that already exist and already own their state; this module owns only
the questions those modes cannot answer — which phase you are on today, how long
you have spent in it, and whether today counts. Each phase carries the route it
hands off to, and the running clock follows the player into that tab.

Two rules shape everything here:

* **Five minutes is a floor, not a cap.** ``target_seconds`` is what the ring
  fills to; nothing stops at it and nothing is discarded past it. A phase ends
  when the player says it ends.
* **The day is the player's day, not the server's.** The client sends its local
  date because only it knows the timezone. The value is clamped to ±1 day of
  UTC so a wrong clock cannot mint a week of streak, which is the only abuse
  worth defending against in a personal trainer.

Pure over its connection: no engine, no network. The review phase picks a game
from rows the reviews already stored.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

#: How long a phase's ring takes to fill. Five minutes each, so the whole
#: check-in is a 25-minute floor — short enough to do before work, which is the
#: only length a daily habit survives at.
TARGET_SECONDS = 300

#: Largest time jump one heartbeat may add. The client ticks every few seconds;
#: anything larger is a throttled background tab or a machine that slept, and
#: crediting it would turn "left the tab open overnight" into eight hours of
#: practice.
MAX_HEARTBEAT_SECONDS = 120

PENDING = "pending"
ACTIVE = "active"
DONE = "done"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Phase:
    """One step of the check-in, and where the player goes to do it."""

    key: str
    title: str
    blurb: str
    route: str
    #: Which existing mode this hands off to, for the UI's own labelling.
    mode: str


PHASES: tuple[Phase, ...] = (
    Phase(
        key="openings",
        title="Opening repertoire",
        blurb="Your own lines, drilled — the ones you play most and the ones "
        "costing you the most.",
        route="/daily/repertoire",
        mode="repertoire",
    ),
    Phase(
        key="puzzles",
        title="Puzzles",
        blurb="Tactic or quiet move? You are not told which until you have "
        "played.",
        route="/puzzles",
        mode="puzzles",
    ),
    Phase(
        key="endgame",
        title="Endgame Arena",
        blurb="Hold it, convert it or save it — against a Maia picked to make "
        "it hard.",
        route="/training/endgame",
        mode="endgame",
    ),
    Phase(
        key="mistakes",
        title="Puzzles from your games",
        blurb="Your own missed wins and blunders, replayed as puzzles.",
        route="/training/mistakes",
        mode="mistakes",
    ),
    Phase(
        key="review",
        title="One loss, reviewed",
        blurb="A game you lost, opened at the move it turned.",
        route="/game-review",
        mode="review",
    ),
)

PHASE_KEYS: tuple[str, ...] = tuple(p.key for p in PHASES)
PHASE_BY_KEY: dict[str, Phase] = {p.key: p for p in PHASES}


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #


def normalize_day(raw: str | None) -> str:
    """The client's local date, clamped to ±1 day of UTC today.

    Without the clamp a stale or wrong clock writes sessions into arbitrary
    dates and the calendar stops describing anything. Without *accepting* the
    client's date at all, anyone west of UTC starts a new day mid-evening.
    """

    today = datetime.now(timezone.utc).date()
    if not raw:
        return today.isoformat()
    try:
        parsed = date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return today.isoformat()
    low, high = today - timedelta(days=1), today + timedelta(days=1)
    return min(max(parsed, low), high).isoformat()


def month_bounds(month: str | None) -> tuple[str, str, str]:
    """``(month, first_day, last_day)`` for a ``YYYY-MM`` string."""

    today = datetime.now(timezone.utc).date()
    try:
        year, mon = (int(part) for part in str(month).split("-")[:2])
        first = date(year, mon, 1)
    except (ValueError, TypeError):
        first = date(today.year, today.month, 1)
    nxt = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return (f"{first.year:04d}-{first.month:02d}", first.isoformat(), last.isoformat())


# --------------------------------------------------------------------------- #
# Session state                                                                #
# --------------------------------------------------------------------------- #


def get_or_create_session(
    connection: sqlite3.Connection,
    user_id: int,
    day: str,
) -> sqlite3.Row:
    """Today's session, created with its five phase rows on first touch."""

    row = connection.execute(
        "SELECT * FROM daily_sessions WHERE user_id = ? AND day = ?",
        (int(user_id), day),
    ).fetchone()
    if row is not None:
        _ensure_phase_rows(connection, int(row["id"]), int(user_id))
        return row

    connection.execute(
        "INSERT INTO daily_sessions (user_id, day) VALUES (?, ?)",
        (int(user_id), day),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM daily_sessions WHERE user_id = ? AND day = ?",
        (int(user_id), day),
    ).fetchone()
    _ensure_phase_rows(connection, int(row["id"]), int(user_id))
    return row


def _ensure_phase_rows(
    connection: sqlite3.Connection, session_id: int, user_id: int
) -> None:
    """Idempotently create the phase rows a session is missing.

    Separate from session creation so adding a sixth phase later fills itself
    in on an existing day rather than leaving that day permanently short of
    one step it can never complete.
    """

    have = {
        str(r["phase"])
        for r in connection.execute(
            "SELECT phase FROM daily_phase_runs WHERE session_id = ?",
            (session_id,),
        )
    }
    missing = [p for p in PHASES if p.key not in have]
    if not missing:
        return
    connection.executemany(
        """
        INSERT OR IGNORE INTO daily_phase_runs
            (session_id, user_id, phase, status, target_seconds)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        [(session_id, int(user_id), p.key, TARGET_SECONDS) for p in missing],
    )
    connection.commit()


def phase_rows(
    connection: sqlite3.Connection, session_id: int
) -> list[sqlite3.Row]:
    rows = {
        str(r["phase"]): r
        for r in connection.execute(
            "SELECT * FROM daily_phase_runs WHERE session_id = ?",
            (int(session_id),),
        )
    }
    # Return them in the order the check-in runs, not the order SQLite hands
    # them back — the sequence is the product.
    return [rows[key] for key in PHASE_KEYS if key in rows]


def start_phase(
    connection: sqlite3.Connection,
    session_id: int,
    phase: str,
    *,
    payload: dict[str, Any] | None = None,
) -> sqlite3.Row:
    """Make one phase the live one. Any other active phase drops to pending.

    One clock at a time is the point: two phases both "active" would both
    accumulate heartbeats and the day's total would be fiction.
    """

    if phase not in PHASE_BY_KEY:
        raise ValueError(f"unknown phase: {phase!r}")
    connection.execute(
        """
        UPDATE daily_phase_runs SET status = 'pending'
        WHERE session_id = ? AND status = 'active' AND phase != ?
        """,
        (int(session_id), phase),
    )
    # A finished phase re-entered goes back to active: the player may keep
    # practising past the point they marked it done, and that time is real.
    connection.execute(
        """
        UPDATE daily_phase_runs
           SET status = 'active',
               started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
               payload = COALESCE(?, payload)
         WHERE session_id = ? AND phase = ?
        """,
        (json.dumps(payload) if payload is not None else None, int(session_id), phase),
    )
    connection.execute(
        "UPDATE daily_sessions SET started_at = COALESCE(started_at, CURRENT_TIMESTAMP)"
        " WHERE id = ?",
        (int(session_id),),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM daily_phase_runs WHERE session_id = ? AND phase = ?",
        (int(session_id), phase),
    ).fetchone()


def add_seconds(
    connection: sqlite3.Connection,
    session_id: int,
    phase: str,
    seconds: float,
) -> int:
    """Credit time to a phase. Returns its new total."""

    if phase not in PHASE_BY_KEY:
        raise ValueError(f"unknown phase: {phase!r}")
    delta = max(0, min(int(MAX_HEARTBEAT_SECONDS), int(seconds or 0)))
    if delta:
        connection.execute(
            """
            UPDATE daily_phase_runs SET seconds_spent = seconds_spent + ?
             WHERE session_id = ? AND phase = ?
            """,
            (delta, int(session_id), phase),
        )
        _resync(connection, session_id)
    row = connection.execute(
        "SELECT seconds_spent FROM daily_phase_runs WHERE session_id = ? AND phase = ?",
        (int(session_id), phase),
    ).fetchone()
    return int(row["seconds_spent"]) if row else 0


def finish_phase(
    connection: sqlite3.Connection,
    session_id: int,
    phase: str,
    *,
    status: str = DONE,
    seconds: float = 0.0,
) -> None:
    """Mark a phase done or skipped, crediting any last unflushed time."""

    if phase not in PHASE_BY_KEY:
        raise ValueError(f"unknown phase: {phase!r}")
    if status not in (DONE, SKIPPED):
        raise ValueError(f"unknown status: {status!r}")
    if seconds:
        add_seconds(connection, session_id, phase, seconds)
    connection.execute(
        """
        UPDATE daily_phase_runs
           SET status = ?, completed_at = CURRENT_TIMESTAMP
         WHERE session_id = ? AND phase = ?
        """,
        (status, int(session_id), phase),
    )
    _resync(connection, session_id)


def reopen_phase(
    connection: sqlite3.Connection, session_id: int, phase: str
) -> None:
    """Undo a done/skipped phase back to pending (a mis-tap is not a verdict)."""

    if phase not in PHASE_BY_KEY:
        raise ValueError(f"unknown phase: {phase!r}")
    connection.execute(
        """
        UPDATE daily_phase_runs SET status = 'pending', completed_at = NULL
         WHERE session_id = ? AND phase = ?
        """,
        (int(session_id), phase),
    )
    _resync(connection, session_id)


def _resync(connection: sqlite3.Connection, session_id: int) -> None:
    """Roll the phase rows up onto the session row.

    Denormalized deliberately: the calendar reads one row per day for a whole
    month, and joining five phase rows per cell to recount them every time is
    the kind of query that is fine at ten days and silly at a year.
    """

    rows = phase_rows(connection, session_id)
    done = sum(1 for r in rows if str(r["status"]) == DONE)
    total = sum(int(r["seconds_spent"] or 0) for r in rows)
    complete = done == len(PHASES)
    connection.execute(
        """
        UPDATE daily_sessions
           SET phases_done = ?, seconds_total = ?,
               completed_at = CASE WHEN ? THEN COALESCE(completed_at, CURRENT_TIMESTAMP)
                                   ELSE NULL END
         WHERE id = ?
        """,
        (done, total, 1 if complete else 0, int(session_id)),
    )
    connection.commit()


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #


def session_payload(
    connection: sqlite3.Connection, session: sqlite3.Row
) -> dict[str, Any]:
    rows = phase_rows(connection, int(session["id"]))
    phases = []
    for row in rows:
        spec = PHASE_BY_KEY[str(row["phase"])]
        try:
            payload = json.loads(row["payload"]) if row["payload"] else None
        except (json.JSONDecodeError, TypeError):
            payload = None
        phases.append(
            {
                "key": spec.key,
                "title": spec.title,
                "blurb": spec.blurb,
                "route": spec.route,
                "mode": spec.mode,
                "status": str(row["status"]),
                "seconds_spent": int(row["seconds_spent"] or 0),
                "target_seconds": int(row["target_seconds"] or TARGET_SECONDS),
                "payload": payload,
            }
        )
    done = sum(1 for p in phases if p["status"] == DONE)
    active = next((p["key"] for p in phases if p["status"] == ACTIVE), None)
    return {
        "session_id": int(session["id"]),
        "day": str(session["day"]),
        "phases": phases,
        "phases_done": done,
        "phases_total": len(PHASES),
        "complete": done == len(PHASES),
        "seconds_total": sum(p["seconds_spent"] for p in phases),
        "target_total": len(PHASES) * TARGET_SECONDS,
        "active_phase": active,
        "next_phase": next(
            (p["key"] for p in phases if p["status"] in (PENDING, ACTIVE)), None
        ),
    }


# --------------------------------------------------------------------------- #
# Calendar and streak                                                          #
# --------------------------------------------------------------------------- #


def month_days(
    connection: sqlite3.Connection, user_id: int, month: str | None
) -> dict[str, Any]:
    """Every day of one month with a session, as calendar cells."""

    label, first, last = month_bounds(month)
    rows = connection.execute(
        """
        SELECT id, day, phases_done, seconds_total, completed_at
        FROM daily_sessions
        WHERE user_id = ? AND day >= ? AND day <= ?
        ORDER BY day
        """,
        (int(user_id), first, last),
    ).fetchall()
    days = []
    for row in rows:
        done_keys = [
            str(r["phase"])
            for r in connection.execute(
                "SELECT phase FROM daily_phase_runs WHERE session_id = ? AND status = 'done'",
                (int(row["id"]),),
            )
        ]
        days.append(
            {
                "day": str(row["day"]),
                "phases_done": int(row["phases_done"] or 0),
                "phases_total": len(PHASES),
                "seconds_total": int(row["seconds_total"] or 0),
                "complete": int(row["phases_done"] or 0) == len(PHASES),
                "done_phases": [k for k in PHASE_KEYS if k in done_keys],
            }
        )
    return {
        "month": label,
        "first_day": first,
        "last_day": last,
        "days": days,
    }


def streaks(
    connection: sqlite3.Connection, user_id: int, today: str
) -> dict[str, Any]:
    """Current and best run of fully-completed days.

    Today not being finished yet does not break the streak — it has not been
    missed until it is over — so the current run is counted from today when
    today is complete and from yesterday otherwise.
    """

    rows = connection.execute(
        """
        SELECT day FROM daily_sessions
        WHERE user_id = ? AND phases_done >= ?
        ORDER BY day
        """,
        (int(user_id), len(PHASES)),
    ).fetchall()
    complete = [str(r["day"]) for r in rows]
    return {
        "current": _current_streak(complete, today),
        "best": _best_streak(complete),
        "total_days": len(complete),
    }


def _current_streak(complete: Sequence[str], today: str) -> int:
    have = set(complete)
    try:
        cursor = date.fromisoformat(today)
    except ValueError:
        return 0
    if cursor.isoformat() not in have:
        cursor -= timedelta(days=1)
    run = 0
    while cursor.isoformat() in have:
        run += 1
        cursor -= timedelta(days=1)
    return run


def _best_streak(complete: Iterable[str]) -> int:
    best = 0
    run = 0
    previous: date | None = None
    for raw in sorted(set(complete)):
        try:
            current = date.fromisoformat(raw)
        except ValueError:
            continue
        run = run + 1 if previous is not None and current - previous == timedelta(days=1) else 1
        previous = current
        best = max(best, run)
    return best


# --------------------------------------------------------------------------- #
# The review phase's pick                                                      #
# --------------------------------------------------------------------------- #


def pick_loss(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    exclude_recent: int = 14,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """A reviewed game the player lost, opened at the move it turned.

    Recently-shown games are a *preference*, not a filter — the same rule the
    Endgame Arena settled on. ``exclude_recent`` is a count of previous review
    phases, not days: a player with three reviewed losses would be told
    "nothing available" by a hard exclusion, which is exactly when the phase
    matters most.
    """

    rng = rng or random.Random()
    rows = connection.execute(
        """
        SELECT r.review_id   AS review_id,
               r.game_id     AS game_id,
               r.user_color  AS user_color,
               g.result      AS result,
               g.white_name  AS white_name,
               g.black_name  AS black_name,
               g.opening_name AS opening_name,
               g.played_at   AS played_at
        FROM reviews r
        JOIN games g ON g.game_id = r.game_id
        WHERE r.user_id = ? AND r.status = 'complete'
        GROUP BY r.game_id
        """,
        (int(user_id),),
    ).fetchall()
    losses = [
        row
        for row in rows
        if (str(row["result"]) == "1-0" and str(row["user_color"]) == "black")
        or (str(row["result"]) == "0-1" and str(row["user_color"]) == "white")
    ]
    if not losses:
        return None

    seen_rows = connection.execute(
        """
        SELECT payload FROM daily_phase_runs
        WHERE user_id = ? AND phase = 'review' AND payload IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (int(user_id), max(1, int(exclude_recent))),
    ).fetchall()
    recent = {gid for gid in (_payload_game(r) for r in seen_rows) if gid}
    fresh = [row for row in losses if str(row["game_id"]) not in recent]
    pool = fresh or losses
    # Prefer a loss whose review actually scored a move, so the phase can open
    # where the game turned instead of at move one. Also a preference, not a
    # filter: a shallow-only history still gets a game to look at.
    located = [
        row for row in pool if _worst_miss(connection, str(row["review_id"]))[0]
    ]
    choice = rng.choice(located or pool)
    ply, san, delta = _worst_miss(connection, str(choice["review_id"]))
    return {
        "game_id": str(choice["game_id"]),
        "review_id": str(choice["review_id"]),
        "user_color": str(choice["user_color"]),
        "white_name": choice["white_name"],
        "black_name": choice["black_name"],
        "opening": choice["opening_name"],
        "played_at": choice["played_at"],
        "ply": ply,
        "san": san,
        "delta_w": delta,
        "repeat": not fresh,
    }


def _payload_game(row: sqlite3.Row) -> str | None:
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    game_id = payload.get("game_id")
    return str(game_id) if game_id else None


def _worst_miss(
    connection: sqlite3.Connection, review_id: str
) -> tuple[int | None, str | None, float | None]:
    """The player's costliest move in that game — where the review should open.

    Dropping someone at move 1 of a game they already played makes them hunt
    for the moment; ``review_moves.ply`` is 1-based and is exactly what
    ``__volOpenGameById`` wants.
    """

    row = connection.execute(
        """
        SELECT ply, san, delta_w FROM review_moves
        WHERE review_id = ? AND is_user_move = 1 AND delta_w IS NOT NULL
        ORDER BY delta_w DESC LIMIT 1
        """,
        (str(review_id),),
    ).fetchone()
    if row is None:
        return (None, None, None)
    return (int(row["ply"]), str(row["san"]), float(row["delta_w"]))
