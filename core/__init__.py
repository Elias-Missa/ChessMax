"""Shared chess primitives used by Puzzles 2.0 and Game Review 2.0.

``core`` is an internal package with **no FastAPI or route imports** (Game
Review 2.0 spec §1). It owns the primitives both features need so they cannot
drift:

    - :mod:`core.volatility`   — the volatility algorithm (moved here from
      ``chess_vol.volatility``; that module is now a thin re-export shim).
    - :mod:`core.evaluation`   — cp <-> WDL <-> win% conversion and ``delta_w``.
    - :mod:`core.acceptable`   — the acceptable-move set ``A`` and the single
      ``tau`` site that Phase 4 makes ``f(volatility)``.
    - :mod:`core.features`     — per-move feature extraction for findability.
    - :mod:`core.findability`  — the findability score itself.
    - :mod:`core.human`        — rating-conditioned human-policy wrapper.
    - :mod:`core.cache`        — Zobrist-keyed SQLite feature cache.
"""

from __future__ import annotations

__all__: list[str] = []
