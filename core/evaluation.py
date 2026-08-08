"""Evaluation conversions: centipawns <-> WDL <-> win probability.

Game Review 2.0 spec §1 (`core/evaluation.py`). The cardinal rule: **never
compare centipawns directly.** 50cp at eval 0.0 is decisive; 50cp at +7 is
noise. Everything downstream (the acceptable set, findability, classification)
reasons in *win probability* — expected score in ``[0, 1]``.

Two independent routes to a win probability are provided because the engine
may or may not report WDL:

* :func:`win_prob` — from Stockfish ``UCI_ShowWDL`` (a per-mille W/D/L triple).
  Preferred: it is contempt- and eval-shape-aware.
* :func:`win_prob_cp` — logistic fallback from a plain centipawn score, using
  the same constant as the existing game-review win model so the two agree.

:func:`delta_w` is source-agnostic — it accepts either WDL triples or bare win
probabilities and returns win-percentage **points** lost (always ``>= 0``).
"""

from __future__ import annotations

import math

Wdl = tuple[int, int, int]

#: Logistic steepness for cp -> win%. Matches the constant baked into the
#: existing ``chess_vol.game_review`` win model so both agree to the digit.
CP_LOGISTIC_K: float = 0.00368208

#: Centipawn magnitude a mate is clamped to before the logistic, so a forced
#: mate resolves to win_prob 1.0 / 0.0 rather than a finite plateau.
MATE_CP_CLAMP: int = 10_000


def win_prob(wdl: Wdl) -> float:
    """WDL (per-mille, side-to-move POV) -> expected score in ``[0, 1]``.

    ``(w + 0.5 * d) / (w + d + l)``. Robust to totals other than 1000 (some
    builds report slightly off), and returns ``0.5`` for a degenerate all-zero
    triple rather than dividing by zero.
    """
    w, d, l = wdl
    total = w + d + l
    if total <= 0:
        return 0.5
    return (w + 0.5 * d) / total


def win_prob_cp(cp: int | float) -> float:
    """Centipawn score (side-to-move POV) -> expected score in ``[0, 1]``.

    Logistic fallback for when the engine does not report WDL. Mate-magnitude
    scores should be pre-clamped with :func:`clamp_mate_cp`.
    """
    bounded = max(-float(MATE_CP_CLAMP), min(float(MATE_CP_CLAMP), float(cp)))
    return 1.0 / (1.0 + math.exp(-CP_LOGISTIC_K * bounded))


def clamp_mate_cp(cp: int | float | None, mate: int | None) -> float:
    """Collapse a (cp, mate) pair to a single centipawn value.

    ``mate`` (plies, side-to-move POV, ``+`` = we mate) dominates when present:
    it clamps to ``+/-MATE_CP_CLAMP`` so the logistic saturates. Otherwise the
    plain ``cp`` is returned. Raises if neither is available.
    """
    if mate is not None:
        return float(MATE_CP_CLAMP) if mate > 0 else -float(MATE_CP_CLAMP)
    if cp is not None:
        return float(cp)
    raise ValueError("clamp_mate_cp requires either cp or mate")


def _as_prob(value: Wdl | float | int) -> float:
    """Coerce a WDL triple *or* a bare win probability into ``[0, 1]``."""
    if isinstance(value, tuple):
        return win_prob(value)
    return float(value)


def delta_w(best: Wdl | float, move: Wdl | float) -> float:
    """Win-percentage **points** lost by ``move`` versus ``best``. Always ``>= 0``.

    Accepts WDL triples or bare win probabilities for either argument. Because
    ``best`` is by construction the maximum, the result is non-negative; it is
    clamped at 0 to stay faithful to that contract even under tiny float noise
    or a caller that passes an off-by-epsilon "best".
    """
    return max(0.0, 100.0 * (_as_prob(best) - _as_prob(move)))


__all__ = [
    "CP_LOGISTIC_K",
    "MATE_CP_CLAMP",
    "Wdl",
    "clamp_mate_cp",
    "delta_w",
    "win_prob",
    "win_prob_cp",
]
