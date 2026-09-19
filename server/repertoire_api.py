"""Opening repertoire API (``/api/repertoire/*``).

Engine-free by construction: everything served here is derived from games and
reviews already in the database (see ``server/repertoire.py``), so the routes
are plain reads plus one insert for a drill answer. There is no
``app.state`` seam because there is nothing to inject.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import chess
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.repertoire import RepertoireConstants, VERDICTS, fen_key
from server import repertoire
from server.deps import current_user, get_connection


class AttemptRequest(BaseModel):
    card_id: str = Field(min_length=1, max_length=200)
    fen: str = Field(min_length=10, max_length=120)
    expected_uci: str = Field(min_length=4, max_length=5)
    played_uci: str = Field(min_length=4, max_length=5)
    verdict: str | None = None


def build_repertoire_router(app: FastAPI) -> APIRouter:  # noqa: ARG001 — parity
    router = APIRouter(prefix="/api/repertoire")

    @router.get("")
    def get_repertoire(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        constants = RepertoireConstants.load()
        payload = repertoire.build_repertoire(
            connection, int(user["id"]), constants=constants
        )
        payload["constants"] = {
            "max_plies": constants.max_plies,
            "min_node_games": constants.min_node_games,
            "consistent_share": constants.consistent_share,
            "fix_delta_w": constants.fix_delta_w,
        }
        return payload

    @router.get("/drill")
    def get_drill(
        limit: int = 20,
        color: str | None = None,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        if color is not None and color not in repertoire.COLORS:
            raise HTTPException(status_code=422, detail=f"unknown colour: {color!r}")
        return repertoire.build_drill(
            connection,
            int(user["id"]),
            limit=max(1, min(100, int(limit))),
            color=color,
        )

    @router.post("/attempt")
    def post_attempt(
        request: AttemptRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            board = chess.Board(request.fen)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid FEN: {exc}") from exc
        played = _legal_move(board, request.played_uci)
        if played is None:
            raise HTTPException(
                status_code=422,
                detail=f"{request.played_uci} is not legal in that position",
            )
        expected = _legal_move(board, request.expected_uci)
        if expected is None:
            raise HTTPException(
                status_code=422,
                detail=f"{request.expected_uci} is not legal in that position",
            )
        verdict = request.verdict if request.verdict in VERDICTS else None
        correct = played == expected
        result = repertoire.record_attempt(
            connection,
            int(user["id"]),
            card_id=request.card_id,
            fen=request.fen,
            expected_uci=expected.uci(),
            played_uci=played.uci(),
            correct=correct,
            verdict=verdict,
        )
        result.update(
            {
                "expected_san": board.san(expected),
                "played_san": board.san(played),
                "position": fen_key(request.fen),
            }
        )
        return result

    return router


def _legal_move(board: chess.Board, uci: str) -> chess.Move | None:
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    return move if move in board.legal_moves else None
