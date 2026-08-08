from __future__ import annotations

import chess
import pytest

from core.features import (
    MoveEval,
    calc_term,
    compute_features,
    depthness,
    forcingness,
    reweight,
)


class TestDepthness:
    def test_shallow_is_one(self) -> None:
        assert depthness(1) == pytest.approx(1.0)

    def test_deep_clamps_to_zero(self) -> None:
        assert depthness(15) == pytest.approx(0.0)
        assert depthness(40) == 0.0

    def test_monotone_decreasing(self) -> None:
        assert depthness(1) > depthness(5) > depthness(10)


class TestForcingness:
    def test_empty_pv(self) -> None:
        assert forcingness(chess.Board(), []) == 0.0

    def test_quiet_line_is_zero(self) -> None:
        board = chess.Board()
        pv = [chess.Move.from_uci(u) for u in ("g1f3", "g8f6", "b1c3", "b8c6")]
        assert forcingness(board, pv) == 0.0

    def test_captures_and_checks_count(self) -> None:
        # Scholar's-mate-ish: Qxf7 is a capture and check.
        board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 0 1")
        board.push_san("Qh5")
        board.push_san("Nc6")
        # Now white to move; Qxf7 is capture + check.
        pv = [board.parse_san("Qxf7")]
        assert forcingness(board, pv) == pytest.approx(1.0)

    def test_recapture_counts_as_forcing(self) -> None:
        board = chess.Board()
        board.push_san("e4")
        board.push_san("d5")
        # exd5 (capture), then Qxd5 is a recapture on d5.
        pv = [board.parse_san("exd5")]
        b2 = board.copy()
        b2.push_san("exd5")
        pv.append(b2.parse_san("Qxd5"))
        assert forcingness(board, pv, plies=2) == pytest.approx(1.0)


class TestCalcTerm:
    def test_formula(self) -> None:
        # (0.45*1 + 0.35*0 + 0.20*0) * (1 - 0.6*0) = 0.45
        assert calc_term(1.0, 0.0, 0.0, 0) == pytest.approx(0.45)

    def test_q_suppresses(self) -> None:
        boosted = calc_term(1.0, 1.0, 1.0, 0)
        suppressed = calc_term(1.0, 1.0, 1.0, 1)
        assert suppressed == pytest.approx(boosted * (1 - 0.6))


def test_compute_features_combines() -> None:
    board = chess.Board()
    e4 = chess.Move.from_uci("e2e4")
    me = MoveEval(move=e4, cp=30, pv=[e4], d_star=1)
    feats = compute_features(me, board)
    assert feats.dep == pytest.approx(1.0)
    assert feats.forc == 0.0  # a single quiet pawn push
    assert feats.calc == pytest.approx(0.45)


class TestReweight:
    def test_empty(self) -> None:
        assert reweight({}, {}) == {}

    def test_forcing_move_gets_boosted(self) -> None:
        policy = {"tactic": 0.20, "quiet": 0.20}  # equal priors, 0.60 tail
        calc = {"tactic": 0.9, "quiet": 0.0}
        out = reweight(policy, calc, beta=1.2)
        assert out["tactic"] > out["quiet"]

    def test_sums_to_at_most_one_with_tail(self) -> None:
        policy = {"a": 0.20, "b": 0.10}  # tail 0.70
        out = reweight(policy, {"a": 0.5, "b": 0.5}, beta=1.2)
        assert 0.0 < sum(out.values()) < 1.0

    def test_full_mass_no_tail_normalizes_to_one(self) -> None:
        policy = {"a": 0.5, "b": 0.5}
        out = reweight(policy, {"a": 0.0, "b": 0.0}, beta=1.2)
        assert sum(out.values()) == pytest.approx(1.0)
