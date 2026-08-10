"""Phase 9 — Shapley attribution: the leak board as a budget, not a leaderboard.

Leaks overlap by construction — one move can be critical, in the endgame *and*
played in a scramble — so the report ranks them and never sums them. That is the
right call, but it caps the product: users want to know where their lost win%
actually went, and a ranked list cannot answer that.

This module decomposes the observed total loss additively:

    total_loss = baseline + Σ_feature contribution_feature

using exact Shapley values over the conditional-mean model. For a move ``x`` and
a feature subset ``S`` the value function is ``E[Δw | X_S = x_S]``, so
``v(∅)`` is the player's global mean and ``v(full)`` is the mean of the cell
``x`` falls in. Summed over every move, the cell means reproduce the observed
total exactly — which is why the budget closes, and why the test asserting it
is not merely a smoke test.

No ML dependency and no approximation: moves sharing a feature vector share a
Shapley value, so the work is done once per *distinct cell* (a few hundred)
rather than once per move.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from math import factorial
from typing import Any, Callable, Sequence

from server.insights_pro import (
    CRITICAL_VOLATILITY,
    QUIET_VOLATILITY,
    SCRAMBLE_CLOCK_SECONDS,
)

#: Human labels for the decomposition, in the order they are reported.
FEATURE_LABELS = {
    "phase": "Game phase",
    "volatility": "Position criticality",
    "clock": "Time pressure",
    "opponent": "Opponent strength",
    "move_number": "Stage of the game",
    "findability": "Difficulty of the right move",
    "capture": "Exchanges",
}


def _phase(row: dict[str, Any]) -> str:
    return str(row.get("phase") or "unknown")


def _volatility(row: dict[str, Any]) -> str:
    value = row.get("volatility")
    if value is None:
        return "unknown"
    v = float(value)
    if v >= CRITICAL_VOLATILITY:
        return "critical"
    if v <= QUIET_VOLATILITY:
        return "quiet"
    return "tense"


def _clock(row: dict[str, Any]) -> str:
    value = row.get("clock_remaining")
    if value is None:
        return "unknown"
    c = float(value)
    if c < SCRAMBLE_CLOCK_SECONDS:
        return "scramble"
    if c < 30:
        return "low"
    if c < 60:
        return "medium"
    return "deep"


def _opponent(row: dict[str, Any]) -> str:
    return str(row.get("rating_band") or "unknown")


def _move_number(row: dict[str, Any]) -> str:
    move_no = (int(row.get("ply") or 0) + 1) // 2
    if move_no <= 10:
        return "1-10"
    if move_no <= 20:
        return "11-20"
    if move_no <= 30:
        return "21-30"
    if move_no <= 40:
        return "31-40"
    return "41+"


def _findability(row: dict[str, Any]) -> str:
    value = row.get("findability")
    if value is None:
        return "unknown"
    f = float(value)
    return "obvious" if f >= 70 else "hard" if f <= 40 else "moderate"


def _capture(row: dict[str, Any]) -> str:
    return "capture" if "x" in str(row.get("san") or "") else "quiet-move"


FEATURES: dict[str, Callable[[dict[str, Any]], str]] = {
    "phase": _phase,
    "volatility": _volatility,
    "clock": _clock,
    "opponent": _opponent,
    "move_number": _move_number,
    "findability": _findability,
    "capture": _capture,
}


def _shapley_weights(n: int) -> dict[int, float]:
    """``|S|!(n-|S|-1)!/n!`` for every coalition size, computed once."""

    return {
        size: factorial(size) * factorial(n - size - 1) / factorial(n)
        for size in range(n)
    }


def compute_attribution(
    rows: Sequence[dict[str, Any]],
    *,
    band_by_game: dict[str, str] | None = None,
    min_moves: int = 200,
) -> dict[str, Any]:
    """Split the observed loss across the features that explain it.

    ``band_by_game`` supplies the opponent-strength feature, which lives on the
    game rather than the move.
    """

    band_by_game = band_by_game or {}
    moves = [
        {**r, "rating_band": band_by_game.get(str(r.get("game_id")), "unknown")}
        for r in rows
        if r.get("is_user_move") and not r.get("is_book")
    ]
    total_loss = sum(float(m.get("delta_w") or 0.0) for m in moves)
    n_moves = len(moves)
    if n_moves < min_moves:
        return {
            "available": False,
            "n": n_moves,
            "min_moves": min_moves,
            "reason": f"Needs at least {min_moves} scored moves to decompose.",
        }

    names = list(FEATURES)
    n_features = len(names)

    # Collapse to distinct feature vectors: moves sharing one share a Shapley
    # value, so the exact computation costs cells, not moves.
    cells: dict[tuple[str, ...], dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "loss": 0.0}
    )
    vectors: list[tuple[str, ...]] = []
    for move in moves:
        vector = tuple(FEATURES[name](move) for name in names)
        vectors.append(vector)
        entry = cells[vector]
        entry["count"] += 1
        entry["loss"] += float(move.get("delta_w") or 0.0)

    global_mean = total_loss / n_moves

    # Conditional means for every feature subset, keyed by the projected vector.
    subset_means: dict[frozenset[int], dict[tuple[str, ...], float]] = {}
    for size in range(n_features + 1):
        for subset in combinations(range(n_features), size):
            key = frozenset(subset)
            grouped: dict[tuple[str, ...], list[float]] = defaultdict(
                lambda: [0.0, 0.0]
            )
            for vector, entry in cells.items():
                projected = tuple(vector[i] for i in subset)
                bucket = grouped[projected]
                bucket[0] += entry["loss"]
                bucket[1] += entry["count"]
            subset_means[key] = {
                projected: (loss / count) if count else global_mean
                for projected, (loss, count) in grouped.items()
            }

    weights = _shapley_weights(n_features)

    def value(subset: frozenset[int], vector: tuple[str, ...]) -> float:
        projected = tuple(vector[i] for i in sorted(subset))
        return subset_means[subset].get(projected, global_mean)

    # Shapley value per distinct cell, then weighted by how often it occurs.
    #
    # Summed over the whole dataset each feature's raw total is zero — SHAP
    # values are deviations from the mean and cancel by construction. The
    # informative split is therefore between the moves a feature makes *worse*
    # than baseline and the moves it makes better: `added` is the loss a
    # condition piles on where it hurts, `saved` the loss it spares elsewhere.
    added = {name: 0.0 for name in names}
    saved = {name: 0.0 for name in names}
    for vector, entry in cells.items():
        count = entry["count"]
        for idx, name in enumerate(names):
            rest = [j for j in range(n_features) if j != idx]
            contribution = 0.0
            for size in range(len(rest) + 1):
                weight = weights[size]
                for subset in combinations(rest, size):
                    without = frozenset(subset)
                    with_i = frozenset(subset + (idx,))
                    contribution += weight * (value(with_i, vector) - value(without, vector))
            if contribution >= 0:
                added[name] += contribution * count
            else:
                saved[name] += contribution * count

    baseline = global_mean * n_moves
    net = {name: added[name] + saved[name] for name in names}
    redistributed = sum(added.values())

    rows_out = [
        {
            "feature": name,
            "label": FEATURE_LABELS[name],
            "added": added[name],
            "saved": saved[name],
            "net": net[name],
            "per_game": added[name] / max(1, len({m["game_id"] for m in moves})),
            "share_of_excess": (added[name] / redistributed) if redistributed else None,
            "share_of_total": (added[name] / total_loss) if total_loss else None,
        }
        for name in names
    ]
    rows_out.sort(key=lambda r: -r["added"])

    worst = rows_out[0] if rows_out else None
    return {
        "available": True,
        "n": n_moves,
        "games": len({m["game_id"] for m in moves}),
        "cells": len(cells),
        "total_loss": total_loss,
        "baseline": baseline,
        "baseline_share": (baseline / total_loss) if total_loss else None,
        # baseline + Σ net reconstructs the observed loss exactly; the residual
        # is reported so a regression is visible rather than silent.
        "net_total": sum(net.values()),
        "residual": total_loss - (baseline + sum(net.values())),
        "redistributed": redistributed,
        "features": rows_out,
        "headline": (
            f"{worst['added']:.0f} of your {total_loss:.0f} lost win% is attributable to "
            f"{worst['label'].lower()}, independent of the other conditions."
            if worst and worst["added"] > 0
            else None
        ),
        "note": (
            "Every move starts from your average loss — that is the baseline. Each "
            "condition then adds loss where it hurts and spares it elsewhere, and "
            "the two sides cancel exactly back to your observed total. Unlike the "
            "leak board these figures do not overlap: conditions that travel "
            "together, like phase and move number, split the shared credit evenly "
            "rather than both claiming it."
        ),
    }
