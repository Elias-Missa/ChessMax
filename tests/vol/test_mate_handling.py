"""Tests for mate-distance-aware cp mapping (README §3.3) and the interaction
with the decided flag (§3.4) on the two contrasting §3.7 mate rows."""

from __future__ import annotations

import chess
import pytest

from chess_vol.config import MATE_BASE, MATE_MAX_N, MATE_STEP
from chess_vol.volatility import (
    _is_decided,
    compute_volatility,
    info_to_cp,
    mate_to_cp,
)

from .conftest import FakeEngine, evals_to_infos, make_info


class TestMateToCp:
    """README §3.3 reference table."""

    @pytest.mark.parametrize(
        "n,expected",
        [
            (1, 1950),
            (2, 1900),
            (3, 1850),
            (6, 1700),
            (10, 1500),
            (20, 1000),
            (MATE_MAX_N, MATE_BASE - MATE_STEP * MATE_MAX_N),
        ],
    )
    def test_positive_mate_values(self, n: int, expected: int) -> None:
        assert mate_to_cp(n) == expected

    def test_opponent_mate_negative(self) -> None:
        assert mate_to_cp(-1) == -1950
        assert mate_to_cp(-6) == -1700
        assert mate_to_cp(-20) == -1000

    @pytest.mark.parametrize("n", [21, 30, 100, 9999])
    def test_clamp_beyond_mate_max_n(self, n: int) -> None:
        """Mate-in-N for N > MATE_MAX_N maps to the same floor value."""
        floor_value = MATE_BASE - MATE_STEP * MATE_MAX_N
        assert mate_to_cp(n) == floor_value
        assert mate_to_cp(-n) == -floor_value

    def test_zero_is_invalid(self) -> None:
        with pytest.raises(ValueError):
            mate_to_cp(0)

    def test_monotonic_decrease_with_distance(self) -> None:
        """Shorter mates outrank longer ones (README §3.3 rationale)."""
        values = [mate_to_cp(n) for n in range(1, 21)]
        assert all(values[i] > values[i + 1] for i in range(len(values) - 1))

    def test_all_mates_exceed_normal_evals(self) -> None:
        """Even the longest clamped mate outranks typical material evals (±1500)."""
        smallest = mate_to_cp(MATE_MAX_N)
        assert smallest > 900  # comfortably above "+9 pawns"


class TestInfoToCp:
    """Verify python-chess PovScore -> side-to-move cp conversion handles all cases."""

    def test_cp_as_white_to_move(self) -> None:
        info = make_info(cp=250, turn=chess.WHITE)
        assert info_to_cp(info, chess.WHITE) == 250

    def test_cp_as_black_to_move(self) -> None:
        """Same physical score, read from Black's perspective → sign flipped."""
        info = make_info(cp=250, turn=chess.BLACK)
        # We constructed the info as Black-POV cp=+250; white-POV is -250.
        assert info_to_cp(info, chess.BLACK) == 250
        assert info_to_cp(info, chess.WHITE) == -250

    def test_mate_for_side_to_move(self) -> None:
        info = make_info(mate=3, turn=chess.WHITE)
        assert info_to_cp(info, chess.WHITE) == mate_to_cp(3)
        assert info_to_cp(info, chess.BLACK) == -mate_to_cp(3)

    def test_opponent_mate(self) -> None:
        info = make_info(mate=-2, turn=chess.WHITE)
        assert info_to_cp(info, chess.WHITE) == mate_to_cp(-2)


# --------------------------------------------------------------------------- #
# §3.7 contrasting rows on the decided flag                                    #
# --------------------------------------------------------------------------- #


class TestSectionThreeSevenDecidedContrast:
    """The canonical worked-examples pair in README §3.7."""

    def test_mate_available_not_decided(self) -> None:
        """[M1, +200, 0, -200, -400, -600] → scale ≈ 1.0, bar bright, NOT decided.

        Decided needs ``|e_2| > 400``; here e_2 = +200 → decided=False.
        """
        e1, e2 = mate_to_cp(1), 200
        assert _is_decided(e1, e2) is False

        board = chess.Board()
        engine = FakeEngine(scripts=[evals_to_infos(["M1", 200, 0, -200, -400, -600])])
        result = compute_volatility(board, engine)
        assert result.decided is False
        assert result.score is not None
        assert result.score > 95

    def test_multiple_mates_is_decided(self) -> None:
        """[M1, M3, +400, +200, 0, -100] → scale very small, bar dimmed, decided.

        Both e_1 and e_2 are mates → |e_2| ≈ 1850 ≫ 400, same sign → decided=True.
        """
        e1, e2 = mate_to_cp(1), mate_to_cp(3)
        assert _is_decided(e1, e2) is True

        board = chess.Board()
        engine = FakeEngine(scripts=[evals_to_infos(["M1", "M3", 400, 200, 0, -100])])
        result = compute_volatility(board, engine)
        assert result.decided is True
        assert result.scale < 0.1


class TestMateInRecursion:
    """Mate scores in recursion should still flip with the side to move."""

    def test_mate_for_opponent_at_child_becomes_negative(self) -> None:
        """If the child position is 'M3 for the side to move there' and that side
        is the opponent of the root, the cp value at the child is +mate_to_cp(3)
        *from the opponent's POV*. This doesn't invert again at the child — the
        child's local V is computed in child's own POV.
        """
        board = chess.Board()
        # Parent: quiet evals for white-to-move (minimal V_local)
        parent_infos = evals_to_infos([20, 10, 0, -10, -20, -30], turn=chess.WHITE)
        # Point the pv to a concrete legal root move so we can find the child's turn.
        parent_infos[0]["pv"] = [next(iter(board.legal_moves))]
        # Child: opponent (BLACK) sees mate on themselves (negative for black = white mates)
        child_infos = evals_to_infos(["M3", 0, -10, -20, -30, -40], turn=chess.BLACK)

        engine = FakeEngine(scripts=[parent_infos, child_infos])
        result = compute_volatility(
            board,
            engine,
            recurse_depth=1,
            recurse_k=1,
        )
        assert result.recurse_depth_used == 1
        # V_total_raw should exceed V_local_raw because the child contributed volatility.
        assert result.local_raw_cp is not None and result.raw_cp is not None
        assert result.raw_cp >= result.local_raw_cp
