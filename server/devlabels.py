"""Dev tab: manual calibration labels over every stored review position.

The tab is a labelling harness, not a product surface. It walks
``review_moves`` — every ply of every completed review the user owns — shows
the volatility we stored and the findability we stored, and records a verdict
on each ("about right", "should be higher", …) so the constants behind them can
be refit against human judgement.

Two things about the data matter and are easy to get wrong:

* **Findability scores the best move, not the move played** (see the review UI
  note in ``CLAUDE.md``). Every row therefore carries ``best_uci``/``best_san``
  from the stored MultiPV top line, and the label means "this *best move* was
  easier/harder to find than the number says".
* **Findability only exists at full tier.** Shallow rows (what Insights ingests
  at) carry volatility and nothing else, so a row's two labels are independent
  and either may be absent.

Scores are snapshotted onto the label row at write time: a refit changes what
``review_moves.findability`` says, and a verdict is only interpretable against
the number it was given.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Ordered low → high in the direction each scale runs, so the UI can lay both
# rows out left-to-right as "the number should be smaller" → "larger" and mean
# the same thing in both. ``delta`` is that direction as a signed step, which is
# what a fitter wants.
VOLATILITY_LABELS: dict[str, int] = {
    "way_lower": -2,
    "lower": -1,
    "about_right": 0,
    "higher": 1,
    "way_higher": 2,
}

# Findability is "how easy is this to find", so a *higher* score means easier.
# "way_harder" therefore asks for a much lower number, keeping the sign
# convention identical to volatility's.
FINDABILITY_LABELS: dict[str, int] = {
    "way_harder": -2,
    "harder": -1,
    "about_right": 0,
    "easier": 1,
    "way_easier": 2,
}

SCOPES = ("all", "unlabeled", "labeled")
REQUIRES = ("any", "volatility", "findability")

MAX_NOTE = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_detail(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _where_clauses(scope: str, require: str) -> tuple[str, list[Any]]:
    """SQL fragment + params shared by the listing and its COUNT."""

    clauses = ["r.user_id = ?", "r.status = 'complete'"]
    params: list[Any] = []

    if require == "volatility":
        clauses.append("rm.volatility IS NOT NULL")
    elif require == "findability":
        clauses.append("rm.findability IS NOT NULL")
    else:
        clauses.append("(rm.volatility IS NOT NULL OR rm.findability IS NOT NULL)")

    if scope == "unlabeled":
        clauses.append(
            "(dl.volatility_label IS NULL AND dl.findability_label IS NULL)"
        )
    elif scope == "labeled":
        clauses.append(
            "(dl.volatility_label IS NOT NULL OR dl.findability_label IS NOT NULL)"
        )

    return " AND ".join(clauses), params


# The join is written once: the listing and its COUNT must filter identically or
# the client's "position 40 of 12" progress counter drifts.
_FROM = """
FROM review_moves rm
JOIN reviews r ON r.review_id = rm.review_id
JOIN games g ON g.game_id = r.game_id
LEFT JOIN dev_labels dl
       ON dl.user_id = r.user_id AND dl.review_id = rm.review_id AND dl.ply = rm.ply
