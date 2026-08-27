"""Dev-tab piece-value lab (``POST /api/dev/piece-values``).

The first engine-dependent route under ``/api/*``. Everything else in the dev
API reads stored rows; this one runs ~30 searches per request, so it is
deliberately synchronous and internal — a lab bench, not a product surface.

Engine access goes through :data:`ENGINE_FACTORY`, mirroring
``chess_vol.server.ENGINE_FACTORY``, so the tests stay engine-free.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import chess
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.piece_values import PieceValueConstants, compute_piece_values
from server.deps import current_user

logger = logging.getLogger("server.piecevalues")

EngineFactory = Callable[[], AbstractContextManager[Any]]


@contextmanager
def _default_engine_factory() -> Iterator[Any]:
    from chess_vol.engine import Engine

    with Engine() as engine:
        yield engine


#: Tests monkey-patch this to inject a scripted engine instead of Stockfish.
ENGINE_FACTORY: EngineFactory = _default_engine_factory

# The shipped probe depth is 16 and it is load-bearing (see the JSON's
# _probe_depth_comment). The floor here is lower only so the lab can trade
# accuracy for interactivity while exploring; the ceiling keeps one request
# from pinning a core for minutes.
MIN_DEPTH = 6
MAX_DEPTH = 22
MAX_RELOCATE = 4


class PieceValuesRequest(BaseModel):
    fen: str = Field(min_length=10, max_length=120)
    depth: int | None = Field(default=None, ge=MIN_DEPTH, le=MAX_DEPTH)
    relocate_top_k: int = Field(default=0, ge=0, le=MAX_RELOCATE)


def build_piece_values_router(app: FastAPI) -> APIRouter:  # noqa: ARG001
    router = APIRouter(prefix="/api/dev")

    @router.post("/piece-values")
    def piece_values(
        body: PieceValuesRequest,
        user: object = Depends(current_user),  # noqa: ARG001 — auth gate only
    ) -> dict[str, Any]:
        try:
            board = chess.Board(body.fen)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Unparseable FEN: {exc}") from exc
        if not board.is_valid():
            raise HTTPException(
                status_code=400,
                detail=f"Position is not legal ({board.status()!r})",
            )

        constants = PieceValueConstants.load()
        started = time.time()
        try:
            with ENGINE_FACTORY() as engine:
                result = compute_piece_values(
                    board,
                    engine,
                    depth=body.depth,
                    relocate_top_k=body.relocate_top_k,
                    constants=constants,
                )
        except Exception as exc:  # noqa: BLE001
            # EngineNotFoundError is the expected failure on a machine with no
            # Stockfish, and it is a 503 rather than a 500: the request was
            # fine, the box just cannot answer it.
            if type(exc).__name__ == "EngineNotFoundError":
                raise HTTPException(
                    status_code=503,
                    detail="Stockfish is not installed. Set STOCKFISH_PATH or put "
                    "`stockfish` on PATH — piece values are measured by running "
                    "the engine once per piece.",
                ) from exc
            logger.exception("piece-value computation failed")
            raise HTTPException(status_code=500, detail=repr(exc)) from exc

        payload = result.as_dict()
        payload["elapsed_ms"] = int((time.time() - started) * 1000)
        payload["shrinkage"] = constants.shrinkage
        return payload

    return router
