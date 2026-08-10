"""Phase 8 — latent skill model (Item Response Theory).

IRT needs an item difficulty and a binary response, and the pipeline already
produces both:

* **Difficulty** ``b`` = ``r_find``, the calibrated rating at which a position
  becomes findable. Already on the rating scale, so ``θ`` comes out in rating
  points with no rescaling — exactly what should be displayed.
* **Response** = did the user play an acceptable move.

Strictly better than an accuracy average, because it accounts for the fact that
different players face different difficulty distributions: getting easy
positions right should not outrank getting hard ones right.

The 2PL model, per skill category:

    P(correct | θ, b, a) = 1 / (1 + exp(-a (θ - b) / 400))

``θ`` is fitted by Newton–Raphson on the log-likelihood, with the standard error
from the Fisher information — which is also what makes the same model a
principled adaptive selector for Puzzles 2.0 (serve items with ``b`` near ``θ``,
where information is maximal).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

from server.insights_pro import CRITICAL_HANDLED_DELTA_W, CRITICAL_VOLATILITY

#: Rating points per logit unit — keeps θ on the chess rating scale.
SCALE = 400.0

#: Minimum scored items before a category's θ is shown at all (spec 8.5).
MIN_ITEMS = 60

#: Bounds for the fit, so a category answered perfectly cannot run away.
THETA_MIN, THETA_MAX = 400.0, 3000.0

#: Per-category discrimination. Fitted across a corpus in principle; until one
#: exists these are documented defaults, and the card says so.
DEFAULT_DISCRIMINATION = {
    "tactics": 1.2,
    "endgame": 1.0,
    "defense": 1.0,
    "calculation": 1.3,
    "positional": 0.8,
}

CATEGORY_LABELS = {
    "tactics": "Tactics",
    "endgame": "Endgame technique",
    "defense": "Defense",
    "calculation": "Calculation",
    "positional": "Positional judgement",
}


def categorize(row: dict[str, Any]) -> str:
    """Assign a scored position to a skill category from its tags and phase."""

    if row.get("tactic_tags"):
        return "tactics"
    phase = str(row.get("phase") or "")
    if phase == "endgame":
        return "endgame"
    win_prob = row.get("win_prob")
    if win_prob is not None and float(win_prob) < 0.35:
        return "defense"
    volatility = row.get("volatility")
    if volatility is not None and float(volatility) >= CRITICAL_VOLATILITY:
        return "calculation"
    return "positional"


def _probability(theta: float, b: float, a: float) -> float:
    z = a * (theta - b) / SCALE
    # Guard the exponential so a wide gap cannot overflow.
    if z > 30:
        return 1.0 - 1e-12
    if z < -30:
        return 1e-12
    return 1.0 / (1.0 + math.exp(-z))


def fit_theta(
    items: Sequence[tuple[float, bool]],
    *,
    discrimination: float = 1.0,
    max_iterations: int = 60,
) -> tuple[float | None, float | None]:
    """Maximum-likelihood ``θ`` and its standard error, in rating points.

    ``items`` are ``(difficulty, correct)`` pairs. Returns ``(None, None)`` when
    the responses are all-correct or all-wrong, where the likelihood has no
    interior maximum and any point estimate would be an artefact of the bounds.
    """

    if not items:
        return None, None
    correct = sum(1 for _, ok in items if ok)
    if correct == 0 or correct == len(items):
        return None, None

    theta = sum(b for b, _ in items) / len(items)
    scale = discrimination / SCALE
    for _ in range(max_iterations):
        gradient = 0.0
        information = 0.0
        for b, ok in items:
            p = _probability(theta, b, discrimination)
            gradient += scale * ((1.0 if ok else 0.0) - p)
            information += (scale ** 2) * p * (1.0 - p)
        if information <= 1e-12:
            break
        step = gradient / information
        theta += max(-400.0, min(400.0, step))
        theta = max(THETA_MIN, min(THETA_MAX, theta))
        if abs(step) < 0.01:
            break

    information = sum(
        (scale ** 2) * _probability(theta, b, discrimination) * (1 - _probability(theta, b, discrimination))
        for b, _ in items
    )
    stderr = (1.0 / math.sqrt(information)) if information > 1e-12 else None
    return theta, stderr


def compute_skill_model(
    rows: Sequence[dict[str, Any]],
    *,
    min_items: int = MIN_ITEMS,
) -> dict[str, Any]:
    """Per-category ability with standard errors.

    Only positions carrying ``r_find`` are usable, which today means full-tier
    reviews — so the model reports its own coverage prominently rather than
    quietly fitting on a handful of items.
    """

    by_category: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    scored = 0
    for row in rows:
        if not row.get("is_user_move") or row.get("is_book"):
            continue
        difficulty = row.get("r_find")
        if difficulty is None:
            continue
        scored += 1
        acceptable = float(row.get("delta_w") or 0.0) <= CRITICAL_HANDLED_DELTA_W
        by_category[categorize(row)].append((float(difficulty), acceptable))

    categories = []
    for key, items in by_category.items():
        theta, stderr = fit_theta(
            items, discrimination=DEFAULT_DISCRIMINATION.get(key, 1.0)
        )
        categories.append({
            "category": key,
            "label": CATEGORY_LABELS.get(key, key.title()),
            "items": len(items),
            "correct": sum(1 for _, ok in items if ok),
            "theta": round(theta) if theta is not None else None,
            "stderr": round(stderr) if stderr is not None else None,
            "below_floor": len(items) < min_items,
            "mean_difficulty": sum(b for b, _ in items) / len(items) if items else None,
        })
    categories.sort(key=lambda c: -(c["theta"] or 0))

    usable = [c for c in categories if c["theta"] is not None and not c["below_floor"]]
    note = None
    if len(usable) >= 2:
        best, worst = usable[0], usable[-1]
        if best["theta"] - worst["theta"] >= 100:
            note = (
                f"You play {best['label'].lower()} like a {best['theta']} and "
                f"{worst['label'].lower()} like a {worst['theta']}."
            )

    return {
        "available": bool(usable),
        "scored_positions": scored,
        "min_items": min_items,
        "categories": categories,
        "usable": [c["category"] for c in usable],
        "note": note,
        "model": (
            "2PL item response model. Difficulty is r_find — the rating at which a "
            "position becomes findable — so θ is already in rating points. "
            "Discrimination uses documented defaults until a reference corpus "
            "allows fitting it."
        ),
        "coverage_note": (
            None if scored >= min_items
            else f"Only {scored} positions carry a findability rating. Ability "
                 f"estimates need roughly {min_items} per category, which means "
                 f"more full-tier reviews."
        ),
    }


def next_item_difficulty(theta: float | None) -> float | None:
    """Where a 2PL adaptive selector should aim (spec 8.4).

    Fisher information for 2PL peaks at ``b = θ``, so the most informative next
    item is one at the player's own ability. Shared with Puzzles 2.0 so both
    products run on one model.
    """

    return theta
