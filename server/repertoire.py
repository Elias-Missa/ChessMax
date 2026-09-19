"""Assemble the opening repertoire from what the database already knows.

``core/repertoire.py`` owns the rules and stays pure; this module is the part
that talks to SQLite. It reads three things and nothing else:

* ``reviews`` + ``games`` — the player's own games, with the colour they had
  and the result, which is what the tree is built from.
* ``review_moves`` — the opening plies a review already scored, which is where
  a ``fix`` verdict's evidence comes from (the engine's move and the win% the
  player's habit costs). No engine is run here.
* the newest completed ``insight_runs`` — the opening ROI ranking, so the lines
  that cost the most points are drilled first.

Every one of those is optional. With no reviews the repertoire is still built
from the games and every node is ``keep`` or ``gap``; with no insights run the
lines fall back to frequency order. A player with no games at all gets an empty
repertoire and a reason, not an error.
"""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import chess
import chess.pgn

from core.repertoire import (
    BLACK,
    WHITE,
    DrillCard,
    GameLine,
    PositionEvidence,
    RepLine,
    RepertoireConstants,
    build_tree,
    cards_from_lines,
    extract_lines,
    fen_key,
    order_cards,
)
from server.insights_pro import user_points

#: Colours in the order the UI shows them. White first: it is the half of the
#: repertoire the player chooses, so it is where a decision is even available.
COLORS: tuple[str, ...] = (WHITE, BLACK)


# --------------------------------------------------------------------------- #
# Reading the player's games                                                   #
# --------------------------------------------------------------------------- #


def load_game_lines(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    constants: RepertoireConstants | None = None,
    limit: int = 500,
) -> list[GameLine]:
    """Every reviewed game of this user, as a truncated UCI mainline.

    A game reviewed at both tiers has two ``reviews`` rows; the ``GROUP BY``
    keeps one so the move counts are games and not reviews — without it a
    re-reviewed game would quietly count double and outrank a line the player
    actually plays more.
    """

    constants = constants or RepertoireConstants.load()
    rows = connection.execute(
        """
        SELECT r.game_id       AS game_id,
               r.user_color    AS user_color,
               g.pgn           AS pgn,
               g.result        AS result,
               g.opening_name  AS opening_name,
               g.eco           AS eco,
               COALESCE(g.played_at, r.created_at) AS played_at
        FROM reviews r
        JOIN games g ON g.game_id = r.game_id
        WHERE r.user_id = ? AND r.status = 'complete'
        GROUP BY r.game_id
        ORDER BY played_at DESC
        LIMIT ?
        """,
        (int(user_id), int(limit)),
    ).fetchall()

    lines: list[GameLine] = []
    for row in rows:
        color = str(row["user_color"] or "").lower()
        if color not in COLORS:
            continue
        moves = mainline_uci(row["pgn"], constants.max_plies)
        if not moves:
            continue
        lines.append(
            GameLine(
                game_id=str(row["game_id"]),
                user_color=color,
                moves=moves,
                points=user_points(row["result"], color),
                opening=row["opening_name"] or None,
                eco=row["eco"] or None,
            )
        )
    return lines


def mainline_uci(pgn: Any, max_plies: int) -> list[str]:
    """First ``max_plies`` mainline moves as UCI, or ``[]`` if the PGN is bad.

    A game that starts from a set-up position is skipped outright: its first
    move is not a first move, and folding it into the tree would file a
    middlegame under the Caro-Kann.
    """

    if not pgn:
        return []
    try:
        game = chess.pgn.read_game(io.StringIO(str(pgn)))
    except (ValueError, RuntimeError):
        return []
    if game is None:
        return []
    if game.headers.get("SetUp") == "1" or game.headers.get("FEN"):
        return []
    board = game.board()
    if board.fen() != chess.STARTING_FEN:
        return []
    out: list[str] = []
    try:
        for move in game.mainline_moves():
            if len(out) >= max_plies:
                break
            if move not in board.legal_moves:
                break
            out.append(move.uci())
            board.push(move)
    except (ValueError, RuntimeError):
        return out
    return out


# --------------------------------------------------------------------------- #
# Review evidence                                                              #
# --------------------------------------------------------------------------- #


