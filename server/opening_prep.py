"""What to prep next, from the openings that are actually costing you.

Insights already ranks openings by **return on practice**
(``server/insights_pro.py:compute_opening_roi``): exposure × deficit-below-par ×
confidence, in win% lost per 100 games, with an attribution term that demotes a
line you score badly in but *play accurately* — because studying that line
returns nothing, you lost those games somewhere else. That ranking is the right
answer to "what should I work on"; it just had nowhere to go. This module is the
bridge from that answer to the builder.

Two things it has to work out that the ROI row does not carry:

* **Where in the line to start.** A row says "Sicilian Najdorf as White, 12
  games, 7.4 points per 100". It does not say which position to open. So the
  games behind the row are replayed and their **common prefix** is taken: the
  deepest position the player reaches in (nearly) every game of that opening.
  That is where their preparation demonstrably runs out and improvisation
  starts, which is exactly the position worth a decision.
* **Whether they already did something about it.** A leak the book already
  answers is not a to-do. The suggestion carries the authored book's coverage at
  the prep position, so a line already built reads as done rather than nagging.

Pure-ish: reads the database, no engine and no network. The ROI numbers are
taken as given — this module never re-ranks, because two rankings that can
disagree is worse than one that can be wrong.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

import chess
import chess.pgn

from core.repertoire import BLACK, WHITE, canonical_fen, position_key
from server import openings_build

#: How deep a prep line is allowed to run. Past this it stops being an opening
#: decision and becomes a middlegame you happen to have reached — the same
#: reasoning (and the same number) as ``core/constants/repertoire.json``.
MAX_PREP_PLIES = 16

#: A move has to appear in at least this share of the line's games to count as
#: "what you play here". Below it the player is already improvising, and the
#: position *before* the disagreement is the one worth prepping.
PREFIX_SHARE = 0.6

#: Never suggest a line worth less than this. PLACEHOLDER, in win% per 100
#: games — matching the leak board's own floor for reporting an opening leak.
MIN_POINTS_PER_100 = 1.0

#: How many suggestions to return. The point is a short to-do list; a ranked
#: list of twenty is the same as no ranking at all.
MAX_SUGGESTIONS = 5


def latest_metrics(
    connection: sqlite3.Connection, user_id: int
) -> tuple[str | None, dict[str, Any]]:
    """The newest complete run's metrics blob, or ``({}, None)`` if there is none."""

    try:
        row = connection.execute(
            """
            SELECT run_id, metrics FROM insight_runs
            WHERE user_id = ? AND status = 'complete' AND metrics IS NOT NULL
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    except sqlite3.Error:
        return None, {}
    if row is None:
        return None, {}
    try:
        return str(row["run_id"]), json.loads(row["metrics"]) or {}
    except (json.JSONDecodeError, TypeError):
        return str(row["run_id"]), {}


def roi_rows(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ROI table out of a metrics blob, newest schema first.

    Defensive about shape: Insights runs are immutable snapshots kept for ten
    generations, so an older row may predate ``pro.openings.roi`` entirely and
    must read as "no suggestions" rather than raising.
    """

    pro = metrics.get("pro")
    if not isinstance(pro, Mapping):
        return []
    openings = pro.get("openings")
    if not isinstance(openings, Mapping):
        return []
    roi = openings.get("roi")
    if not isinstance(roi, Mapping):
        return []
    rows = roi.get("rows")
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


def game_mainlines(
    connection: sqlite3.Connection,
    game_ids: Sequence[str],
    *,
    max_plies: int = MAX_PREP_PLIES,
) -> list[list[str]]:
    """UCI mainlines for the given games, truncated to ``max_plies``.

    A game whose PGN will not parse is skipped rather than failing the batch —
    the ingest accepts whatever chess.com and lichess send, and one malformed
    archive row should not cost the user their whole prep list.
    """

    if not game_ids:
        return []
    placeholders = ",".join("?" for _ in game_ids)
    try:
        rows = connection.execute(
            f"SELECT game_id, pgn FROM games WHERE game_id IN ({placeholders})",
            tuple(game_ids),
        ).fetchall()
    except sqlite3.Error:
        return []

    out: list[list[str]] = []
    for row in rows:
        pgn = str(row["pgn"] or "")
        if not pgn.strip():
            continue
        try:
            game = chess.pgn.read_game(io.StringIO(pgn))
        except Exception:  # noqa: BLE001 — a bad archive row is not an error here
            continue
        if game is None:
            continue
        board = game.board()
        line: list[str] = []
        for move in game.mainline_moves():
            if len(line) >= max_plies:
                break
            if move not in board.legal_moves:
                break
            line.append(move.uci())
            board.push(move)
        if line:
            out.append(line)
    return out


def common_prefix(
    lines: Sequence[Sequence[str]], *, share: float = PREFIX_SHARE
) -> list[str]:
    """The line the player actually walks: moves agreed by ``share`` of games.

    Not a strict common prefix. Requiring unanimity makes one odd game cut the
    prefix to nothing, and the position before *that* is not where preparation
    ran out — it is where one game went strange. A super-majority is the honest
    reading of "this is my line".
    """

    if not lines:
        return []
    prefix: list[str] = []
    alive = [list(line) for line in lines]
    depth = 0
    while alive:
        at_depth = [line[depth] for line in alive if len(line) > depth]
        if not at_depth:
            break
        move, count = Counter(at_depth).most_common(1)[0]
        # Measured against the games still in the line, not against all games:
        # a branch that only half the games reach is still that half's main line.
        if count / len(at_depth) < share:
            break
        prefix.append(move)
        alive = [line for line in alive if len(line) > depth and line[depth] == move]
        depth += 1
    return prefix


def line_to_position(prefix: Sequence[str]) -> tuple[str, list[str]]:
    """Replay a UCI prefix into ``(fen, sans)``. Stops at the first illegal move."""

    board = chess.Board()
    sans: list[str] = []
    for uci in prefix:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        sans.append(board.san(move))
        board.push(move)
    return board.fen(), sans


def _book_state(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str,
    fen: str,
) -> dict[str, Any]:
    """What the authored book already says at this position."""

    node = openings_build.find_node(connection, user_id, color=color, fen=fen)
    if node is None:
        return {"in_book": False, "chosen": []}
    edges = openings_build.load_edges(connection, user_id, int(node["id"]))
    return {"in_book": bool(edges), "chosen": [e["uci"] for e in edges]}


def _first_gap(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str,
    prefix: Sequence[str],
) -> int | None:
    """Ply index of the first position on this line the book does not answer.

    Only the player's *own* turns count: an unanswered opponent reply is not a
    hole in the repertoire, it is simply a branch not yet explored. Returns
    ``None`` when the book covers every decision on the line.
    """

    board = chess.Board()
    user_is_white = color == WHITE
    for index, uci in enumerate(prefix):
        on_move_is_user = (board.turn == chess.WHITE) == user_is_white
        if on_move_is_user:
            state = _book_state(connection, user_id, color=color, fen=board.fen())
            if not state["chosen"]:
                return index
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return index
        if move not in board.legal_moves:
            return index
        board.push(move)
    return None


def suggestions(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    limit: int = MAX_SUGGESTIONS,
    min_points: float = MIN_POINTS_PER_100,
    max_plies: int = MAX_PREP_PLIES,
) -> dict[str, Any]:
    """Prep suggestions, ranked by the ROI the Insights run already computed.

    Returns ``reason`` rather than an empty list when there is nothing to
    suggest — "run Insights first" and "your repertoire is fine" are different
    answers and the UI needs to tell them apart.
    """

    run_id, metrics = latest_metrics(connection, user_id)
    if run_id is None:
        return {
            "available": False,
            "reason": "Run Insights on your account and the lines costing you "
                      "most will show up here.",
            "run_id": None,
            "suggestions": [],
        }

    rows = [
        r
        for r in roi_rows(metrics)
        if float(r.get("points_per_100_games") or 0.0) >= min_points
    ]
    if not rows:
        return {
            "available": True,
            "reason": "Nothing in your repertoire is scoring below par for the "
                      "opponents you met.",
            "run_id": run_id,
            "suggestions": [],
        }

    # Never re-rank: `roi_score` is the ranking key Insights already decided on,
    # and a second ranking that can disagree with the dashboard is worse than
    # one that can be wrong.
    rows.sort(key=lambda r: -float(r.get("roi_score") or 0.0))

    out: list[dict[str, Any]] = []
    for row in rows[: limit * 3]:
        color = str(row.get("color") or WHITE)
        if color not in (WHITE, BLACK):
            continue
        lines = game_mainlines(
            connection, list(row.get("game_ids") or []), max_plies=max_plies
        )
        prefix = common_prefix(lines)
        fen, sans = line_to_position(prefix)
        state = _book_state(connection, user_id, color=color, fen=fen)
        gap = _first_gap(connection, user_id, color=color, prefix=prefix)

        out.append(
            {
                "opening": row.get("opening"),
                "eco": row.get("eco"),
                "color": color,
                "games": int(row.get("n") or 0),
                "share": float(row.get("share") or 0.0),
                "score_pct": row.get("score_pct"),
                "par_pct": row.get("par_pct"),
                "points_per_100_games": float(row.get("points_per_100_games") or 0.0),
                "accuracy_gap": row.get("accuracy_gap"),
                # Where to open the builder, and how the player got there.
                "line_uci": prefix,
                "line_san": sans,
                "fen": canonical_fen(fen),
                "position_key": position_key(fen),
                "plies": len(prefix),
                "in_book": state["in_book"],
                "chosen": state["chosen"],
                "first_gap_ply": gap,
                "why": _why(row, prefix, state, gap),
            }
        )
        if len(out) >= limit:
            break

    return {
        "available": True,
        "reason": None,
        "run_id": run_id,
        "suggestions": out,
    }


def _why(
    row: Mapping[str, Any],
    prefix: Sequence[str],
    state: Mapping[str, Any],
    gap: int | None,
) -> str:
    """One line the player can act on. No jargon — this is a product surface.

    Deliberately avoids Δw / volatility / findability, the same rule the
    narrative layer enforces (``test_no_jargon_reaches_the_narrative_tabs``).
    """

    score = row.get("score_pct")
    par = row.get("par_pct")
    points = float(row.get("points_per_100_games") or 0.0)
    games = int(row.get("n") or 0)

    head = (
        f"You score {round(float(score) * 100)}% here against "
        f"{round(float(par) * 100)}% par, over {games} game"
        f"{'' if games == 1 else 's'}."
        if score is not None and par is not None
        else f"Worth about {points:.1f} points per 100 games."
    )

    accuracy_gap = row.get("accuracy_gap")
    if isinstance(accuracy_gap, (int, float)) and accuracy_gap < -1:
        # ROI already demoted this, but say why so the ranking is legible.
        return (
            f"{head} You actually play it more accurately than usual, so the "
            "games were probably lost later — prep is unlikely to be the fix."
        )
    if not prefix:
        return f"{head} Your games diverge immediately — pick a first move and commit."
    if gap == 0:
        # Ply 0 is the root, so "no answer by move 1" is technically true and
        # reads as nonsense next to a three-move line. It means the book is
        # simply empty on this line.
        return f"{head} Nothing chosen in this line yet — start at move one."
    if gap is not None:
        return f"{head} Your book follows it to move {gap // 2 + 1}, then runs out."
    if state.get("in_book"):
        return f"{head} You have a line here already — drill it rather than rebuild it."
    return f"{head} Nothing chosen here yet; {points:.1f} points per 100 games at stake."


__all__ = [
    "MAX_PREP_PLIES",
    "MAX_SUGGESTIONS",
    "MIN_POINTS_PER_100",
    "PREFIX_SHARE",
    "common_prefix",
    "game_mainlines",
    "latest_metrics",
    "line_to_position",
    "roi_rows",
    "suggestions",
]
