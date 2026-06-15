"""FastAPI routes for the Chess Volatility Bar (README §8).

Endpoints (exposed on :data:`api_router`, mounted into the combined ChessMax
app by ``server.main`` and into the standalone :func:`create_app`):

* ``POST /analyze/fen`` — JSON in, :class:`FenReportJson` JSON out.
* ``POST /analyze/pgn`` — JSON in, Server-Sent Events stream of per-ply results.
* ``GET  /healthz`` — liveness probe.

Static frontend serving lives in ``server.main`` (the combined ChessMax app);
this module is API-only.

Engine lifecycle: one Stockfish process per POST, via the module-level
:data:`ENGINE_FACTORY` (mirrors :mod:`chess_vol.cli`). Tests monkey-patch that
factory to inject a :class:`FakeEngine`, so nothing in this module talks to
Stockfish directly.

The JSON schema is shared with the CLI via :mod:`chess_vol.cli_report` — that
module is the single source of truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, TypedDict

import chess
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from chess_vol.analyze import PlyResult, analyze_pgn
from chess_vol.cli_report import (
    FenReportJson,
    ParamsJson,
    build_fen_report,
    build_params,
    mode_label,
    ply_to_json,
)
from chess_vol.config import (
    DEFAULT_CHILD_DEPTH,
    DEFAULT_DEPTH,
    DEFAULT_MULTIPV,
    DEFAULT_RECURSE_ALPHA,
    DEFAULT_RECURSE_K,
)
from chess_vol.engine import Engine, EngineNotFoundError
from chess_vol.volatility import EngineLike, compute_volatility

logger = logging.getLogger("chess_vol.server")


# --------------------------------------------------------------------------- #
# Engine factory — overridable in tests                                       #
# --------------------------------------------------------------------------- #


EngineFactory = Callable[[], AbstractContextManager[EngineLike]]


@contextmanager
def _default_engine_factory() -> Iterator[EngineLike]:
    """Open a real Stockfish process as a context manager."""
    with Engine() as engine:
        yield engine


#: Tests monkey-patch this to inject a :class:`FakeEngine` instead of Stockfish.
ENGINE_FACTORY: EngineFactory = _default_engine_factory


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #


class _CommonOptions(BaseModel):
    """Shared analysis knobs. All optional; defaults come from ``config.py``."""

    depth: int = Field(default=DEFAULT_DEPTH, ge=1, le=40)
    multipv: int = Field(default=DEFAULT_MULTIPV, ge=1, le=12)
    deep: bool = False
    recurse_depth: int | None = Field(default=None, ge=0, le=3)
    recurse_k: int = Field(default=DEFAULT_RECURSE_K, ge=1, le=6)
    child_depth: int = Field(default=DEFAULT_CHILD_DEPTH, ge=1, le=40)


class AnalyzeFenRequest(_CommonOptions):
    fen: str = Field(..., description="FEN of the position to analyse.")


class AnalyzePgnRequest(_CommonOptions):
    pgn: str = Field(..., description="PGN text; only the first game is analysed.")
    max_plies: int | None = Field(default=None, ge=1, le=1000)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _resolve_recurse_depth(deep: bool, recurse_depth: int | None) -> int:
    """Same rule as the CLI: ``--deep`` is shorthand for ``recurse_depth=2``.

    Raises :class:`HTTPException` ``400`` if the two arguments contradict.
    """
    if deep and recurse_depth is not None and recurse_depth == 0:
        raise HTTPException(
            status_code=400,
            detail="`deep=true` conflicts with `recurse_depth=0`; drop one.",
        )
    if recurse_depth is not None:
        return recurse_depth
    return 2 if deep else 0


def _build_params(opts: _CommonOptions, effective_recurse: int, *, max_plies: int | None = None) -> ParamsJson:
    return build_params(
        depth=opts.depth,
        multipv=opts.multipv,
        recurse_depth=effective_recurse,
        recurse_k=opts.recurse_k,
        recurse_alpha=DEFAULT_RECURSE_ALPHA,
        child_depth=opts.child_depth,
        max_plies=max_plies,
    )


class _SseEvent(TypedDict):
    """Payload shape we push through ``EventSourceResponse``."""

    event: str
    data: str


def _sse_event(event: str, payload: dict[str, Any]) -> _SseEvent:
    return {"event": event, "data": json.dumps(payload, separators=(",", ":"))}


# --------------------------------------------------------------------------- #
# API router + app factory                                                    #
# --------------------------------------------------------------------------- #


api_router = APIRouter()


def add_cors_middleware(app: FastAPI) -> None:
    """Attach the vol CORS policy (localhost + chess.com) to ``app``."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
            "https://www.chess.com",
            "https://chess.com",
        ],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


