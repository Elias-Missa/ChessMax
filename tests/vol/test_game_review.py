from __future__ import annotations

import pytest

from chess_vol.game_review import expected_points, move_accuracy


def test_expected_points_is_logistic_and_clamps_mates() -> None:
    assert expected_points(0) == pytest.approx(0.5)
    assert expected_points(10_000) > 0.999
    assert expected_points(-10_000) < 0.001
    assert expected_points(50_000) == expected_points(10_000)


def test_caps2_accuracy_penalizes_expected_point_loss() -> None:
    perfect = move_accuracy(0)
    small_loss = move_accuracy(0.05)
    blunder = move_accuracy(0.30)

    assert perfect == pytest.approx(99.9999)
    assert perfect > small_loss > blunder
    assert 0 <= blunder <= 100
