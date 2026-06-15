"""Import Lichess puzzle CSV rows into the trainer database.

M2 intentionally trusts the Lichess puzzle source for ratings, themes, and
solutions. It does not run Stockfish during import.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import chess

from pipeline.io_utils import open_text_source


MIN_PUZZLE_RATING = 1000
MAX_PUZZLE_RATING = 2400
DEFAULT_BATCH_SIZE = 5000

POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    fen TEXT NOT NULL,
    side_to_move TEXT NOT NULL,
    source TEXT NOT NULL,
    classification TEXT NOT NULL,
    opening_tag TEXT,
    best_move TEXT NOT NULL,
    best_eval REAL NOT NULL,
    solution_moves TEXT,
    themes TEXT,
    rating INTEGER NOT NULL,
    rating_deviation INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_positions_classification
    ON positions(classification);
CREATE INDEX IF NOT EXISTS idx_positions_opening
    ON positions(opening_tag);
CREATE INDEX IF NOT EXISTS idx_positions_themes
    ON positions(themes);
"""


@dataclass(frozen=True)
class PuzzlePosition:
    fen: str
    side_to_move: str
    source: str
    classification: str
    opening_tag: str | None
    best_move: str
    best_eval: float
    solution_moves: str
    themes: str | None
    rating: int
    rating_deviation: int | None


def ensure_positions_schema(connection: sqlite3.Connection) -> None:
    """Create the M2 subset of the trainer schema needed for puzzle import."""

    connection.executescript(POSITIONS_SCHEMA)


def opening_tag_from_family(opening_family: str | None) -> str | None:
    """Map Lichess OpeningFamily text to launch opening tags."""

    normalized = (opening_family or "").strip().casefold()
    if "london" in normalized:
        return "london"
    if "caro-kann" in normalized or "caro kann" in normalized:
        return "caro-kann"
    return None


def row_to_position(row: dict[str, str]) -> PuzzlePosition | None:
    """Convert one Lichess CSV row into a DB-ready position, or skip it."""

    rating = parse_optional_int(row.get("Rating"))
    if rating is None or not (MIN_PUZZLE_RATING <= rating <= MAX_PUZZLE_RATING):
        return None

    fen = require_field(row, "FEN")
    solution_moves = require_field(row, "Moves")
    moves = solution_moves.split()
    if len(moves) < 2:
        return None

    board = chess.Board(fen)
    try:
        opponent_move = chess.Move.from_uci(moves[0])
    except ValueError:
        return None

    if opponent_move not in board.legal_moves:
        return None

    board.push(opponent_move)
    best_move = moves[1]
    try:
        move = chess.Move.from_uci(best_move)
    except ValueError:
        return None

    if move not in board.legal_moves:
        return None

    themes = normalize_empty(row.get("Themes"))

    return PuzzlePosition(
        fen=board.fen(),
        side_to_move="w" if board.turn == chess.WHITE else "b",
        source="lichess_puzzle",
        classification="tactical",
        opening_tag=opening_tag_from_family(
            row.get("OpeningFamily") or row.get("OpeningTags")
        ),
        best_move=best_move,
        # Lichess puzzle CSV has no eval, and M2 must not analyze with Stockfish.
        best_eval=0.0,
        solution_moves=" ".join(moves[1:]),
        themes=themes.replace(" ", ",") if themes else None,
        rating=rating,
        rating_deviation=parse_optional_int(row.get("RatingDeviation")),
    )


def import_puzzles(
    csv_file: TextIO,
    connection: sqlite3.Connection,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Stream-import Lichess puzzle CSV rows and return inserted row count."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    ensure_positions_schema(connection)

    reader = csv.DictReader(csv_file)
    inserted = 0

    for batch in batched(iter_positions(reader), batch_size):
        insert_positions(connection, batch)
        inserted += len(batch)

    connection.commit()
    return inserted


def iter_positions(rows: Iterable[dict[str, str]]) -> Iterator[PuzzlePosition]:
    for row in rows:
        position = row_to_position(row)
        if position is not None:
            yield position


def insert_positions(
    connection: sqlite3.Connection,
    positions: Iterable[PuzzlePosition],
) -> None:
    connection.executemany(
        """
        INSERT INTO positions (
            fen,
            side_to_move,
            source,
            classification,
            opening_tag,
            best_move,
            best_eval,
            solution_moves,
            themes,
            rating,
            rating_deviation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                position.fen,
                position.side_to_move,
                position.source,
                position.classification,
                position.opening_tag,
                position.best_move,
                position.best_eval,
                position.solution_moves,
                position.themes,
                position.rating,
                position.rating_deviation,
            )
            for position in positions
        ],
    )


def batched(
    values: Iterable[PuzzlePosition],
    batch_size: int,
) -> Iterator[list[PuzzlePosition]]:
    batch: list[PuzzlePosition] = []
    for value in values:
        batch.append(value)
        if len(batch) >= batch_size:
            yield batch
            batch = []

    if batch:
        yield batch


def parse_optional_int(value: str | None) -> int | None:
    value = normalize_empty(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def normalize_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require_field(row: dict[str, str], field_name: str) -> str:
    value = normalize_empty(row.get(field_name))
    if value is None:
        raise ValueError(f"Missing required CSV field: {field_name}")
    return value


def _main() -> None:
    parser = argparse.ArgumentParser(description="Import Lichess puzzle CSV into SQLite.")
    parser.add_argument("csv_source", help="Path or HTTPS URL to lichess_db_puzzle.csv")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "trainer.db",
        help="SQLite database path to create or update",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with open_text_source(args.csv_source) as csv_file:
        with sqlite3.connect(args.db) as connection:
            inserted = import_puzzles(csv_file, connection, batch_size=args.batch_size)

    print(f"Imported {inserted} tactical puzzles into {args.db}")


if __name__ == "__main__":
    _main()
