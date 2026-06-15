"""Chess Volatility Bar — core library.

Public API:
    - :class:`Engine` — context-managed Stockfish wrapper.
    - :func:`compute_volatility` — one-ply (Phase 1) and recursive (Phase 1.5).
    - :class:`VolatilityResult` — return type of ``compute_volatility``.
    - :func:`analyze_pgn` — per-ply volatility analysis of a PGN.
    - :func:`classify_move` — eval/volatility move classification.
    - :class:`Classification` — structured output of ``classify_move``.
    - :func:`explain` — deterministic explainer over a :class:`VolatilityResult`.
    - :class:`Explanation` — structured output of :func:`explain`.
    - :exc:`EngineNotFoundError` — raised when Stockfish cannot be located.
"""

from chess_vol.analyze import PlyResult, analyze_pgn
from chess_vol.classify import Classification, classify_move
from chess_vol.engine import Engine, EngineNotFoundError
from chess_vol.explain import Explanation, explain
from chess_vol.volatility import VolatilityResult, compute_volatility, mate_to_cp

__all__ = [
    "Classification",
    "Engine",
    "EngineNotFoundError",
    "Explanation",
    "PlyResult",
    "VolatilityResult",
    "analyze_pgn",
    "classify_move",
    "compute_volatility",
    "explain",
    "mate_to_cp",
]
