"""Oracle positions for contextual piece values, against a real engine.

These are the positions where a human already knows the answer: a bishop with no
squares is not worth three pawns, a protected passer on the seventh is worth far
more than one. The engine-free tests next door pin the *math*; these pin that the
math describes chess.

Skipped automatically without Stockfish, like the rest of the integration suite —
so a green run on a machine with no engine says nothing about these.
"""

from __future__ import annotations

import chess
import pytest

from core.piece_values import PieceValueConstants, compute_piece_values
from tests.vol.conftest import requires_stockfish

pytestmark = [pytest.mark.integration, requires_stockfish]

# Depth 16 is the shipped probe depth and it is load-bearing, not a dial: at
# depth 12 the ablation noise swamps the signal (see the JSON's
# _probe_depth_comment). Lower it here and these tests get flaky, not fast.
DEPTH = 16


@pytest.fixture(scope="module")
def engine():
    from chess_vol.engine import Engine

    with Engine() as e:
        yield e


@pytest.fixture(scope="module")
def constants() -> PieceValueConstants:
    return PieceValueConstants.load()


def value_on(result, square: str):
    return next(p for p in result.pieces if p.square == square)


def test_the_identities_hold_on_a_real_position(engine, constants) -> None:
    board = chess.Board(
        "r1bqkb1r/pp3ppp/2n2n2/3N4/4P3/8/PPP2PPP/R1BQKB1R b KQkq - 0 1"
    )
    result = compute_piece_values(board, engine, depth=DEPTH, constants=constants)

    signed = sum((1 if p.color else -1) * p.anchored_cp for p in result.pieces)
    assert signed == pytest.approx(result.eval_material_cp, abs=1e-6)

    premiums = sum((1 if p.color else -1) * p.premium_cp for p in result.pieces)
    assert premiums == pytest.approx(result.gap_cp, abs=1e-6)


def test_a_bishop_with_no_squares_is_worth_far_less_than_three_pawns(
    engine, constants
) -> None:
    # Bishop entombed on a8: its only square, b7, is defended by c6.
    board = chess.Board("B3k3/1p6/2p5/8/8/8/6K1/8 w - - 0 1")
    result = compute_piece_values(board, engine, depth=DEPTH, constants=constants)

    bishop = value_on(result, "a8")
    assert "trapped" in bishop.tags
    assert bishop.anchored_cp < 200, f"entombed bishop priced at {bishop.anchored_cp}"


def test_a_protected_passer_on_the_seventh_beats_a_plain_pawn(
    engine, constants
) -> None:
    board = chess.Board("8/1P6/P7/8/6k1/8/8/6K1 w - - 0 1")
    assert board.is_valid()
    result = compute_piece_values(board, engine, depth=DEPTH, constants=constants)

    passer = value_on(result, "b7")
    assert "protected_passer" in passer.tags
    assert passer.anchored_cp > 300, f"passer on the 7th priced at {passer.anchored_cp}"


def test_an_outpost_knight_reads_above_a_book_knight(engine, constants) -> None:
    board = chess.Board(
        "r1bqkb1r/pp3ppp/2n2n2/3N4/4P3/8/PPP2PPP/R1BQKB1R b KQkq - 0 1"
    )
    result = compute_piece_values(board, engine, depth=DEPTH, constants=constants)

    knight = value_on(result, "d5")
    assert "outpost" in knight.tags
    assert knight.anchored_cp > knight.static_cp


def test_saturated_positions_say_so(engine, constants) -> None:
    board = chess.Board("6k1/8/8/8/8/8/6P1/QQ4K1 w - - 0 1")
    result = compute_piece_values(board, engine, depth=DEPTH, constants=constants)
    assert result.saturated


def test_relocation_finds_a_better_square_for_a_rim_knight(engine, constants) -> None:
    # "A knight on the rim is dim" — it should want to be somewhere else.
    board = chess.Board("4k3/pppppppp/8/8/8/8/PPPPPPPP/N3K3 w - - 0 1")
    result = compute_piece_values(
        board, engine, depth=DEPTH, relocate_top_k=1, constants=constants
    )
    relocated = [p for p in result.pieces if p.relocation is not None]
    # Not every position has a better square, but if one is reported it must be
    # an improvement worth the threshold.
    for piece in relocated:
        assert piece.relocation.gain_cp >= constants.relocate_min_gain_cp
        assert piece.relocation.to_square != piece.square
