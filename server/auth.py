"""Account auth core: password hashing, sessions, registration.

Email + password accounts for the local app. Passwords are hashed with the
standard library's :func:`hashlib.scrypt` (no third-party dependency). Sessions
are opaque random tokens stored in the ``sessions`` table and carried in an
httpOnly cookie; they are revocable (logout / expiry).

This module is pure persistence + crypto — no FastAPI. The HTTP layer lives in
:mod:`server.auth_api` and the request dependency in :mod:`server.deps`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SESSION_COOKIE = "chessmax_session"
SESSION_TTL_DAYS = 30
DEFAULT_RATING = 1500

# scrypt parameters (RFC 7914 interactive-login range).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class AuthError(Exception):
    """Raised for registration/login failures (duplicate email, bad creds)."""


# --------------------------------------------------------------------------- #
# Password hashing                                                            #
# --------------------------------------------------------------------------- #


def hash_password(password: str, *, salt: bytes | None = None) -> tuple[str, str]:
    """Return ``(hash_hex, salt_hex)`` for ``password`` using scrypt."""

    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str | None, salt_hex: str | None) -> bool:
    """Constant-time check of ``password`` against a stored scrypt hash."""

    if not hash_hex or not salt_hex:
        return False
    try:
        candidate, _ = hash_password(password, salt=bytes.fromhex(salt_hex))
    except ValueError:
        return False
    return hmac.compare_digest(candidate, hash_hex)


# --------------------------------------------------------------------------- #
# Sessions                                                                    #
# --------------------------------------------------------------------------- #


def create_session(connection: sqlite3.Connection, user_id: int) -> str:
    """Create a session row and return its opaque token."""

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    connection.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at),
    )
    connection.commit()
    return token


def resolve_session(connection: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    """Return the user row for a valid, unexpired session token, else ``None``."""

    if not token:
        return None
    row = connection.execute(
        """
        SELECT u.* FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ?
        """,
        (token, datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    return row


def delete_session(connection: sqlite3.Connection, token: str | None) -> None:
    if not token:
        return
    connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
    connection.commit()


# --------------------------------------------------------------------------- #
# Registration / login                                                        #
# --------------------------------------------------------------------------- #


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def find_user_by_email(connection: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM users WHERE email = ?", (_normalize_email(email),)
    ).fetchone()


def register_user(connection: sqlite3.Connection, email: str, password: str) -> sqlite3.Row:
    """Create an account. The first account claims the legacy ``default`` row.

    Claim rule: if no account has an email yet (fresh accounts world) and an
    unclaimed ``username='default'`` row exists, attach the credentials to it so
    its history (rating, attempts, mined mistakes, openings) carries over.
    Otherwise insert a brand-new user. Raises :class:`AuthError` on duplicate
    email or empty input.
    """

    email = _normalize_email(email)
    if not email or "@" not in email:
        raise AuthError("A valid email is required.")
    if len(password) < 6:
        raise AuthError("Password must be at least 6 characters.")
    if find_user_by_email(connection, email) is not None:
        raise AuthError("An account with that email already exists.")

    hash_hex, salt_hex = hash_password(password)

    accounts = connection.execute(
        "SELECT COUNT(*) FROM users WHERE email IS NOT NULL"
    ).fetchone()[0]
    legacy = connection.execute(
        "SELECT * FROM users WHERE username = 'default' AND email IS NULL"
    ).fetchone()

    if accounts == 0 and legacy is not None:
        connection.execute(
            "UPDATE users SET email = ?, password_hash = ?, password_salt = ? WHERE id = ?",
            (email, hash_hex, salt_hex, legacy["id"]),
        )
        connection.commit()
        user_id = int(legacy["id"])
    else:
        cursor = connection.execute(
            """
            INSERT INTO users (username, email, password_hash, password_salt, rating, selected_openings)
            VALUES (?, ?, ?, ?, ?, '[]')
            """,
            (email, email, hash_hex, salt_hex, DEFAULT_RATING),
        )
        connection.commit()
        user_id = int(cursor.lastrowid)

    return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def authenticate(connection: sqlite3.Connection, email: str, password: str) -> sqlite3.Row:
    """Return the user row for valid credentials, else raise :class:`AuthError`."""

    user = find_user_by_email(connection, email)
    if user is None or not verify_password(password, user["password_hash"], user["password_salt"]):
        raise AuthError("Incorrect email or password.")
    return user
