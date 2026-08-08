"""The acceptable-move set ``A`` and the single ``tau`` site.

Game Review 2.0 spec §1 (`core/acceptable.py`). ``A`` is the set of moves whose
win-percentage loss versus the best move is within ``tau``. It is the same
concept as Puzzles 2.0's "any move that doesn't tank the eval" rule and the
review's acceptable set — there must be exactly one implementation.

``tau`` is a constant (2.5 win% points) in Phases 0-3 and becomes
``f(volatility)`` in Phase 4. **Every** caller routes its ``tau`` through
:func:`tau_for`, so Phase 4 is a single-site change here — no call site needs
to know volatility exists until then.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import TypeVar

from core.evaluation import Wdl, delta_w

#: Constant tau (win-percentage points) for Phases 0-3.
TAU_DEFAULT: float = 2.5

M = TypeVar("M", bound=Hashable)


def tau_for(
    volatility: float | None = None,
    *,
    base: float = TAU_DEFAULT,
    enabled: bool = False,
    tau_min: float | None = None,
    tau_max: float | None = None,
    vol_full: float = 100.0,
) -> float:
    """Return the acceptable-set threshold in win-% points — the **single site**.

    This is the one place ``tau`` knowledge lives (spec §1, §5), so Phase 4 is a
    single-function change and every caller is agnostic.

    * Phases 0-3 / ``enabled=False`` (default) or no ``volatility``: constant
      ``base`` (:data:`TAU_DEFAULT`) — bit-for-bit the old behavior.
    * Phase 4 / ``enabled=True``: ``tau = f(volatility)``. A sharp tactical
      position (high volatility) tolerates a wider band; a quiet maneuvering
      position a narrower one. Linear in normalized volatility between
      ``tau_min`` (quiet) and ``tau_max`` (sharp).

    The mapping is a *mechanism*; ``tau_min``/``tau_max`` are placeholders until
    fit in the Phase 3 harness — hence ``enabled`` defaults off.
    """
    if not enabled or volatility is None:
        return base
    lo = base if tau_min is None else tau_min
    hi = base if tau_max is None else tau_max
    frac = max(0.0, min(1.0, volatility / vol_full)) if vol_full > 0 else 0.0
    return lo + (hi - lo) * frac


def acceptable_set(
    move_evals: dict[M, Wdl | float],
    tau: float | None = None,
    *,
    volatility: float | None = None,
) -> set[M]:
    """Moves whose win% loss versus the best move is ``<= tau``.

    Parameters
    ----------
    move_evals:
        Maps each candidate move to its evaluation, expressed as either a WDL
        triple (per-mille, side-to-move POV) or a bare win probability in
        ``[0, 1]``. The best move is taken as the maximum win probability.
    tau:
        Explicit threshold in win-% points. When ``None`` (the normal case),
        it is derived from :func:`tau_for` — pass ``volatility`` to influence it
        once Phase 4 lands.
    volatility:
        Position volatility, forwarded to :func:`tau_for`. Ignored in Phases 0-3.

    Returns
    -------
    set
        Always contains the best move (its own loss is 0). Empty only when
        ``move_evals`` is empty.
    """
    if not move_evals:
        return set()
    threshold = tau if tau is not None else tau_for(volatility)
    best = max(move_evals.values(), key=_prob)
    return {move for move, ev in move_evals.items() if delta_w(best, ev) <= threshold}


def _prob(value: Wdl | float) -> float:
    if isinstance(value, tuple):
        w, d, l = value
        total = w + d + l
        return 0.5 if total <= 0 else (w + 0.5 * d) / total
    return float(value)


__all__ = ["TAU_DEFAULT", "acceptable_set", "tau_for"]
