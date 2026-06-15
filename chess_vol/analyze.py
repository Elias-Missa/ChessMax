"""PGN → per-ply volatility analysis."""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import chess
import chess.pgn

from chess_vol.classify import Classification, classify_move
from chess_vol.volatility import (
    EngineLike,
    VolatilityResult,
    compute_volatility,
)


@dataclass
class PlyResult:
    """One row of output from :func:`analyze_pgn`."""

    ply: int
    """1-based half-move index (1 = white's first move, 2 = black's reply, ...)."""

    san: str
    """Standard Algebraic Notation of the move played at this ply."""

    fen_before: str
    """FEN of the position *before* the move was played — what we analysed."""

    fen_after: str
    """FEN of the position *after* the move was played."""

    eval_cp: int
    """Engine best-line eval for the pre-move position, side-to-move POV."""

    volatility: VolatilityResult
    """Full volatility result for the pre-move position."""

    move_uci: str = ""
    """UCI of the move played at this ply."""

    classification: Classification | None = None
    """Optional move classification attached after neighbouring plies are known."""


ProgressCallback = Callable[[int, int, PlyResult], None]
"""Signature: ``(ply_done, total_plies_or_0, last_result) -> None``."""


def analyze_pgn(
    pgn: str,
    engine: EngineLike,
    *,
    max_plies: int | None = None,
    progress: ProgressCallback | None = None,
    **volatility_kwargs: Any,
) -> list[PlyResult]:
    """Parse a PGN string and compute per-ply volatility.

    Parameters
    ----------
    pgn:
        PGN game text. Only the first game in the file is analysed.
    engine:
        Reused across every ply (README §6).
    max_plies:
        Optional cap on number of plies to analyse.
    progress:
        Optional callback invoked after each ply completes.
    **volatility_kwargs:
        Passed straight through to :func:`compute_volatility`. Typical use:
        ``recurse_depth=2`` for deep mode, ``depth=20``, etc.

    Returns
    -------
    list[PlyResult]
        One entry per analysed ply, in move order.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError("Could not parse any game from the PGN input")

    board = game.board()
    results: list[PlyResult] = []
    total_plies = sum(1 for _ in game.mainline_moves())
    ply_cap = total_plies if max_plies is None else min(total_plies, max_plies)

    for ply_index, move in enumerate(game.mainline_moves(), start=1):
        if ply_index > ply_cap:
            break

        fen_before = board.fen()
        san = board.san(move)
        move_uci = move.uci()

        vol = compute_volatility(board, engine, **volatility_kwargs)

        board.push(move)
        fen_after = board.fen()

        result = PlyResult(
            ply=ply_index,
            san=san,
            fen_before=fen_before,
            fen_after=fen_after,
            eval_cp=vol.best_eval_cp,
            volatility=vol,
            move_uci=move_uci,
        )
        results.append(result)

        if progress is not None:
            progress(ply_index, ply_cap, result)

    for idx, result in enumerate(results):
        next_result = results[idx + 1] if idx + 1 < len(results) else None
        result.classification = classify_move(result, next_result)

    return results
