import csv
import sqlite3
from io import StringIO

import chess
import pytest

from pipeline.import_puzzles import (
    import_puzzles,
    opening_tag_from_family,
    row_to_position,
)


CSV_HEADER = (
    "PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,"
    "Themes,GameUrl,OpeningTags\n"
)
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
BLACK_TO_MOVE_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2"


@pytest.mark.parametrize(
    ("opening_family", "expected_tag"),
    [
        ("London System", "london"),
        ("Queen's Pawn Game: London System", "london"),
        ("Caro-Kann Defense", "caro-kann"),
        ("Caro Kann Defense", "caro-kann"),
        ("Sicilian Defense", None),
        ("", None),
        (None, None),
    ],
)
def test_opening_tag_from_family(
    opening_family: str | None,
    expected_tag: str | None,
) -> None:
    assert opening_tag_from_family(opening_family) == expected_tag


def test_row_to_position_converts_accepted_lichess_puzzle() -> None:
    position = row_to_position(
        {
            "PuzzleId": "puzzle-1",
            "FEN": BLACK_TO_MOVE_FEN,
            "Moves": "g8f6 b1c3",
            "Rating": "1800",
            "RatingDeviation": "76",
            "Popularity": "93",
            "NbPlays": "100",
            "Themes": "fork short",
            "GameUrl": "https://lichess.org/example",
            "OpeningTags": "Caro-Kann_Defense",
        }
    )

    assert position is not None
    assert position.fen == fen_after_move(BLACK_TO_MOVE_FEN, "g8f6")
    assert position.side_to_move == "w"
    assert position.source == "lichess_puzzle"
    assert position.classification == "tactical"
    assert position.opening_tag == "caro-kann"
    assert position.best_move == "b1c3"
    assert position.best_eval == 0.0
    assert position.solution_moves == "b1c3"
    assert position.themes == "fork,short"
    assert position.rating == 1800
    assert position.rating_deviation == 76


@pytest.mark.parametrize("rating", ["999", "2401", "", "not-a-number"])
def test_row_to_position_skips_out_of_scope_ratings(rating: str) -> None:
    row = {
        "FEN": START_FEN,
        "Moves": "e2e4 e7e5",
        "Rating": rating,
        "RatingDeviation": "80",
        "Themes": "opening",
        "OpeningTags": "London_System",
    }

    assert row_to_position(row) is None


def test_row_to_position_skips_illegal_first_move() -> None:
    assert (
        row_to_position(
            {
                "FEN": START_FEN,
                "Moves": "e2e5",
                "Rating": "1500",
                "RatingDeviation": "80",
                "Themes": "",
                "OpeningTags": "",
            }
        )
        is None
    )


def test_import_puzzles_filters_and_inserts_rows() -> None:
    csv_file = StringIO(
        CSV_HEADER
        + csv_row("1", START_FEN, "e2e4 e7e5", 1500, "London System")
        + csv_row("2", START_FEN, "d2d4 d7d5", 999, "London System")
        + csv_row("3", BLACK_TO_MOVE_FEN, "g8f6 b1c3", 2100, "Caro-Kann Defense")
        + csv_row("4", START_FEN, "c2c4 e7e5", 2401, "English Opening")
    )
    connection = sqlite3.connect(":memory:")

    inserted = import_puzzles(csv_file, connection, batch_size=2)

    assert inserted == 2
    rows = connection.execute(
        """
        SELECT fen, side_to_move, source, classification, opening_tag, best_move,
               best_eval, solution_moves, themes, rating, rating_deviation
        FROM positions
        ORDER BY rating
        """
    ).fetchall()
    assert rows == [
        (
            fen_after_move(START_FEN, "e2e4"),
            "b",
            "lichess_puzzle",
            "tactical",
            "london",
            "e7e5",
            0.0,
            "e7e5",
            "fork,pin",
            1500,
            80,
        ),
        (
            fen_after_move(BLACK_TO_MOVE_FEN, "g8f6"),
            "w",
            "lichess_puzzle",
            "tactical",
            "caro-kann",
            "b1c3",
            0.0,
            "b1c3",
            "fork,pin",
            2100,
            80,
        ),
    ]


def test_import_puzzles_rejects_invalid_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        import_puzzles(StringIO(CSV_HEADER), sqlite3.connect(":memory:"), batch_size=0)


def csv_row(
    puzzle_id: str,
    fen: str,
    moves: str,
    rating: int,
    opening_family: str,
) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            puzzle_id,
            fen,
            moves,
            rating,
            80,
            90,
            10,
            "fork pin",
            f"https://lichess.org/{puzzle_id}",
            opening_family,
        ]
    )
    return output.getvalue()


def fen_after_move(fen: str, move: str) -> str:
    board = chess.Board(fen)
    board.push(chess.Move.from_uci(move))
    return board.fen()
