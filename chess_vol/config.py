"""All tunable constants for the volatility algorithm.

Values here map directly to README §3.1 (one-ply formula), §3.3 (mate handling),
§3.4 (decided flag), §3.6 (color mapping), and §6 (recursion defaults).

Per README §7, Phase 2 is responsible for calibrating these against a real
corpus. The values below are the documented starting points.
"""

from __future__ import annotations

from typing import Final

# --- §3.1 One-ply formula ---------------------------------------------------

K: Final[float] = 150.0
"""Normalization scaling constant (centipawns). Phase 1 default.

Phase 2 will tune separate values for shallow vs. deep modes (see
``K_SHALLOW`` / ``K_DEEP``) — both initially alias to this value.
"""

K_SHALLOW: Final[float] = K
"""Normalization constant used for one-ply (``recurse_depth=0``)."""

K_DEEP: Final[float] = K
"""Normalization constant used for recursive mode. Phase 2 re-tunes this."""

DROP_CAP: Final[float] = 2000.0
"""Maximum value for any single ``drop_i = e_1 - e_i``."""

EVAL_SCALE_GRACE: Final[float] = 200.0
"""Centipawns below which no eval-based dampening applies."""

EVAL_SCALE_WIDTH: Final[float] = 300.0
"""Controls how fast dampening kicks in above the grace zone."""

EVAL_SCALE_MAX: Final[float] = 2000.0
"""Cap on ``scale_eval`` so mate scores don't collapse V to zero."""

# --- §3.1 defaults for analysis --------------------------------------------

DEFAULT_DEPTH: Final[int] = 18
DEFAULT_MULTIPV: Final[int] = 6

# --- §3.2 / §6 Recursion defaults ------------------------------------------

DEFAULT_RECURSE_DEPTH: Final[int] = 0
"""``0`` → pure one-ply (Phase 1 behavior)."""

DEFAULT_RECURSE_K: Final[int] = 3
"""Recurse into the top-k candidate moves, not all MultiPV lines."""

DEFAULT_RECURSE_ALPHA: Final[float] = 0.5
"""Each recursion level contributes ``alpha`` times the one above it."""

DEFAULT_CHILD_DEPTH: Final[int] = 12
"""Recursed calls use shallower search — children are sensors, not oracles."""

# --- §3.3 Mate handling ----------------------------------------------------

MATE_BASE: Final[int] = 2000
MATE_STEP: Final[int] = 50
MATE_MAX_N: Final[int] = 20

# --- §3.4 Decided-position flag --------------------------------------------

DECIDED_BEST_CP: Final[float] = 800.0
"""``|e_1|`` must exceed this for ``decided=True``."""

DECIDED_ALT_CP: Final[float] = 400.0
"""``|e_2|`` must also exceed this for ``decided=True``."""

RECAPTURE_DAMPEN: Final[float] = 1.0 / 3.0
"""Multiplier applied to ``V_local`` when the side-to-move is in a forced
recapture (opponent just captured on square S, best move recaptures on S).

Forced recaptures spuriously inflate volatility: every non-recapture move
loses material, producing a knife-edge drop pattern even though the player
has no real choice. Dampening pulls the bar back down so trades read as
the calm sequences they are."""

# --- §3.6 Color mapping ----------------------------------------------------

COLOR_LOW_MAX: Final[float] = 25.0
"""V below this → gray/green."""

COLOR_MED_MAX: Final[float] = 60.0
"""V in ``[COLOR_LOW_MAX, COLOR_MED_MAX)`` → yellow; at/above → red."""


def color_for(score: float) -> str:
    """Return one of ``"low"`` / ``"medium"`` / ``"high"`` per §3.6 thresholds."""
    if score < COLOR_LOW_MAX:
        return "low"
    if score < COLOR_MED_MAX:
        return "medium"
    return "high"
