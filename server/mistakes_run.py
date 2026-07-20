"""Generation orchestration for "Your Mistakes".

Ties Chess.com ingest → detector → DB insert together, emitting progress events.
Engine access is passed in as plain callables (``analysis_fn``, ``maia_topk_fn``,
``volatility_fn``) and the HTTP layer as ``fetch_json``, so this whole pipeline
is unit-testable without Stockfish, lc0, or the network. The API layer
(:mod:`server.mistakes_api`) is responsible for opening the real engines and
threading these callables in.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from typing import Any, Callable

import chess
import chess.pgn

from pipeline import chesscom
from server.mistakes import MistakePuzzle, detect_mistakes

EventFn = Callable[[str, dict[str, Any]], None]

_INSERT_COLUMNS = (
    "user_id", "run_id", "bucket", "fen", "side_to_move", "user_color",
    "best_move", "solution_moves", "user_actual_move",
    "eval_before_cp", "eval_best_cp", "eval_played_cp", "second_best_gap_cp",
    "volatility", "maia1900_p_solution", "maia_solution_rank", "maia_best_in_top3",
    "clock_seconds", "ply_number", "game_url", "game_id", "game_date",
    "time_class", "opponent", "caption",
)


def _start_run(connection: sqlite3.Connection, user_id: int, username: str, since: datetime) -> int:
    # Retire any earlier runs left stuck at 'running' (a worker killed mid-run by
    # a dev reload or hard disconnect never reaches its finaliser). Doing this at
    # the start of each new run keeps the latest run row trustworthy.
    connection.execute(
        "UPDATE mistake_runs SET status = 'interrupted', updated_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ? AND status = 'running'",
        (user_id,),
    )
    cur = connection.execute(
        "INSERT INTO mistake_runs (user_id, chesscom_user, since_date, status) "
        "VALUES (?, ?, ?, 'running')",
        (user_id, username, since.date().isoformat()),
    )
    connection.commit()
    return int(cur.lastrowid)


def _update_run(connection: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    connection.execute(
        f"UPDATE mistake_runs SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), run_id),
    )
    connection.commit()


def _count_unsolved(connection: sqlite3.Connection, user_id: int) -> int:
    """How many unsolved mistake puzzles this user has waiting (across all runs)."""

    row = connection.execute(
        "SELECT COUNT(*) AS n FROM mistake_puzzles WHERE user_id = ? AND solved = 0",
        (user_id,),
    ).fetchone()
    return int(row["n"] if row is not None else 0)


def _insert_puzzle(
    connection: sqlite3.Connection, user_id: int, run_id: int, puzzle: MistakePuzzle
) -> int | None:
    """Insert one puzzle; return its id, or ``None`` if it was a dedupe hit."""

    values = (
        user_id, run_id, puzzle.bucket, puzzle.fen, puzzle.side_to_move,
        puzzle.user_color, puzzle.best_move, puzzle.solution_moves,
        puzzle.user_actual_move, puzzle.eval_before_cp, puzzle.eval_best_cp,
        puzzle.eval_played_cp, puzzle.second_best_gap_cp, puzzle.volatility,
        None, puzzle.maia_solution_rank, puzzle.maia_best_in_top3,
        puzzle.clock_seconds, puzzle.ply_number, puzzle.game_url, puzzle.game_id,
        puzzle.game_date, puzzle.time_class, puzzle.opponent, puzzle.caption,
    )
    placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
    cur = connection.execute(
        f"INSERT OR IGNORE INTO mistake_puzzles ({', '.join(_INSERT_COLUMNS)}) "
        f"VALUES ({placeholders})",
        values,
    )
    connection.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def generate_mistakes(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    username: str,
    since: datetime,
    analysis_fn: Callable[..., Any],
    maia_topk_fn: Callable[[str], list[str] | None] | None,
    volatility_fn: Callable[[str], float | None] | None,
    fetch_json: chesscom.FetchJson | None = None,
    on_event: EventFn | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    detect_kwargs: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Run the full generate pipeline, inserting puzzles as they're found.

    Returns ``{games_scanned, games_eligible, puzzles_created}``.
    """

    emit = on_event or (lambda _e, _p: None)
    cancelled = is_cancelled or (lambda: False)
    detect_kwargs = detect_kwargs or {}

    run_id = _start_run(connection, user_id, username, since)
    scanned = eligible = created = 0
    emit("start", {"run_id": run_id, "username": username, "since": since.date().isoformat()})

    try:
        for pgn, meta in chesscom.iter_games(username, since, fetch_json=fetch_json):
            if cancelled():
                break
            scanned += 1
            game = chess.pgn.read_game(io.StringIO(pgn))
            if game is None:
                continue
            user_color = chess.WHITE if meta.get("user_color") == "white" else chess.BLACK
            eligible += 1

            for puzzle in detect_mistakes(
                game, user_color,
                analysis_fn=analysis_fn,
                maia_topk_fn=maia_topk_fn,
                volatility_fn=volatility_fn,
                meta=meta,
                **detect_kwargs,
            ):
                puzzle_id = _insert_puzzle(connection, user_id, run_id, puzzle)
                if puzzle_id is not None:
                    created += 1
                    emit("puzzle", {"id": puzzle_id, "bucket": puzzle.bucket, "fen": puzzle.fen})

            _update_run(
                connection, run_id,
                games_scanned=scanned, games_eligible=eligible, puzzles_created=created,
            )
            emit("progress", {
                "games_scanned": scanned,
                "games_eligible": eligible,
                "puzzles_created": created,
            })

    except Exception as exc:  # noqa: BLE001 — mark the run, re-raise for the worker to report
        # A client disconnect surfaces as a cancellation exception raised from
        # `emit`; that's a clean stop, not a failure, so record it as such.
        terminal = "interrupted" if cancelled() else "error"
        _update_run(
            connection, run_id, status=terminal,
            detail=None if terminal == "interrupted" else repr(exc),
            games_scanned=scanned, games_eligible=eligible, puzzles_created=created,
        )
        raise

    total_unsolved = _count_unsolved(connection, user_id)
    _update_run(
        connection, run_id, status="done",
        games_scanned=scanned, games_eligible=eligible, puzzles_created=created,
    )
    # The run is durably 'done' before we emit; a disconnect during this final
    # event must not downgrade an already-completed run.
    try:
        emit("done", {
            "run_id": run_id,
            "games_scanned": scanned,
            "games_eligible": eligible,
            "puzzles_created": created,
            "total_unsolved": total_unsolved,
        })
    except Exception:  # noqa: BLE001 — status already committed as 'done'
        pass

    return {
        "games_scanned": scanned,
        "games_eligible": eligible,
        "puzzles_created": created,
        "total_unsolved": total_unsolved,
    }
