from __future__ import annotations

import chess
import pytest

from core.calibration import (
    PuzzleRow,
    brier_score,
    keep_puzzle,
    linear_fit,
    parse_puzzle_row,
    pearson_r,
    rating_band,
    solution_plies,
    solver_position,
    stratified_sample,
)


def _row(**kw) -> PuzzleRow:
    base = dict(
        puzzle_id="P",
        fen=chess.STARTING_FEN,
        moves=("e2e4", "e7e5"),
        rating=1500,
        rating_deviation=70,
        nb_plays=5000,
        themes=(),
    )
    base.update(kw)
    return PuzzleRow(**base)


def test_parse_row_from_csv_record() -> None:
    record = [
        "abcde",
        chess.STARTING_FEN,
        "e2e4 e7e5 g1f3",
        "1600",
        "75",
        "90",
        "3200",
        "mateIn2 short",
        "https://lichess.org/x",
        "Kings_Pawn",
    ]
    row = parse_puzzle_row(record)
    assert row.puzzle_id == "abcde"
    assert row.moves == ("e2e4", "e7e5", "g1f3")
    assert row.rating == 1600
    assert row.nb_plays == 3200
    assert row.themes == ("mateIn2", "short")


def test_fen_convention_setup_move_then_solver() -> None:
    """THE TRAP (spec §4.1): FEN is *before* the opponent's setup move.

    ``moves[0]`` is the opponent's; the solver moves *second* with ``moves[1]``.
    Getting this backwards silently poisons the whole dataset.
    """
    row = _row(fen=chess.STARTING_FEN, moves=("e2e4", "e7e5"))
    fen, solver_move = solver_position(row)

    board = chess.Board(fen)
    # After the opponent's setup move (e4), it is the *solver's* turn (Black).
    assert board.turn == chess.BLACK
    # The solver's move is moves[1], and it is legal in the faced position.
    assert solver_move == chess.Move.from_uci("e7e5")
    assert solver_move in board.legal_moves
    # And the faced FEN is NOT the raw puzzle FEN.
    assert fen != row.fen


class TestFiltering:
    def test_drops_low_play_count(self) -> None:
        assert not keep_puzzle(_row(nb_plays=500))
        assert keep_puzzle(_row(nb_plays=5000))

    def test_drops_mate_in_1(self) -> None:
        assert not keep_puzzle(_row(themes=("mateIn1",)))

    def test_drops_long_solutions(self) -> None:
        long_row = _row(moves=("e2e4", "e7e5", "g1f3", "b8c6", "f1b5"))
        assert solution_plies(long_row) == 4
        assert not keep_puzzle(long_row, max_solution_plies=3)


def test_rating_band_buckets() -> None:
    assert rating_band(1490) == 1400
    assert rating_band(1500) == 1400
    assert rating_band(1600) == 1600


def test_stratified_sample_caps_each_band() -> None:
    rows = [_row(rating=1500) for _ in range(50)] + [_row(rating=2100) for _ in range(3)]
    sample = stratified_sample(rows, per_band=10, seed=1)
    bands: dict[int, int] = {}
    for row in sample:
        bands[rating_band(row.rating)] = bands.get(rating_band(row.rating), 0) + 1
    assert bands[1400] == 10  # dense band capped
    assert bands[2000] == 3  # sparse band kept whole


class TestMetrics:
    def test_pearson_perfect_and_degenerate(self) -> None:
        assert pearson_r([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
        assert pearson_r([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
        assert pearson_r([1, 1, 1], [1, 2, 3]) == 0.0

    def test_brier(self) -> None:
        assert brier_score([1.0, 0.0], [1, 0]) == 0.0
        assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)

    def test_linear_fit_recovers_slope_and_intercept(self) -> None:
        slope, intercept = linear_fit([0, 1, 2, 3], [1, 3, 5, 7])  # y = 2x + 1
        assert slope == pytest.approx(2.0)
        assert intercept == pytest.approx(1.0)
