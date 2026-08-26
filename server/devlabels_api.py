"""Dev tab API (``/api/dev/*``): browse stored review positions and label them.

Auth-gated like the rest of ``/api/*``; every query is scoped to the logged-in
user's own reviews, so one account can never page through another's games.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from core.findability import FindabilityConstants, band_for
from server import devlabels
from server.deps import current_user, get_connection

PAGE_MAX = 100


class LabelRequest(BaseModel):
    review_id: str = Field(min_length=1, max_length=64)
    ply: int = Field(ge=1)
    # ``None`` clears that half of the verdict; the two are independent because
    # shallow-tier rows have volatility and no findability at all.
    volatility_label: str | None = None
    findability_label: str | None = None
    note: str | None = Field(default=None, max_length=devlabels.MAX_NOTE)


def build_dev_labels_router(app: FastAPI) -> APIRouter:  # noqa: ARG001
    router = APIRouter(prefix="/api/dev")

    def _bandify(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach the findability band, which is a pure function of the score."""

        constants = FindabilityConstants.load()
        for item in items:
            score = item.get("findability")
            item["findability_band"] = (
                band_for(int(score), constants) if score is not None else None
            )
        return items

    @router.get("/positions")
    def list_positions(
        scope: Literal["all", "unlabeled", "labeled"] = "all",
        require: Literal["any", "volatility", "findability"] = "any",
        limit: int = Query(default=50, ge=1, le=PAGE_MAX),
        offset: int = Query(default=0, ge=0),
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        user_id = int(user["id"])
        items = devlabels.list_positions(
            connection, user_id, scope=scope, require=require, limit=limit, offset=offset
        )
        return {
            "positions": _bandify(items),
            "total": devlabels.count_positions(
                connection, user_id, scope=scope, require=require
            ),
            "offset": offset,
            "limit": limit,
            "scope": scope,
            "require": require,
        }

    @router.post("/label")
    def write_label(
        body: LabelRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        if (
            body.volatility_label is not None
            and body.volatility_label not in devlabels.VOLATILITY_LABELS
        ):
            raise HTTPException(status_code=422, detail="Unknown volatility label")
        if (
            body.findability_label is not None
            and body.findability_label not in devlabels.FINDABILITY_LABELS
        ):
            raise HTTPException(status_code=422, detail="Unknown findability label")

        user_id = int(user["id"])
        move = devlabels.find_move(connection, user_id, body.review_id, body.ply)
        if move is None:
            raise HTTPException(status_code=404, detail="Position not found")
        return devlabels.upsert_label(
            connection,
            user_id,
            review_id=body.review_id,
            ply=body.ply,
            move=move,
            volatility_label=body.volatility_label,
            findability_label=body.findability_label,
            note=body.note,
        )

    @router.get("/stats")
    def stats(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        user_id = int(user["id"])
        payload = devlabels.label_stats(connection, user_id)
        payload["total_positions"] = devlabels.count_positions(connection, user_id)
        payload["labels"] = {
            "volatility": list(devlabels.VOLATILITY_LABELS),
            "findability": list(devlabels.FINDABILITY_LABELS),
        }
        return payload

    @router.get("/export")
    def export(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        return {"labels": devlabels.export_labels(connection, int(user["id"]))}

    return router
