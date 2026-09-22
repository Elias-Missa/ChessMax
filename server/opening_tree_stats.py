"""How you actually do at each position in your book.

The authored tree (``server/openings_build.py``) knows what you *decided*. It
knows nothing about what happened when you played it. This joins the two: every
node in the book is matched against the games you have reviewed that passed
through that exact position, and comes back with how you scored and how
accurately you played from there.

**Colour is a comparison, not a level.** A node is red when you do worse *there*
than you do everywhere else — not when you are simply a 1200 playing 1400s. So
both halves are measured against a baseline:

* **Results** against Elo expectancy for the opponents actually faced, falling
  back to the player's own overall score when a game carries no ratings. Raw win%
  would paint a whole repertoire red for anyone who plays up, which is the same
  mistake the opening ROI work already rejected for the leak board.
* **Accuracy** against the player's own accuracy *at the same ply*. Accuracy
  falls with depth — a first move is near-perfect for everybody — so comparing
  a shallow node against an all-depths mean makes the top of every tree glow
  green. Per-ply also keeps a careful player and a wild one comparable, since
  the question is always "is this line worse *for me*".

**Small samples are shrunk toward neutral, never hidden.** Two games is not
evidence that a line is a disaster, but it is also not nothing — a node reached
twice glows faintly rather than either screaming or vanishing. Same
``n/(n+prior)`` device the ROI board uses.

Positions are matched by ``position_key``, so a node reached by transposition
collects the games from every move order that reaches it — which is the whole
reason identity is the position rather than the path.

No engine and no network: every number here comes from reviews already stored.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any, Mapping, Sequence

import chess
import chess.pgn

from core.repertoire import position_key
from server.insights_pro import accuracy_for_delta_w, expected_score, user_points

#: How deep to walk a game when matching it against the book. Matches the
#: builder's own horizon; past it the position is not in the book anyway.
MAX_PLIES = 16

#: Shrinkage prior, in games. PLACEHOLDER. Higher than the ROI board's 4 because
#: a *node* is reached by far fewer games than an opening is, and a single
#: blow-out otherwise dominates the colour of a deep node.
PRIOR_GAMES = 6.0

#: How far from neutral a node has to land before it is worth colouring at all.
#: Below this it reads as "nothing to say here", which is most of a young book.
NEUTRAL_BAND = 0.04

#: Weight of results vs accuracy in the blended health. PLACEHOLDER: results are
#: what the player cares about, accuracy is the more stable estimate of it, and
#: an even split says neither is trusted over the other.
RESULT_WEIGHT = 0.5

#: Score deltas beyond this are clipped before blending. A node where you score
#: 40 points below expectancy over three games is not four times worse than one
#: 10 points below; it is "bad", and the scale should saturate.
SCORE_SPAN = 0.30

#: Accuracy points that count as a full swing, for the same reason.
ACCURACY_SPAN = 12.0


@dataclass
class NodeStats:
    games: int = 0
    points: float = 0.0
    expected: float = 0.0
    rated_games: int = 0
    accuracies: list[float] = field(default_factory=list)
    #: The plies those accuracies were played at, parallel to ``accuracies``.
    #: Needed because accuracy falls with depth: comparing a first move, which
    #: is trivially near-perfect, against the average over all depths makes
    #: every shallow node glow green for no reason at all.
    plies: list[int] = field(default_factory=list)

    @property
    def score_pct(self) -> float | None:
        return self.points / self.games if self.games else None

    @property
    def expected_pct(self) -> float | None:
        return self.expected / self.rated_games if self.rated_games else None

    @property
    def accuracy(self) -> float | None:
        return fmean(self.accuracies) if self.accuracies else None


def _review_rows(connection: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    try:
        return connection.execute(
            """
            SELECT r.review_id, r.user_color, r.user_rating,
                   g.pgn, g.result, g.white_rating, g.black_rating
            FROM reviews r JOIN games g ON g.game_id = r.game_id
            WHERE r.user_id = ? AND r.status = 'complete'
            """,
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return []


def _accuracy_by_ply(
    connection: sqlite3.Connection, review_ids: Sequence[str], *, max_plies: int
) -> dict[tuple[str, int], float]:
    """User-move accuracy per (review, ply), for the opening window only."""

    if not review_ids:
        return {}
    out: dict[tuple[str, int], float] = {}
    # Chunked: SQLite's variable limit is 999 by default and a busy account can
    # carry more reviews than that.
    for start in range(0, len(review_ids), 400):
        chunk = review_ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = connection.execute(
                f"""
                SELECT review_id, ply, delta_w, is_book
                FROM review_moves
                WHERE review_id IN ({placeholders})
                  AND is_user_move = 1 AND ply <= ?
                """,
                (*chunk, max_plies),
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            out[(str(row["review_id"]), int(row["ply"]))] = accuracy_for_delta_w(
                row["delta_w"], is_book=bool(row["is_book"])
            )
    return out


def collect(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str | None = None,
    max_plies: int = MAX_PLIES,
) -> tuple[dict[str, NodeStats], dict[str, Any]]:
    """``(stats_by_position_key, baseline)`` over the user's reviewed games.

    A game contributes to every position it passes through, so a node reached by
    two different move orders collects both — the point of keying on position.
    The user's accuracy is attributed to the position they played the move *in*,
    which is the position the tree node represents.
    """

    reviews = _review_rows(connection, user_id)
    if color is not None:
        reviews = [r for r in reviews if str(r["user_color"]) == color]
    if not reviews:
        return {}, {"accuracy": None, "score_shift": 0.0, "games": 0}

    accuracy = _accuracy_by_ply(
        connection, [str(r["review_id"]) for r in reviews], max_plies=max_plies
    )

    stats: dict[str, NodeStats] = {}
    all_accuracies: list[float] = []
    by_ply: dict[int, list[float]] = {}  # ply -> [sum, count]
    total_points = 0.0
    total_expected = 0.0
    rated = 0
    counted = 0

    for review in reviews:
        user_color = str(review["user_color"])
        points = user_points(review["result"], user_color)
        if points is None:
            continue  # unfinished game: no result to attribute
        counted += 1
        total_points += points

        opp_rating = (
            review["black_rating"] if user_color == "white" else review["white_rating"]
        )
        own_rating = (
            review["white_rating"] if user_color == "white" else review["black_rating"]
        ) or review["user_rating"]
        expectancy = expected_score(
            int(own_rating) if own_rating else None,
            int(opp_rating) if opp_rating else None,
        )
        if expectancy is not None:
            total_expected += expectancy
            rated += 1

        try:
            game = chess.pgn.read_game(io.StringIO(str(review["pgn"] or "")))
        except Exception:  # noqa: BLE001 — one bad archive row is not an error
            continue
        if game is None:
            continue

        board = game.board()
        review_id = str(review["review_id"])
        seen: set[str] = set()

        def visit(key: str) -> NodeStats:
            entry = stats.setdefault(key, NodeStats())
            # A repetition inside the window must not count the game twice.
            if key not in seen:
                seen.add(key)
                entry.games += 1
                entry.points += points
                if expectancy is not None:
                    entry.expected += expectancy
                    entry.rated_games += 1
            return entry

        for index, move in enumerate(game.mainline_moves()):
            if index >= max_plies:
                break
            if move not in board.legal_moves:
                break
            entry = visit(position_key(board.fen()))
            # `review_moves.ply` is 1-based (see reviews_api / dev labels).
            acc = accuracy.get((review_id, index + 1))
            if acc is not None:
                entry.accuracies.append(acc)
                entry.plies.append(index + 1)
                all_accuracies.append(acc)
                bucket = by_ply.setdefault(index + 1, [0.0, 0])
                bucket[0] += acc
                bucket[1] += 1
            board.push(move)
        # The position the game *ended* the window in is reached too, and a leaf
        # of the book is usually exactly that position. Crediting only positions
        # a move was played *from* leaves every deepest node with zero games.
        visit(position_key(board.fen()))

    baseline = {
        "accuracy": fmean(all_accuracies) if all_accuracies else None,
        "score_pct": (total_points / counted) if counted else None,
        "score_shift": ((total_points - total_expected) / rated) if rated else 0.0,
        "games": counted,
        # Raw totals, so `health_for` can subtract a node's own contribution
        # before comparing it to them — see the leave-one-out note there.
        "_points": total_points,
        "_expected": total_expected,
        "_rated": rated,
        "_accuracy_sum": sum(all_accuracies),
        "_accuracy_n": len(all_accuracies),
        "_by_ply": by_ply,
    }
    return stats, baseline


def _clip(value: float, span: float) -> float:
    """Map a signed delta onto [-1, 1], saturating past ``span``."""

    if span <= 0:
        return 0.0
    return max(-1.0, min(1.0, value / span))


def health_for(
    entry: NodeStats,
    baseline: Mapping[str, Any],
    *,
    prior_games: float = PRIOR_GAMES,
    result_weight: float = RESULT_WEIGHT,
) -> dict[str, Any]:
    """Colour and the numbers behind it for one node.

    ``health`` is 0 (red) to 1 (green) with 0.5 neutral, and is ``None`` when
    there is nothing to say — no games, or a node whose two halves are both
    unmeasurable. ``None`` renders grey, which is not the same as 0.5 and must
    not be collapsed into it: "you have never been here" and "you do exactly
    averagely here" are different facts.
    """

    if entry.games <= 0:
        return {"health": None, "games": 0}

    # Both baselines are LEAVE-ONE-OUT: measured over every game *except* the
    # ones through this node. Without it a line that is most of what you play
    # sets the baseline it is judged against and scores a flat zero — the exact
    # failure `insights_pro.compute_opening_roi` already fixed the same way.
    rest_rated = int(baseline.get("_rated") or 0) - entry.rated_games
    shift = (
        (
            (float(baseline.get("_points") or 0.0) - entry.points)
            - (float(baseline.get("_expected") or 0.0) - entry.expected)
        )
        / rest_rated
        if rest_rated > 0
        else 0.0
    )

    score_pct = entry.score_pct
    expected = entry.expected_pct
    if expected is not None:
        par: float | None = float(expected) + shift
    elif rest_rated > 0 or baseline.get("score_pct") is None:
        par = baseline.get("score_pct")
    else:
        # No ratings anywhere and no other games: nothing to compare against.
        par = None
    score_delta = (
        None if (score_pct is None or par is None) else float(score_pct) - float(par)
    )

    # Accuracy is compared PER PLY, leave-one-out. A first move is near-perfect
    # for everyone, so measuring it against the mean over all depths says every
    # shallow node is played unusually well — which is an artefact of depth, not
    # a fact about the repertoire.
    acc = entry.accuracy
    by_ply: Mapping[int, Sequence[float]] = baseline.get("_by_ply") or {}
    expected_acc: list[float] = []
    for ply, own in zip(entry.plies, entry.accuracies):
        bucket = by_ply.get(ply)
        if not bucket:
            continue
        rest_n = int(bucket[1]) - 1
        if rest_n <= 0:
            continue
        expected_acc.append((float(bucket[0]) - own) / rest_n)
    base_acc = fmean(expected_acc) if expected_acc else None
    acc_delta = (
        None if (acc is None or base_acc is None) else float(acc) - float(base_acc)
    )

    parts: list[tuple[float, float]] = []
    if score_delta is not None:
        parts.append((_clip(score_delta, SCORE_SPAN), result_weight))
    if acc_delta is not None:
        parts.append((_clip(acc_delta, ACCURACY_SPAN), 1.0 - result_weight))
    if not parts:
        return {"health": None, "games": entry.games}

    weight_total = sum(w for _, w in parts)
    raw = sum(v * w for v, w in parts) / weight_total if weight_total else 0.0

    # Shrink toward neutral by sample size: two games is not evidence of a
    # disaster, but it is not nothing either.
    confidence = entry.games / (entry.games + prior_games)
    shrunk = raw * confidence

    return {
        "health": round(0.5 + shrunk / 2.0, 4),
        "confidence": round(confidence, 3),
        "games": entry.games,
        "score_pct": None if score_pct is None else round(score_pct, 4),
        "par_pct": None if par is None else round(float(par), 4),
        "baseline_accuracy": None if base_acc is None else round(base_acc, 1),
        "score_delta": None if score_delta is None else round(score_delta, 4),
        "accuracy": None if acc is None else round(acc, 1),
        "accuracy_delta": None if acc_delta is None else round(acc_delta, 1),
        # Below the band the node is "nothing to say", which is most of a young
        # book — the UI draws those neutral rather than faintly tinted.
        "notable": abs(shrunk) >= NEUTRAL_BAND,
    }


def stats_for_nodes(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str,
    position_keys: Sequence[str],
    max_plies: int = MAX_PLIES,
) -> dict[str, Any]:
    """Per-node health for the given book positions, plus the baselines used."""

    collected, baseline = collect(
        connection, user_id, color=color, max_plies=max_plies
    )
    nodes = {
        key: health_for(collected.get(key, NodeStats()), baseline)
        for key in position_keys
    }
    return {
        "nodes": nodes,
        "baseline": {
            "accuracy": (
                None if baseline["accuracy"] is None else round(baseline["accuracy"], 1)
            ),
            "score_pct": (
                None
                if baseline.get("score_pct") is None
                else round(float(baseline["score_pct"]), 4)
            ),
            "score_shift": round(float(baseline.get("score_shift") or 0.0), 4),
            "games": baseline["games"],
        },
        "covered": sum(1 for v in nodes.values() if v.get("games")),
    }


__all__ = [
    "ACCURACY_SPAN",
    "MAX_PLIES",
    "NEUTRAL_BAND",
    "PRIOR_GAMES",
    "RESULT_WEIGHT",
    "SCORE_SPAN",
    "NodeStats",
    "collect",
    "health_for",
    "stats_for_nodes",
]
