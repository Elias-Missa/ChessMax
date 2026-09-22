"""Opening-builder API (``/api/opening-book/*``).

Deliberately **not** ``/api/openings`` — that path is already the legacy
selected-openings preference (``server/main.py``), and hanging a router off the
same prefix makes which handler answers a question of registration order.

Engine and Maia arrive through ``app.state`` like the rest of the trainer, so
the routes stay testable on a box with neither:

* ``app.state.analyze_fn``   — Stockfish, for the engine dot and the eval column.
* ``app.state.maia_topk_fn`` — the strongest Maia net, for the human dot.

The Lichess explorer is reached through :mod:`server.opening_cache`, whose
``FETCH_JSON`` seam is monkey-patched in tests the same way
``chess_vol.server.ENGINE_FACTORY`` is.

**Nothing here 503s when a source is missing.** Every evidence source degrades
to an unknown dot independently and the panel still renders — a builder that
refuses to show you legal moves because a statistics service is unreachable is
worse than one that shows them with a grey dot.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import chess
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from core.opening_book import OpeningBookConstants
from server import opening_import, opening_prep, opening_tree_stats, openings_build
from server.deps import current_user, get_connection
from server.openings_build import COLORS, START_FEN


class EdgeRequest(BaseModel):
    color: str = Field(min_length=1, max_length=10)
    fen: str = Field(min_length=10, max_length=120)
    uci: str = Field(min_length=4, max_length=5)
    path: list[str] = Field(default_factory=list, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    signals: dict[str, Any] | None = None
    opening_name: str | None = Field(default=None, max_length=160)
    opening_eco: str | None = Field(default=None, max_length=10)


class EdgeDeleteRequest(BaseModel):
    color: str = Field(min_length=1, max_length=10)
    fen: str = Field(min_length=10, max_length=120)
    uci: str = Field(min_length=4, max_length=5)


class ImportRequest(BaseModel):
    color: str = Field(min_length=1, max_length=10)
    # 400k is a generous study chapter and still far under any request limit;
    # the real bound on the work is `max_moves` in server.opening_import.
    pgn: str = Field(min_length=2, max_length=400_000)
    max_plies: int = Field(default=opening_import.MAX_PLIES, ge=2, le=60)


def build_openings_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api/opening-book")

    def _color(value: str) -> str:
        if value not in COLORS:
            raise HTTPException(status_code=422, detail=f"unknown colour: {value!r}")
        return value

    def _fen(value: str) -> str:
        try:
            board = chess.Board(value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid FEN: {exc}") from exc
        if not board.is_valid():
            raise HTTPException(status_code=422, detail="that position is not legal")
        return value

    @router.get("/candidates")
    def candidates(
        fen: str = Query(default=START_FEN),
        color: str = Query(default="white"),
        network: bool = Query(default=True),
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        return openings_build.candidates_for(
            connection,
            _fen(fen),
            user_id=int(user["id"]),
            color=_color(color),
            analyze_fn=getattr(app.state, "analyze_fn", None),
            maia_fn=getattr(app.state, "maia_topk_fn", None),
            constants=OpeningBookConstants.load(),
            allow_network=network,
        )

    @router.get("/tree")
    def tree(
        color: str = Query(default="white"),
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        return openings_build.load_tree(
            connection, int(user["id"]), color=_color(color)
        )

    @router.get("/graph")
    def graph(
        color: str = Query(default="white"),
        stats: bool = Query(default=True),
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        """The book as a drawable tree, each node coloured by how you do there."""

        resolved = _color(color)
        tree = openings_build.load_tree(connection, int(user["id"]), color=resolved)
        graph = openings_build.build_graph(tree["nodes"], tree["edges"])
        if stats:
            graph["stats"] = opening_tree_stats.stats_for_nodes(
                connection,
                int(user["id"]),
                color=resolved,
                position_keys=[n["key"] for n in graph["nodes"]],
            )
        graph["color"] = resolved
        graph["decisions"] = tree["decisions"]
        graph["moves"] = tree["moves"]
        return graph

    @router.get("/coverage")
    def coverage(
        fen: str = Query(default=START_FEN),
        color: str = Query(default="white"),
        network: bool = Query(default=False),
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        return openings_build.coverage_for(
            connection,
            _fen(fen),
            user_id=int(user["id"]),
            color=_color(color),
            allow_network=network,
        )

    @router.post("/edges")
    def add_edge(
        request: EdgeRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return openings_build.add_edge(
                connection,
                int(user["id"]),
                color=_color(request.color),
                fen=_fen(request.fen),
                uci=request.uci,
                path_uci=request.path,
                note=request.note,
                signals=request.signals,
                opening_name=request.opening_name,
                opening_eco=request.opening_eco,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/prep")
    def prep(
        limit: int = Query(default=opening_prep.MAX_SUGGESTIONS, ge=1, le=20),
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        """The openings Insights says are costing most, as places to build."""

        return opening_prep.suggestions(connection, int(user["id"]), limit=limit)

    @router.post("/import")
    def import_pgn(
        request: ImportRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        report = opening_import.import_pgn(
            connection,
            int(user["id"]),
            color=_color(request.color),
            pgn_text=request.pgn,
            max_plies=request.max_plies,
        )
        return report.as_dict()

    @router.post("/edges/delete")
    def delete_edge(
        request: EdgeDeleteRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        removed = openings_build.remove_edge(
            connection,
            int(user["id"]),
            color=_color(request.color),
            fen=_fen(request.fen),
            uci=request.uci,
        )
        return {"removed": removed}

    return router


__all__ = ["build_openings_router"]
