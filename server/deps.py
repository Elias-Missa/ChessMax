"""Shared FastAPI request dependencies: DB connection + authenticated user.

Centralizes the per-request SQLite connection (previously a closure in each
router) and the ``current_user`` resolver that replaces the old single-user
``db.get_singleton_user`` calls. FastAPI caches sub-dependencies per request, so
a handler depending on both ``get_connection`` and ``current_user`` shares one
connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request

from server import db
from server.auth import SESSION_COOKIE, resolve_session


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    connection = db.connect(request.app.state.db_path)
    try:
        yield connection
    finally:
        connection.close()


def current_user(
    request: Request,
    connection: sqlite3.Connection = Depends(get_connection),
) -> sqlite3.Row:
    """Resolve the logged-in user from the session cookie, or 401."""

    token = request.cookies.get(SESSION_COOKIE)
    user = resolve_session(connection, token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
