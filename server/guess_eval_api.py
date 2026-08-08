"""Routes for Guess the Eval Duels (mounted under ``/api`` by ``create_app``).

Thin HTTP layer over :mod:`server.guess_eval`: matchmaking, eval guess submission,
and duel polling.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import guess_eval
from server.deps import current_user, get_connection


class EvalGuessRequest(BaseModel):
    duel_id: int
    guess: int = Field(ge=guess_eval.EVAL_MIN - 500, le=guess_eval.EVAL_MAX + 500)


def build_guess_eval_router() -> APIRouter:
    router = APIRouter(prefix="/api/eval")

    @router.post("/match")
    def match(
        user: sqlite3.Row = Depends(current_user),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        guess_eval.ensure_eval_positions_pool(connection)
        if guess_eval.pool_size(connection) == 0:
            raise HTTPException(
                status_code=503,
                detail="No eval positions available.",
            )
        status, duel = guess_eval.find_or_create_match(connection, int(user["id"]))
        if status == "matched" and duel is not None:
            return {"status": "matched", "duel": guess_eval.duel_public(connection, duel, int(user["id"]))}
        return {"status": "searching"}

    @router.post("/leave")
    def leave(user: sqlite3.Row = Depends(current_user)) -> dict[str, object]:
        guess_eval.leave_queue(int(user["id"]))
        return {"status": "left"}

    @router.get("/duel/{duel_id}")
    def duel_state(
        duel_id: int,
        user: sqlite3.Row = Depends(current_user),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        duel = guess_eval.get_duel(connection, duel_id)
        if duel is None or guess_eval.side_of(duel, int(user["id"])) is None:
            raise HTTPException(status_code=404, detail="Duel not found")
        duel = guess_eval.maybe_resolve(connection, duel, time.time())
        return guess_eval.duel_public(connection, duel, int(user["id"]))

    @router.post("/guess")
    def guess(
        body: EvalGuessRequest,
        user: sqlite3.Row = Depends(current_user),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        duel = guess_eval.get_duel(connection, body.duel_id)
        if duel is None:
            raise HTTPException(status_code=404, detail="Duel not found")
        side = guess_eval.side_of(duel, int(user["id"]))
        if side is None:
            raise HTTPException(status_code=403, detail="Not your duel")
        now = time.time()
        if duel["status"] != "active" or now >= int(duel["deadline_ts"]):
            duel = guess_eval.maybe_resolve(connection, duel, now)
            return guess_eval.duel_public(connection, duel, int(user["id"]))
        existing = duel["guess_a"] if side == "a" else duel["guess_b"]
        if existing is not None:
            raise HTTPException(status_code=409, detail="You already guessed")
        duel = guess_eval.submit_guess(connection, duel, side, body.guess, now)
        return guess_eval.duel_public(connection, duel, int(user["id"]))

    @router.get("/stats")
    def stats(
        user: sqlite3.Row = Depends(current_user),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        uid = int(user["id"])
        rows = connection.execute(
            """
            SELECT player_a, player_b, winner FROM eval_duels
            WHERE status = 'done' AND (player_a = ? OR player_b = ?)
            """,
            (uid, uid),
        ).fetchall()
        wins = losses = draws = 0
        for row in rows:
            side = "a" if int(row["player_a"]) == uid else "b"
            if row["winner"] == "draw":
                draws += 1
            elif row["winner"] == side:
                wins += 1
            else:
                losses += 1
        return {"wins": wins, "losses": losses, "draws": draws, "played": len(rows)}

    return router


__all__ = ["build_guess_eval_router"]
