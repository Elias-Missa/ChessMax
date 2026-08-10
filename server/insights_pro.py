"""Professional-tier Insights metrics.

``insights_metrics`` owns the Tier 1–3 catalogue from ``Insights.md``. This
module adds the layer a player actually reads first: headline KPIs, move-quality
mix, critical-moment performance, rating/accuracy timelines, an opening tree,
endgame conversion, resilience, blunder timing, and a ranked, *quantified* leak
board that turns every metric into "this costs you N win% per game".

Everything here is pure over already-persisted rows — no engine, no network — so
it re-runs for free whenever a run is recomputed.

Units, once, so nothing below has to restate them:

* ``review_moves.delta_w`` is an expected-points loss in **win% points (0–100)**.
* ``review_moves.win_prob`` is expected points **before** the move in ``[0, 1]``.
* ``reviews.accuracy`` is the user's CAPS2-style game accuracy in ``[0, 100]``.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from statistics import fmean, median, pstdev
from typing import Any

from chess_vol.game_review import move_accuracy
from server.insights_stats import (
    DISPLAY_FLOOR_N,
    RECENCY_HALF_LIFE_DAYS,
    performance_gap,
    recency_weight,
    shrink_buckets,
    weighted_mean,
    wilson_interval,
)

# ── Thresholds ────────────────────────────────────────────────────────────────
# One place, so the UI copy and the maths can never drift apart.

BLUNDER_DELTA_W = 25.0
MISTAKE_DELTA_W = 10.0
INACCURACY_DELTA_W = 5.0

CRITICAL_VOLATILITY = 60.0
QUIET_VOLATILITY = 35.0
#: A critical move is "handled" when it leaks less than this much win%.
CRITICAL_HANDLED_DELTA_W = 5.0

WINNING_WIN_PROB = 0.70
LOSING_WIN_PROB = 0.30
DECISIVE_WIN_PROB = 0.85

SCRAMBLE_CLOCK_SECONDS = 10.0

MOVE_NUMBER_BUCKETS = (
    ("1-10", 1, 10),
    ("11-20", 11, 20),
    ("21-30", 21, 30),
    ("31-40", 31, 40),
    ("41+", 41, 10_000),
)

_CASTLE_RE = re.compile(r"^(O-O-O|O-O|0-0-0|0-0)")
_PIECE_FROM_SAN = {"N": "knight", "B": "bishop", "R": "rook", "Q": "queen", "K": "king"}


# ── Shared row helpers ────────────────────────────────────────────────────────
# ``insights_metrics`` imports these; the dependency runs one way only
# (insights_metrics → insights_pro) so there is no import cycle.


def user_won(result: str | None, user_color: str) -> bool:
    if result == "1-0":
        return user_color == "white"
    if result == "0-1":
        return user_color == "black"
    return False


def user_drew(result: str | None) -> bool:
    return result == "1/2-1/2"


def user_points(result: str | None, user_color: str) -> float | None:
    """Actual score for the user: 1 / 0.5 / 0, or ``None`` for unfinished games."""

    if user_drew(result):
        return 0.5
    if result in ("1-0", "0-1"):
        return 1.0 if user_won(result, user_color) else 0.0
    return None


def parse_detail(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def hour_from_played_at(played_at: Any, pgn: Any) -> int | None:
    if played_at:
        text = str(played_at)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt).hour
            except ValueError:
                continue
    if pgn:
        for tag in ("UTCTime", "Time"):
            match = re.search(rf'\[{tag}\s+"(\d{{2}}):(\d{{2}}):(\d{{2}})"\]', str(pgn))
            if match:
                return int(match.group(1))
    return None


def parse_dt(played_at: Any, pgn: Any) -> datetime | None:
    if played_at:
        text = str(played_at)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y.%m.%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y.%m.%d",
        ):
            try:
                return datetime.strptime(text[: len(fmt) + 2], fmt).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
    hour = hour_from_played_at(played_at, pgn)
    if played_at and hour is not None:
        try:
            day = datetime.strptime(str(played_at)[:10].replace(".", "-"), "%Y-%m-%d")
            return day.replace(hour=hour, tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def castle_side(moves: list[Any]) -> str | None:
    for m in moves:
        san = (m["san"] or "").replace("0", "O")
        match = _CASTLE_RE.match(san)
        if match:
            return "queenside" if match.group(1) == "O-O-O" else "kingside"
    return None


def accuracy_for_delta_w(delta_w: float | None, *, is_book: bool = False) -> float:
    """Per-move accuracy from a win%-point loss.

    Delegates to ``chess_vol.game_review.move_accuracy`` so the review tab and
    Insights can never disagree about what "accuracy" means.
    """

    if is_book:
        return 100.0
    return move_accuracy(max(0.0, float(delta_w or 0.0)) / 100.0)


def _mean(xs: list[float]) -> float | None:
    return fmean(xs) if xs else None


def _rate(hits: float, total: float) -> float:
    return (hits / total) if total else 0.0


def _score_pct(score: float, decided: int) -> float | None:
    """Score fraction in ``[0, 1]``; ``None`` when nothing was decided."""

    return (score / decided) if decided else None


def bucket_performance(facts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Score for a subset of games, expectation-adjusted (spec Phase 2).

    A raw win rate is partly a statement about who you happened to face, so no
    bucket in the report reports one alone: every one carries the points gained
    or lost against what the rating gaps predicted, its Wilson interval, and
    whether it clears the display floor.
    """

    decided = [f for f in facts if f.get("points") is not None]
    n = len(decided)
    score = sum(float(f["points"]) for f in decided)
    return {
        "n": n,
        "score": score,
        "score_pct": _score_pct(score, n),
        "ci": list(wilson_interval(score, n) or ()) or None,
        "expectation": performance_gap(decided),
        "below_floor": n < DISPLAY_FLOOR_N,
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── Elo maths ─────────────────────────────────────────────────────────────────


def expected_score(user_rating: int | None, opp_rating: int | None) -> float | None:
    """Standard Elo expectancy for one game."""

    if user_rating is None or opp_rating is None:
        return None
    return 1.0 / (1.0 + 10 ** ((opp_rating - user_rating) / 400.0))


def rating_difference(score_fraction: float) -> float:
    """FIDE ``dp``: the rating gap a given score fraction implies.

    Clamped to ±800 because a clean sweep is mathematically infinite and
    a headline number of "+∞" helps nobody.
    """

    p = min(0.999, max(0.001, float(score_fraction)))
    return max(-800.0, min(800.0, -400.0 * math.log10(1.0 / p - 1.0)))


def performance_rating(
    opponent_ratings: list[int], score: float, games: int
) -> int | None:
    """Average-opponent performance rating over the window."""

    if not opponent_ratings or games <= 0:
        return None
    return int(round(fmean(opponent_ratings) + rating_difference(score / games)))


# ── Per-game fact table ───────────────────────────────────────────────────────


def build_game_facts(
    game_rows: list[Any],
    moves_by_review: dict[str, list[Any]],
) -> list[dict[str, Any]]:
    """One rich row per analyzed game.

    This is the client's filtering substrate: every dashboard aggregate that can
    be recomputed at game granularity is derived from these rows, so the
    colour/outcome/opponent filters re-aggregate the whole page instead of only
    the game list.
    """

    enriched: list[dict[str, Any]] = []
    for row in game_rows:
        rid = str(row["review_id"])
        color = row["user_color"] or "white"
        result = row["result"]
        points = user_points(result, color)
        outcome = (
            "win" if points == 1.0 else "draw" if points == 0.5 else
            "loss" if points == 0.0 else "unfinished"
        )

        all_moves = moves_by_review.get(rid, [])
        user_moves = [m for m in all_moves if m["is_user_move"]]
        opp_moves = [m for m in all_moves if not m["is_user_move"]]

        phase_loss: dict[str, float] = defaultdict(float)
        phase_moves: dict[str, int] = defaultdict(int)
        phase_acc: dict[str, list[float]] = defaultdict(list)
        counts: dict[str, int] = defaultdict(int)
        accuracies: list[float] = []
        total_delta_w = 0.0
        blunders = mistakes = inaccuracies = 0
        critical_moves = 0
        critical_delta_w = 0.0
        critical_handled = 0
        quiet_moves = 0
        quiet_acc: list[float] = []
        crit_acc: list[float] = []
        scramble_moves = 0
        scramble_delta_w = 0.0
        findable_delta_w = 0.0
        findable_moves = 0
        deviation_ply: int | None = None
        first_error_move: int | None = None
        worst: dict[str, Any] | None = None
        crit_times: list[float] = []
        quiet_times: list[float] = []

        for m in user_moves:
            dw = float(m["delta_w"] or 0.0)
            is_book = bool(m["is_book"])
            phase = str(m["phase"] or "middlegame")
            acc = accuracy_for_delta_w(dw, is_book=is_book)
            ply = int(m["ply"] or 0)
            move_no = (ply + 1) // 2

            total_delta_w += dw
            accuracies.append(acc)
            phase_loss[phase] += dw
            phase_moves[phase] += 1
            phase_acc[phase].append(acc)
            counts[str(m["classification"] or "unclassified")] += 1

            if dw >= BLUNDER_DELTA_W:
                blunders += 1
            elif dw >= MISTAKE_DELTA_W:
                mistakes += 1
            elif dw >= INACCURACY_DELTA_W:
                inaccuracies += 1

            if not is_book and deviation_ply is None:
                deviation_ply = ply
            if first_error_move is None and dw >= MISTAKE_DELTA_W:
                first_error_move = move_no

            vol = m["volatility"]
            if vol is not None:
                v = float(vol)
                if v >= CRITICAL_VOLATILITY:
                    critical_moves += 1
                    critical_delta_w += dw
                    crit_acc.append(acc)
                    if dw < CRITICAL_HANDLED_DELTA_W:
                        critical_handled += 1
                    if m["time_spent"] is not None:
                        crit_times.append(float(m["time_spent"]))
                elif v <= QUIET_VOLATILITY:
                    quiet_moves += 1
                    quiet_acc.append(acc)
                    if m["time_spent"] is not None:
                        quiet_times.append(float(m["time_spent"]))

            clock = m["clock_remaining"]
            if clock is not None and float(clock) < SCRAMBLE_CLOCK_SECONDS:
                scramble_moves += 1
                scramble_delta_w += dw

            findability = m["findability"]
            if findability is not None:
                findable_moves += 1
                if int(findability) > 60:
                    findable_delta_w += dw

            if worst is None or dw > worst["delta_w"]:
                detail = parse_detail(m["detail"])
                lines = detail.get("top_lines") or []
                worst = {
                    "ply": ply,
                    "move_no": move_no,
                    "san": m["san"],
                    "delta_w": dw,
                    "findability": _int_or_none(findability),
                    "volatility": float(vol) if vol is not None else None,
                    "fen": detail.get("fen_before"),
                    "move_uci": detail.get("move_uci"),
                    "best_uci": (lines[0] or {}).get("uci") if lines else None,
                    "best_san": (lines[0] or {}).get("san") if lines else None,
                }

        win_probs = [
            float(m["win_prob"]) for m in user_moves if m["win_prob"] is not None
        ]
        traj = [float(m["win_prob"]) for m in all_moves if m["win_prob"] is not None]
        if len(traj) > 20:
            step = len(traj) / 20.0
            sparkline = [round(traj[int(i * step)], 3) for i in range(20)]
        else:
            sparkline = [round(v, 3) for v in traj]

        user_rating = _int_or_none(
            row["white_rating"] if color == "white" else row["black_rating"]
        )
        opp_rating = _int_or_none(
            row["black_rating"] if color == "white" else row["white_rating"]
        )
        opp_name = (row["black_name"] if color == "white" else row["white_name"]) or "Opponent"

        gap = None if (user_rating is None or opp_rating is None) else opp_rating - user_rating
        band = (
            "unknown" if gap is None
            else "lower" if gap <= -100
            else "higher" if gap >= 100
            else "similar"
        )

        mine = castle_side(user_moves)
        theirs = castle_side(opp_moves)
        dt = parse_dt(row["played_at"], row["pgn"])

        enriched.append({
            "review_id": rid,
            "game_id": row["game_id"],
            "played_at": row["played_at"],
            "timestamp": dt.isoformat() if dt else None,
            "hour": hour_from_played_at(row["played_at"], row["pgn"]),
            "user_color": color,
            "opponent": opp_name,
            "opponent_rating": opp_rating,
            "user_rating": user_rating,
            "rating_band": band,
            "rating_gap": gap,
            "result": result,
            "outcome": outcome,
            "points": points,
            "expected_points": expected_score(user_rating, opp_rating),
            "accuracy": float(row["accuracy"]) if row["accuracy"] is not None else _mean(accuracies),
            "eco": (row["eco"] or "").strip() or "Unknown",
            "opening_name": (row["opening_name"] or "").strip() or "Unknown opening",
            "ply_count": int(row["ply_count"] or 0),
            "user_moves": len(user_moves),
            "mean_volatility": float(row["mean_vol"]) if row["mean_vol"] is not None else None,
            "total_delta_w": total_delta_w,
            "findable_delta_w": findable_delta_w if findable_moves else None,
            "findable_moves": findable_moves,
            "blunders": blunders,
            "mistakes": mistakes,
            "inaccuracies": inaccuracies,
            "classification_counts": dict(counts),
            "phase_delta_w": {k: v for k, v in phase_loss.items()},
            "phase_moves": {k: v for k, v in phase_moves.items()},
            "phase_accuracy": {k: fmean(v) for k, v in phase_acc.items() if v},
            "critical_moves": critical_moves,
            "critical_delta_w": critical_delta_w,
            "critical_handled": critical_handled,
            "critical_accuracy": _mean(crit_acc),
            "quiet_moves": quiet_moves,
            "quiet_accuracy": _mean(quiet_acc),
            "critical_time": _mean(crit_times),
            "quiet_time": _mean(quiet_times),
            "scramble_moves": scramble_moves,
            "scramble_delta_w": scramble_delta_w,
            "castle_side": mine,
            "opponent_castle_side": theirs,
            "castle_relation": (
                None if mine is None or theirs is None
                else "same_side" if mine == theirs
                else "opposite_side"
            ),
            "peak_win_prob": max(win_probs) if win_probs else None,
            "trough_win_prob": min(win_probs) if win_probs else None,
            "deviation_ply": deviation_ply,
            "first_error_move": first_error_move,
            "biggest_miss": worst,
            "sparkline": sparkline,
        })

    enriched.sort(key=lambda r: r.get("timestamp") or r.get("played_at") or "", reverse=True)

    # Recency weights (spec Phase 2): a 30-day window is estimating *current*
    # ability, so day 1 and day 30 should not count equally. Measured against
    # the newest game in the run rather than wall-clock now, so recomputing an
    # old run cannot silently reweight it.
    newest = next((parse_dt(f["played_at"], None) for f in enriched if f["played_at"]), None)
    for fact in enriched:
        when = parse_dt(fact["played_at"], None)
        days = (
            max(0.0, (newest - when).total_seconds() / 86400.0)
            if newest is not None and when is not None
            else 0.0
        )
        fact["days_ago"] = days
        fact["recency_weight"] = recency_weight(days)
    return enriched


# ── Headline KPIs ─────────────────────────────────────────────────────────────


def compute_headline(
    facts: list[dict[str, Any]],
    *,
    total_loss: float,
    fixable_loss: float | None,
) -> dict[str, Any]:
    """The six numbers a player wants before they read anything else."""

    decided = [f for f in facts if f["points"] is not None]
    n = len(decided)
    score = sum(f["points"] for f in decided)
    wins = sum(1 for f in decided if f["points"] == 1.0)
    draws = sum(1 for f in decided if f["points"] == 0.5)
    losses = sum(1 for f in decided if f["points"] == 0.0)

    ratings = [f["user_rating"] for f in facts if f["user_rating"] is not None]
    # ``facts`` is newest-first; the timeline reads oldest-first.
    chrono = list(reversed(facts))
    chrono_ratings = [f["user_rating"] for f in chrono if f["user_rating"] is not None]
    opp_ratings = [f["opponent_rating"] for f in facts if f["opponent_rating"] is not None]

    accuracies = [f["accuracy"] for f in facts if f["accuracy"] is not None]
    expected = [f["expected_points"] for f in decided if f["expected_points"] is not None]

    user_moves = sum(f["user_moves"] for f in facts)
    blunders = sum(f["blunders"] for f in facts)
    mistakes = sum(f["mistakes"] for f in facts)
    inaccuracies = sum(f["inaccuracies"] for f in facts)

    best_moves = sum(
        f["classification_counts"].get("best", 0)
        + f["classification_counts"].get("brilliant", 0)
        + f["classification_counts"].get("great", 0)
        for f in facts
    )

    elo = _elo_left_on_board(decided, fixable_loss=fixable_loss, total_loss=total_loss)

    return {
        "record": {
            "games": len(facts),
            "decided": n,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "score": score,
            "score_pct": _score_pct(score, n),
        },
        "rating": {
            "start": chrono_ratings[0] if chrono_ratings else None,
            "end": chrono_ratings[-1] if chrono_ratings else None,
            "delta": (
                chrono_ratings[-1] - chrono_ratings[0] if len(chrono_ratings) >= 2 else None
            ),
            "peak": max(ratings) if ratings else None,
            "floor": min(ratings) if ratings else None,
            "mean": _mean([float(r) for r in ratings]),
        },
        "opponents": {
            "mean_rating": _mean([float(r) for r in opp_ratings]),
            "toughest": max(opp_ratings) if opp_ratings else None,
            "n_rated": len(opp_ratings),
        },
        "performance_rating": performance_rating(opp_ratings, score, n),
        "expectancy": _expectancy(decided, expected, score),
        "accuracy": {
            "mean": _mean(accuracies),
            # Recency-weighted alongside the plain mean: the report is estimating
            # current ability, but the raw number stays available for comparison.
            "weighted_mean": weighted_mean(
                [f["accuracy"] for f in facts if f["accuracy"] is not None],
                [f.get("recency_weight", 1.0) for f in facts if f["accuracy"] is not None],
            ),
            "half_life_days": RECENCY_HALF_LIFE_DAYS,
            "median": median(accuracies) if accuracies else None,
            "best": max(accuracies) if accuracies else None,
            "worst": min(accuracies) if accuracies else None,
            "stdev": pstdev(accuracies) if len(accuracies) >= 2 else None,
            "consistency": _consistency_label(accuracies),
            "n": len(accuracies),
        },
        "volume": {
            "user_moves": user_moves,
            "moves_per_game": _rate(user_moves, len(facts)) if facts else 0.0,
        },
        "error_rates": {
            "blunders": blunders,
            "mistakes": mistakes,
            "inaccuracies": inaccuracies,
            "blunders_per_100": 100 * _rate(blunders, user_moves),
            "mistakes_per_100": 100 * _rate(mistakes, user_moves),
            "inaccuracies_per_100": 100 * _rate(inaccuracies, user_moves),
            "best_move_rate": _rate(best_moves, user_moves),
            "clean_game_rate": _rate(
                sum(1 for f in facts if f["blunders"] == 0), len(facts)
            ),
            "moves_per_blunder": (user_moves / blunders) if blunders else None,
        },
        "loss": {
            "total": total_loss,
            "fixable": fixable_loss,
            "total_per_game": (total_loss / len(facts)) if facts else 0.0,
            "fixable_per_game": (
                (fixable_loss / len(facts)) if fixable_loss is not None and facts else None
            ),
        },
        "elo_left_on_board": elo,
    }


def _expectancy(
    decided: list[dict[str, Any]], expected: list[float], score: float
) -> dict[str, Any]:
    """Actual score against what the rating gaps predicted."""

    rated = [f for f in decided if f["expected_points"] is not None]
    if not rated:
        return {"expected": None, "actual": None, "delta": None, "n": 0, "note": None}
    actual = sum(f["points"] for f in rated)
    exp = sum(expected)
    delta = actual - exp
    note = None
    if abs(delta) >= 1.0:
        note = (
            f"{abs(delta):.1f} points {'above' if delta > 0 else 'below'} "
            f"what the rating gaps predicted"
        )
    return {
        "expected": exp,
        "actual": actual,
        "delta": delta,
        "per_game": delta / len(rated),
        "n": len(rated),
        "note": note,
    }


def _consistency_label(accuracies: list[float]) -> str | None:
    if len(accuracies) < 3:
        return None
    spread = pstdev(accuracies)
    if spread < 6:
        return "Very consistent"
    if spread < 10:
        return "Consistent"
    if spread < 15:
        return "Streaky"
    return "Volatile — your floor is the problem, not your ceiling"


def _elo_left_on_board(
    decided: list[dict[str, Any]],
    *,
    fixable_loss: float | None,
    total_loss: float,
) -> dict[str, Any]:
    """Translate recoverable win% into rating points.

    The model, stated plainly because the number is only worth showing if the
    assumption behind it is visible: 100 win% points recovered inside one game is
    worth at most one full game point, and never more than the game actually
    lost. Recovered points lift the score fraction; the rating gap implied by the
    lift (FIDE ``dp``) is the estimate.

    Per game the recoverable pool is the findability-weighted loss when that game
    has full-tier data, and the loss from outright blunders otherwise. ``basis``
    reports which of the two actually drove the number — ``mixed`` when the run
    spans both, since most runs are shallow with a handful of upgrades.
    """

    if not decided:
        return {"points": None, "basis": None, "recoverable_score": None}

    use_findability = fixable_loss is not None
    full_tier = 0
    recovered = 0.0
    for f in decided:
        has_full = use_findability and f["findable_delta_w"] is not None
        if has_full:
            full_tier += 1
        dropped = 1.0 - float(f["points"])
        if dropped <= 0:
            continue
        pool = (
            f["findable_delta_w"] if has_full
            else float(f["blunders"]) * BLUNDER_DELTA_W
        )
        recovered += min(dropped, float(pool or 0.0) / 100.0)

    n = len(decided)
    basis = (
        "findability" if full_tier == n
        else "blunders" if full_tier == 0
        else "mixed"
    )
    actual = sum(f["points"] for f in decided) / n
    potential = min(0.999, actual + recovered / n)
    points = rating_difference(potential) - rating_difference(actual)
    return {
        "points": round(points),
        "basis": basis,
        "full_tier_games": full_tier,
        "games": n,
        "recoverable_score": recovered,
        "actual_score_pct": actual,
        "potential_score_pct": potential,
        "recoverable_win_pct": fixable_loss if basis == "findability" else total_loss,
        "model": (
            "Recovered win% capped by the result actually dropped, converted to "
            "rating via the FIDE score-to-difference curve."
        ),
    }


# ── Move quality ──────────────────────────────────────────────────────────────

CLASSIFICATION_ORDER = (
    "brilliant", "great", "best", "excellent", "good", "book",
    "inaccuracy", "mistake", "miss", "blunder",
)


def compute_move_quality(moves_by_review: dict[str, list[Any]]) -> dict[str, Any]:
    """Classification mix plus accuracy attribution per phase."""

    counts: dict[str, int] = defaultdict(int)
    total = 0
    phase: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"moves": 0, "acc": [], "loss": 0.0, "blunders": 0, "counts": defaultdict(int)}
    )
    accuracies: list[float] = []

    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"]:
                continue
            total += 1
            label = str(m["classification"] or "unclassified")
            counts[label] += 1
            dw = float(m["delta_w"] or 0.0)
            acc = accuracy_for_delta_w(dw, is_book=bool(m["is_book"]))
            accuracies.append(acc)
            key = str(m["phase"] or "middlegame")
            bucket = phase[key]
            bucket["moves"] += 1
            bucket["acc"].append(acc)
            bucket["loss"] += dw
            bucket["counts"][label] += 1
            if dw >= BLUNDER_DELTA_W:
                bucket["blunders"] += 1

    mix = [
        {
            "label": label,
            "n": counts.get(label, 0),
            "rate": _rate(counts.get(label, 0), total),
        }
        for label in CLASSIFICATION_ORDER
        if counts.get(label, 0)
    ]
    unclassified = counts.get("unclassified", 0)
    if unclassified:
        mix.append({
            "label": "unclassified",
            "n": unclassified,
            "rate": _rate(unclassified, total),
        })

    by_phase = []
    for key in ("opening", "middlegame", "endgame"):
        b = phase.get(key)
        if not b or not b["moves"]:
            continue
        by_phase.append({
            "phase": key,
            "moves": b["moves"],
            "accuracy": fmean(b["acc"]),
            "delta_w_per_move": b["loss"] / b["moves"],
            "total_delta_w": b["loss"],
            "blunder_rate": _rate(b["blunders"], b["moves"]),
        })

    weakest = min(by_phase, key=lambda p: p["accuracy"]) if by_phase else None
    return {
        "total_moves": total,
        "mix": mix,
        "mean_accuracy": _mean(accuracies),
        "by_phase": by_phase,
        "weakest_phase": weakest["phase"] if weakest else None,
    }


