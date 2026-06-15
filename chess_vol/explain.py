"""Deterministic, template-based explainer for :class:`VolatilityResult`.

Given a result, produces a structured :class:`Explanation` with:

* a one-sentence ``summary`` written for a chess player, not an algorithm,
* a list of ``components`` that decompose the score into named contributions
  (move-choice, reply, eval-scale dampening) so a UI can render bar splits,
* a list of ``patterns`` (machine-readable tags) that describe *why* the
  position scored the way it did — for downstream filtering, badges, or
  optional LLM prose enrichment.

No engine, no LLM, no I/O — pure logic over the data already in
:class:`VolatilityResult`. Tests live in ``tests/test_explain.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

from chess_vol.config import (
    COLOR_LOW_MAX,
    COLOR_MED_MAX,
    K_DEEP,
    K_SHALLOW,
)
from chess_vol.volatility import VolatilityResult

# --------------------------------------------------------------------------- #
# Tunable thresholds for pattern detection                                     #
# --------------------------------------------------------------------------- #

#: Drop (cp) below which an alternative is "still fine" — used to count how
#: many credible options the position offers.
CREDIBLE_DROP_CP: Final[int] = 50

#: Drop (cp) above which an alternative is "losing" — used to detect "only one
#: move keeps the advantage" patterns.
LOSING_DROP_CP: Final[int] = 200

#: Eval (cp) above which a position is considered clearly winning for STM.
WINNING_EVAL_CP: Final[int] = 200

#: Eval (cp) below which a position is considered clearly losing for STM.
LOSING_EVAL_CP: Final[int] = -200

#: Scale below which we explicitly call out eval-aware dampening to the user.
SCALE_DAMPENED_THRESHOLD: Final[float] = 0.6

#: Mate-cp threshold (mirrors :func:`mate_to_cp` for ``MATE_MAX_N`` / 2).
#: Any best-eval at or above this is treated as "mate available" prose.
MATE_THRESHOLD_CP: Final[int] = 1500

# --------------------------------------------------------------------------- #
# Public types                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Component:
    """One named contribution to the V score.

    Components are intended for UI rendering (e.g. a stacked bar with
    ``move_choice`` and ``reply`` segments) and should be treated as a
    *visualisation aid*, not an exact algebraic decomposition.
    """

    name: str
    """Stable identifier — ``"move_choice"``, ``"reply"``, ``"eval_scale"``."""

    label: str
    """Human-readable label for the UI (``"Move-choice volatility"``)."""

    value: float
    """Approximate contribution in the same 0-100 space as ``score``.
    For ``eval_scale`` this is the *amount removed* by dampening (negative
    in spirit, but stored positive for layout simplicity — read with
    ``direction``)."""

    direction: str
    """``"adds"`` or ``"removes"``. ``adds`` means this component pushed V up;
    ``removes`` means dampening pulled V down. UIs use this to choose color."""

    detail: str
    """One short sentence explaining this component for a tooltip."""


@dataclass(frozen=True)
class Explanation:
    """Structured explanation of a :class:`VolatilityResult`."""

    summary: str
    """One-sentence headline written for a player. Always present."""

    components: list[Component] = field(default_factory=list)
    """Named contributions to V — for stacked-bar UI rendering."""

    patterns: list[str] = field(default_factory=list)
    """Machine-readable tags — see ``PATTERN_*`` constants below."""

    headline_pattern: str | None = None
    """The single most important pattern (drives the summary). ``None`` for
    terminal/only-move/decided where no pattern is the primary driver."""


# --------------------------------------------------------------------------- #
# Pattern tags (stable identifiers — UI may map to icons/badges)               #
# --------------------------------------------------------------------------- #

PATTERN_ONLY_MOVE: Final[str] = "only_move"
"""Exactly one legal move; V is undefined and reported as ``—``."""

PATTERN_CHECKMATE: Final[str] = "checkmate"
PATTERN_STALEMATE: Final[str] = "stalemate"

PATTERN_MATE_AVAILABLE: Final[str] = "mate_available"
"""Best line is a mate sequence. Distinct from "decided" — the mate may be
the only winning option."""

PATTERN_DECIDED: Final[str] = "decided"
"""Position is technically over: best AND backup both clearly winning for the
same side (README §3.4). UI dims the bar."""

PATTERN_KNIFE_EDGE: Final[str] = "knife_edge"
"""One move keeps the advantage; every other move loses meaningfully. The
classic 'high move-choice volatility' case."""

PATTERN_FEW_GOOD_MOVES: Final[str] = "few_good_moves"
"""2-3 credible moves, the rest lose. Sharp but not knife-edge."""

PATTERN_FORGIVING: Final[str] = "forgiving"
"""Multiple moves are within ``CREDIBLE_DROP_CP`` of best — mistakes here
are cheap. Drives the green-bar 'position plays itself' narrative."""

PATTERN_REPLY_DOMINATES: Final[str] = "reply_dominates"
"""Deep mode only: the resulting position contributes more V than the
current move choice does. Explains why a 'simple' move can have a high
deep V — the next move is the hard one."""

PATTERN_SCALE_DAMPENED: Final[str] = "scale_dampened"
"""Eval-aware scaling pulled V down because both best and backup are already
crushing. Without this tag, users see a green bar in a +500 position and
think the algorithm is broken."""

PATTERN_DEFENSIVE_CRISIS: Final[str] = "defensive_crisis"
"""Best move is roughly 0 / slightly worse, alternatives all losing. A
common knife-edge sub-case where you're not winning, you're just *holding*."""


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _format_eval(cp: int) -> str:
    """Render a centipawn eval the way a chess UI would (e.g. ``+1.34``).

    Mate-translated values (above the realistic eval ceiling) are shown as
    ``M?`` — we don't know the exact mate-distance from cp alone, but we know
    it's a mate. Use ``MATE_THRESHOLD_CP`` as the cutoff."""
    if abs(cp) >= MATE_THRESHOLD_CP:
        return f"{'M' if cp > 0 else '-M'}?"
    return f"{cp / 100:+.2f}"


