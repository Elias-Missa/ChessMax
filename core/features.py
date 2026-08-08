"""Per-move feature extraction for findability (Game Review 2.0 spec §3.3).

Maia is pure pattern recognition with no search: it will call a forced 6-move
mate "unfindable" because it never calculates. That is the single largest
false-positive source, so the raw human prior ``pi_r`` is reweighted by a
*search-visibility* term ``calc(m)`` built from four features:

    dep(m)  — shallow moves (found early in iterative deepening) are visible.
    forc(m) — forcing lines (checks/captures/promotions/recaptures) get calculated.
    narr(m) — narrow lines (few reasonable opponent replies) are trees a human walks.
    q(m)    — a *quiet* uniquely-winning move buried deep is the "no human finds
              this" detector; it *suppresses* the calc boost.

Everything here is **pure** — it consumes an already-captured
:class:`MoveEval` and a board, and returns numbers. The engine probing that
produces ``d_star`` / ``narr`` / ``q`` lives in :mod:`core.engine`; keeping the
math pure is what makes findability testable without a Stockfish binary and
reproducible across hardware.
"""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import TypeVar

import chess

from core.evaluation import Wdl

M = TypeVar("M", bound=Hashable)


@dataclass
class MoveEval:
    """One candidate move as captured from the engine's MultiPV search.

    The engine adapter (:mod:`core.engine`) fills this in; features are then
    derived purely. ``narr`` and ``q`` require probing beyond the root PV, so
    they are captured here rather than recomputed — they default to the neutral
    "no calculation credit" values so a fake/degraded engine never *inflates*
    findability.
    """

    move: chess.Move
    cp: int | None = None
    mate: int | None = None
    pv: list[chess.Move] = field(default_factory=list)
    d_star: int = 1
    """Depth (iterative-deepening iteration) at which this move first entered
    the root PV. 1 = seen immediately / very shallow → maximally visible."""
    wdl: Wdl | None = None
    narr: float = 0.0
    """Pre-computed narrowness in ``[0, 1]``; 0 = wide (no calc credit)."""
    q: int = 0
    """Quiet-deep flag (0/1); 1 suppresses the calc boost for this move."""


@dataclass(frozen=True)
class MoveFeatures:
    """Derived, unit-scaled features and the combined ``calc`` term."""

    dep: float
    forc: float
    narr: float
    q: int
    calc: float


def depthness(d_star: int, *, offset: int = 1, span: int = 14) -> float:
    """``dep(m) = clamp(1 - (d_star - offset) / span, 0, 1)`` (spec §3.3)."""
    raw = 1.0 - (d_star - offset) / span
    return max(0.0, min(1.0, raw))


def forcingness(board_before: chess.Board, pv: list[chess.Move], *, plies: int = 4) -> float:
    """Fraction of the first ``plies`` PV half-moves that are *forcing*.

    A ply is forcing if it is a check, a capture, a promotion, or a recapture
    on the square the previous ply captured on (a near-forced reply). Returns
    ``0.0`` for an empty PV.
    """
    if not pv:
        return 0.0
    board = board_before.copy(stack=True)
    considered = 0
    forcing = 0
    last_capture_square: int | None = None
    for move in pv[:plies]:
        if move not in board.legal_moves:
            break
        considered += 1
        is_capture = board.is_capture(move)
        gives_check = board.gives_check(move)
        is_promotion = move.promotion is not None
        is_recapture = is_capture and move.to_square == last_capture_square
        if is_capture or gives_check or is_promotion or is_recapture:
            forcing += 1
        last_capture_square = move.to_square if is_capture else None
        board.push(move)
    if considered == 0:
        return 0.0
    return forcing / considered


def calc_term(
    dep: float,
    forc: float,
    narr: float,
    q: int,
    *,
    w_dep: float = 0.45,
    w_forc: float = 0.35,
    w_narr: float = 0.20,
    q_weight: float = 0.6,
) -> float:
    """``calc(m) = (w_dep*dep + w_forc*forc + w_narr*narr) * (1 - q_weight*q)`` (spec §3.3)."""
    base = w_dep * dep + w_forc * forc + w_narr * narr
    return base * (1.0 - q_weight * q)


def compute_features(
    move_eval: MoveEval,
    board_before: chess.Board,
    *,
    dep_offset: int = 1,
    dep_span: int = 14,
    forc_plies: int = 4,
    w_dep: float = 0.45,
    w_forc: float = 0.35,
    w_narr: float = 0.20,
    q_weight: float = 0.6,
) -> MoveFeatures:
    """Derive :class:`MoveFeatures` for one candidate move (pure)."""
    dep = depthness(move_eval.d_star, offset=dep_offset, span=dep_span)
    forc = forcingness(board_before, move_eval.pv, plies=forc_plies)
    narr = max(0.0, min(1.0, move_eval.narr))
    q = 1 if move_eval.q else 0
    calc = calc_term(dep, forc, narr, q, w_dep=w_dep, w_forc=w_forc, w_narr=w_narr, q_weight=q_weight)
    return MoveFeatures(dep=dep, forc=forc, narr=narr, q=q, calc=calc)


def reweight(
    policy: dict[M, float],
    calc_by_move: dict[M, float],
    *,
    beta: float = 1.2,
) -> dict[M, float]:
    """Calculation-reweight a human prior: ``pi_tilde ∝ pi * exp(beta * calc)``.

    ``policy`` maps each MultiPV move to its human prior ``pi_r(m)``; those need
    not sum to 1 — the leftover mass ``1 - sum(pi)`` is a *tail bucket* of moves
    outside the MultiPV set (spec §3.3 Step 5). The tail carries ``calc = 0``
    (``exp(0) = 1``), so it is renormalized alongside the boosted moves but
    never boosted itself. Returns ``pi_tilde`` over the same moves as ``policy``
    (the tail is implicit: the returned values sum to ``<= 1``).
    """
    if not policy:
        return {}
    numerators: dict[M, float] = {}
    for move, pi in policy.items():
        calc = calc_by_move.get(move, 0.0)
        numerators[move] = pi * math.exp(beta * calc)
    tail = max(0.0, 1.0 - sum(policy.values()))
    z = sum(numerators.values()) + tail
    if z <= 0:
        return {move: 0.0 for move in policy}
    return {move: num / z for move, num in numerators.items()}


__all__ = [
    "MoveEval",
    "MoveFeatures",
    "calc_term",
    "compute_features",
    "depthness",
    "forcingness",
    "reweight",
]
