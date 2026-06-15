"""Mine quiet training positions from Lichess PGN games.

The core sampler is deliberately small and dependency-injected so tests can
verify the one-window / one-position rules without launching Stockfish.

Quiet positions are opening-agnostic: opening-flavored practice comes from the
tagged Lichess puzzle pool, not from this pipeline.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import chess
import chess.pgn

from pipeline.import_puzzles import ensure_positions_schema
from pipeline.io_utils import open_text_source
from server.engine import StockfishAnalyzer, analyze, is_quiet_position


MIN_PLAYER_RATING = 1500
MAX_PLAYER_RATING = 2200
MIN_GAME_LENGTH_MOVES = 25
QUIET_POSITION_RATING = 1500
MAX_LOPSIDED_EVAL_CP = 400
DEFAULT_BATCH_SIZE = 500
DEFAULT_TARGET_COUNT = 100
MOVE_WINDOWS: tuple[tuple[int, int], ...] = ((12, 15), (18, 22), (25, 30))

AnalysisFn = Callable[[str], dict[str, list[dict[str, int | str]]]]
QuietPredicate = Callable[[dict[str, list[dict[str, int | str]]]], bool]


@dataclass(frozen=True)
class QuietPosition:
    fen: str
    side_to_move: str
    best_move: str
    best_eval: float


def game_passes_filters(game: chess.pgn.Game) -> bool:
    """Return whether a PGN game is eligible before Stockfish sampling."""

    white_elo = parse_rating(game.headers.get("WhiteElo"))
    black_elo = parse_rating(game.headers.get("BlackElo"))
    if not rating_in_range(white_elo) or not rating_in_range(black_elo):
        return False

    if count_full_moves(game) < MIN_GAME_LENGTH_MOVES:
        return False

    return True


def sample_quiet_position(
    game: chess.pgn.Game,
    rng: random.Random | None = None,
    analysis_fn: AnalysisFn | None = None,
    quiet_predicate: QuietPredicate = is_quiet_position,
) -> QuietPosition | None:
    """Sample at most one quiet position from one randomly chosen window."""

    rng = rng or random.Random()
    analysis_fn = analysis_fn or stockfish_analysis
    start_move, end_move = rng.choice(MOVE_WINDOWS)
    candidates: list[QuietPosition] = []

    for board in positions_in_move_window(game, range(start_move, end_move + 1)):
        if board.legal_moves.count() <= 1:
            continue

        analysis = analysis_fn(board.fen())
        top_moves = analysis.get("top_moves", [])
        if not top_moves:
            continue

        best_eval = float(top_moves[0]["eval"])
        if abs(best_eval) > MAX_LOPSIDED_EVAL_CP:
            continue

        if quiet_predicate(analysis):
            candidates.append(
                QuietPosition(
                    fen=board.fen(),
                    side_to_move="w" if board.turn == chess.WHITE else "b",
                    best_move=str(top_moves[0]["move"]),
                    best_eval=best_eval,
                )
            )

    if not candidates:
        return None

    return rng.choice(candidates)


def mine_quiet_positions(
    pgn_file: TextIO,
    connection: sqlite3.Connection,
    target_count: int = DEFAULT_TARGET_COUNT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    rng: random.Random | None = None,
    analysis_fn: AnalysisFn | None = None,
    quiet_predicate: QuietPredicate = is_quiet_position,
    progress_every: int | None = None,
) -> int:
    """Stream PGN games, insert quiet positions, and return inserted count."""

    if target_count < 1:
        raise ValueError("target_count must be at least 1")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    ensure_positions_schema(connection)
    existing_fens = load_existing_quiet_fens(connection)
    initial_count = len(existing_fens)

    inserted = 0
    scanned = 0
    eligible = 0
    skipped_duplicate = 0
    batch: list[QuietPosition] = []

    while initial_count + inserted < target_count:
        game = chess.pgn.read_game(pgn_file)
        if game is None:
            break
        scanned += 1
        if not game_passes_filters(game):
            if progress_every and scanned % progress_every == 0:
                print_progress(scanned, eligible, inserted, skipped_duplicate)
            continue
        eligible += 1

        position = sample_quiet_position(
            game,
            rng=rng,
            analysis_fn=analysis_fn,
            quiet_predicate=quiet_predicate,
        )
        if position is None:
            if progress_every and scanned % progress_every == 0:
                print_progress(scanned, eligible, inserted, skipped_duplicate)
            continue

        if position.fen in existing_fens:
            skipped_duplicate += 1
            if progress_every and scanned % progress_every == 0:
                print_progress(scanned, eligible, inserted, skipped_duplicate)
            continue
        existing_fens.add(position.fen)

        batch.append(position)
        if len(batch) >= batch_size:
            inserted += insert_quiet_positions(connection, batch)
            connection.commit()
            batch = []
            if progress_every:
                print_progress(scanned, eligible, inserted, skipped_duplicate)

    if batch and initial_count + inserted < target_count:
        remaining = target_count - initial_count - inserted
        inserted += insert_quiet_positions(connection, batch[:remaining])
        connection.commit()

    connection.commit()
    return inserted


def load_existing_quiet_fens(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT fen FROM positions WHERE classification = 'quiet'"
        ).fetchall()
    }


def insert_quiet_positions(
    connection: sqlite3.Connection,
    positions: Sequence[QuietPosition],
) -> int:
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
                "pipeline_quiet",
                "quiet",
                None,
                position.best_move,
                position.best_eval,
                None,
                None,
                QUIET_POSITION_RATING,
                None,
            )
            for position in positions
        ],
    )
    return len(positions)


def positions_in_move_window(
    game: chess.pgn.Game,
    target_moves: range,
) -> Iterator[chess.Board]:
    """Yield board copies after each ply whose just-played full-move is in ``target_moves``.

    Yields both colors-to-move so the mined pool isn't biased toward White.
    """

    targets = set(target_moves)
    board = game.board()
    for move in game.mainline_moves():
        board.push(move)
        # After White's move N: turn=BLACK, fullmove_number=N
        # After Black's move N: turn=WHITE, fullmove_number=N+1
        just_played = (
            board.fullmove_number
            if board.turn == chess.BLACK
            else board.fullmove_number - 1
        )
        if just_played in targets:
            yield board.copy(stack=False)


def stockfish_analysis(fen: str) -> dict[str, list[dict[str, int | str]]]:
    return analyze(fen, depth=20, multipv=5)


def analyzer_at_depth(
    analyzer: StockfishAnalyzer,
    depth: int,
) -> AnalysisFn:
    def analyze_at_depth(fen: str) -> dict[str, list[dict[str, int | str]]]:
        return analyzer.analyze(fen, depth=depth, multipv=5)

    return analyze_at_depth


def count_full_moves(game: chess.pgn.Game) -> int:
    board = game.board()
    completed_full_moves = 0
    for move in game.mainline_moves():
        board.push(move)
        if board.turn == chess.WHITE:
            completed_full_moves = board.fullmove_number - 1
    return completed_full_moves


def parse_rating(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def rating_in_range(rating: int | None) -> bool:
    return rating is not None and MIN_PLAYER_RATING <= rating <= MAX_PLAYER_RATING


def print_progress(
    scanned: int,
    eligible: int,
    inserted: int,
    skipped_duplicate: int = 0,
) -> None:
    print(
        f"scanned={scanned} eligible={eligible} inserted={inserted} "
        f"dup_skipped={skipped_duplicate}",
        file=sys.stderr,
        flush=True,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Mine quiet positions from PGN games.")
    parser.add_argument("pgn_source", help="Path or HTTPS URL to a PGN or PGN .zst file")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "trainer.db",
        help="SQLite database path to create or update",
    )
    parser.add_argument("--target-count", type=int, default=DEFAULT_TARGET_COUNT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--seed", type=int, help="Optional RNG seed for repeatable runs")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument(
        "--depth",
        type=int,
        default=20,
        help="Stockfish analysis depth for candidate quiet positions",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Stockfish UCI Threads option",
    )
    parser.add_argument(
        "--hash-mb",
        type=int,
        default=128,
        help="Stockfish UCI Hash option in MB",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with open_text_source(args.pgn_source) as pgn_file:
        with sqlite3.connect(args.db) as connection:
            with StockfishAnalyzer(threads=args.threads, hash_mb=args.hash_mb) as analyzer:
                inserted = mine_quiet_positions(
                    pgn_file,
                    connection,
                    target_count=args.target_count,
                    batch_size=args.batch_size,
                    rng=rng,
                    analysis_fn=analyzer_at_depth(analyzer, args.depth),
                    progress_every=args.progress_every,
                )

    print(f"Inserted {inserted} quiet positions into {args.db}")


if __name__ == "__main__":
    _main()
