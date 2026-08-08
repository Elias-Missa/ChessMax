from __future__ import annotations

import chess
import pytest

from core.features import MoveEval
from core.findability import (
    FindabilityConstants,
    band_for,
    gate_reason,
    interp_at,
    invert_monotone,
    pava,
    score_position,
)

CONSTANTS = FindabilityConstants()

E4 = chess.Move.from_uci("e2e4")
D4 = chess.Move.from_uci("d2d4")
A3 = chess.Move.from_uci("a2a3")


class TestPava:
    def test_already_monotone_unchanged(self) -> None:
        assert pava([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]

    def test_violators_pooled(self) -> None:
        assert pava([3.0, 1.0, 2.0]) == pytest.approx([2.0, 2.0, 2.0])

    def test_output_is_non_decreasing(self) -> None:
        out = pava([0.9, 0.1, 0.5, 0.4, 0.8])
        assert all(b >= a for a, b in zip(out, out[1:]))


class TestInvertMonotone:
    def test_interpolates_crossing(self) -> None:
        grid = [800, 1100, 1400]
        # crosses 0.5 halfway between 1100 (0.4) and 1400 (0.6)
        assert invert_monotone(grid, [0.2, 0.4, 0.6], 0.5) == pytest.approx(1250.0)

    def test_never_crosses_returns_none(self) -> None:
        assert invert_monotone([800, 1100], [0.1, 0.2], 0.5) is None

    def test_already_above_returns_first(self) -> None:
        assert invert_monotone([800, 1100], [0.6, 0.7], 0.5) == 800.0


class TestInterpAt:
    def test_clamps_to_range(self) -> None:
        assert interp_at([800, 1400], [0.2, 0.8], 500) == 0.2
        assert interp_at([800, 1400], [0.2, 0.8], 2000) == 0.8

    def test_midpoint(self) -> None:
        assert interp_at([800, 1400], [0.2, 0.8], 1100) == pytest.approx(0.5)


def test_band_for_thresholds() -> None:
    assert band_for(95, CONSTANTS) == "Obvious"
    assert band_for(75, CONSTANTS) == "Natural"
    assert band_for(50, CONSTANTS) == "Needs thought"
    assert band_for(30, CONSTANTS) == "Hard"
    assert band_for(5, CONSTANTS) == "Engine-only"


class TestGate:
    def test_single_move_is_forced(self) -> None:
        assert gate_reason([MoveEval(move=E4, cp=0)], CONSTANTS) == "forced"

    def test_both_winning_is_decided(self) -> None:
        evals = [MoveEval(move=E4, cp=3000), MoveEval(move=D4, cp=2600)]
        assert gate_reason(evals, CONSTANTS) == "decided"

    def test_both_losing_is_decided(self) -> None:
        evals = [MoveEval(move=E4, cp=-3000), MoveEval(move=D4, cp=-2600)]
        assert gate_reason(evals, CONSTANTS) == "decided"

    def test_real_decision_not_gated(self) -> None:
        evals = [MoveEval(move=E4, cp=30), MoveEval(move=D4, cp=20)]
        assert gate_reason(evals, CONSTANTS) is None


def _ramp_policy(fen, rating, moves):
    """Fake human model: P(e4) ramps up with rating; d4 steady; a3 fades."""
    frac = (rating - 800) / (2600 - 800)
    table = {E4: 0.05 + 0.8 * frac, D4: 0.20, A3: 0.10 * (1 - frac)}
    return {m: table.get(m, 0.0) for m in moves}


def _position() -> list[MoveEval]:
    # e4 best, d4 within tau (acceptable), a3 clearly worse (excluded from A).
    return [
        MoveEval(move=E4, cp=30, pv=[E4], d_star=1),
        MoveEval(move=D4, cp=25, pv=[D4], d_star=1),
        MoveEval(move=A3, cp=-300, pv=[A3], d_star=1),
    ]


class TestScorePosition:
    def test_forced_scores_100(self) -> None:
        result = score_position(
            chess.STARTING_FEN, [MoveEval(move=E4, cp=0)], _ramp_policy, CONSTANTS
        )
        assert result is not None
        assert result.forced is True
        assert result.score == 100

    def test_decided_is_none(self) -> None:
        evals = [MoveEval(move=E4, cp=3000), MoveEval(move=D4, cp=2600)]
        assert score_position(chess.STARTING_FEN, evals, _ramp_policy, CONSTANTS) is None

    def test_curve_is_monotone_and_score_in_range(self) -> None:
        result = score_position(chess.STARTING_FEN, _position(), _ramp_policy, CONSTANTS)
        assert result is not None
        c_a = [v for _, v in result.curve]
        assert all(b >= a - 1e-9 for a, b in zip(c_a, c_a[1:]))
        assert 0 <= result.score <= 100
        assert result.r_find is None or 600 <= result.r_find <= 2600

    def test_deterministic(self) -> None:
        a = score_position(chess.STARTING_FEN, _position(), _ramp_policy, CONSTANTS)
        b = score_position(chess.STARTING_FEN, _position(), _ramp_policy, CONSTANTS)
        assert a is not None and b is not None
        assert a.score == b.score
        assert a.curve == b.curve

    def test_personal_and_alternate_for_low_rated_user(self) -> None:
        result = score_position(
            chess.STARTING_FEN,
            _position(),
            _ramp_policy,
            CONSTANTS,
            user_rating=800,
        )
        assert result is not None
        assert result.personal is not None and 0.0 < result.personal < 1.0
        assert result.personal_star is not None
        # m* (e4) is nearly unfindable at 800; d4 is the recommended alternate.
        assert result.alternate is not None
        assert result.alternate.uci == D4.uci()
        assert result.alternate.delta_w >= 0.0

    def test_no_alternate_when_no_user_rating(self) -> None:
        result = score_position(chess.STARTING_FEN, _position(), _ramp_policy, CONSTANTS)
        assert result is not None
        assert result.alternate is None
        assert result.personal is None


X = chess.Move.from_uci("h2h3")


def _position_with_borderline() -> list[MoveEval]:
    # e4 best; d4 always acceptable; X (cp=-15) loses ~4 win% — inside a widened
    # tau (Phase 4, sharp) but outside the constant 2.5.
    return [
        MoveEval(move=E4, cp=30, pv=[E4], d_star=1),
        MoveEval(move=D4, cp=25, pv=[D4], d_star=1),
        MoveEval(move=X, cp=-15, pv=[X], d_star=1),
    ]


def _policy_with_x(fen, rating, moves):
    frac = (rating - 800) / (2600 - 800)
    table = {E4: 0.05 + 0.5 * frac, D4: 0.15, X: 0.15}
    return {m: table.get(m, 0.0) for m in moves}


class TestPhase4VolatilityTau:
    def test_wider_tau_in_sharp_position_cannot_lower_score(self) -> None:
        """Phase 4: enabling tau=f(volatility) at high volatility widens A, which
        can only raise C_A — so findability is >= the constant-tau score."""
        constant = FindabilityConstants()
        sharp = FindabilityConstants(
            tau_volatility_enabled=True, tau_volatility_min=1.0, tau_volatility_max=6.0
        )
        base = score_position(
            chess.STARTING_FEN, _position_with_borderline(), _policy_with_x, constant, volatility=90
        )
        widened = score_position(
            chess.STARTING_FEN, _position_with_borderline(), _policy_with_x, sharp, volatility=90
        )
        assert base is not None and widened is not None
        assert widened.score >= base.score

    def test_disabled_constants_ignore_volatility(self) -> None:
        constant = FindabilityConstants()
        a = score_position(
            chess.STARTING_FEN, _position_with_borderline(), _policy_with_x, constant, volatility=5
        )
        b = score_position(
            chess.STARTING_FEN, _position_with_borderline(), _policy_with_x, constant, volatility=95
        )
        assert a is not None and b is not None
        assert a.score == b.score  # tau is constant when disabled
