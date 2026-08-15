"""The calibrated findability model: visibility reweight + Elo-shaped scale.

These are the properties the score is *for* — an obvious move has to come out
near 100, a move only an engine sees has to come out near 0, and the two must
not be able to swap places. They run engine-free on synthetic policies so they
pin behaviour rather than a particular Maia build.
"""

from __future__ import annotations

import chess
import pytest

from core.features import (
    MoveEval,
    VisibilityWeights,
    centered,
    first_forcing_ply,
    visibility,
)
from core.findability import (
    Calibration,
    FindabilityConstants,
    band_for,
    score_position,
)

WEIGHTS = VisibilityWeights()

# 1.e4 e5 2.Nf3 Nc6 3.Bc4 — White to move in a normal opening position, so every
# move below is legal and the capture/check tests are real.
OPENING = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 5 4"


def _eval(board: chess.Board, san: str, pv_sans: list[str] | None = None, **kw) -> MoveEval:
    move = board.parse_san(san)
    pv = [move]
    if pv_sans:
        scratch = board.copy(stack=False)
        pv = []
        for text in [san, *pv_sans]:
            parsed = scratch.parse_san(text)
            pv.append(parsed)
            scratch.push(parsed)
    return MoveEval(move=move, pv=pv, **kw)


class TestVisibility:
    def test_a_check_is_more_visible_than_a_quiet_move(self) -> None:
        board = chess.Board(OPENING)
        check = _eval(board, "Bxf7+")
        quiet = _eval(board, "d3")
        assert visibility(check, board, WEIGHTS) > visibility(quiet, board, WEIGHTS)

    def test_a_capture_is_more_visible_than_a_quiet_move(self) -> None:
        board = chess.Board(OPENING)
        assert visibility(_eval(board, "Nxe5"), board, WEIGHTS) > visibility(
            _eval(board, "d3"), board, WEIGHTS
        )

    def test_a_quiet_move_whose_point_comes_late_is_penalised(self) -> None:
        board = chess.Board(OPENING)
        # Both quiet at the root; only the second turns forcing inside the window.
        late = _eval(board, "d3", ["d6", "Nc3", "Nf6"])
        early = _eval(board, "d3", ["d6", "Nxe5", "Nxe5"])
        assert visibility(late, board, WEIGHTS) < visibility(early, board, WEIGHTS)

    def test_first_forcing_ply_finds_the_movers_own_forcing_move(self) -> None:
        board = chess.Board(OPENING)
        quiet = _eval(board, "d3", ["d6", "Nc3", "Nf6"])
        assert first_forcing_ply(board, quiet.pv) == 99
        forcing = _eval(board, "Nxe5", ["Nxe5"])
        assert first_forcing_ply(board, forcing.pv) == 0

    def test_centering_makes_the_reweight_mass_neutral(self) -> None:
        out = centered({"a": 1.0, "b": 0.0, "c": 0.5})
        assert sum(out.values()) == pytest.approx(0.0)


class TestCalibration:
    def test_difficulty_is_the_rating_where_half_of_players_find_it(self) -> None:
        cal = Calibration()
        grid = [1100, 1400, 1700]
        # A curve that is exactly the Elo expectancy for D = 1400 must invert
        # back to 1400.
        curve = [cal.apply(1400, r) for r in grid]
        assert cal.difficulty(curve, grid) == pytest.approx(1400.0, abs=1.0)

    def test_an_easier_curve_gives_a_lower_difficulty(self) -> None:
        cal = Calibration()
        grid = [1100, 1400, 1700]
        easy = cal.difficulty([0.80, 0.90, 0.95], grid)
        hard = cal.difficulty([0.05, 0.10, 0.20], grid)
        assert easy < hard

    def test_expectancy_is_a_half_at_the_difficulty_rating(self) -> None:
        assert Calibration().apply(1500, 1500) == pytest.approx(0.5)

    def test_clamp_bounds_how_far_difficulty_can_run(self) -> None:
        cal = Calibration()
        grid = [1100, 1550, 2000]
        # Even an all-zero curve must not report an absurd rating.
        assert cal.difficulty([0.0, 0.0, 0.0], grid) < 2800.0


def _board_evals(*, best_san: str, others: list[str], best_cp: int, other_cp: int):
    board = chess.Board(OPENING)
    evals = [MoveEval(move=board.parse_san(best_san), cp=best_cp,
                      pv=[board.parse_san(best_san)], d_star=1)]
    for san in others:
        evals.append(MoveEval(move=board.parse_san(san), cp=other_cp,
                              pv=[board.parse_san(san)], d_star=1))
    return board, evals


