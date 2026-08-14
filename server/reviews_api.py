"""Async Game Review persistence API (POST/GET /api/review*)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.findability import FindabilityConstants, band_for
from server import db
from server import game_identity
from server.deps import current_user, get_connection
from server.findability_features import ensure_fresh_findability, score_from_payload
from server.reviews import (
    analyze_and_store,
    create_pending_review,
    find_review,
    get_review,
    list_reviews,
    mark_review_error,
    upsert_game,
)

logger = logging.getLogger("server.reviews")


def review_engine_count() -> int:
    """How many Stockfish processes a single review may walk the game on.

    Defaults to half the cores, capped at 6 so a review never starves the rest
    of the app. Every engine is single-threaded and each position is analysed
    from a clean transposition table, so the pool size changes wall-clock time
    and nothing else. ``CHESS_REVIEW_ENGINES=1`` walks the game serially.
    """
    raw = os.environ.get("CHESS_REVIEW_ENGINES")
    if raw:
        try:
            return max(1, min(16, int(raw)))
        except ValueError:
            pass
    return max(1, min(6, (os.cpu_count() or 2) // 2))


def _findability_detail(
    detail: Any,
    score: int,
    constants: FindabilityConstants,
    user_rating: int | None = None,
) -> dict[str, Any]:
    """Rebuild band / curve / alternate for one stored move.

    The band always comes back (it is a pure function of the score). The curve
    and the alternate move need the stored feature vector, which only full-tier
    reviews carry — shallow rows get the band alone.
    """
    out: dict[str, Any] = {"band": band_for(score, constants)}
    payload = detail.get("findability_features") if isinstance(detail, dict) else None
    if not isinstance(payload, dict) or not payload.get("moves"):
        return out
    try:
        result = score_from_payload(payload, constants, user_rating=user_rating)
    except Exception:  # noqa: BLE001 — display extra, never fail the read
        logger.exception("findability detail rebuild failed")
        return out
    if result is None:
        return out
    out["band"] = result.band
    out["curve"] = [[int(r), float(v)] for r, v in result.curve]
    out["forced"] = bool(result.forced)
    if result.alternate is not None:
        out["alternate"] = {
            "uci": result.alternate.uci,
            "san": result.alternate.san,
            "delta_w": float(result.alternate.delta_w),
            "pi": float(result.alternate.pi),
        }
    return out


class ReviewStartRequest(BaseModel):
    pgn: str = Field(min_length=10)
    source: Literal["chesscom", "lichess", "pgn"] = "pgn"
    user_color: Literal["white", "black"] = "white"
    user_rating: int | None = Field(default=None, ge=100, le=3500)
    depth_tier: Literal["shallow", "full"] = "full"
    game_url: str | None = None
    meta: dict[str, Any] | None = None


def build_reviews_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api")
    jobs_lock = threading.Lock()
    active_reviews: set[str] = set()

    def _run_job(review_id: str, pgn: str, user_color: str, depth_tier: str, user_rating: int | None) -> None:
        connection = db.connect(app.state.db_path)
        try:
            analyze_fn = getattr(app.state, "review_analyze_fn", None)
            if analyze_fn is not None:
                analyze_fn(
                    connection,
                    review_id=review_id,
                    pgn=pgn,
                    user_color=user_color,
                    depth_tier=depth_tier,
                    user_rating=user_rating,
                )
                return

            from chess_vol.engine import Engine
            from chess_vol.findability_review import attach_findability
            from core.human import best_available_policy

            # Every ply is an independent search, so the game is walked on a
            # small pool of single-threaded engines rather than one. Fixed-depth
            # MultiPV barely benefits from Stockfish's own SMP (more threads
            # searches more nodes for the same depth), but N processes on N
            # positions scales almost linearly and leaves each position's
            # numbers untouched.
            with contextlib.ExitStack() as stack:
                pool = [stack.enter_context(Engine()) for _ in range(review_engine_count())]
                policy = best_available_policy()
                attach_fn = None
                if depth_tier == "full" and policy is not None:
                    def attach_fn(plies: Any, user_rating: int | None = None) -> None:  # noqa: ANN401
                        attach_findability(plies, policy, user_rating=user_rating)

                analyze_and_store(
                    connection,
                    review_id=review_id,
                    pgn=pgn,
                    user_color=user_color,
                    depth_tier=depth_tier,
                    engine=pool[0],
                    engines=pool if len(pool) > 1 else None,
                    attach_findability_fn=attach_fn,
                    policy_fn=policy,
                    user_rating=user_rating,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("review job failed")
            mark_review_error(connection, review_id, repr(exc))
        finally:
            connection.close()
            with jobs_lock:
                active_reviews.discard(review_id)

    def _ensure_job(
        review_id: str,
        *,
        pgn: str,
        user_color: str,
        depth_tier: str,
        user_rating: int | None,
    ) -> bool:
        """Start a worker if this review isn't already running in-process.

        Recovers jobs orphaned by a server restart (row still pending/running
        in SQLite but no live thread).
        """

        with jobs_lock:
            if review_id in active_reviews:
                return False
            active_reviews.add(review_id)
        threading.Thread(
            target=_run_job,
            args=(review_id, pgn, user_color, depth_tier, user_rating),
            daemon=True,
        ).start()
        return True

    @router.post("/review")
    def start_review(
        body: ReviewStartRequest,
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        meta = dict(body.meta or {})
        if body.game_url:
            meta.setdefault("url", body.game_url)
        game_id = game_identity.resolve_game_id(
            source=body.source,
            pgn=body.pgn,
            meta=meta,
            url=body.game_url,
        )
        connection = db.connect(app.state.db_path)
        try:
            upsert_game(
                connection,
                game_id=game_id,
                source=body.source,
                pgn=body.pgn,
                meta=meta,
            )
            existing = find_review(
                connection,
                user_id=int(user["id"]),
                game_id=game_id,
                depth_tier=body.depth_tier,
            )
            if existing is not None and existing["status"] == "complete":
                return {
                    "review_id": existing["review_id"],
                    "cached": True,
                    "status": "complete",
                    "game_id": game_id,
                    "depth_tier": body.depth_tier,
                }

            review_id = create_pending_review(
                connection,
                user_id=int(user["id"]),
                game_id=game_id,
                user_color=body.user_color,
                depth_tier=body.depth_tier,
                user_rating=body.user_rating,
                force_new=existing is not None and existing["status"] == "error",
            )
        finally:
            connection.close()

        _ensure_job(
            review_id,
            pgn=body.pgn,
            user_color=body.user_color,
            depth_tier=body.depth_tier,
            user_rating=body.user_rating,
        )

        return {
            "review_id": review_id,
            "cached": False,
            "status": "pending",
            "game_id": game_id,
        }

    @router.get("/review/{review_id}")
    def get_review_route(
        review_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = get_review(connection, review_id, user_id=int(user["id"]))
        if row is None:
            raise HTTPException(status_code=404, detail="Review not found")
        # Resume orphaned pending/running rows (e.g. after server restart).
        if row["status"] in ("pending", "running"):
            game_row = connection.execute(
                "SELECT pgn FROM games WHERE game_id = ?", (row["game_id"],)
            ).fetchone()
            if game_row is not None and game_row["pgn"]:
                rating = row["user_rating"]
                try:
                    rating = int(rating) if rating is not None else None
                except (TypeError, ValueError):
                    rating = None
                resumed = _ensure_job(
                    review_id,
                    pgn=game_row["pgn"],
                    user_color=row["user_color"] or "white",
                    depth_tier=row["depth_tier"] or "full",
                    user_rating=rating,
                )
                if resumed:
                    logger.info("resumed orphaned review job %s", review_id)
        # B.3: constants refit → recompute scores from stored features (no engine).
        if row["status"] == "complete" and row["depth_tier"] == "full":
            try:
                ensure_fresh_findability(connection, review_id)
                row = get_review(connection, review_id, user_id=int(user["id"])) or row
            except Exception:  # noqa: BLE001
                logger.exception("findability recompute failed for %s", review_id)
        game = connection.execute(
            "SELECT pgn, white_name, black_name, white_rating, black_rating, result, "
            "played_at, time_class, source FROM games WHERE game_id = ?",
            (row["game_id"],),
        ).fetchone()
        moves = connection.execute(
            "SELECT ply, san, is_user_move, phase, classification, win_prob, delta_w, "
            "volatility, findability, findability_personal, r_find, time_spent, "
            "clock_remaining, detail FROM review_moves WHERE review_id = ? ORDER BY ply",
            (review_id,),
        ).fetchall()
        # The three findability columns are the *score*; the band, the C_A curve
        # and the realistic alternate move live only in the stored feature
        # vector. Rebuilding them here (pure math over cached `pi_r`, no engine)
        # is what lets a re-opened review show the same panel the live SSE run
        # did — otherwise the panel renders a bare meter with no band.
        constants = FindabilityConstants.load()
        try:
            row_rating = int(row["user_rating"]) if row["user_rating"] is not None else None
        except (TypeError, ValueError):
            row_rating = None
        move_list = []
        for m in moves:
            d = dict(m)
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except json.JSONDecodeError:
                    pass
            if d.get("findability") is not None:
                d["findability_detail"] = _findability_detail(
                    d.get("detail"), int(d["findability"]), constants, row_rating
                )
            move_list.append(d)
        detail = None
        if row["detail_json"]:
            try:
                detail = json.loads(row["detail_json"])
            except json.JSONDecodeError:
                detail = row["detail_json"]
        payload = {
            **dict(row),
            "detail": detail,
            "moves": move_list,
        }
        if game is not None:
            payload.update({
                "pgn": game["pgn"],
                "white_name": game["white_name"],
                "black_name": game["black_name"],
                "white_rating": game["white_rating"],
                "black_rating": game["black_rating"],
                "result": game["result"],
                "played_at": game["played_at"],
                "time_class": game["time_class"],
                "source": game["source"],
            })
        return payload

    @router.get("/reviews")
    def list_reviews_route(
        limit: int = 20,
        offset: int = 0,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        items = list_reviews(
            connection,
            int(user["id"]),
            limit=max(1, min(100, int(limit))),
            offset=max(0, int(offset)),
        )
        return {"reviews": items}

    return router
