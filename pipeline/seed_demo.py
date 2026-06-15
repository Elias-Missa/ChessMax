"""Seed a tiny demo position set for local UI smoke testing.

The real app data comes from M2 and M3. This script is just a convenience so
the M4 training UI has something to serve before large datasets are imported.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import chess

from server import db


@dataclass(frozen=True)
class DemoPosition:
    fen: str
    classification: str
    best_move: str
    solution_moves: str | None
    themes: str | None
    rating: int = 1500


DEMO_POSITIONS: tuple[DemoPosition, ...] = (
    DemoPosition(
        fen="rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq g3 0 2",
        classification="tactical",
        best_move="d8h4",
        solution_moves="d8h4",
        themes="mateIn1",
    ),
    DemoPosition(
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
        classification="tactical",
        best_move="g8f6",
        solution_moves="g8f6",
        themes="development",
    ),
    DemoPosition(
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        classification="quiet",
        best_move="e2e4",
        solution_moves=None,
        themes=None,
    ),
    DemoPosition(
        fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
        classification="quiet",
        best_move="g1f3",
        solution_moves=None,
        themes=None,
    ),
    DemoPosition(
        fen="rnbqkb1r/ppp2ppp/5n2/3pp3/3P4/5NP1/PPP1PPBP/RNBQK2R w KQkq - 0 5",
        classification="quiet",
        best_move="e1g1",
        solution_moves=None,
        themes=None,
    ),
    # Multi-move forced line for the Forced Lines mode (mate in 2):
    # 1.Qd8+ Bxd8 2.Re8#
    DemoPosition(
        fen="r1b2k1r/ppp1bppp/8/1B1Q4/5q2/2P5/PPP2PPP/R3R1K1 w - - 1 1",
        classification="tactical",
        best_move="d5d8",
        solution_moves="d5d8 e7d8 e1e8",
        themes="mateIn2,sacrifice",
    ),
    # Worse-but-holdable positions for the Defense Gym (mover ~ -1 to -2).
    DemoPosition(
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPP1PPPP/RNBQKBNR w KQkq - 0 1",
        classification="quiet",
        best_move="g1f3",
        solution_moves=None,
        themes=None,
    ),
    DemoPosition(
        fen="rnbqkbnr/pppppppp/8/8/8/8/1PPPPPPP/RNBQKBNR w KQkq - 0 1",
        classification="quiet",
        best_move="e2e4",
        solution_moves=None,
        themes=None,
    ),
)


def seed_demo_positions(connection: sqlite3.Connection) -> int:
    db.ensure_app_schema(connection)

    # Remove rows that reference existing demo positions before reseeding.
    stale_ids = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM positions WHERE source = ?", ("demo_seed",)
        ).fetchall()
    ]
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        for table in (
            "attempts",
            "playouts",
            "playout_sessions",
            "position_evals",
            "hold_sessions",
            "hold_results",
            "forced_attempts",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE position_id IN ({placeholders})",
                stale_ids,
            )
    connection.execute("DELETE FROM positions WHERE source = ?", ("demo_seed",))

    inserted = 0
    for position in DEMO_POSITIONS:
        board = chess.Board(position.fen)
        move = chess.Move.from_uci(position.best_move)
        if move not in board.legal_moves:
            raise ValueError(f"Illegal demo best move {position.best_move} for {position.fen}")

        connection.execute(
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
            (
                position.fen,
                "w" if board.turn == chess.WHITE else "b",
                "demo_seed",
                position.classification,
                None,
                position.best_move,
                0.0,
                position.solution_moves,
                position.themes,
                position.rating,
                None,
            ),
        )
        inserted += 1

    connection.commit()
    return inserted


def _main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo positions for local testing.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "trainer.db",
        help="SQLite database path to create or update",
    )
    args = parser.parse_args()

    with db.connect(args.db) as connection:
        inserted = seed_demo_positions(connection)

    print(f"Seeded {inserted} demo positions into {args.db}")


if __name__ == "__main__":
    _main()
