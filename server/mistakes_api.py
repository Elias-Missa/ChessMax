"""API routes for "Your Mistakes" — personalized puzzles from the user's games.

Factory mirrors :func:`server.modes_api.build_modes_router`; the SSE generation
endpoint mirrors the volatility ``/analyze/pgn`` streamer in
:mod:`chess_vol.server` (asyncio queue + background worker thread, with
client-disconnect handling that lets the worker finish so inserts persist).

Engine access is taken from ``app.state`` so tests inject fakes:
- ``app.state.maia_topk_fn``        — Maia top-K findability (server.maia.maia_top_moves)
- ``app.state.mistakes_generate_fn``— override the whole generate worker (no engines in tests)
- ``app.state.fetch_json``          — Chess.com HTTP layer (stubbed in tests)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import AsyncIterator
from typing import Any, Callable

import chess
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from pipeline import chesscom
from server import db
from server.deps import current_user, get_connection
from server.mistakes_run import generate_mistakes

logger = logging.getLogger("server.mistakes")


class _Cancelled(Exception):
    """Raised in the worker thread when the SSE client disconnected."""


class UsernameRequest(BaseModel):
    chesscom_username: str = Field(min_length=1, max_length=64)


class GenerateRequest(BaseModel):
    chesscom_username: str | None = Field(default=None, max_length=64)
    since_days: int = Field(default=chesscom.LOOKBACK_DAYS, ge=1, le=365)


class MistakeAttemptRequest(BaseModel):
    move: str = Field(min_length=4, max_length=5)
    step: int = Field(default=0, ge=0)


def _default_generate(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    username: str,
    since: Any,
    on_event: Callable[[str, dict[str, Any]], None],
    is_cancelled: Callable[[], bool],
    fetch_json: chesscom.FetchJson | None,
    maia_topk_fn: Callable[[str], list[str] | None] | None,
) -> dict[str, int]:
    """Real-engine generation: one Stockfish + one lc0 + one vol engine per run."""

    import chess as _chess
    from chess_vol.engine import Engine
    from chess_vol.volatility import compute_volatility
    from server.engine import StockfishAnalyzer
    from server.maia import MaiaPolicyEngine

    # One Stockfish + one vol engine + one lc0 (Maia) process for the WHOLE run.
    # Reusing a single lc0 instead of cold-starting one per candidate position
    # (the old `maia_top_moves` behaviour) is the difference between a run taking
    # minutes vs. effectively hanging on the weight-file reload each move.
    with StockfishAnalyzer() as analyzer, Engine() as vol_engine, MaiaPolicyEngine() as maia:
        def volatility_fn(fen: str) -> float | None:
            try:
                return compute_volatility(_chess.Board(fen), vol_engine).score
            except Exception:  # noqa: BLE001 — volatility is a non-critical tag
                return None

        # Prefer the persistent engine; fall back to the injected cold-start seam
        # (e.g. tests) only when the persistent lc0 couldn't be opened.
        run_maia_topk = maia.top_moves if maia.available else maia_topk_fn

        return generate_mistakes(
            connection,
            user_id=user_id,
            username=username,
            since=since,
            analysis_fn=analyzer.analyze,
            maia_topk_fn=run_maia_topk,
            volatility_fn=volatility_fn,
            fetch_json=fetch_json,
            on_event=on_event,
            is_cancelled=is_cancelled,
        )


def build_mistakes_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api")

    # ------------------------------------------------------------------ #
    # Username (mirrors the openings get/set pair)                        #
    # ------------------------------------------------------------------ #

    @router.get("/mistakes/username")
    def get_username(
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return {"chesscom_username": user["chesscom_username"]}

    @router.put("/mistakes/username")
    def set_username(
        request: UsernameRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        name = request.chesscom_username.strip()
        connection.execute(
            "UPDATE users SET chesscom_username = ? WHERE id = ?", (name, user["id"])
        )
        connection.commit()
        return {"chesscom_username": name}

    @router.get("/mistakes/run")
    def latest_run(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM mistake_runs WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        counts = connection.execute(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN solved = 0 THEN 1 ELSE 0 END) AS unsolved "
            "FROM mistake_puzzles WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        return {
            "run": dict(row) if row is not None else None,
            "total_puzzles": int(counts["total"] or 0),
            "unsolved_puzzles": int(counts["unsolved"] or 0),
        }

    # ------------------------------------------------------------------ #
    # Serve + solve                                                       #
    # ------------------------------------------------------------------ #

    @router.get("/mistakes/next")
    def next_mistake(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM mistake_puzzles WHERE user_id = ? AND solved = 0 "
            "ORDER BY id LIMIT 1",
            (user["id"],),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="No mistake puzzles yet — generate from your games first.",
            )
        pv = (row["solution_moves"] or "").split()
        return {
            "position_id": row["id"],
            "fen": row["fen"],
            "side_to_move": row["side_to_move"],
            "bucket": row["bucket"],
            "ply_count": len(pv),
        }

    @router.post("/mistakes/{puzzle_id}/attempt")
    def attempt(
        puzzle_id: int,
        request: MistakeAttemptRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM mistake_puzzles WHERE id = ? AND user_id = ?",
            (puzzle_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Puzzle not found")

        best_uci = row["best_move"]
        solved = request.move == best_uci
        connection.execute(
            "UPDATE mistake_puzzles SET attempts = attempts + 1, solved = MAX(solved, ?) "
            "WHERE id = ?",
            (1 if solved else 0, puzzle_id),
        )
        connection.commit()

        board = chess.Board(row["fen"])

        def san(uci: str) -> str:
            try:
                return board.san(chess.Move.from_uci(uci))
            except (chess.InvalidMoveError, chess.IllegalMoveError, ValueError):
                return uci

        return {
            "status": "solved" if solved else "failed",
            "solved": solved,
            "bucket": row["bucket"],
            "best_move_uci": best_uci,
            "best_move_san": san(best_uci),
            "user_actual_uci": row["user_actual_move"],
            "user_actual_san": san(row["user_actual_move"]),
            "solution_line": row["solution_moves"],
            "caption": row["caption"],
            "game_url": row["game_url"],
            "eval_before_cp": row["eval_before_cp"],
            "eval_best_cp": row["eval_best_cp"],
            "eval_played_cp": row["eval_played_cp"],
            "volatility": row["volatility"],
        }

    # ------------------------------------------------------------------ #
    # Generate (SSE stream)                                               #
    # ------------------------------------------------------------------ #

    @router.post("/mistakes/generate")
    async def generate(
        request_body: GenerateRequest,
        request: Request,
        user: sqlite3.Row = Depends(current_user),
    ) -> EventSourceResponse:
        user_id = int(user["id"])
        username = (request_body.chesscom_username or user["chesscom_username"] or "").strip()
        if not username:
            raise HTTPException(status_code=400, detail="No Chess.com username set.")
        if request_body.chesscom_username:
            # Persist a freshly supplied name (short-lived connection; the worker
            # opens its own for the long-running generation).
            setup = db.connect(app.state.db_path)
            try:
                setup.execute(
                    "UPDATE users SET chesscom_username = ? WHERE id = ?", (username, user_id)
                )
                setup.commit()
            finally:
                setup.close()

        since = chesscom.default_since(request_body.since_days)
        gen_fn = getattr(app.state, "mistakes_generate_fn", None)
        fetch_json = getattr(app.state, "fetch_json", None)
        maia_topk_fn = getattr(app.state, "maia_topk_fn", None)

        queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()

        def schedule(event: dict[str, str] | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def on_event(name: str, payload: dict[str, Any]) -> None:
            if cancelled.is_set():
                raise _Cancelled
            schedule({"event": name, "data": json.dumps(payload, separators=(",", ":"))})

        def worker() -> None:
            connection = db.connect(app.state.db_path)
            try:
                if gen_fn is not None:
                    gen_fn(
                        connection,
                        user_id=user_id, username=username, since=since,
                        on_event=on_event, is_cancelled=cancelled.is_set,
                    )
                else:
                    _default_generate(
                        connection,
                        user_id=user_id, username=username, since=since,
                        on_event=on_event, is_cancelled=cancelled.is_set,
                        fetch_json=fetch_json, maia_topk_fn=maia_topk_fn,
                    )
            except _Cancelled:
                logger.info("client disconnected; mistakes generation stopped")
            except Exception as exc:  # noqa: BLE001 — surface to the client
                logger.exception("mistakes generation crashed")
                schedule({
                    "event": "error",
                    "data": json.dumps({"message": repr(exc), "kind": "internal"}),
                })
            finally:
                connection.close()
                schedule(None)

        async def event_stream() -> AsyncIterator[dict[str, str]]:
            w: asyncio.Future[Any] = loop.run_in_executor(None, worker)
            disconnected = False
            try:
                while True:
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except TimeoutError:
                        continue
                    if event is None:
                        break
                    yield event
            finally:
                cancelled.set()
                if not disconnected:
                    try:
                        await w
                    except Exception:  # noqa: BLE001
                        logger.exception("mistakes worker crashed during shutdown")

        return EventSourceResponse(event_stream())

    return router
