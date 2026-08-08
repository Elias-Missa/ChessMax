"""Attach findability verdicts to an analyzed game (Game Review 2.0 §3).

This is the *integration seam* between the pure :mod:`core.findability` machinery
and the existing volatility analysis pipeline. It is deliberately **opt-in and
null-safe**: the classic review (classification, eval graph, accuracy) works
without it, and findability is only populated when a human ``policy_fn`` is
supplied.

Two fidelity levels are possible:

* **Reuse mode (this module).** Build :class:`core.features.MoveEval`s from the
  MultiPV lines the volatility pass already produced (``ply.volatility.top_lines``).
  Cheap — no extra engine work — but ``d_star`` / ``narr`` / ``q`` are not
  available and default to neutral, so the score leans on the human prior more
  than on calculation reweighting.
* **Full fidelity (future dedicated pass).** Re-search each position with
  :func:`core.engine.multipv_move_evals` (MultiPV-8, fixed nodes, iterative-
  deepening capture) plus :func:`core.engine.enrich_narr_q`. Same
  :func:`core.findability.score_position` call — only the ``MoveEval`` source
  differs.
"""

from __future__ import annotations

import chess

from chess_vol.analyze import PlyResult
from core.features import MoveEval
from core.findability import FindabilityConstants, PolicyFn, score_position


def _parse_pv(board: chess.Board, pv_san: list[str]) -> list[chess.Move]:
    """Parse a SAN principal variation into moves, replayed from ``board``.

    Stops at the first unparseable move (engine PVs can outrun legality after
    truncation). Returns whatever prefix parsed.
    """
    scratch = board.copy(stack=False)
    moves: list[chess.Move] = []
    for san in pv_san:
        try:
            move = scratch.parse_san(san)
        except (ValueError, AssertionError):
            break
        moves.append(move)
        scratch.push(move)
    return moves


def move_evals_from_ply(ply: PlyResult) -> list[MoveEval]:
    """Build :class:`MoveEval`s from a ply's volatility MultiPV lines (reuse mode)."""
    board = chess.Board(ply.fen_before)
    evals: list[MoveEval] = []
    for line in ply.volatility.top_lines:
        try:
            move = chess.Move.from_uci(line.uci)
        except ValueError:
            continue
        pv = _parse_pv(board, list(line.pv_san)) or [move]
        evals.append(MoveEval(move=move, cp=int(line.eval_cp), pv=pv, d_star=1))
    return evals


def _is_book(ply: PlyResult) -> bool:
    return ply.review is not None and ply.review.classification == "book"


def attach_findability(
    results: list[PlyResult],
    policy_fn: PolicyFn,
    constants: FindabilityConstants | None = None,
    *,
    user_rating: int | None = None,
) -> None:
    """Attach a :class:`core.findability.PositionFindability` to each scorable ply.

    Gating (spec §3.3 Step 1): terminal / only-move positions (volatility
    ``reason`` set) and book moves are skipped and left ``None``; the
    decided-position gate lives inside :func:`score_position` and also yields
    ``None`` (the payload is ``null``, never ``0``). Mutates ``results`` in place.
    """
    consts = constants if constants is not None else FindabilityConstants.load()
    for ply in results:
        if ply.volatility.reason is not None or _is_book(ply):
            continue
        move_evals = move_evals_from_ply(ply)
        if len(move_evals) < 2:
            continue
        ply.findability = score_position(
            ply.fen_before,
            move_evals,
            policy_fn,
            consts,
            m_star=move_evals[0].move,
            user_rating=user_rating,
            volatility=ply.volatility.score,
        )


__all__ = ["attach_findability", "move_evals_from_ply"]
