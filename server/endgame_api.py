"""Endgame Arena API (``/api/endgame/*``).

Engine access goes through ``app.state`` like the rest of the trainer, so the
whole mode is testable without lc0 or Stockfish:

* ``app.state.playout_move_fn`` — Maia's reply (shared with Play Out).
* ``app.state.analyze_fn`` — Stockfish, used for the draw verdict and the hint.

Both are optional at runtime. A missing Stockfish makes the draw offer lenient
(``draw_offer_verdict`` accepts on an unknown evaluation rather than trapping
someone in a position they hold) and turns the hint off; a missing Maia falls
back to a legal move. Neither breaks the board.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Literal

import chess
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.endgame import BUCKETS, EndgameConstants
from server import endgame
from server.deps import current_user, get_connection

logger = logging.getLogger("server.endgame")

DEFAULT_USER_RATING = 1500


class StartRequest(BaseModel):
    bucket: Literal["drawn", "winning", "losing"] | None = None


class MoveRequest(BaseModel):
    move: str = Field(min_length=4, max_length=5)


class HintRequest(BaseModel):
    fen: str = Field(min_length=10, max_length=120)
    maia_rating: int = Field(default=1500, ge=100, le=3500)


def build_endgame_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api/endgame")

    def _move_selector(fen: str, maia_rating: int) -> str | None:
        fn = getattr(app.state, "playout_move_fn", None)
        if fn is None:
            return None
        return fn(fen, maia_rating)

    def _eval_fn(fen: str) -> int | None:
        """White-POV centipawns for ``fen``, or ``None`` without an engine."""

        analyze = getattr(app.state, "analyze_fn", None)
        if analyze is None:
            return None
        try:
            data = analyze(fen)
        except Exception:  # noqa: BLE001 — a dead engine is not a broken arena
            return None
        moves = (data or {}).get("top_moves") or []
        if not moves or moves[0].get("eval") is None:
            return None
        # `analyze` reports side-to-move POV; the arena reasons in White POV.
        stm_cp = int(moves[0]["eval"])
        try:
            turn_white = chess.Board(fen).turn == chess.WHITE
        except ValueError:
            return None
        return stm_cp if turn_white else -stm_cp

    def _rating_of(user: sqlite3.Row) -> int:
        try:
            return int(user["rating"])
        except (KeyError, TypeError, ValueError):
            return DEFAULT_USER_RATING

    @router.post("/start")
    def start(
        body: StartRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        state = endgame.start_session(
            connection,
            user_id=int(user["id"]),
            user_rating=_rating_of(user),
            bucket=body.bucket,
            move_selector=_move_selector,
        )
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No endgame positions available yet. Review a game or import "
                    "the Lichess puzzle set, and the arena will mine from both."
                ),
            )
        return state.as_dict()

    @router.get("/active")
    def active(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        state = endgame.active_session(connection, int(user["id"]))
        return {"session": state.as_dict() if state else None}

    @router.post("/{session_id}/move")
    def move(
        session_id: int,
        body: MoveRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            state = endgame.play_move(
                connection,
                session_id=session_id,
                user_id=int(user["id"]),
                move_uci=body.move,
                move_selector=_move_selector,
                eval_fn=_eval_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return state.as_dict()

    @router.post("/{session_id}/takeback")
    def takeback(
        session_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            state = endgame.takeback(
                connection, session_id=session_id, user_id=int(user["id"])
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return state.as_dict()

    @router.post("/{session_id}/draw")
    def draw(
        session_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            state = endgame.offer_draw(
                connection,
                session_id=session_id,
                user_id=int(user["id"]),
                eval_fn=_eval_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return state.as_dict()

    @router.post("/{session_id}/resign")
    def resign(
        session_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            state = endgame.resign(
                connection,
                session_id=session_id,
                user_id=int(user["id"]),
                eval_fn=_eval_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return state.as_dict()

    @router.post("/hint")
    def hint(
        body: HintRequest,
        user: sqlite3.Row = Depends(current_user),  # noqa: ARG001 — auth gate
    ) -> dict[str, Any]:
        """The two live-analysis arrows.

        ``best`` is Stockfish's move (drawn green in the arena) and ``human`` is
        what Maia at this level plays (pink). They are returned together because
        the interesting case is when they differ — that gap is the lesson.
        Either may be ``null`` if its engine is absent; the toggle degrades to
        whichever half is available rather than erroring.
        """

        try:
            board = chess.Board(body.fen)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Unparseable FEN: {exc}") from exc
        if not board.is_valid():
            raise HTTPException(status_code=400, detail="Position is not legal")

        best: dict[str, Any] | None = None
        analyze = getattr(app.state, "analyze_fn", None)
        if analyze is not None:
            try:
                data = analyze(body.fen) or {}
                top = (data.get("top_moves") or [])[:1]
                if top and top[0].get("move"):
                    best = {"uci": top[0]["move"], "eval_cp": top[0].get("eval")}
            except Exception:  # noqa: BLE001
                logger.exception("arena hint: engine analysis failed")

        human: dict[str, Any] | None = None
        topk = getattr(app.state, "maia_topk_fn", None)
        if topk is not None:
            try:
                picks = topk(body.fen, body.maia_rating, 1)
                if picks:
                    human = {"uci": picks[0], "rating": body.maia_rating}
            except Exception:  # noqa: BLE001
                logger.exception("arena hint: maia lookup failed")

        return {"best": best, "human": human}

    @router.get("/summary")
    def summary(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        user_id = int(user["id"])
        payload = endgame.summary(connection, user_id)
        payload["recent"] = endgame.recent_results(connection, user_id)
        payload["buckets_order"] = list(BUCKETS)
        payload["ladder"] = EndgameConstants.load().ladder_steps
        return payload

    @router.get("/{session_id}/pgn")
    def pgn(
        session_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        """The finished game as PGN, for hand-off to the full Game Review."""

        row = connection.execute(
            "SELECT pgn, bucket, outcome, result FROM endgame_results "
            "WHERE session_id = ? AND user_id = ?",
            (session_id, int(user["id"])),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No finished game here yet")
        return dict(row)

    return router