def _policy_putting(mass: float, on_san: str):
    """A human model that puts ``mass`` on one move and spreads the rest thin."""
    board = chess.Board(OPENING)
    target = board.parse_san(on_san)

    def policy(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
        rest = max(0.0, (0.9 - mass)) / max(1, len(moves) - 1)
        return {m: (mass if m == target else rest) for m in moves}

    return policy


CONSTANTS = FindabilityConstants.load()


class TestTheScoreMeansWhatItSays:
    """The headline requirement: obvious near 100, engine-only near 0."""

    def test_a_move_everyone_plays_scores_obvious(self) -> None:
        board, evals = _board_evals(best_san="Nxe5", others=["d3", "a3", "h3"],
                                    best_cp=300, other_cp=-200)
        result = score_position(board.fen(), evals, _policy_putting(0.9, "Nxe5"), CONSTANTS)
        assert result is not None
        assert result.score >= 85
        assert band_for(result.score, CONSTANTS) == "Obvious"

    def test_a_move_almost_nobody_plays_scores_engine_only(self) -> None:
        board, evals = _board_evals(best_san="d3", others=["a3", "h3", "Nc3"],
                                    best_cp=300, other_cp=-200)
        result = score_position(board.fen(), evals, _policy_putting(0.002, "d3"), CONSTANTS)
        assert result is not None
        assert result.score <= 15
        assert band_for(result.score, CONSTANTS) == "Engine-only"

    def test_score_falls_as_the_human_model_gets_worse_at_the_move(self) -> None:
        board, evals = _board_evals(best_san="d3", others=["a3", "h3", "Nc3"],
                                    best_cp=300, other_cp=-200)
        scores = []
        for mass in (0.80, 0.40, 0.10, 0.01):
            result = score_position(board.fen(), evals, _policy_putting(mass, "d3"), CONSTANTS)
            assert result is not None
            scores.append(result.score)
        assert scores == sorted(scores, reverse=True)

    def test_more_acceptable_moves_can_only_raise_the_score(self) -> None:
        """A wide-open position is easier: any of several moves will do."""
        narrow_board, narrow = _board_evals(best_san="d3", others=["a3", "h3", "Nc3"],
                                            best_cp=40, other_cp=-300)
        wide_board, wide = _board_evals(best_san="d3", others=["a3", "h3", "Nc3"],
                                        best_cp=40, other_cp=38)
        policy = _policy_putting(0.25, "d3")
        tight = score_position(narrow_board.fen(), narrow, policy, CONSTANTS)
        loose = score_position(wide_board.fen(), wide, policy, CONSTANTS)
        assert tight is not None and loose is not None
        assert loose.score > tight.score

    def test_r_find_is_a_rating_and_tracks_the_score(self) -> None:
        board, evals = _board_evals(best_san="d3", others=["a3", "h3", "Nc3"],
                                    best_cp=300, other_cp=-200)
        easy = score_position(board.fen(), evals, _policy_putting(0.85, "d3"), CONSTANTS)
        hard = score_position(board.fen(), evals, _policy_putting(0.02, "d3"), CONSTANTS)
        assert easy is not None and hard is not None
        assert easy.r_find is not None and hard.r_find is not None
        assert easy.r_find < hard.r_find
        assert 300 < easy.r_find < 3000 and 300 < hard.r_find < 3000

    def test_curve_is_monotone_and_personal_uses_the_users_own_rating(self) -> None:
        board, evals = _board_evals(best_san="d3", others=["a3", "h3", "Nc3"],
                                    best_cp=300, other_cp=-200)
        result = score_position(board.fen(), evals, _policy_putting(0.3, "d3"),
                                CONSTANTS, user_rating=1200)
        assert result is not None
        values = [v for _, v in result.curve]
        assert all(b >= a - 1e-9 for a, b in zip(values, values[1:]))
        assert result.personal is not None
        weaker = score_position(board.fen(), evals, _policy_putting(0.3, "d3"),
                                CONSTANTS, user_rating=2400)
        assert weaker is not None and weaker.personal is not None
        assert weaker.personal > result.personal


class TestVisibilityChangesTheScore:
    def test_a_forcing_best_move_scores_above_a_quiet_one(self) -> None:
        """Same human prior, same evals — only the move's visibility differs."""
        board = chess.Board(OPENING)
        forcing = [
            MoveEval(move=board.parse_san("Nxe5"), cp=300,
                     pv=[board.parse_san("Nxe5")], d_star=1),
            MoveEval(move=board.parse_san("a3"), cp=-200,
                     pv=[board.parse_san("a3")], d_star=1),
            MoveEval(move=board.parse_san("h3"), cp=-200,
                     pv=[board.parse_san("h3")], d_star=1),
        ]
        quiet = [
            MoveEval(move=board.parse_san("d3"), cp=300,
                     pv=[board.parse_san("d3")], d_star=1),
            MoveEval(move=board.parse_san("a3"), cp=-200,
                     pv=[board.parse_san("a3")], d_star=1),
            MoveEval(move=board.parse_san("h3"), cp=-200,
                     pv=[board.parse_san("h3")], d_star=1),
        ]

        def flat(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
            return {m: 0.2 for m in moves}

        sharp = score_position(board.fen(), forcing, flat, CONSTANTS)
        dull = score_position(board.fen(), quiet, flat, CONSTANTS)
        assert sharp is not None and dull is not None
        assert sharp.score > dull.score