"""

# Stable across pages and across sessions: a labelling pass that reshuffles
# under you is unusable. ``review_id`` breaks ties between reviews created in
# the same second.
_ORDER = "ORDER BY r.created_at, rm.review_id, rm.ply"


def count_positions(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    scope: str = "all",
    require: str = "any",
) -> int:
    where, params = _where_clauses(scope, require)
    row = connection.execute(
        f"SELECT COUNT(*) AS n {_FROM} WHERE {where}", (user_id, *params)
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def list_positions(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    scope: str = "all",
    require: str = "any",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """One page of labellable positions, newest review last."""

    where, params = _where_clauses(scope, require)
    rows = connection.execute(
        f"""
        SELECT rm.review_id, rm.ply, rm.san, rm.is_user_move, rm.phase,
               rm.classification, rm.win_prob, rm.delta_w, rm.volatility,
               rm.findability, rm.findability_personal, rm.r_find, rm.detail,
               r.game_id, r.user_color, r.depth_tier, r.constants_version,
               g.white_name, g.black_name, g.white_rating, g.black_rating,
               g.result, g.played_at, g.opening_name,
               dl.volatility_label, dl.findability_label, dl.note,
               dl.updated_at AS labeled_at
        {_FROM}
        WHERE {where}
        {_ORDER}
        LIMIT ? OFFSET ?
        """,
        (user_id, *params, max(1, limit), max(0, offset)),
    ).fetchall()
    return [_serialize(row, index=offset + i) for i, row in enumerate(rows)]


def _serialize(row: sqlite3.Row, *, index: int) -> dict[str, Any]:
    detail = _parse_detail(row["detail"])
    top_lines = detail.get("top_lines")
    top_lines = top_lines if isinstance(top_lines, list) else []
    best = top_lines[0] if top_lines and isinstance(top_lines[0], dict) else {}
    return {
        "index": index,
        "review_id": row["review_id"],
        "game_id": row["game_id"],
        "ply": int(row["ply"]),
        "san": row["san"],
        "is_user_move": bool(row["is_user_move"]),
        "phase": row["phase"],
        "classification": row["classification"],
        "win_prob": row["win_prob"],
        "delta_w": row["delta_w"],
        "eval_cp": detail.get("eval_cp"),
        "fen": detail.get("fen_before"),
        "fen_after": detail.get("fen_after"),
        "move_uci": detail.get("move_uci"),
        "best_uci": best.get("uci"),
        "best_san": best.get("san"),
        "best_eval_cp": best.get("eval_cp"),
        # Trimmed: the panel shows the shortlist the volatility number came
        # from, not the whole MultiPV payload.
        "top_lines": [
            {"uci": t.get("uci"), "san": t.get("san"), "eval_cp": t.get("eval_cp")}
            for t in top_lines[:6]
            if isinstance(t, dict)
        ],
        "volatility": row["volatility"],
        "findability": row["findability"],
        "findability_personal": row["findability_personal"],
        "r_find": row["r_find"],
        "depth_tier": row["depth_tier"],
        "constants_version": row["constants_version"],
        "user_color": row["user_color"],
        "white_name": row["white_name"],
        "black_name": row["black_name"],
        "white_rating": row["white_rating"],
        "black_rating": row["black_rating"],
        "result": row["result"],
        "played_at": row["played_at"],
        "opening_name": row["opening_name"],
        "volatility_label": row["volatility_label"],
        "findability_label": row["findability_label"],
        "note": row["note"],
        "labeled_at": row["labeled_at"],
    }


def find_move(
    connection: sqlite3.Connection,
    user_id: int,
    review_id: str,
    ply: int,
) -> sqlite3.Row | None:
    """The stored review move, ownership-checked against ``user_id``."""

    return connection.execute(
        """
        SELECT rm.volatility, rm.findability, rm.detail, r.constants_version
        FROM review_moves rm
        JOIN reviews r ON r.review_id = rm.review_id
        WHERE r.user_id = ? AND rm.review_id = ? AND rm.ply = ?
        """,
        (user_id, review_id, ply),
    ).fetchone()


def upsert_label(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    review_id: str,
    ply: int,
    move: sqlite3.Row,
    volatility_label: str | None,
    findability_label: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Write (or clear) one position's verdicts.

    ``None`` for a label clears it — the UI's selected button is a toggle, so
    clicking the current verdict again takes it back off. A row with both
    labels cleared and no note is deleted rather than left as a tombstone,
    which keeps the "unlabeled" scope honest.
    """

    detail = _parse_detail(move["detail"])
    top_lines = detail.get("top_lines")
    best = (
        top_lines[0]
        if isinstance(top_lines, list) and top_lines and isinstance(top_lines[0], dict)
        else {}
    )
    note = (note or "").strip()[:MAX_NOTE] or None

    if volatility_label is None and findability_label is None and note is None:
        connection.execute(
            "DELETE FROM dev_labels WHERE user_id = ? AND review_id = ? AND ply = ?",
            (user_id, review_id, ply),
        )
        connection.commit()
        return {
            "review_id": review_id,
            "ply": ply,
            "volatility_label": None,
            "findability_label": None,
            "note": None,
            "cleared": True,
        }

    connection.execute(
        """
        INSERT INTO dev_labels (
            user_id, review_id, ply, fen, move_uci, best_uci, volatility,
            findability, volatility_label, findability_label, note,
            constants_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, review_id, ply) DO UPDATE SET
            fen=excluded.fen,
            move_uci=excluded.move_uci,
            best_uci=excluded.best_uci,
            volatility=excluded.volatility,
            findability=excluded.findability,
            volatility_label=excluded.volatility_label,
            findability_label=excluded.findability_label,
            note=excluded.note,
            constants_version=excluded.constants_version,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            review_id,
            ply,
            detail.get("fen_before"),
            detail.get("move_uci"),
            best.get("uci"),
            move["volatility"],
            move["findability"],
            volatility_label,
            findability_label,
            note,
            move["constants_version"],
            _now_iso(),
        ),
    )
    connection.commit()
    return {
        "review_id": review_id,
        "ply": ply,
        "volatility_label": volatility_label,
        "findability_label": findability_label,
        "note": note,
        "cleared": False,
    }


def label_stats(connection: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Per-verdict counts — the labelling pass's own progress bar."""

    rows = connection.execute(
        "SELECT volatility_label, findability_label FROM dev_labels WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    volatility = dict.fromkeys(VOLATILITY_LABELS, 0)
    findability = dict.fromkeys(FINDABILITY_LABELS, 0)
    for row in rows:
        if row["volatility_label"] in volatility:
            volatility[row["volatility_label"]] += 1
        if row["findability_label"] in findability:
            findability[row["findability_label"]] += 1
    return {
        "labeled_positions": len(rows),
        "volatility": volatility,
        "findability": findability,
        "volatility_total": sum(volatility.values()),
        "findability_total": sum(findability.values()),
    }


def export_labels(connection: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """Every label with the position and the score it was judged against.

    This is the tuning input: FEN, the move findability actually scored, the
    numbers the model produced, and the signed direction a human says they
    should move in.
    """

    rows = connection.execute(
        """
        SELECT dl.*, g.game_id, r.user_color, r.depth_tier, rm.san, rm.phase,
               rm.classification, rm.delta_w, rm.win_prob
        FROM dev_labels dl
        JOIN reviews r ON r.review_id = dl.review_id
        JOIN games g ON g.game_id = r.game_id
        LEFT JOIN review_moves rm
               ON rm.review_id = dl.review_id AND rm.ply = dl.ply
        WHERE dl.user_id = ?
        ORDER BY dl.updated_at
        """,
        (user_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "review_id": row["review_id"],
            "game_id": row["game_id"],
            "ply": row["ply"],
            "san": row["san"],
            "phase": row["phase"],
            "classification": row["classification"],
            "fen": row["fen"],
            "move_uci": row["move_uci"],
            "best_uci": row["best_uci"],
            "win_prob": row["win_prob"],
            "delta_w": row["delta_w"],
            "volatility": row["volatility"],
            "findability": row["findability"],
            "volatility_label": row["volatility_label"],
            "volatility_delta": VOLATILITY_LABELS.get(row["volatility_label"] or ""),
            "findability_label": row["findability_label"],
            "findability_delta": FINDABILITY_LABELS.get(row["findability_label"] or ""),
            "note": row["note"],
            "depth_tier": row["depth_tier"],
            "constants_version": row["constants_version"],
            "labeled_at": row["updated_at"],
        })
    return out
