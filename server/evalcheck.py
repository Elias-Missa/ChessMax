"""Shared eval-delta checker.

This is the single implementation of "did this move drop the eval more than
X centipawns?" used by quiet-style validation in the eval-hold and defense
modes. It mirrors the math in :mod:`server.grading` (mover-POV centipawns,
mate folded to ±100k) but is session-agnostic and reusable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import chess

from server.engine import MATE_SCORE_CP, analyze

DEFAULT_CHECK_DEPTH = 14

Analysis = dict[str, list[dict[str, Any]]]
AnalysisFn = Callable[..., Analysis]


@dataclass(frozen=True)
class EvalCheck:
    """Result of checking one move's eval delta (all centipawns, mover POV)."""

    ok: bool
    drop_cp: float                 # best_eval - played_eval (>= 0 means worse)
    best_eval_cp: float            # eval of the position before the move
    played_eval_cp: float          # eval after the move, from the mover's POV
    best_move_uci: str | None      # engine's preferred move in the position
    game_over: bool                # the move ended the game
    mate_delivered: bool           # the mover checkmated the opponent
    drawn: bool                    # the move produced a drawn terminal position


def position_eval_cp(
    fen: str,
    *,
    depth: int = DEFAULT_CHECK_DEPTH,
    analyzer: AnalysisFn = analyze,
) -> int:
    """Best-move eval of ``fen`` in centipawns from the side-to-move POV."""

    board = chess.Board(fen)
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        if outcome.winner is None:
            return 0
        mover_won = outcome.winner == board.turn
        return MATE_SCORE_CP if mover_won else -MATE_SCORE_CP

    top_moves = analyzer(fen, depth=depth, multipv=1).get("top_moves", [])
    if not top_moves:
        raise RuntimeError("Engine returned no analysis for position")
    return int(top_moves[0]["eval"])


def check_eval_drop(
    fen: str,
    move_uci: str,
    *,
    threshold_cp: float = 100.0,
    depth: int = DEFAULT_CHECK_DEPTH,
    analyzer: AnalysisFn = analyze,
) -> EvalCheck:
    """Compare ``move_uci`` against the engine's best move in ``fen``.

    ``ok`` is true when the eval drop stays within ``threshold_cp``.
    Raises :class:`ValueError` for illegal moves.
    """

    board = chess.Board(fen)
    move = chess.Move.from_uci(move_uci)
    if move not in board.legal_moves:
        raise ValueError(f"Illegal move for position: {move_uci}")

    analysis = analyzer(fen, depth=depth, multipv=1).get("top_moves", [])
    if not analysis:
        raise RuntimeError("Engine returned no analysis for position")
    best_eval = float(analysis[0]["eval"])
    best_move_uci = str(analysis[0]["move"])

    board.push(move)
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        mate_delivered = outcome.winner is not None and outcome.winner != board.turn
        played_eval = float(MATE_SCORE_CP) if mate_delivered else 0.0
        drop = best_eval - played_eval
        return EvalCheck(
            ok=drop <= threshold_cp,
            drop_cp=drop,
            best_eval_cp=best_eval,
            played_eval_cp=played_eval,
            best_move_uci=best_move_uci,
            game_over=True,
            mate_delivered=mate_delivered,
            drawn=outcome.winner is None,
        )

    after = analyzer(board.fen(), depth=depth, multipv=1).get("top_moves", [])
    if not after:
        raise RuntimeError("Engine returned no analysis for resulting position")
    played_eval = -float(after[0]["eval"])
    drop = best_eval - played_eval
    return EvalCheck(
        ok=drop <= threshold_cp,
        drop_cp=drop,
        best_eval_cp=best_eval,
        played_eval_cp=played_eval,
        best_move_uci=best_move_uci,
        game_over=False,
        mate_delivered=False,
        drawn=False,
    )
