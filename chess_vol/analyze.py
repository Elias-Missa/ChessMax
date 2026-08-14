"""PGN → per-ply volatility analysis."""

from __future__ import annotations

import io
import queue
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import chess
import chess.pgn

from chess_vol.classify import Classification, classify_move
from chess_vol.game_review import MoveReview, attach_move_reviews
from core.findability import PositionFindability
from core.volatility import (
    EngineLike,
    VolatilityResult,
    compute_volatility,
)


@dataclass
class PlyResult:
    """One row of output from :func:`analyze_pgn`."""

    ply: int
    """1-based half-move index (1 = white's first move, 2 = black's reply, ...)."""

    san: str
    """Standard Algebraic Notation of the move played at this ply."""

    fen_before: str
    """FEN of the position *before* the move was played — what we analysed."""

    fen_after: str
    """FEN of the position *after* the move was played."""

    eval_cp: int
    """Engine best-line eval for the pre-move position, side-to-move POV."""

    volatility: VolatilityResult
    """Full volatility result for the pre-move position."""

    move_uci: str = ""
    """UCI of the move played at this ply."""

    classification: Classification | None = None
    """Optional move classification attached after neighbouring plies are known."""

    review: MoveReview | None = None
    """Expected-points game-review verdict attached after all plies are known."""

    findability: PositionFindability | None = None
    """Findability verdict (Game Review 2.0 §3). ``None`` when the position is
    gated out (decided position, book move) or no human model is installed.
    Attached opt-in by :func:`chess_vol.findability_review.attach_findability`."""


ProgressCallback = Callable[[int, int, PlyResult], None]
"""Signature: ``(ply_done, total_plies_or_0, last_result) -> None``."""


@dataclass
class _PlyJob:
    """One position to score, captured before any analysis runs."""

    ply: int
    board: chess.Board
    san: str
    move_uci: str
    fen_before: str
    fen_after: str


def _ply_jobs(game: chess.pgn.Game, ply_cap: int) -> list[_PlyJob]:
    """Walk the mainline once and snapshot every position we will analyse.

    Each snapshot keeps its move stack: the forced-recapture rule in
    :func:`core.volatility.compute_volatility` reads ``board.peek()``.
    """
    board = game.board()
    jobs: list[_PlyJob] = []
    for ply_index, move in enumerate(game.mainline_moves(), start=1):
        if ply_index > ply_cap:
            break
        fen_before = board.fen()
        san = board.san(move)
        move_uci = move.uci()
        snapshot = board.copy()
        board.push(move)
        jobs.append(
            _PlyJob(
                ply=ply_index,
                board=snapshot,
                san=san,
                move_uci=move_uci,
                fen_before=fen_before,
                fen_after=board.fen(),
            )
        )
    return jobs


def analyze_pgn(
    pgn: str,
    engine: EngineLike,
    *,
    max_plies: int | None = None,
    progress: ProgressCallback | None = None,
    engines: Sequence[EngineLike] | None = None,
    **volatility_kwargs: Any,
) -> list[PlyResult]:
    """Parse a PGN string and compute per-ply volatility.

    Parameters
    ----------
    pgn:
        PGN game text. Only the first game in the file is analysed.
    engine:
        Reused across every ply (README §6).
    max_plies:
        Optional cap on number of plies to analyse.
    progress:
        Optional callback invoked after each ply completes.
    engines:
        Optional pool of *additional* engines. Every ply is an independent
        position — nothing in the per-ply computation reads another ply's
        result — so handing over N engine processes lets the game be walked N
        plies at a time. Each position is still searched to the same depth
        with the same MultiPV by a single-threaded engine, so the numbers are
        unchanged; only wall-clock time moves. ``None`` or a single entry
        keeps the original sequential path.
    **volatility_kwargs:
        Passed straight through to :func:`compute_volatility`. Typical use:
        ``recurse_depth=2`` for deep mode, ``depth=20``, etc.

    Returns
    -------
    list[PlyResult]
        One entry per analysed ply, in move order.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError("Could not parse any game from the PGN input")

    total_plies = sum(1 for _ in game.mainline_moves())
    ply_cap = total_plies if max_plies is None else min(total_plies, max_plies)
    jobs = _ply_jobs(game, ply_cap)

    pool: list[EngineLike] = [engine] if not engines else list(engines)
    if len(pool) > 1:
        results = _analyze_parallel(jobs, pool, ply_cap, progress, volatility_kwargs)
    else:
        results = _analyze_sequential(jobs, pool[0], ply_cap, progress, volatility_kwargs)

    for idx, result in enumerate(results):
        next_result = results[idx + 1] if idx + 1 < len(results) else None
        result.classification = classify_move(result, next_result)

    attach_move_reviews(results, pgn)
    return results


def _to_result(job: _PlyJob, vol: VolatilityResult) -> PlyResult:
    return PlyResult(
        ply=job.ply,
        san=job.san,
        fen_before=job.fen_before,
        fen_after=job.fen_after,
        eval_cp=vol.best_eval_cp,
        volatility=vol,
        move_uci=job.move_uci,
    )


def _analyze_sequential(
    jobs: list[_PlyJob],
    engine: EngineLike,
    ply_cap: int,
    progress: ProgressCallback | None,
    volatility_kwargs: dict[str, Any],
) -> list[PlyResult]:
    results: list[PlyResult] = []
    for job in jobs:
        result = _to_result(job, compute_volatility(job.board, engine, **volatility_kwargs))
        results.append(result)
        if progress is not None:
            progress(job.ply, ply_cap, result)
    return results


def _analyze_parallel(
    jobs: list[_PlyJob],
    pool: Sequence[EngineLike],
    ply_cap: int,
    progress: ProgressCallback | None,
    volatility_kwargs: dict[str, Any],
) -> list[PlyResult]:
    """Same work, spread over the engine pool. Output order is still ply order.

    Progress fires in completion order, so it reports *how many* plies are
    done rather than pretending they finish in sequence.
    """
    available: queue.Queue[EngineLike] = queue.Queue()
    for eng in pool:
        available.put(eng)

    slots: list[PlyResult | None] = [None] * len(jobs)
    done = 0
    lock = threading.Lock()

    def run(index: int, job: _PlyJob) -> None:
        nonlocal done
        eng = available.get()
        try:
            vol = compute_volatility(job.board, eng, **volatility_kwargs)
        finally:
            available.put(eng)
        result = _to_result(job, vol)
        slots[index] = result
        if progress is not None:
            with lock:
                done += 1
                progress(done, ply_cap, result)

    with ThreadPoolExecutor(max_workers=len(pool)) as executor:
        list(executor.map(lambda pair: run(*pair), list(enumerate(jobs))))

    return [r for r in slots if r is not None]
