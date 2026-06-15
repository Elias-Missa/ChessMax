import sqlite3
from io import StringIO
from typing import Any

import chess
import chess.pgn
import pytest

from pipeline.mine_quiet import (
    MOVE_WINDOWS,
    game_passes_filters,
    mine_quiet_positions,
    positions_in_move_window,
    sample_quiet_position,
)


class FirstWindowFirstCandidateRng:
    def choice(self, values: Any) -> Any:
        if values == MOVE_WINDOWS:
            return (12, 15)
        return values[0]


class LateWindowFirstCandidateRng:
    def choice(self, values: Any) -> Any:
        if values == MOVE_WINDOWS:
            return (25, 30)
        return values[0]


def quiet_analysis(fen: str) -> dict[str, list[dict[str, int | str]]]:
    board = chess.Board(fen)
    moves = list(board.legal_moves)[:5]
    evals = [10, 20, 30, 40, 50]
    return {
        "top_moves": [
            {"move": move.uci(), "eval": eval_cp}
            for move, eval_cp in zip(moves, evals, strict=True)
        ]
    }


def lopsided_analysis(fen: str) -> dict[str, list[dict[str, int | str]]]:
    board = chess.Board(fen)
    moves = list(board.legal_moves)[:5]
    evals = [401, 20, 30, 40, 50]
    return {
        "top_moves": [
            {"move": move.uci(), "eval": eval_cp}
            for move, eval_cp in zip(moves, evals, strict=True)
        ]
    }


def test_game_passes_filters_accepts_eligible_game() -> None:
    game = make_game(white_elo="1800", black_elo="1900")

    assert game_passes_filters(game) is True


@pytest.mark.parametrize(
    ("white_elo", "black_elo", "expected"),
    [
        ("1499", "1900", False),
        ("1800", "2201", False),
        ("?", "1900", False),
        ("1800", "1900", True),
    ],
)
def test_game_passes_filters_checks_rating_range(
    white_elo: str,
    black_elo: str,
    expected: bool,
) -> None:
    game = make_game(white_elo=white_elo, black_elo=black_elo)

    assert game_passes_filters(game) is expected


def test_game_passes_filters_rejects_short_games() -> None:
    game = make_game(full_moves=24)

    assert game_passes_filters(game) is False


def test_positions_in_move_window_yields_both_colors() -> None:
    game = make_game(full_moves=30)

    boards = list(positions_in_move_window(game, range(12, 15)))

    # 3 target move numbers × 2 plies each = 6 positions
    assert len(boards) == 6
    # First yield in each pair is after White's move (Black to move)
    assert [board.turn for board in boards] == [
        chess.BLACK,
        chess.WHITE,
        chess.BLACK,
        chess.WHITE,
        chess.BLACK,
        chess.WHITE,
    ]


def test_sample_quiet_position_uses_one_random_window_and_one_position() -> None:
    seen_fens: list[str] = []

    def recording_analysis(fen: str) -> dict[str, list[dict[str, int | str]]]:
        seen_fens.append(fen)
        return quiet_analysis(fen)

    position = sample_quiet_position(
        make_game(full_moves=30),
        rng=FirstWindowFirstCandidateRng(),
        analysis_fn=recording_analysis,
    )

    assert position is not None
    # First candidate in window (12, 15) is the position after White's move 12 → Black to move.
    assert position.side_to_move == "b"
    assert chess.Move.from_uci(position.best_move) in chess.Board(position.fen).legal_moves
    assert position.best_eval == 10.0
    # 4 move numbers (12-15) × 2 plies each = 8 candidate FENs analyzed
    assert len(seen_fens) == 8


def test_sample_quiet_position_rejects_lopsided_positions() -> None:
    position = sample_quiet_position(
        make_game(full_moves=30),
        rng=FirstWindowFirstCandidateRng(),
        analysis_fn=lopsided_analysis,
    )

    assert position is None


def test_mine_quiet_positions_filters_games_and_inserts_rows() -> None:
    pgn_file = StringIO(
        export_game(make_game(white_elo="1800", black_elo="1900"))
        + "\n\n"
        + export_game(make_game(white_elo="1400", black_elo="1900"))
    )
    connection = sqlite3.connect(":memory:")

    inserted = mine_quiet_positions(
        pgn_file,
        connection,
        target_count=5,
        batch_size=1,
        rng=FirstWindowFirstCandidateRng(),
        analysis_fn=quiet_analysis,
    )

    assert inserted == 1
    rows = connection.execute(
        """
        SELECT source, classification, opening_tag, best_eval,
               solution_moves, themes, rating, rating_deviation
        FROM positions
        """
    ).fetchall()
    assert rows == [
        (
            "pipeline_quiet",
            "quiet",
            None,
            10.0,
            None,
            None,
            1500,
            None,
        )
    ]


def test_mine_quiet_positions_stops_at_target_count() -> None:
    pgn_file = StringIO(
        export_game(make_game())
        + "\n\n"
        + export_game(make_game())
    )
    connection = sqlite3.connect(":memory:")

    inserted = mine_quiet_positions(
        pgn_file,
        connection,
        target_count=1,
        rng=LateWindowFirstCandidateRng(),
        analysis_fn=quiet_analysis,
    )

    assert inserted == 1
    count = connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 1


def test_mine_quiet_positions_validates_arguments() -> None:
    with pytest.raises(ValueError, match="target_count"):
        mine_quiet_positions(StringIO(""), sqlite3.connect(":memory:"), target_count=0)

    with pytest.raises(ValueError, match="batch_size"):
        mine_quiet_positions(StringIO(""), sqlite3.connect(":memory:"), batch_size=0)


def make_game(
    full_moves: int = 25,
    white_elo: str = "1800",
    black_elo: str = "1900",
) -> chess.pgn.Game:
    game = chess.pgn.Game()
    game.headers["Event"] = "Test game"
    game.headers["WhiteElo"] = white_elo
    game.headers["BlackElo"] = black_elo

    board = game.board()
    node = game
    for full_move_index in range(full_moves):
        move_pair = (
            ("g1f3", "g8f6")
            if full_move_index % 2 == 0
            else ("f3g1", "f6g8")
        )
        for uci in move_pair:
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves
            node = node.add_variation(move)
            board.push(move)

    return game


def export_game(game: chess.pgn.Game) -> str:
    return str(game)
