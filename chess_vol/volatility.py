"""Backward-compatible re-export shim for the volatility algorithm.

The implementation was **moved** to :mod:`core.volatility` in Game Review 2.0
Phase 0 so Puzzles 2.0 and Game Review share one copy (spec §1, §8). This
module keeps ``from chess_vol.volatility import ...`` working for all existing
callers and tests — it is a pure alias, not a second implementation.

New code should import from :mod:`core.volatility` directly.
"""

from __future__ import annotations

# Public surface (dataclasses, protocols, and the pure helpers) — anything a
# star-import would have picked up before the move.
from core.volatility import *  # noqa: F401,F403

# Private helpers and type aliases that the existing test-suite imports by name
# (star-import skips leading-underscore names and does not re-export aliases).
from core.volatility import (  # noqa: F401
    EngineLike,
    ScaleFn,
    TopLine,
    VolatilityResult,
    WeightsFn,
    _build_top_lines,
    _compute_local,
    _compute_raw,
    _is_decided,
    _is_obvious_recapture,
    _RawResult,
    compute_volatility,
    default_scale_fn,
    default_weights,
    info_to_cp,
    mate_to_cp,
)
