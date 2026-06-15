"""Unit tests for the auth core (server/auth.py) — no HTTP."""

from __future__ import annotations

import sqlite3

import pytest

from server import auth, db


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


# --------------------------------------------------------------------------- #
# Password hashing                                                            #
# --------------------------------------------------------------------------- #


def test_hash_verify_roundtrip() -> None:
    h, s = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", h, s) is True
    assert auth.verify_password("wrong", h, s) is False


def test_hash_is_salted() -> None:
    h1, s1 = auth.hash_password("same")
    h2, s2 = auth.hash_password("same")
    assert s1 != s2 and h1 != h2  # distinct salts -> distinct hashes


def test_verify_handles_missing_hash() -> None:
    assert auth.verify_password("x", None, None) is False


# --------------------------------------------------------------------------- #
# Sessions                                                                    #
# --------------------------------------------------------------------------- #


def test_session_create_resolve_delete(conn: sqlite3.Connection) -> None:
    user = auth.register_user(conn, "a@b.com", "secret1")
    token = auth.create_session(conn, int(user["id"]))

    resolved = auth.resolve_session(conn, token)
    assert resolved is not None and resolved["id"] == user["id"]

    auth.delete_session(conn, token)
    assert auth.resolve_session(conn, token) is None


def test_expired_session_does_not_resolve(conn: sqlite3.Connection) -> None:
    user = auth.register_user(conn, "a@b.com", "secret1")
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        ("stale", int(user["id"]), "2000-01-01T00:00:00+00:00"),
    )
    conn.commit()
    assert auth.resolve_session(conn, "stale") is None


def test_resolve_none_token(conn: sqlite3.Connection) -> None:
    assert auth.resolve_session(conn, None) is None


# --------------------------------------------------------------------------- #
# Registration / claim-default / login                                        #
# --------------------------------------------------------------------------- #


def test_first_account_claims_default_user(conn: sqlite3.Connection) -> None:
    # Seed the legacy singleton with history.
    default = db.get_singleton_user(conn)
    conn.execute("UPDATE users SET rating = 1654 WHERE id = ?", (default["id"],))
    conn.commit()

    user = auth.register_user(conn, "owner@x.com", "secret1")

    # Same row was claimed (not a new one): id preserved, rating carried over.
    assert user["id"] == default["id"]
    assert user["rating"] == 1654
    assert user["email"] == "owner@x.com"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_second_account_is_fresh(conn: sqlite3.Connection) -> None:
    db.get_singleton_user(conn)
    first = auth.register_user(conn, "first@x.com", "secret1")
    second = auth.register_user(conn, "second@x.com", "secret1")

    assert second["id"] != first["id"]
    assert second["rating"] == auth.DEFAULT_RATING
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_register_without_default_row_creates_fresh(conn: sqlite3.Connection) -> None:
    # No singleton provisioned; first register still works (new row).
    user = auth.register_user(conn, "solo@x.com", "secret1")
    assert user["email"] == "solo@x.com"
    assert user["rating"] == auth.DEFAULT_RATING


def test_duplicate_email_rejected(conn: sqlite3.Connection) -> None:
    auth.register_user(conn, "dup@x.com", "secret1")
    with pytest.raises(auth.AuthError):
        auth.register_user(conn, "DUP@x.com", "secret2")  # case-insensitive


@pytest.mark.parametrize("email,password", [("noat", "secret1"), ("a@b.com", "短い")])
def test_register_validates_input(conn: sqlite3.Connection, email: str, password: str) -> None:
    with pytest.raises(auth.AuthError):
        auth.register_user(conn, email, password)


def test_authenticate_roundtrip(conn: sqlite3.Connection) -> None:
    auth.register_user(conn, "Me@X.com", "secret1")
    assert auth.authenticate(conn, "me@x.com", "secret1")["email"] == "me@x.com"
    with pytest.raises(auth.AuthError):
        auth.authenticate(conn, "me@x.com", "nope")
    with pytest.raises(auth.AuthError):
        auth.authenticate(conn, "ghost@x.com", "secret1")