@api_router.post("/analyze/fen", response_model=None)
def analyze_fen_endpoint(req: AnalyzeFenRequest) -> JSONResponse:
    effective_recurse = _resolve_recurse_depth(req.deep, req.recurse_depth)

    try:
        board = chess.Board(req.fen.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid FEN: {exc}") from exc

    try:
        with ENGINE_FACTORY() as engine:
            result = compute_volatility(
                board,
                engine,
                depth=req.depth,
                multipv=req.multipv,
                recurse_depth=effective_recurse,
                recurse_k=req.recurse_k,
                child_depth=req.child_depth,
            )
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    params = _build_params(req, effective_recurse)
    report: FenReportJson = build_fen_report(board.fen(), result, params=params)
    return JSONResponse(content=dict(report))


@api_router.post("/analyze/pgn")
async def analyze_pgn_endpoint(req: AnalyzePgnRequest, request: Request) -> EventSourceResponse:
    effective_recurse = _resolve_recurse_depth(req.deep, req.recurse_depth)
    params = _build_params(req, effective_recurse, max_plies=req.max_plies)
    mode = mode_label(effective_recurse)

    queue: asyncio.Queue[_SseEvent | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    cancelled = threading.Event()

    def schedule(event: _SseEvent | None) -> None:
        """Thread-safe push into the asyncio queue."""
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def on_progress(done: int, total: int, ply: PlyResult) -> None:
        if cancelled.is_set():
            raise _ClientDisconnectedError
        payload: dict[str, Any] = {
            "done": done,
            "total": total,
            "ply": dict(ply_to_json(ply)),
        }
        schedule(_sse_event("ply", payload))

    def run_analysis() -> None:
        try:
            schedule(
                _sse_event(
                    "start",
                    {"mode": mode, "params": dict(params)},
                )
            )
            with ENGINE_FACTORY() as engine:
                results = analyze_pgn(
                    req.pgn,
                    engine,
                    max_plies=req.max_plies,
                    progress=on_progress,
                    depth=req.depth,
                    multipv=req.multipv,
                    recurse_depth=effective_recurse,
                    recurse_k=req.recurse_k,
                    child_depth=req.child_depth,
                )
            total_analyses = sum(r.volatility.analyses for r in results)
            schedule(
                _sse_event(
                    "done",
                    {
                        "plies_analysed": len(results),
                        "total_analyses": total_analyses,
                        "mode": mode,
                        "plies": [dict(ply_to_json(ply)) for ply in results],
                    },
                )
            )
        except _ClientDisconnectedError:
            logger.info("client disconnected mid-analysis; stopping")
        except EngineNotFoundError as exc:
            schedule(_sse_event("error", {"message": str(exc), "kind": "engine_not_found"}))
        except ValueError as exc:
            schedule(_sse_event("error", {"message": str(exc), "kind": "bad_input"}))
        except Exception as exc:
            logger.exception("analysis crashed")
            schedule(_sse_event("error", {"message": repr(exc), "kind": "internal"}))
        finally:
            schedule(None)

    async def event_stream() -> AsyncIterator[_SseEvent]:
        worker: asyncio.Future[None] = loop.run_in_executor(None, run_analysis)
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
            # On a graceful close we drain the worker; on disconnect we let
            # it finish in the background (Stockfish is uninterruptible mid-
            # analysis, so awaiting would block shutdown).
            if not disconnected:
                try:
                    await worker
                except Exception:
                    logger.exception("analysis worker crashed during shutdown")

    return EventSourceResponse(event_stream())


@api_router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    """Build a standalone API-only FastAPI application (no static frontend).

    The combined ChessMax app (``server.main``) includes :data:`api_router`
    directly and owns frontend serving; this factory remains for the vol CLI
    (``chess-vol serve``) and the vol test suite.
    """
    app = FastAPI(
        title="Chess Volatility Bar",
        version="0.1.0",
        description="Local volatility-analysis server. See README §8.",
    )
    add_cors_middleware(app)
    app.include_router(api_router)
    return app


class _ClientDisconnectedError(Exception):
    """Raised inside the analysis worker thread when the SSE client went away."""


app = create_app()


__all__ = [
    "ENGINE_FACTORY",
    "AnalyzeFenRequest",
    "AnalyzePgnRequest",
    "add_cors_middleware",
    "api_router",
    "app",
    "create_app",
]