def _count_credible_alts(best_cp: int, alts_cp: list[int]) -> int:
    """How many alternatives are within ``CREDIBLE_DROP_CP`` of the best?
    The best move itself is *not* counted; the return is purely the number of
    *backup* options the position offers."""
    return sum(1 for e in alts_cp if (best_cp - e) <= CREDIBLE_DROP_CP)


def _count_losing_alts(best_cp: int, alts_cp: list[int]) -> int:
    """How many alternatives drop by ``LOSING_DROP_CP`` or more? These are
    the moves that 'lose' relative to the best."""
    return sum(1 for e in alts_cp if (best_cp - e) >= LOSING_DROP_CP)


def _k_for(recurse_depth: int) -> float:
    return K_SHALLOW if recurse_depth == 0 else K_DEEP


def _score_from_raw(raw_cp: float, recurse_depth: int) -> float:
    """Re-normalize a raw cp value to the 0-100 V scale."""
    return 100.0 * (1.0 - math.exp(-raw_cp / _k_for(recurse_depth)))


# --------------------------------------------------------------------------- #
# Component builders                                                           #
# --------------------------------------------------------------------------- #


def _build_components(result: VolatilityResult) -> list[Component]:
    """Decompose ``result`` into UI-friendly components.

    The decomposition is approximate by design: the 0-100 V scale is
    *non-linear* (exp-normalized), so contributions don't add to V exactly.
    What we want is a stacked-bar that *looks right*: move-choice and reply
    segments sized in proportion to their share of ``raw_cp``.
    """
    components: list[Component] = []
    score = result.score
    raw = result.raw_cp
    local_raw = result.local_raw_cp
    if score is None or raw is None or local_raw is None:
        return components

    if raw > 0:
        local_share = local_raw / raw
    else:
        local_share = 1.0
    reply_share = max(0.0, 1.0 - local_share)

    components.append(
        Component(
            name="move_choice",
            label="Move-choice volatility",
            value=round(score * local_share, 1),
            direction="adds",
            detail=(
                "How punishing it is to pick the wrong move *right now*, "
                "based on the gap between the best line and the alternatives."
            ),
        )
    )

    if result.recurse_depth_used > 0:
        components.append(
            Component(
                name="reply",
                label="Reply volatility",
                value=round(score * reply_share, 1),
                direction="adds",
                detail=(
                    "How sharp the resulting position becomes after the "
                    "best move — the danger living one move deeper."
                ),
            )
        )

    if result.scale < SCALE_DAMPENED_THRESHOLD:
        # Estimate what V *would* have been without dampening, then report the
        # delta as the dampening contribution. Approximate (non-linear), but
        # close enough for a stacked-bar visualisation.
        if result.scale > 0 and raw > 0:
            undampened_raw = raw / result.scale
            undampened_v = _score_from_raw(undampened_raw, result.recurse_depth_used)
            removed = max(0.0, undampened_v - score)
        else:
            removed = 0.0
        components.append(
            Component(
                name="eval_scale",
                label="Decisive-position dampening",
                value=round(removed, 1),
                direction="removes",
                detail=(
                    "Both your best move and your backup are already "
                    "crushing, so picking the second-best doesn't hurt much."
                ),
            )
        )

    return components