# ── Critical moments ──────────────────────────────────────────────────────────


def compute_critical_moments(moves_by_review: dict[str, list[Any]]) -> dict[str, Any]:
    """Performance on the moves that actually decide games.

    A 92% average accuracy means nothing if every point of the missing 8% lands
    on the handful of moves where the position was genuinely balanced on a knife
    edge. This splits by volatility and reports the gap.
    """

    buckets = {
        "critical": {"label": f"Critical (V ≥ {CRITICAL_VOLATILITY:.0f})", "acc": [], "loss": 0.0, "n": 0, "handled": 0, "times": [], "blunders": 0},
        "tense": {"label": "Tense (V 35–60)", "acc": [], "loss": 0.0, "n": 0, "handled": 0, "times": [], "blunders": 0},
        "quiet": {"label": f"Quiet (V ≤ {QUIET_VOLATILITY:.0f})", "acc": [], "loss": 0.0, "n": 0, "handled": 0, "times": [], "blunders": 0},
    }

    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"] or m["volatility"] is None:
                continue
            v = float(m["volatility"])
            key = (
                "critical" if v >= CRITICAL_VOLATILITY
                else "quiet" if v <= QUIET_VOLATILITY
                else "tense"
            )
            b = buckets[key]
            dw = float(m["delta_w"] or 0.0)
            b["n"] += 1
            b["loss"] += dw
            b["acc"].append(accuracy_for_delta_w(dw, is_book=bool(m["is_book"])))
            if dw < CRITICAL_HANDLED_DELTA_W:
                b["handled"] += 1
            if dw >= BLUNDER_DELTA_W:
                b["blunders"] += 1
            if m["time_spent"] is not None:
                b["times"].append(float(m["time_spent"]))

    rows = []
    for key in ("critical", "tense", "quiet"):
        b = buckets[key]
        rows.append({
            "key": key,
            "label": b["label"],
            "moves": b["n"],
            "accuracy": _mean(b["acc"]),
            "delta_w_per_move": (b["loss"] / b["n"]) if b["n"] else 0.0,
            "total_delta_w": b["loss"],
            "handled_rate": _rate(b["handled"], b["n"]),
            "blunder_rate": _rate(b["blunders"], b["n"]),
            "mean_time": _mean(b["times"]),
        })

    crit = rows[0]
    quiet = rows[2]
    gap = None
    if crit["accuracy"] is not None and quiet["accuracy"] is not None:
        gap = quiet["accuracy"] - crit["accuracy"]

    note = None
    if gap is not None and crit["moves"] >= 10:
        if gap >= 12:
            note = (
                f"Your accuracy falls {gap:.0f} points on critical moves. The "
                f"average is fine; the moments that decide games are not."
            )
        elif gap <= 3:
            note = "You hold your level when the position sharpens — a genuine strength."

    time_note = None
    if crit["mean_time"] is not None and quiet["mean_time"] is not None:
        if crit["mean_time"] < quiet["mean_time"]:
            time_note = (
                f"You spend {crit['mean_time']:.0f}s on critical moves and "
                f"{quiet['mean_time']:.0f}s on quiet ones — the budget is inverted."
            )

    return {
        "buckets": rows,
        "criticality_gap": gap,
        "critical_conversion": crit["handled_rate"],
        "critical_moves": crit["moves"],
        "note": note,
        "time_note": time_note,
    }