def load_evidence(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    constants: RepertoireConstants | None = None,
) -> dict[str, PositionEvidence]:
    """What the stored reviews know about the player's opening positions.

    Keyed by :func:`core.repertoire.fen_key` so a position reached by
    transposition lands on one record. ``delta_w`` is already win% lost from
    the mover's side, so averaging it per move is the honest cost of the habit;
    plies a review never scored contribute nothing rather than a zero.
    """

    constants = constants or RepertoireConstants.load()
    rows = connection.execute(
        """
        SELECT m.delta_w AS delta_w, m.detail AS detail
        FROM review_moves m
        JOIN reviews r ON r.review_id = m.review_id
        WHERE r.user_id = ?
          AND r.status = 'complete'
          AND m.is_user_move = 1
          AND m.ply <= ?
        """,
        (int(user_id), int(constants.max_plies)),
    ).fetchall()

    losses: dict[str, dict[str, list[float]]] = {}
    best: dict[str, dict[tuple[str, str], int]] = {}
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        fen = detail.get("fen_before")
        played = detail.get("move_uci")
        if not fen or not played:
            continue
        key = fen_key(str(fen))
        if row["delta_w"] is not None:
            losses.setdefault(key, {}).setdefault(str(played), []).append(
                float(row["delta_w"])
            )
        top = detail.get("top_lines") or []
        if top and top[0].get("uci"):
            pair = (str(top[0]["uci"]), str(top[0].get("san") or ""))
            best.setdefault(key, {})
            best[key][pair] = best[key].get(pair, 0) + 1

    evidence: dict[str, PositionEvidence] = {}
    for key in set(losses) | set(best):
        by_move = losses.get(key, {})
        top_pair = None
        if key in best:
            top_pair = max(best[key].items(), key=lambda kv: (kv[1], kv[0][0]))[0]
        evidence[key] = PositionEvidence(
            best_uci=top_pair[0] if top_pair else None,
            best_san=(top_pair[1] or None) if top_pair else None,
            loss_by_uci={
                uci: sum(values) / len(values) for uci, values in by_move.items()
            },
            n_by_uci={uci: len(values) for uci, values in by_move.items()},
        )
    return evidence


# --------------------------------------------------------------------------- #
# Opening ROI (from the newest insights run)                                   #
# --------------------------------------------------------------------------- #


def load_roi(connection: sqlite3.Connection, user_id: int) -> dict[tuple[str, str], float]:
    """``(colour, opening name) -> roi_score`` from the latest completed run.

    ROI is the ranking key Insights already uses for "what to practise next"
    (``insights_pro.compute_opening_roi``); reusing it here is what keeps the
    daily drill and the dashboard from recommending different openings.
    """

    row = connection.execute(
        """
        SELECT metrics FROM insight_runs
        WHERE user_id = ? AND status = 'complete' AND metrics IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()
    if row is None:
        return {}
    try:
        metrics = json.loads(row["metrics"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    rows = (
        ((metrics.get("pro") or {}).get("openings") or {}).get("roi") or {}
    ).get("rows") or []
    out: dict[tuple[str, str], float] = {}
    for entry in rows:
        color = str(entry.get("color") or "").lower()
        opening = entry.get("opening")
        if color not in COLORS or not opening:
            continue
        out[(color, str(opening))] = float(entry.get("roi_score") or 0.0)
    return out


# --------------------------------------------------------------------------- #
# Attempt history                                                              #
# --------------------------------------------------------------------------- #


def load_attempt_history(
    connection: sqlite3.Connection, user_id: int
) -> dict[str, dict[str, Any]]:
    """Most recent attempt per card: ``{"correct": bool, "days_ago": float}``."""

    rows = connection.execute(
        """
        SELECT card_id, correct, timestamp
        FROM repertoire_attempts
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (int(user_id),),
    ).fetchall()
    now = datetime.now(timezone.utc)
    history: dict[str, dict[str, Any]] = {}
    for row in rows:
        card_id = str(row["card_id"])
        if card_id in history:
            continue
        history[card_id] = {
            "correct": bool(row["correct"]),
            "days_ago": _days_ago(row["timestamp"], now),
        }
    return history


def _days_ago(raw: Any, now: datetime) -> float:
    if not raw:
        return 0.0
    text = str(raw).replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp).total_seconds() / 86400.0)


# --------------------------------------------------------------------------- #
# The repertoire                                                               #
# --------------------------------------------------------------------------- #


def build_repertoire(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    constants: RepertoireConstants | None = None,
) -> dict[str, Any]:
    """The full repertoire payload: lines per colour, plus why it is thin.

    ``available`` is false with a stated reason rather than an empty page —
    a cold account has no games to mine, and "build one by reviewing games"
    is the actionable answer.
    """

    constants = constants or RepertoireConstants.load()
    games = load_game_lines(connection, user_id, constants=constants)
    if not games:
        return {
            "available": False,
            "reason": (
                "No reviewed games yet. Review a few of your games (or run "
                "Insights on your account) and your own openings will build "
                "themselves from them."
            ),
            "games": 0,
            "lines": {color: [] for color in COLORS},
            "counts": {"fix": 0, "gap": 0, "keep": 0},
        }

    evidence = load_evidence(connection, user_id, constants=constants)
    roi = load_roi(connection, user_id)

    lines_by_color: dict[str, list[RepLine]] = {}
    for color in COLORS:
        tree = build_tree(games, color=color, constants=constants)
        lines = extract_lines(
            tree, color=color, evidence=evidence, constants=constants
        )
        for line in lines:
            line.roi_score = roi.get((color, line.opening or ""), 0.0)
        # ROI first, then how often the line actually comes up. A line with no
        # ROI entry sorts on frequency alone rather than vanishing.
        lines.sort(key=lambda line: (-line.roi_score, -line.games, line.key))
        lines_by_color[color] = lines

    counts = {"fix": 0, "gap": 0, "keep": 0}
    for lines in lines_by_color.values():
        for line in lines:
            for move in line.moves:
                if move.verdict in counts:
                    counts[move.verdict] += 1

    by_color_games = {
        color: sum(1 for g in games if g.user_color == color) for color in COLORS
    }
    return {
        "available": any(lines_by_color[color] for color in COLORS),
        "reason": (
            None
            if any(lines_by_color[color] for color in COLORS)
            else (
                f"Your {len(games)} reviewed games do not repeat an opening "
                f"{constants.min_node_games} times yet, so there is no line to "
                "drill. Play or import a few more and it will fill in."
            )
        ),
        "games": len(games),
        "games_by_color": by_color_games,
        "reviewed_positions": len(evidence),
        "roi_openings": len(roi),
        "counts": counts,
        "lines": {
            color: [_line_payload(line) for line in lines_by_color[color]]
            for color in COLORS
        },
    }