# --------------------------------------------------------------------------- #
# Pattern detection                                                            #
# --------------------------------------------------------------------------- #


def _detect_patterns(result: VolatilityResult) -> tuple[list[str], str | None]:
    """Return ``(all_patterns, headline_pattern)``.

    The headline is the single most important pattern — the one that should
    drive the summary sentence. Other patterns may still be set as badges.
    """
    patterns: list[str] = []
    headline: str | None = None

    if result.reason == "checkmate":
        return [PATTERN_CHECKMATE], PATTERN_CHECKMATE
    if result.reason == "stalemate":
        return [PATTERN_STALEMATE], PATTERN_STALEMATE
    if result.reason == "only_move":
        return [PATTERN_ONLY_MOVE], PATTERN_ONLY_MOVE

    best = result.best_eval_cp
    alts = result.alt_evals_cp

    mate_available = best >= MATE_THRESHOLD_CP
    if mate_available:
        patterns.append(PATTERN_MATE_AVAILABLE)

    if result.decided:
        patterns.append(PATTERN_DECIDED)
        # Decided is the headline — bar is dimmed, that's the dominant story.
        return patterns, PATTERN_DECIDED

    if result.scale < SCALE_DAMPENED_THRESHOLD:
        patterns.append(PATTERN_SCALE_DAMPENED)

    if alts:
        credible = _count_credible_alts(best, alts)
        losing = _count_losing_alts(best, alts)
        total_alts = len(alts)

        if credible == 0 and losing >= max(1, total_alts - 1):
            # No backup is comparable to the best, and almost every other
            # move loses meaningfully. Knife edge.
            patterns.append(PATTERN_KNIFE_EDGE)
            if (
                LOSING_EVAL_CP <= best <= WINNING_EVAL_CP
                and best <= 0
            ):
                # You're not winning — you're holding. Defensive crisis.
                patterns.append(PATTERN_DEFENSIVE_CRISIS)
                headline = PATTERN_DEFENSIVE_CRISIS
            else:
                headline = PATTERN_KNIFE_EDGE
        elif credible <= 2 and losing >= total_alts - credible - 1:
            patterns.append(PATTERN_FEW_GOOD_MOVES)
            headline = headline or PATTERN_FEW_GOOD_MOVES
        elif credible >= 3:
            patterns.append(PATTERN_FORGIVING)
            headline = headline or PATTERN_FORGIVING

    # Reply dominates: deep mode only, and only if reply share > local share.
    if (
        result.recurse_depth_used > 0
        and result.raw_cp is not None
        and result.local_raw_cp is not None
        and result.raw_cp > 0
        and result.local_raw_cp / result.raw_cp < 0.4
    ):
        patterns.append(PATTERN_REPLY_DOMINATES)
        # If we don't have a stronger headline yet, this becomes it.
        headline = headline or PATTERN_REPLY_DOMINATES

    # Mate-available is a strong narrative; only use it as headline if nothing
    # more specific (knife-edge, reply-dominates) already won.
    if mate_available and headline is None:
        headline = PATTERN_MATE_AVAILABLE

    # Pure dampening as headline only if nothing else fired — e.g. quiet
    # winning conversion.
    if headline is None and PATTERN_SCALE_DAMPENED in patterns:
        headline = PATTERN_SCALE_DAMPENED

    return patterns, headline


# --------------------------------------------------------------------------- #
# Summary writer                                                               #
# --------------------------------------------------------------------------- #


