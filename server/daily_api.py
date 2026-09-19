"""Daily check-in API (``/api/daily/*``).

Every route takes the client's local ``day`` because only the browser knows the
player's timezone; :func:`server.daily.normalize_day` clamps it. The routes are
thin — the state machine lives in ``server/daily.py`` and the phases themselves
live in the modes they hand off to.

Engine-free: the one phase that needs data of its own (``review``) picks from
rows the reviews already stored.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from server import daily
from server.deps import current_user, get_connection


class DayRequest(BaseModel):
    day: str | None = Field(default=None, max_length=32)


class HeartbeatRequest(DayRequest):
    seconds: float = Field(default=0.0, ge=0, le=3600)


class FinishRequest(HeartbeatRequest):
    status: Literal["done", "skipped"] = "done"


def build_daily_router(app: FastAPI) -> APIRouter:  # noqa: ARG001 — parity
    router = APIRouter(prefix="/api/daily")

    def _session(
        connection: sqlite3.Connection, user: sqlite3.Row, day: str | None
    ) -> tuple[sqlite3.Row, str]:
        resolved = daily.normalize_day(day)
        return daily.get_or_create_session(connection, int(user["id"]), resolved), resolved

    def _state(
        connection: sqlite3.Connection, user: sqlite3.Row, session: sqlite3.Row, day: str
    ) -> dict[str, Any]:
        payload = daily.session_payload(connection, session)
        payload["streak"] = daily.streaks(connection, int(user["id"]), day)
        return payload

    @router.get("/today")
    def today(
        day: str | None = None,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        session, resolved = _session(connection, user, day)
        return _state(connection, user, session, resolved)

    @router.post("/phase/{phase}/start")
    def start(
        phase: str,
        request: DayRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        _require_phase(phase)
        session, resolved = _session(connection, user, request.day)
        payload = None
        if phase == "review":
            # Picked once and stored on the phase row, so re-entering the phase
            # (or reloading mid-session) returns the same game rather than
            # silently swapping it for another one.
            existing = _phase_payload(connection, int(session["id"]), phase)
            payload = existing or daily.pick_loss(connection, int(user["id"]))
        daily.start_phase(connection, int(session["id"]), phase, payload=payload)
        state = _state(connection, user, session, resolved)
        state["started"] = phase
        return state

    @router.post("/phase/{phase}/heartbeat")
    def heartbeat(
        phase: str,
        request: HeartbeatRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        _require_phase(phase)
        session, _ = _session(connection, user, request.day)
        seconds = daily.add_seconds(
            connection, int(session["id"]), phase, request.seconds
        )
        return {"phase": phase, "seconds_spent": seconds}

    @router.post("/phase/{phase}/finish")
    def finish(
        phase: str,
        request: FinishRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        _require_phase(phase)
        session, resolved = _session(connection, user, request.day)
        daily.finish_phase(
            connection,
            int(session["id"]),
            phase,
            status=request.status,
            seconds=request.seconds,
        )
        return _state(connection, user, session, resolved)

    @router.post("/phase/{phase}/reopen")
    def reopen(
        phase: str,
        request: DayRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        _require_phase(phase)
        session, resolved = _session(connection, user, request.day)
        daily.reopen_phase(connection, int(session["id"]), phase)
        return _state(connection, user, session, resolved)

    @router.get("/calendar")
    def calendar(
        month: str | None = None,
        day: str | None = None,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, Any]:
        resolved = daily.normalize_day(day)
        payload = daily.month_days(connection, int(user["id"]), month)
        payload["today"] = resolved
        payload["streak"] = daily.streaks(connection, int(user["id"]), resolved)
        payload["phases"] = [
            {"key": p.key, "title": p.title, "mode": p.mode} for p in daily.PHASES
        ]
        return payload

    return router


def _require_phase(phase: str) -> None:
    if phase not in daily.PHASE_BY_KEY:
        raise HTTPException(status_code=404, detail=f"unknown phase: {phase!r}")


def _phase_payload(
    connection: sqlite3.Connection, session_id: int, phase: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT payload FROM daily_phase_runs WHERE session_id = ? AND phase = ?",
        (int(session_id), phase),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        decoded = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None