def _line_payload(line: RepLine) -> dict[str, Any]:
    return {
        "color": line.color,
        "opening": line.opening,
        "eco": line.eco,
        "games": line.games,
        "score_pct": line.score_pct,
        "roi_score": line.roi_score,
        "key": line.key,
        "moves": [
            {
                "ply": move.ply,
                "fen": move.fen,
                "user_to_move": move.user_to_move,
                "uci": move.uci,
                "san": move.san,
                "games": move.games,
                "share": move.share,
                "score_pct": move.score_pct,
                "verdict": move.verdict,
                "recommended_uci": move.recommended_uci,
                "recommended_san": move.recommended_san,
                "mean_loss": move.mean_loss,
                "reason": move.reason,
                "alternatives": [
                    {
                        "uci": alt.uci,
                        "san": alt.san,
                        "games": alt.games,
                        "share": alt.share,
                        "score_pct": alt.score_pct,
                        "mean_loss": alt.mean_loss,
                    }
                    for alt in move.alternatives
                ],
            }
            for move in line.moves
        ],
    }


def build_drill(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    limit: int = 20,
    color: str | None = None,
    constants: RepertoireConstants | None = None,
) -> dict[str, Any]:
    """The ordered drill queue — what to practise, hardest-earned first."""

    constants = constants or RepertoireConstants.load()
    games = load_game_lines(connection, user_id, constants=constants)
    if not games:
        return {"available": False, "cards": [], "reason": _NO_GAMES, "counts": {}}

    evidence = load_evidence(connection, user_id, constants=constants)
    roi = load_roi(connection, user_id)
    history = load_attempt_history(connection, user_id)

    cards: list[DrillCard] = []
    for one in COLORS:
        if color and one != color:
            continue
        tree = build_tree(games, color=one, constants=constants)
        lines = extract_lines(tree, color=one, evidence=evidence, constants=constants)
        for line in lines:
            line.roi_score = roi.get((one, line.opening or ""), 0.0)
        lines.sort(key=lambda line: (-line.roi_score, -line.games, line.key))
        cards.extend(cards_from_lines(lines, constants=constants))

    ordered = order_cards(cards, history, constants=constants)
    counts = {"fix": 0, "gap": 0, "keep": 0}
    for card in ordered:
        if card.verdict in counts:
            counts[card.verdict] += 1
    return {
        "available": bool(ordered),
        "reason": None if ordered else _NO_LINES,
        "total": len(ordered),
        "counts": counts,
        "cards": [card.as_dict() for card in ordered[: max(1, int(limit))]],
    }


_NO_GAMES = (
    "No reviewed games yet. Review a few of your games (or run Insights on "
    "your account) and your own openings will build themselves from them."
)
_NO_LINES = (
    "Your games do not repeat an opening often enough to drill yet. A few "
    "more and the lines will appear."
)


def record_attempt(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    card_id: str,
    fen: str,
    expected_uci: str,
    played_uci: str,
    correct: bool,
    verdict: str | None = None,
) -> dict[str, Any]:
    """Log one drill answer. Every attempt is kept, not just the latest.

    The queue only reads the most recent attempt per card, but the history is
    what a future spaced-repetition schedule would be fitted on, and throwing
    it away now would make that impossible later.
    """

    connection.execute(
        """
        INSERT INTO repertoire_attempts (
            user_id, card_id, fen, expected_uci, played_uci, correct, verdict
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(card_id),
            str(fen),
            str(expected_uci),
            str(played_uci),
            1 if correct else 0,
            verdict,
        ),
    )
    connection.commit()
    row = connection.execute(
        """
        SELECT COUNT(*) AS n, SUM(correct) AS hits
        FROM repertoire_attempts WHERE user_id = ?
        """,
        (int(user_id),),
    ).fetchone()
    return {
        "correct": bool(correct),
        "attempts": int(row["n"] or 0),
        "hits": int(row["hits"] or 0),
    }


def san_of(fen: str, uci: str) -> str | None:
    """SAN for a UCI move in a position, or ``None`` if it is not legal there."""

    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    if move not in board.legal_moves:
        return None
    return board.san(move)