def _summary(result: VolatilityResult, headline: str | None) -> str:
    """Pick a one-sentence summary based on the headline pattern.

    Sentences reference the engine lines using ``top_lines[i].san`` when
    available so the user sees actual moves, not just numbers.
    """
    score = result.score
    best_san = result.top_lines[0].san if result.top_lines else None
    second_san = result.top_lines[1].san if len(result.top_lines) >= 2 else None

    if headline == PATTERN_CHECKMATE:
        return "Checkmate — the game is over."
    if headline == PATTERN_STALEMATE:
        return "Stalemate — the game is drawn."
    if headline == PATTERN_ONLY_MOVE:
        if best_san:
            return f"Only one legal move: **{best_san}**. Volatility is undefined."
        return "Only one legal move available — volatility is undefined."

    if headline == PATTERN_DECIDED:
        return (
            "Position is technically decided — both the best move and the backup "
            "are already winning for the same side. The bar is dimmed."
        )

    if headline == PATTERN_DEFENSIVE_CRISIS:
        if best_san and second_san:
            return (
                f"Defensive crisis: only **{best_san}** holds. Every other move "
                f"loses material — second-best **{second_san}** drops to "
                f"{_format_eval(result.alt_evals_cp[0])}."
            )
        return "Defensive crisis: one move holds, every alternative loses."

    if headline == PATTERN_KNIFE_EDGE:
        if best_san and second_san:
            return (
                f"Knife edge: **{best_san}** keeps the advantage; "
                f"the next best ({second_san}) drops to "
                f"{_format_eval(result.alt_evals_cp[0])}."
            )
        return "Knife edge: only the top move keeps the advantage."

    if headline == PATTERN_FEW_GOOD_MOVES:
        if best_san:
            return (
                f"A handful of credible moves around **{best_san}**, but most "
                "alternatives lose ground — pick carefully."
            )
        return "A handful of credible moves; most alternatives lose ground."

    if headline == PATTERN_FORGIVING:
        if score is not None and score < COLOR_LOW_MAX:
            return "The position plays itself — multiple moves all keep things equal."
        return "Several reasonable options here; the position is forgiving."

    if headline == PATTERN_REPLY_DOMINATES:
        if best_san:
            return (
                f"The current move is easy — **{best_san}** is clearly best — "
                "but the resulting position is sharp. The danger is one move deeper."
            )
        return "The current choice is easy, but the resulting position is sharp."

    if headline == PATTERN_MATE_AVAILABLE:
        if best_san:
            return f"Mate is on the board with **{best_san}**."
        return "Mate is on the board."

    if headline == PATTERN_SCALE_DAMPENED:
        return (
            "Winning with multiple paths — V is dimmed because the second-best "
            "move is also clearly winning."
        )

    # Generic fallback driven only by the score bucket.
    if score is None:
        return "Volatility is undefined for this position."
    if score < COLOR_LOW_MAX:
        return "Quiet position — most reasonable moves keep the evaluation."
    if score < COLOR_MED_MAX:
        return "Moderate sharpness — a sub-optimal move costs noticeable ground."
    return "Sharp position — wrong moves are punished heavily."


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


def explain(result: VolatilityResult) -> Explanation:
    """Build a structured :class:`Explanation` from a :class:`VolatilityResult`.

    Pure function: same input always yields the same output. Safe to call on
    any result, including terminal positions and only-move cases.
    """
    patterns, headline = _detect_patterns(result)
    components = _build_components(result)
    summary = _summary(result, headline)
    return Explanation(
        summary=summary,
        components=components,
        patterns=patterns,
        headline_pattern=headline,
    )


__all__ = [
    "CREDIBLE_DROP_CP",
    "LOSING_DROP_CP",
    "LOSING_EVAL_CP",
    "MATE_THRESHOLD_CP",
    "PATTERN_CHECKMATE",
    "PATTERN_DECIDED",
    "PATTERN_DEFENSIVE_CRISIS",
    "PATTERN_FEW_GOOD_MOVES",
    "PATTERN_FORGIVING",
    "PATTERN_KNIFE_EDGE",
    "PATTERN_MATE_AVAILABLE",
    "PATTERN_ONLY_MOVE",
    "PATTERN_REPLY_DOMINATES",
    "PATTERN_SCALE_DAMPENED",
    "PATTERN_STALEMATE",
    "SCALE_DAMPENED_THRESHOLD",
    "WINNING_EVAL_CP",
    "Component",
    "Explanation",
    "explain",
]
