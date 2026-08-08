from __future__ import annotations

import pytest

from core.evaluation import (
    MATE_CP_CLAMP,
    clamp_mate_cp,
    delta_w,
    win_prob,
    win_prob_cp,
)


class TestWinProb:
    def test_wdl_extremes_and_draw(self) -> None:
        assert win_prob((1000, 0, 0)) == pytest.approx(1.0)
        assert win_prob((0, 0, 1000)) == pytest.approx(0.0)
        assert win_prob((0, 1000, 0)) == pytest.approx(0.5)
        assert win_prob((500, 0, 500)) == pytest.approx(0.5)

    def test_degenerate_triple_is_half(self) -> None:
        assert win_prob((0, 0, 0)) == pytest.approx(0.5)

    def test_off_total_is_normalized(self) -> None:
        # Some builds report totals slightly off 1000; stay a proper fraction.
        assert win_prob((600, 300, 100)) == pytest.approx(0.75)


class TestWinProbCp:
    def test_zero_is_half(self) -> None:
        assert win_prob_cp(0) == pytest.approx(0.5)

    def test_monotone(self) -> None:
        assert win_prob_cp(-200) < win_prob_cp(0) < win_prob_cp(200)

    def test_mate_clamp_saturates(self) -> None:
        assert win_prob_cp(clamp_mate_cp(None, 3)) > 0.999
        assert win_prob_cp(clamp_mate_cp(None, -3)) < 0.001
        # Beyond the clamp, no further change.
        assert win_prob_cp(50_000) == win_prob_cp(MATE_CP_CLAMP)


class TestClampMateCp:
    def test_mate_dominates(self) -> None:
        assert clamp_mate_cp(15, 3) == float(MATE_CP_CLAMP)
        assert clamp_mate_cp(15, -3) == -float(MATE_CP_CLAMP)

    def test_cp_passthrough(self) -> None:
        assert clamp_mate_cp(120, None) == 120.0

    def test_requires_one(self) -> None:
        with pytest.raises(ValueError):
            clamp_mate_cp(None, None)


class TestDeltaW:
    def test_non_negative_and_clamped(self) -> None:
        # A "move" better than "best" still yields 0, never negative.
        assert delta_w(0.5, 0.9) == 0.0

    def test_points_lost(self) -> None:
        assert delta_w(0.60, 0.55) == pytest.approx(5.0)

    def test_accepts_wdl_and_prob_mixed(self) -> None:
        assert delta_w((1000, 0, 0), 0.5) == pytest.approx(50.0)
        assert delta_w((600, 300, 100), (400, 300, 300)) == pytest.approx(20.0)
