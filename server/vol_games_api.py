"""Per-user saved analyzed games (the vol "Library"), server-side.

Replaces the browser-only IndexedDB store. All routes are auth-gated via
:func:`server.deps.current_user`; every row is scoped to and ownership-checked
against the logged-in user. The list endpoint omits the heavy ``report_json`` so
the Library table stays light; the detail endpoint returns the full record.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from server.deps import current_user, get_connection


class VolGameRequest(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    imported_at: int | None = None
    source_name: str | None = None
    pgn: str = Field(default="", max_length=2_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any] = Field(default_factory=dict)
    derived_stats: dict[str, Any] = Field(default_factory=dict)


def _row_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "imported_at": row["imported_at"],
        "source_name": row["source_name"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "derived_stats": json.loads(row["derived_stats_json"] or "{}"),
    }


def _row_full(row: sqlite3.Row) -> dict[str, Any]:
    summary = _row_summary(row)
    summary["pgn"] = row["pgn"]
    summary["report"] = json.loads(row["report_json"] or "{}")
    return summary


def build_vol_games_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api/vol/games")

    @router.get("")
    def list_games(
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM vol_games WHERE user_id = ? ORDER BY imported_at DESC",
            (user["id"],),
        ).fetchall()
        return {"games": [_row_summary(r) for r in rows]}

    @router.get("/{game_id}")
    def get_game(
        game_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM vol_games WHERE id = ? AND user_id = ?",
            (game_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Game not found")
        return _row_full(row)

    @router.post("")
    def save_game(
        body: VolGameRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        cur = connection.execute(
            """
            INSERT INTO vol_games (
                id, user_id, imported_at, source_name, pgn,
                metadata_json, report_json, derived_stats_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                imported_at = excluded.imported_at,
                source_name = excluded.source_name,
                pgn = excluded.pgn,
                metadata_json = excluded.metadata_json,
                report_json = excluded.report_json,
                derived_stats_json = excluded.derived_stats_json
            WHERE vol_games.user_id = excluded.user_id
            """,
            (
                body.id,
                user["id"],
                body.imported_at,
                body.source_name,
                body.pgn,
                json.dumps(body.metadata, separators=(",", ":")),
                json.dumps(body.report, separators=(",", ":")),
                json.dumps(body.derived_stats, separators=(",", ":")),
            ),
        )
        connection.commit()
        if cur.rowcount == 0:
            # The id exists but belongs to another user: the guarded upsert
            # touched nothing. Never report a silent no-op as saved.
            raise HTTPException(
                status_code=409,
                detail="A game with this id belongs to another account.",
            )
        return {"id": body.id, "saved": True}

    @router.delete("/{game_id}")
    def delete_game(
        game_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        cur = connection.execute(
            "DELETE FROM vol_games WHERE id = ? AND user_id = ?",
            (game_id, user["id"]),
        )
        connection.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Game not found")
        return {"id": game_id, "deleted": True}

    return router