# ── Timelines ─────────────────────────────────────────────────────────────────


def compute_timeline(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Rating, accuracy and cumulative score over the window (oldest first)."""

    chrono = list(reversed(facts))
    points = []
    cumulative = 0.0
    for idx, f in enumerate(chrono):
        if f["points"] is not None:
            cumulative += f["points"]
        points.append({
            "index": idx + 1,
            "game_id": f["game_id"],
            "played_at": f["played_at"],
            "rating": f["user_rating"],
            "opponent_rating": f["opponent_rating"],
            "accuracy": f["accuracy"],
            "outcome": f["outcome"],
            "points": f["points"],
            "cumulative_score": cumulative,
            "total_delta_w": f["total_delta_w"],
        })

    ratings = [p["rating"] for p in points if p["rating"] is not None]
    accs = [p["accuracy"] for p in points if p["accuracy"] is not None]
    return {
        "points": points,
        "rating_trend": _linear_trend(
            [(i, float(r)) for i, r in enumerate(ratings)]
        ),
        "accuracy_trend": _linear_trend(
            [(i, float(a)) for i, a in enumerate(accs)]
        ),
    }


def _linear_trend(pairs: list[tuple[int, float]]) -> float | None:
    """Least-squares slope per game; ``None`` when there is nothing to fit."""

    n = len(pairs)
    if n < 3:
        return None
    mean_x = fmean([p[0] for p in pairs])
    mean_y = fmean([p[1] for p in pairs])
    num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    den = sum((x - mean_x) ** 2 for x, _ in pairs)
    return (num / den) if den else None


# ── Opening tree ──────────────────────────────────────────────────────────────


def compute_opening_tree(facts: list[dict[str, Any]], *, min_games: int = 2) -> dict[str, Any]:
    """Per-opening performance, split by colour, with post-book quality."""

    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "n": 0, "score": 0.0, "decided": 0, "acc": [], "loss": [],
            "deviation": [], "blunders": 0, "moves": 0, "eco": "", "games": [],
            "facts": [],
        }
    )
    for f in facts:
        key = (f["user_color"], f["opening_name"])
        g = groups[key]
        g["facts"].append(f)
        g["n"] += 1
        g["eco"] = g["eco"] or f["eco"]
        if f["points"] is not None:
            g["score"] += f["points"]
            g["decided"] += 1
        if f["accuracy"] is not None:
            g["acc"].append(f["accuracy"])
        opening_loss = float(f["phase_delta_w"].get("opening", 0.0))
        opening_moves = int(f["phase_moves"].get("opening", 0))
        if opening_moves:
            g["loss"].append(opening_loss / opening_moves)
        if f["deviation_ply"] is not None:
            g["deviation"].append(f["deviation_ply"])
        g["blunders"] += f["blunders"]
        g["moves"] += f["user_moves"]
        g["games"].append(f["game_id"])

    rows = []
    for (color, name), g in groups.items():
        performance = bucket_performance(g["facts"])
        rows.append({
            "color": color,
            "opening": name,
            "eco": g["eco"],
            "n": g["n"],
            "score_pct": _score_pct(g["score"], g["decided"]),
            "mean_accuracy": _mean(g["acc"]),
            "opening_delta_w_per_move": _mean(g["loss"]),
            "mean_deviation_ply": _mean([float(d) for d in g["deviation"]]),
            "blunders_per_100": 100 * _rate(g["blunders"], g["moves"]),
            "game_ids": g["games"][:20],
            # Phase 2/3: never a bare rate.
            "ci": performance["ci"],
            "expectation": performance["expectation"],
            "below_floor": performance["below_floor"],
        })
    rows.sort(key=lambda r: (-r["n"], r["opening"]))

    # Phase 3.2: partial pooling. A 3-game opening barely moves off the player's
    # own baseline; a 40-game one speaks for itself. This is what replaces the
    # hand-tuned ``min_games`` gate for ranking purposes.
    pooled = shrink_buckets(
        [
            {
                "key": (r["color"], r["opening"]),
                "value": r["score_pct"],
                "n": r["n"],
                # Score is in [0,1]; Bernoulli-ish variance is the right scale.
                "variance": (r["score_pct"] or 0.5) * (1 - (r["score_pct"] or 0.5)),
            }
            for r in rows
            if r["score_pct"] is not None
        ],
        value_key="value",
    )
    shrunk_by_key = {b["key"]: b["shrunk"] for b in pooled["buckets"]}
    for r in rows:
        r["shrunk_score_pct"] = shrunk_by_key.get((r["color"], r["opening"]))

    # Rank on the shrunk estimate, and only among buckets clearing the floor —
    # otherwise "worst opening" is reliably whichever one has three games.
    scored = [
        r for r in rows
        if r["n"] >= min_games
        and r["shrunk_score_pct"] is not None
        and not r["below_floor"]
    ]
    if not scored:  # fall back to the raw gate when nothing clears the floor
        scored = [r for r in rows if r["n"] >= min_games and r["score_pct"] is not None]
        rank_key = "score_pct"
    else:
        rank_key = "shrunk_score_pct"
    best = max(scored, key=lambda r: r[rank_key]) if scored else None
    worst = min(scored, key=lambda r: r[rank_key]) if scored else None

    return {
        "rows": rows,
        "as_white": [r for r in rows if r["color"] == "white"],
        "as_black": [r for r in rows if r["color"] == "black"],
        "best": best,
        "worst": worst,
        "min_games": min_games,
        "pooling_k": pooled["k"],
        "baseline_score": pooled["global_mean"],
        "ranked_on": rank_key,
        "distinct_openings": len({r["opening"] for r in rows}),
    }


# ── Endgame, resilience, blunder timing ───────────────────────────────────────


def compute_endgame(
    facts: list[dict[str, Any]],
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    """Do endgames get reached, and are they converted from where they start?"""

    entry_buckets = {
        "winning": {"label": "Entered winning", "n": 0, "score": 0.0, "decided": 0, "facts": []},
        "equal": {"label": "Entered level", "n": 0, "score": 0.0, "decided": 0, "facts": []},
        "losing": {"label": "Entered worse", "n": 0, "score": 0.0, "decided": 0, "facts": []},
    }
    reached = 0
    loss_sum = 0.0
    move_sum = 0

    for f in facts:
        moves = moves_by_review.get(f["review_id"], [])
        endgame_moves = [
            m for m in moves
            if m["is_user_move"] and str(m["phase"] or "") == "endgame"
        ]
        if not endgame_moves:
            continue
        reached += 1
        loss_sum += sum(float(m["delta_w"] or 0.0) for m in endgame_moves)
        move_sum += len(endgame_moves)

        entry = next((m["win_prob"] for m in endgame_moves if m["win_prob"] is not None), None)
        if entry is None or f["points"] is None:
            continue
        wp = float(entry)
        key = "winning" if wp > 0.6 else "losing" if wp < 0.4 else "equal"
        b = entry_buckets[key]
        b["n"] += 1
        b["score"] += f["points"]
        b["decided"] += 1
        b["facts"].append(f)

    rows = [
        {
            "key": key,
            "label": b["label"],
            "n": b["n"],
            "score_pct": _score_pct(b["score"], b["decided"]),
            **{
                k: v for k, v in bucket_performance(b["facts"]).items()
                if k in ("ci", "expectation", "below_floor")
            },
        }
        for key, b in entry_buckets.items()
    ]
    return {
        "reached": reached,
        "reach_rate": _rate(reached, len(facts)),
        "delta_w_per_move": (loss_sum / move_sum) if move_sum else None,
        "moves": move_sum,
        "entry": rows,
    }


def compute_resilience(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Conversion of winning positions and recovery from losing ones."""

    winning = [f for f in facts if (f["peak_win_prob"] or 0) > WINNING_WIN_PROB and f["points"] is not None]
    losing = [f for f in facts if (f["trough_win_prob"] if f["trough_win_prob"] is not None else 1) < LOSING_WIN_PROB and f["points"] is not None]
    decisive = [f for f in facts if (f["peak_win_prob"] or 0) > DECISIVE_WIN_PROB and f["points"] is not None]

    dropped = sum(1.0 - f["points"] for f in winning)
    rescued = sum(f["points"] for f in losing)

    return {
        "conversion": {
            "n": len(winning),
            "score_pct": _score_pct(sum(f["points"] for f in winning), len(winning)),
            "points_dropped": dropped,
            "threshold": WINNING_WIN_PROB,
            **{
                k: v for k, v in bucket_performance(winning).items()
                if k in ("ci", "expectation", "below_floor")
            },
        },
        "comeback": {
            "n": len(losing),
            "score_pct": _score_pct(rescued, len(losing)),
            "points_rescued": rescued,
            "threshold": LOSING_WIN_PROB,
            **{
                k: v for k, v in bucket_performance(losing).items()
                if k in ("ci", "expectation", "below_floor")
            },
        },
        "missed_wins": {
            "n": sum(1 for f in decisive if f["points"] < 1.0),
            "of": len(decisive),
            "threshold": DECISIVE_WIN_PROB,
            "games": [
                {
                    "game_id": f["game_id"],
                    "opponent": f["opponent"],
                    "peak_win_prob": f["peak_win_prob"],
                    "result": f["result"],
                    "biggest_miss": f["biggest_miss"],
                }
                for f in sorted(
                    (f for f in decisive if f["points"] < 1.0),
                    key=lambda f: -(f["peak_win_prob"] or 0),
                )[:10]
            ],
        },
    }


def compute_blunder_timing(
    facts: list[dict[str, Any]],
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    """When inside a game do things go wrong?"""

    buckets = {
        label: {"label": f"Moves {label}", "moves": 0, "blunders": 0, "loss": 0.0}
        for label, _, _ in MOVE_NUMBER_BUCKETS
    }
    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"]:
                continue
            move_no = (int(m["ply"] or 0) + 1) // 2
            for label, lo, hi in MOVE_NUMBER_BUCKETS:
                if lo <= move_no <= hi:
                    b = buckets[label]
                    dw = float(m["delta_w"] or 0.0)
                    b["moves"] += 1
                    b["loss"] += dw
                    if dw >= BLUNDER_DELTA_W:
                        b["blunders"] += 1
                    break

    rows = [
        {
            "key": label,
            "label": buckets[label]["label"],
            "moves": buckets[label]["moves"],
            "blunders": buckets[label]["blunders"],
            "blunder_rate": _rate(buckets[label]["blunders"], buckets[label]["moves"]),
            "delta_w_per_move": (
                buckets[label]["loss"] / buckets[label]["moves"]
                if buckets[label]["moves"] else 0.0
            ),
        }
        for label, _, _ in MOVE_NUMBER_BUCKETS
    ]
    first_errors = [f["first_error_move"] for f in facts if f["first_error_move"] is not None]
    worst = max((r for r in rows if r["moves"] >= 10), key=lambda r: r["blunder_rate"], default=None)
    return {
        "buckets": rows,
        "mean_first_error_move": _mean([float(x) for x in first_errors]),
        "n_first_error": len(first_errors),
        "worst_window": worst["key"] if worst else None,
    }


# ── Leak board ────────────────────────────────────────────────────────────────


def _leak(
    leak_id: str,
    title: str,
    detail: str,
    impact: float,
    *,
    ceiling: float,
    practice: str | None = None,
    section: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Package one leak, capped at what the player actually lost.

    Each estimate is ``excess rate × exposure``, which is unbounded above: a bad
    enough endgame rate over enough endgame moves "costs" more win% than a game
    contains. Capping at the observed mean loss per game keeps every number
    physically meaningful. Leaks overlap by construction (a scramble move can
    also be a critical endgame move), so they are ranked, not summed.
    """

    capped = max(0.0, min(impact, ceiling))
    return {
        "id": leak_id,
        "title": title,
        "detail": detail,
        "impact_win_pct_per_game": round(capped, 1),
        "raw_impact": round(max(0.0, impact), 1),
        "capped": impact > ceiling,
        "severity": "high" if capped >= 8 else "medium" if capped >= 3 else "low",
        "practice": practice,
        "section": section,
        "evidence": evidence or {},
    }


def compute_leaks(
    facts: list[dict[str, Any]],
    *,
    move_quality: dict[str, Any],
    critical: dict[str, Any],
    resilience: dict[str, Any],
    openings: dict[str, Any],
    blunder_timing: dict[str, Any],
    scramble: dict[str, Any],
    tier3: dict[str, Any],
    missed_tactics: dict[str, Any],
    measures: dict[str, Any] | None = None,
    recurrence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank every detectable leak by win% lost per game.

    Impact is always expressed in the same unit so a phase problem, a clock
    problem and an opening problem can be compared on one axis — which is the
    whole point of a coaching page. A leak with no measurable cost is not shown.
    """

    n_games = max(1, len(facts))
    # No single leak can be worth more than everything the player actually lost.
    ceiling = min(
        100.0,
        sum(float(f.get("total_delta_w") or 0.0) for f in facts) / n_games or 100.0,
    )
    leaks: list[dict[str, Any]] = []

    # 1. Phase leak — excess loss over the player's own best phase.
    phases = move_quality.get("by_phase") or []
    if len(phases) >= 2:
        best = min(phases, key=lambda p: p["delta_w_per_move"])
        worst = max(phases, key=lambda p: p["delta_w_per_move"])
        excess = worst["delta_w_per_move"] - best["delta_w_per_move"]
        moves_per_game = worst["moves"] / n_games
        impact = excess * moves_per_game
        if impact >= 1.0:
            leaks.append(_leak(
                "phase",
                f"{worst['phase'].capitalize()} is your weakest phase",
                f"You leak {worst['delta_w_per_move']:.1f} win% per {worst['phase']} move "
                f"versus {best['delta_w_per_move']:.1f} in the {best['phase']} — "
                f"{worst['accuracy']:.0f}% accuracy over {worst['moves']} moves.",
                impact,
                ceiling=ceiling,
                practice="mistakes",
                section="quality",
                evidence={"phase": worst["phase"], "accuracy": worst["accuracy"]},
            ))

    # 2. Critical-moment collapse.
    gap = critical.get("criticality_gap")
    crit_bucket = next((b for b in critical.get("buckets") or [] if b["key"] == "critical"), None)
    if gap is not None and crit_bucket and crit_bucket["moves"] >= 8 and gap > 4:
        impact = crit_bucket["delta_w_per_move"] * (crit_bucket["moves"] / n_games)
        leaks.append(_leak(
            "critical",
            "Your accuracy collapses on critical moves",
            f"{gap:.0f} accuracy points lower on high-volatility positions "
            f"({crit_bucket['accuracy']:.0f}% vs {(crit_bucket['accuracy'] + gap):.0f}% quiet). "
            f"You handle {crit_bucket['handled_rate'] * 100:.0f}% of them cleanly.",
            impact,
            ceiling=ceiling,
            practice="mistakes",
            section="critical",
            evidence={"gap": gap, "moves": crit_bucket["moves"]},
        ))

    # 3. Clock. Scramble moves are cheap in count and expensive in loss.
    rows = scramble.get("buckets") or []
    scr = next((r for r in rows if r["key"] == "scramble"), None)
    deep = next((r for r in rows if r["key"] == "deep"), None)
    if scr and deep and scr["moves"] >= 10 and deep["moves"] >= 10:
        excess = scr["delta_w_per_move"] - deep["delta_w_per_move"]
        impact = excess * (scr["moves"] / n_games)
        if impact >= 1.0:
            leaks.append(_leak(
                "clock",
                "Time scrambles cost you real points",
                f"Under 10s you leak {scr['delta_w_per_move']:.1f} win% per move versus "
                f"{deep['delta_w_per_move']:.1f} with time on the clock — "
                f"{scr['blunder_rate'] * 100:.0f}% of scramble moves are blunders.",
                impact,
                ceiling=ceiling,
                practice="forced",
                section="time",
                evidence={"scramble_moves": scr["moves"]},
            ))

    # 4. Inverted time budget — rushing exactly where thinking pays.
    if critical.get("time_note"):
        crit_time = crit_bucket["mean_time"] if crit_bucket else None
        quiet_bucket = next((b for b in critical.get("buckets") or [] if b["key"] == "quiet"), None)
        quiet_time = quiet_bucket["mean_time"] if quiet_bucket else None
        if crit_time and quiet_time and crit_bucket and crit_bucket["moves"] >= 8:
            impact = crit_bucket["delta_w_per_move"] * (crit_bucket["moves"] / n_games) * 0.5
            leaks.append(_leak(
                "time_allocation",
                "Your thinking time is spent on the wrong moves",
                critical["time_note"],
                impact,
                ceiling=ceiling,
                practice="mistakes",
                section="time",
                evidence={"critical_time": crit_time, "quiet_time": quiet_time},
            ))

    # 5. Failing to convert won positions.
    conv = resilience.get("conversion") or {}
    if conv.get("n", 0) >= 3 and conv.get("score_pct") is not None and conv["score_pct"] < 0.85:
        impact = (conv["points_dropped"] / n_games) * 100 * 0.5
        leaks.append(_leak(
            "conversion",
            "Winning positions slip away",
            f"You reached a winning position in {conv['n']} games and scored only "
            f"{conv['score_pct'] * 100:.0f}% from them — {conv['points_dropped']:.1f} "
            f"full points dropped from positions you had already won.",
            impact,
            ceiling=ceiling,
            practice="defense",
            section="openings",
            evidence={"games": conv["n"], "dropped": conv["points_dropped"]},
        ))

    # 6. A specific opening that is underwater.
    worst_opening = openings.get("worst")
    if (
        worst_opening
        and worst_opening["n"] >= 3
        and worst_opening["score_pct"] is not None
        and worst_opening["score_pct"] < 0.4
    ):
        share = worst_opening["n"] / n_games
        impact = (0.5 - worst_opening["score_pct"]) * 100 * share
        leaks.append(_leak(
            "opening",
            f"{worst_opening['opening']} is losing you games",
            f"{worst_opening['score_pct'] * 100:.0f}% score across {worst_opening['n']} games as "
            f"{worst_opening['color']}, leaking "
            f"{(worst_opening['opening_delta_w_per_move'] or 0):.1f} win% per opening move.",
            impact,
            ceiling=ceiling,
            practice="mistakes",
            section="openings",
            evidence=worst_opening,
        ))

    # 7. Tilt — the game after a loss.
    after = tier3.get("after_loss") or {}
    overall_wins = sum(1 for f in facts if f["outcome"] == "win")
    overall_rate = _rate(overall_wins, len(facts))
    if after.get("n", 0) >= 4 and after.get("win_rate") is not None:
        drop = overall_rate - after["win_rate"]
        if drop > 0.1:
            impact = drop * 100 * (after["n"] / n_games) * 0.5
            leaks.append(_leak(
                "tilt",
                "You play worse immediately after a loss",
                f"{after['win_rate'] * 100:.0f}% win rate in the game following a defeat "
                f"versus {overall_rate * 100:.0f}% overall, across {after['n']} games.",
                impact,
                ceiling=ceiling,
                practice="mistakes",
                section="time",
                evidence={"after_loss": after["win_rate"], "overall": overall_rate},
            ))

    # 8. A repeated tactical motif.
    tags = missed_tactics.get("tags") or []
    if tags and tags[0]["n"] >= 3:
        top = tags[0]
        impact = (top["mean_delta_w"] * top["n"]) / n_games
        leaks.append(_leak(
            "tactic",
            f"You keep missing {top['tag'].replace('_', ' ')}s",
            f"{top['n']} missed in this window at an average cost of "
            f"{top['mean_delta_w']:.0f} win% each.",
            impact,
            ceiling=ceiling,
            practice="mistakes",
            section="critical",
            evidence=top,
        ))

    # 9. A consistent in-game window where it falls apart.
    worst_window = blunder_timing.get("worst_window")
    if worst_window:
        row = next((r for r in blunder_timing["buckets"] if r["key"] == worst_window), None)
        others = [r for r in blunder_timing["buckets"] if r["key"] != worst_window and r["moves"] >= 10]
        if row and others:
            baseline = fmean([r["delta_w_per_move"] for r in others])
            excess = row["delta_w_per_move"] - baseline
            impact = excess * (row["moves"] / n_games)
            if impact >= 1.0:
                leaks.append(_leak(
                    "game_window",
                    f"Moves {worst_window} are where your games break",
                    f"{row['blunder_rate'] * 100:.0f}% blunder rate in that window versus "
                    f"{baseline:.1f} win%/move elsewhere.",
                    impact,
                    ceiling=ceiling,
                    practice="mistakes",
                    section="quality",
                    evidence=row,
                ))

    measures = measures or {}

    # 10. Failing to punish the opponent's errors (spec 5.1). Half of rating is
    # capitalizing on mistakes, and this was the product's largest blind spot.
    punish = measures.get("punish") or {}
    if punish.get("opportunities", 0) >= DISPLAY_FLOOR_N and punish.get("punish_rate") is not None:
        missed_swing = punish["swing_available"] - punish["swing_kept"]
        impact = missed_swing / n_games
        if punish["punish_rate"] < 0.6 and impact >= 1.0:
            leaks.append(_leak(
                "punish",
                "You don't punish your opponents' mistakes",
                f"They erred {punish['opportunities']} times and you converted "
                f"{punish['punish_rate'] * 100:.0f}% of it — "
                f"{missed_swing:.0f} win% handed back.",
                impact,
                ceiling=ceiling,
                practice="mistakes",
                section="opponents",
                evidence={"opportunities": punish["opportunities"]},
            ))

    # 11. Impulse moves with time on the clock (spec 11.1).
    impulse = measures.get("impulsivity") or {}
    if (
        impulse.get("n", 0) >= DISPLAY_FLOOR_N
        and impulse.get("blunder_rate_impulsive") is not None
        and impulse.get("blunder_rate_deliberate") is not None
    ):
        excess = impulse["blunder_rate_impulsive"] - impulse["blunder_rate_deliberate"]
        impact = excess * BLUNDER_DELTA_W * (impulse["impulsive"] / n_games)
        if excess > 0 and impact >= 1.0:
            leaks.append(_leak(
                "impulse",
                "You move before you look",
                f"{impulse['impulse_rate'] * 100:.0f}% of your unhurried moves go in under "
                f"{impulse['threshold_seconds']:.0f}s, and they blunder "
                f"{impulse['blunder_rate_impulsive'] * 100:.1f}% of the time versus "
                f"{impulse['blunder_rate_deliberate'] * 100:.1f}% on comparable slow moves.",
                impact,
                ceiling=ceiling,
                practice="mistakes",
                section="mind",
                evidence={"impulsive": impulse["impulsive"]},
            ))

    # 12. Sunk-cost persistence after a refuted plan (spec 11.4).
    stubborn = measures.get("stubbornness") or {}
    if (
        stubborn.get("refuted_plans", 0) >= DISPLAY_FLOOR_N
        and (stubborn.get("persistence_rate") or 0) > 0.4
    ):
        impact = stubborn["extra_delta_w"] / n_games
        leaks.append(_leak(
            "stubbornness",
            "You keep pushing plans that have already been refuted",
            f"In {stubborn['episodes']} of {stubborn['refuted_plans']} refuted plans you "
            f"moved the same piece again within {stubborn['window']} moves, costing a further "
            f"{stubborn['extra_delta_w']:.0f} win%.",
            impact,
            ceiling=ceiling,
            practice="defense",
            section="mind",
            evidence={"episodes": stubborn["episodes"]},
        ))

    # 13. Bad trading (spec 5.4).
    trades = measures.get("trades") or {}
    if trades.get("captures", 0) >= DISPLAY_FLOOR_N and (trades.get("gap") or 0) > 0.8:
        impact = trades["gap"] * (trades["captures"] / n_games)
        leaks.append(_leak(
            "trades",
            "Your exchanges leak value",
            f"Captures cost {trades['capture_delta_w_per_move']:.2f} win% per move versus "
            f"{trades['other_delta_w_per_move']:.2f} on everything else.",
            impact,
            ceiling=ceiling,
            practice="mistakes",
            section="opponents",
            evidence={"captures": trades["captures"]},
        ))

    # 14. Recurring error signatures (spec 6.3). The strongest available signal:
    # a mistake made once is noise, the same mistake six times is a leak.
    for cluster in (recurrence or {}).get("recurring", [])[:3]:
        impact = cluster["total_delta_w"] / n_games
        span = (
            f" since {cluster['first_seen']}" if cluster.get("first_seen") else ""
        )
        leaks.append(_leak(
            f"signature:{cluster['signature']}",
            f"The same mistake, {cluster['n']} times",
            f"You keep {cluster['label']} — {cluster['n']} times{span}, "
            f"costing {cluster['total_delta_w']:.0f} win% in total.",
            impact,
            ceiling=ceiling,
            practice="mistakes",
            section="recurrence",
            evidence={"signature": cluster["signature"], "n": cluster["n"]},
        ))

    # A leak whose supporting moves share a signature is more actionable than one
    # whose don't, so recurrence boosts the ranking rather than only adding rows.
    recurring_sections = {
        c["parts"].get("phase") for c in (recurrence or {}).get("recurring", [])
    }
    for leak in leaks:
        if leak["id"] == "phase" and leak["evidence"].get("phase") in recurring_sections:
            leak["recurrence_boost"] = True
            leak["impact_win_pct_per_game"] = round(
                min(ceiling, leak["impact_win_pct_per_game"] * 1.15), 1
            )

    leaks.sort(key=lambda l: -l["impact_win_pct_per_game"])
    return leaks


def compute_strengths(
    facts: list[dict[str, Any]],
    *,
    move_quality: dict[str, Any],
    critical: dict[str, Any],
    resilience: dict[str, Any],
    openings: dict[str, Any],
    headline: dict[str, Any],
) -> list[dict[str, Any]]:
    """The counterweight to the leak board — what is already working."""

    out: list[dict[str, Any]] = []

    gap = critical.get("criticality_gap")
    if gap is not None and gap <= 3 and critical.get("critical_moves", 0) >= 8:
        out.append({
            "id": "critical",
            "title": "Steady when it sharpens",
            "detail": (
                f"You hold {critical['critical_conversion'] * 100:.0f}% of critical moves "
                f"cleanly — no accuracy drop when the position gets sharp."
            ),
        })

    conv = resilience.get("conversion") or {}
    if conv.get("n", 0) >= 3 and (conv.get("score_pct") or 0) >= 0.85:
        out.append({
            "id": "conversion",
            "title": "You finish what you start",
            "detail": f"{conv['score_pct'] * 100:.0f}% score from winning positions across {conv['n']} games.",
        })

    comeback = resilience.get("comeback") or {}
    if comeback.get("n", 0) >= 3 and (comeback.get("score_pct") or 0) >= 0.25:
        out.append({
            "id": "comeback",
            "title": "Hard to put away",
            "detail": (
                f"You rescued {comeback['points_rescued']:.1f} points from "
                f"{comeback['n']} games that were going badly."
            ),
        })

    best_opening = openings.get("best")
    if best_opening and best_opening["n"] >= 3 and (best_opening["score_pct"] or 0) >= 0.6:
        out.append({
            "id": "opening",
            "title": f"{best_opening['opening']} is a weapon",
            "detail": (
                f"{best_opening['score_pct'] * 100:.0f}% score over {best_opening['n']} games as "
                f"{best_opening['color']}. Play it more."
            ),
        })

    expectancy = headline.get("expectancy") or {}
    if (expectancy.get("delta") or 0) >= 1.0:
        out.append({
            "id": "expectancy",
            "title": "Outperforming your rating",
            "detail": (
                f"{expectancy['delta']:.1f} points above Elo expectation across "
                f"{expectancy['n']} rated games — your rating is lagging your play."
            ),
        })

    phases = move_quality.get("by_phase") or []
    if len(phases) >= 2:
        best_phase = max(phases, key=lambda p: p["accuracy"])
        if best_phase["accuracy"] >= 88 and best_phase["moves"] >= 30:
            out.append({
                "id": "phase",
                "title": f"Strong {best_phase['phase']}",
                "detail": f"{best_phase['accuracy']:.0f}% accuracy over {best_phase['moves']} {best_phase['phase']} moves.",
            })

    rates = headline.get("error_rates") or {}
    if (rates.get("clean_game_rate") or 0) >= 0.6 and len(facts) >= 5:
        out.append({
            "id": "clean",
            "title": "Blunder-free most nights",
            "detail": f"{rates['clean_game_rate'] * 100:.0f}% of your games contain no blunder at all.",
        })

    return out[:5]


# ── Entry point ───────────────────────────────────────────────────────────────


def compute_pro_metrics(
    game_rows: list[Any],
    moves_by_review: dict[str, list[Any]],
    *,
    total_loss: float,
    fixable_loss: float | None,
    scramble: dict[str, Any],
    tier3: dict[str, Any],
    missed_tactics: dict[str, Any],
    measures: dict[str, Any] | None = None,
    recurrence: dict[str, Any] | None = None,
    geometry: dict[str, Any] | None = None,
    piece_attribution: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute the whole professional layer.

    Returns ``(pro_metrics, game_facts)``. ``game_facts`` is returned separately
    because it doubles as the client's filtering substrate.

    The Insights 3.0 measurement modules (``insights_measures``,
    ``insights_signatures``) import constants from here, so they are computed by
    the caller and passed in — the dependency runs one way only.
    """

    facts = build_game_facts(game_rows, moves_by_review)
    headline = compute_headline(facts, total_loss=total_loss, fixable_loss=fixable_loss)
    move_quality = compute_move_quality(moves_by_review)
    critical = compute_critical_moments(moves_by_review)
    timeline = compute_timeline(facts)
    openings = compute_opening_tree(facts)
    endgame = compute_endgame(facts, moves_by_review)
    resilience = compute_resilience(facts)
    blunder_timing = compute_blunder_timing(facts, moves_by_review)

    leaks = compute_leaks(
        facts,
        move_quality=move_quality,
        critical=critical,
        resilience=resilience,
        openings=openings,
        blunder_timing=blunder_timing,
        scramble=scramble,
        tier3=tier3,
        missed_tactics=missed_tactics,
        measures=measures,
        recurrence=recurrence,
    )
    strengths = compute_strengths(
        facts,
        move_quality=move_quality,
        critical=critical,
        resilience=resilience,
        openings=openings,
        headline=headline,
    )

    return (
        {
            "headline": headline,
            "move_quality": move_quality,
            "critical_moments": critical,
            "timeline": timeline,
            "openings": openings,
            "endgame": endgame,
            "resilience": resilience,
            "blunder_timing": blunder_timing,
            "leaks": leaks,
            "strengths": strengths,
            # Insights 3.0 additions.
            "measures": measures or {},
            "recurrence": recurrence or {},
            "geometry": geometry or {},
            "piece_attribution": piece_attribution or {},
        },
        facts,
    )
