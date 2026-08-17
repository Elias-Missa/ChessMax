"""FastAPI app for ChessMax: puzzle trainer (/api/*) + volatility bar (/analyze/*)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import chess
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server import db
from server.db import VALID_OPENINGS
from server.engine import analyze
from server.maia import (
    MAIA_RATINGS,
    best_move,
    maia_capabilities,
    maia_top_moves,
    normalize_maia_rating,
)
from server.playout import (
    end_playout,
    list_recent_playouts,
    play_user_move,
    start_playout,
    takeback_playout,
)
from server.grading import evaluate_attempt, rating_update
from server.modes import default_guess_actuals
from server.modes_api import build_modes_router
from server.mistakes_api import build_mistakes_router
from server.auth_api import build_auth_router
from server.vol_games_api import build_vol_games_router
from server.guess_elo_api import build_guess_elo_router
from server.guess_eval_api import build_guess_eval_router
from server.insights_api import build_insights_router
from server.reviews_api import build_reviews_router
from server.deps import current_user, get_connection
from server.replies import engine_reply
from server.selection import select_next_position
from server.stats import load_stats

from chess_vol.server import add_cors_middleware as add_vol_cors_middleware
from chess_vol.server import api_router as vol_api_router


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "node_modules"


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that asks the browser to revalidate every request.

    The app's own JS/CSS change during development; ``Cache-Control: no-cache``
    forces a conditional request (304 when unchanged, 200 when edited) so a
    plain reload always reflects the latest files instead of a stale disk copy.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class AttemptRequest(BaseModel):
    move: str = Field(min_length=4, max_length=5)
    step: int = Field(default=0, ge=0)


class OpeningsRequest(BaseModel):
    openings: list[str] = Field(default_factory=list)


class PlayoutStartRequest(BaseModel):
    position_id: int = Field(ge=1)
    maia_rating: int = Field(ge=1)
    fen: str = Field(min_length=8)
    user_color: str = Field(pattern="^[wb]$")


class PlayoutMoveRequest(BaseModel):
    move: str = Field(min_length=4, max_length=5)


class VolMaiaMoveRequest(BaseModel):
    fen: str = Field(min_length=8)
    rating: int = Field(default=1500, ge=1, le=3500)


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="ChessMax")
    app.state.db_path = db_path
    app.state.analyze_fn = analyze
    app.state.playout_move_fn = best_move
    app.state.reply_fn = engine_reply
    app.state.guess_actuals_fn = default_guess_actuals
    app.state.maia_topk_fn = maia_top_moves

    # Accounts: register / login / logout / me (sets the session cookie).
    app.include_router(build_auth_router(app))

    # Training modes (eval-hold, defense gym, guess, forced lines).
    app.include_router(build_modes_router(app))

    # "Your Mistakes": personalized puzzles mined from the user's Chess.com games.
    app.include_router(build_mistakes_router(app))

    # Insights runs (chess.com window → shallow reviews → Tier 1 metrics).
    app.include_router(build_insights_router(app))

    # Async persisted Game Reviews.
    app.include_router(build_reviews_router(app))

    # Per-user saved analyzed games (vol Library).
    app.include_router(build_vol_games_router(app))

    # Guess the Elo Duels (matchmaking + guessing game).
    app.include_router(build_guess_elo_router())

    # Guess the Eval Duels (1-minute eval guessing game).
    app.include_router(build_guess_eval_router())

    # Volatility Bar API (/analyze/fen, /analyze/pgn SSE, /healthz) + its CORS policy.
    add_vol_cors_middleware(app)
    app.include_router(vol_api_router)

    if FRONTEND_DIR.exists():
        app.mount("/static", NoCacheStaticFiles(directory=FRONTEND_DIR), name="static")
    if VENDOR_DIR.exists():
        app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")

    def _spa_index() -> FileResponse:
        index_path = FRONTEND_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Frontend not found")
        return FileResponse(index_path, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def index() -> FileResponse:
        return _spa_index()

    # History-API deep links for the shell (Phase A). Keep ahead of API mounts
    # that already have their own prefixes; these paths never collide with /api
    # or /analyze.
    @app.get("/puzzles")
    @app.get("/training")
    @app.get("/training/{rest:path}")
    @app.get("/game-review")
    @app.get("/game-review/{rest:path}")
    @app.get("/insights")
    @app.get("/insights/{rest:path}")
    @app.get("/duels")
    @app.get("/duels/{rest:path}")
    # Pre-merge aliases: Guess the Elo and Guess the Eval now share the Duels
    # tab, but old links must keep resolving.
    @app.get("/guess-the-elo")
    @app.get("/guess-the-eval")
    def spa_routes(rest: str = "") -> FileResponse:  # noqa: ARG001
        return _spa_index()

    @app.get("/api/user")
    def get_user(
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return serialize_user(user)

    @app.get("/api/openings")
    def get_openings(
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return {
            "selected": db.parse_openings(user["selected_openings"]),
            "available": list(VALID_OPENINGS),
        }

    @app.put("/api/openings")
    def set_openings(
        request: OpeningsRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        invalid = [o for o in request.openings if o not in VALID_OPENINGS]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown opening(s): {', '.join(invalid)}",
            )
        serialized = db.serialize_openings(request.openings)
        connection.execute(
            "UPDATE users SET selected_openings = ? WHERE id = ?",
            (serialized, user["id"]),
        )
        connection.commit()
        return {"selected": db.parse_openings(serialized)}

    @app.get("/api/puzzle/next")
    def next_puzzle(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        position = select_next_position(
            connection,
            user["id"],
            user["rating"],
            selected_openings=db.parse_openings(user["selected_openings"]),
        )
        if position is None:
            raise HTTPException(status_code=404, detail="No positions available")
        return {
            "position_id": position["id"],
            "fen": position["fen"],
            "side_to_move": position["side_to_move"],
        }

    @app.post("/api/puzzle/{position_id}/attempt")
    def submit_attempt(
        position_id: int,
        request: AttemptRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        position = get_position_by_id(connection, position_id)
        if position is None:
            raise HTTPException(status_code=404, detail="Position not found")

        try:
            outcome = evaluate_attempt(
                dict(position),
                request.move,
                app.state.analyze_fn,
                step=request.step,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not outcome.is_terminal:
            # 'continue' — no rating delta, no attempts row, no position update.
            return {
                "status": outcome.status,
                "position_classification": position["classification"],
                "opponent_move": outcome.opponent_move_san,
                "opponent_move_uci": outcome.opponent_move_uci,
                "next_step": outcome.next_step,
                "solved": False,
                "user_rating_after": int(user["rating"]),
            }

        user_before = int(user["rating"])
        position_before = int(position["rating"])
        user_after, position_after = rating_update(
            user_before,
            position_before,
            outcome.actual_score,
        )

        connection.execute(
            "UPDATE users SET rating = ? WHERE id = ?",
            (user_after, user["id"]),
        )
        if outcome.best_move_uci is not None:
            connection.execute(
                "UPDATE positions SET rating = ?, best_move = ?, best_eval = ? WHERE id = ?",
                (position_after, outcome.best_move_uci, outcome.best_eval, position["id"]),
            )
        else:
            connection.execute(
                "UPDATE positions SET rating = ? WHERE id = ?",
                (position_after, position["id"]),
            )
        connection.execute(
            """
            INSERT INTO attempts (
                user_id,
                position_id,
                user_move,
                eval_loss,
                grade,
                user_rating_before,
                user_rating_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                position["id"],
                request.move,
                outcome.eval_loss,
                outcome.grade,
                user_before,
                user_after,
            ),
        )
        connection.commit()

        return {
            "status": outcome.status,
            "grade": outcome.grade,
            "eval_loss": outcome.eval_loss,
            "best_move": outcome.best_move,
            "solution_line": outcome.solution_line,
            "solved": outcome.solved,
            "user_rating_after": user_after,
            "position_classification": position["classification"],
            "position_rating": position_before,
            "opening_tag": position["opening_tag"],
            "can_play_out": outcome.can_play_out,
            "opponent_move": outcome.opponent_move_san,
            "opponent_move_uci": outcome.opponent_move_uci,
            "top_lines": outcome.top_lines,
            "refutation": outcome.refutation_san,
            "refutation_uci": outcome.refutation_uci,
        }

    @app.get("/api/playout/capabilities")
    def get_playout_capabilities() -> dict[str, object]:
        return maia_capabilities()

    @app.post("/api/playout/start")
    def start_playout_route(
        request: PlayoutStartRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        position = get_position_by_id(connection, request.position_id)
        if position is None:
            raise HTTPException(status_code=404, detail="Position not found")
        try:
            result = start_playout(
                connection,
                user_id=int(user["id"]),
                position_id=request.position_id,
                fen=request.fen,
                maia_rating=normalize_maia_rating(request.maia_rating),
                user_color=request.user_color,
                move_selector=app.state.playout_move_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "playout_id": result.playout_id,
            "fen": result.fen,
            "status": result.status,
            "maia_move": result.maia_move,
            "engine": result.engine,
            "maia_rating": result.maia_rating,
            "initial_fen": result.initial_fen,
            "move_list": result.move_list,
            "supported_maia_ratings": list(MAIA_RATINGS),
        }

    @app.post("/api/playout/{playout_id}/move")
    def playout_move_route(
        playout_id: int,
        request: PlayoutMoveRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        try:
            result = play_user_move(
                connection,
                playout_id=playout_id,
                user_id=int(user["id"]),
                move_uci=request.move,
                move_selector=app.state.playout_move_fn,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            # Engine hiccup; the playout is unchanged and the move can be retried.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "maia_move": result.maia_move,
            "fen": result.fen,
            "status": result.status,
            "result": result.result,
            "engine": result.engine,
            "initial_fen": result.initial_fen,
            "move_list": result.move_list,
        }

    @app.post("/api/playout/{playout_id}/takeback")
    def playout_takeback_route(
        playout_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        try:
            result = takeback_playout(
                connection,
                playout_id=playout_id,
                user_id=int(user["id"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "fen": result.fen,
            "status": result.status,
            "engine": result.engine,
            "undone_plies": result.undone_plies,
            "initial_fen": result.initial_fen,
            "move_list": result.move_list,
        }

    @app.post("/api/playout/{playout_id}/end")
    def playout_end_route(
        playout_id: int,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        try:
            result = end_playout(
                connection,
                playout_id=playout_id,
                user_id=int(user["id"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "final_pgn": result.final_pgn,
            "result": result.result,
            "engine": result.engine,
        }

    @app.get("/api/playout/recent")
    def recent_playouts_route(
        limit: int = 20,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        clamped = max(1, min(100, int(limit)))
        return {"playouts": list_recent_playouts(connection, user_id=int(user["id"]), limit=clamped)}

    @app.post("/api/vol/play/maia-move")
    def vol_maia_move(request: VolMaiaMoveRequest) -> dict[str, object]:
        """Play one Maia reply from an arbitrary FEN (Game Review 2.0 Phase 5).

        Stateless and unauthenticated (the review lives on the vol tab): the
        client owns the game state and asks for the opponent's move per ply. Uses
        the same ``playout_move_fn`` seam as the trainer playouts (Maia at the
        requested rating, Stockfish fallback), so tests inject a fake.
        """
        try:
            board = chess.Board(request.fen.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid FEN: {exc}") from exc
        if board.is_game_over(claim_draw=True):
            return {
                "move": None,
                "san": None,
                "engine": None,
                "fen": board.fen(),
                "game_over": True,
                "result": board.result(claim_draw=True),
            }
        rating = normalize_maia_rating(request.rating)
        move_uci, engine = app.state.playout_move_fn(board.fen(), rating)
        if move_uci is None:
            raise HTTPException(status_code=503, detail="engine returned no move")
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"engine returned bad move: {move_uci!r}") from exc
        if move not in board.legal_moves:
            raise HTTPException(status_code=502, detail=f"engine returned illegal move: {move_uci}")
        san = board.san(move)
        board.push(move)
        over = board.is_game_over(claim_draw=True)
        return {
            "move": move_uci,
            "san": san,
            "engine": engine,
            "fen": board.fen(),
            "game_over": over,
            "result": board.result(claim_draw=True) if over else None,
        }

    @app.get("/api/stats")
    def stats_route(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        payload = load_stats(connection, int(user["id"]))
        payload["user_rating"] = int(user["rating"])
        return payload

    return app


def get_position_by_id(
    connection: sqlite3.Connection,
    position_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM positions WHERE id = ?",
        (position_id,),
    ).fetchone()


def serialize_user(user: sqlite3.Row) -> dict[str, object]:
    return {
        "rating": user["rating"],
        "selected_openings": db.parse_openings(user["selected_openings"]),
    }


app = create_app()
