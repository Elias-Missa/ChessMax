"""Integration smoke test for the Maia top-K findability service.

Skips automatically when lc0 + Maia weights aren't installed, matching the
engine-optional convention used elsewhere in the suite.
"""

from __future__ import annotations

import chess
import pytest

from server import maia


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

requires_maia = pytest.mark.skipif(
    not maia.has_maia_assets(1900),
    reason="lc0 + Maia-1900 weights not available; integration test skipped.",
)


@requires_maia
def test_maia_top_moves_returns_legal_policy_ordered_moves() -> None:
    moves = maia.maia_top_moves(START_FEN, net=1900, k=3)

    assert moves is not None
    assert 1 <= len(moves) <= 3
    assert len(set(moves)) == len(moves)  # distinct

    board = chess.Board(START_FEN)
    legal = {m.uci() for m in board.legal_moves}
    assert set(moves) <= legal

    # Maia from the opening overwhelmingly favours a normal first move
    # (e4/d4/Nf3/c4...). The policy top move should be one of these, never a
    # weird edge push.
    assert moves[0] in {"e2e4", "d2d4", "g1f3", "c2c4", "e2e3", "g2g3"}


@requires_maia
def test_maia_top_moves_respects_k() -> None:
    one = maia.maia_top_moves(START_FEN, net=1900, k=1)
    assert one is not None and len(one) == 1


def test_maia_top_moves_terminal_position_returns_none() -> None:
    # Fool's mate — checkmate, no legal moves. Should be None regardless of
    # whether Maia assets exist (terminal guard runs before engine launch).
    mate_fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    assert maia.maia_top_moves(mate_fen, net=1900, k=3) is None
