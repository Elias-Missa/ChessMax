"""API routes for the four training modes (eval-hold, defense, guess, forced).

Built as a factory so the routes share ``app.state`` with the main app:

- ``app.state.analyze_fn``      — Stockfish analysis (tests inject fakes)
- ``app.state.reply_fn``        — shared engine-reply service
- ``app.state.guess_actuals_fn``— eval+sharpness computation for guess mode
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

import chess
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from server.deps import current_user, get_connection
from server.maia import normalize_maia_rating
from server.modes import (
    DEFAULT_DEFENSE_MOVES,
    DEFAULT_DEFENSE_THRESHOLD_CP,
    DEFAULT_HOLD_MOVES,
    DEFAULT_HOLD_THRESHOLD_CP,
    HoldMode,
    end_hold_session,
    forced_summary,
    guess_history,
    hold_summary,
    pick_forced_position,
    pick_random_position,
    play_hold_move,
    record_forced_attempt,
    score_guess,
    start_hold_session,
    validate_forced_line,
)


class HoldStartRequest(BaseModel):
    target_moves: int = Field(default=DEFAULT_HOLD_MOVES, ge=1, le=40)
    threshold_cp: int = Field(default=DEFAULT_HOLD_THRESHOLD_CP, ge=10, le=500)
    maia_rating: int = Field(default=1500, ge=1)


class HoldMoveRequest(BaseModel):
    move: str = Field(min_length=4, max_length=5)


class GuessSubmitRequest(BaseModel):
    position_id: int | None = None
    fen: str = Field(min_length=8)
    guessed_eval_cp: int = Field(ge=-2000, le=2000)
    guessed_sharpness: float = Field(ge=0, le=100)


class ForcedSubmitRequest(BaseModel):
    line: list[str] = Field(min_length=1, max_length=40)


def build_modes_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api")

    # ------------------------------------------------------------------ #
    # Eval-hold + defense gym (shared hold-session machinery)            #
    # ------------------------------------------------------------------ #

    def _start_hold(
        mode: HoldMode,
        request: HoldStartRequest,
        connection: sqlite3.Connection,
        user: sqlite3.Row,
    ) -> dict[str, object]:
        try:
            result = start_hold_session(
                connection,
                user_id=int(user["id"]),
                mode=mode,
                target_moves=request.target_moves,
                threshold_cp=request.threshold_cp,
                maia_rating=normalize_maia_rating(request.maia_rating),
                analyzer=app.state.analyze_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return asdict(result)

    def _play_hold(
        session_id: int,
        request: HoldMoveRequest,
        connection: sqlite3.Connection,
        user: sqlite3.Row,
    ) -> dict[str, object]:
        try:
            result = play_hold_move(
                connection,
                session_id=session_id,
                user_id=int(user["id"]),
                move_uci=request.move,
                analyzer=app.state.analyze_fn,
                reply_fn=app.state.reply_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(result)

    def _end_hold(
        session_id: int, connection: sqlite3.Connection, user: sqlite3.Row
    ) -> dict[str, object]:
        try:
            return end_hold_session(
                connection, session_id=session_id, user_id=int(user["id"])
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/evalhold/start")
    def evalhold_start(
        request: HoldStartRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _start_hold("evalhold", request, connection, user)

    @router.post("/evalhold/{session_id}/move")
    def evalhold_move(
        session_id: int,
        request: HoldMoveRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _play_hold(session_id, request, connection, user)

    @router.post("/evalhold/{session_id}/end")
    def evalhold_end(
        session_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _end_hold(session_id, connection, user)

    @router.get("/evalhold/summary")
    def evalhold_summary(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return hold_summary(connection, int(user["id"]), "evalhold")

    @router.post("/defense/start")
    def defense_start(
        request: HoldStartRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _start_hold("defense", request, connection, user)

    @router.post("/defense/{session_id}/move")
    def defense_move(
        session_id: int,
        request: HoldMoveRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _play_hold(session_id, request, connection, user)

    @router.post("/defense/{session_id}/end")
    def defense_end(
        session_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _end_hold(session_id, connection, user)

    @router.get("/defense/summary")
    def defense_summary(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return hold_summary(connection, int(user["id"]), "defense")

    # ------------------------------------------------------------------ #
    # Eval + sharpness guessing                                          #
    # ------------------------------------------------------------------ #

    @router.get("/guess/next")
    def guess_next(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        position = pick_random_position(connection)
        if position is None:
            raise HTTPException(status_code=404, detail="No positions available")
        return {
            "position_id": int(position["id"]),
            "fen": str(position["fen"]),
            "side_to_move": str(position["side_to_move"]),
        }

    @router.post("/guess/submit")
    def guess_submit(
        request: GuessSubmitRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        try:
            chess.Board(request.fen)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid FEN: {exc}") from exc
        try:
            return score_guess(
                connection,
                user_id=int(user["id"]),
                position_id=request.position_id,
                fen=request.fen,
                guessed_eval_cp=request.guessed_eval_cp,
                guessed_sharpness=request.guessed_sharpness,
                actuals_fn=app.state.guess_actuals_fn,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/guess/history")
    def guess_history_route(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return {"attempts": guess_history(connection, int(user["id"]))}

    # ------------------------------------------------------------------ #
    # Forced-depth puzzles                                               #
    # ------------------------------------------------------------------ #

    @router.get("/forced/next")
    def forced_next(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        position = pick_forced_position(connection)
        if position is None:
            raise HTTPException(status_code=404, detail="No tactical positions available")
        solution = (position["solution_moves"] or "").split()
        return {
            "position_id": int(position["id"]),
            "fen": str(position["fen"]),
            "side_to_move": str(position["side_to_move"]),
            "ply_count": len(solution),
            "rating": int(position["rating"]),
        }

    @router.post("/forced/{position_id}/submit")
    def forced_submit(
        position_id: int,
        request: ForcedSubmitRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        position = connection.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        if position is None:
            raise HTTPException(status_code=404, detail="Position not found")
        solution = (position["solution_moves"] or "").split()
        if not solution:
            raise HTTPException(status_code=400, detail="Position has no solution line")

        outcome = validate_forced_line(
            str(position["fen"]),
            request.line,
            solution,
            analyzer=app.state.analyze_fn,
        )
        record_forced_attempt(
            connection,
            user_id=int(user["id"]),
            position_id=position_id,
            user_line=request.line,
            outcome=outcome,
        )
        return {
            "passed": outcome.passed,
            "matched_plies": outcome.matched_plies,
            "total_plies": outcome.total_plies,
            "verdicts": [asdict(v) for v in outcome.verdicts],
            "solution_san": outcome.solution_san,
            "solution_uci": outcome.solution_uci,
            "summary": forced_summary(connection, int(user["id"])),
        }

    @router.get("/forced/summary")
    def forced_summary_route(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return forced_summary(connection, int(user["id"]))

    return router
