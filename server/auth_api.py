"""Auth HTTP routes: register, login, logout, me.

Sets/clears the ``chessmax_session`` httpOnly cookie. Kept dependency-free of
``email-validator`` (plain ``str`` email; validation lives in
:func:`server.auth.register_user`).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server import auth
from server.auth import SESSION_COOKIE, SESSION_TTL_DAYS
from server.deps import current_user, get_connection


class CredentialsRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def _me_payload(user: sqlite3.Row) -> dict[str, object]:
    return {
        "email": user["email"],
        "username": user["username"],
        "rating": int(user["rating"]),
        "chesscom_username": user["chesscom_username"],
    }


def build_auth_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api/auth")

    def _set_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_TTL_DAYS * 86400,
            path="/",
        )

    @router.post("/register")
    def register(
        body: CredentialsRequest,
        response: Response,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        try:
            user = auth.register_user(connection, body.email, body.password)
        except auth.AuthError as exc:
            status = 409 if "already exists" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        token = auth.create_session(connection, int(user["id"]))
        _set_cookie(response, token)
        return _me_payload(user)

    @router.post("/login")
    def login(
        body: CredentialsRequest,
        response: Response,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        try:
            user = auth.authenticate(connection, body.email, body.password)
        except auth.AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        token = auth.create_session(connection, int(user["id"]))
        _set_cookie(response, token)
        return _me_payload(user)

    @router.post("/logout")
    def logout(
        request: Request,
        response: Response,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        auth.delete_session(connection, request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @router.get("/me")
    def me(user: sqlite3.Row = Depends(current_user)) -> dict[str, object]:
        return _me_payload(user)

    @router.get("/status")
    def status(
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, object]:
        """Unauthenticated: whether any account exists yet (drives first-run signup)."""
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE email IS NOT NULL"
        ).fetchone()[0]
        return {"has_accounts": count > 0}

    return router
