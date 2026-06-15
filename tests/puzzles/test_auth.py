"""HTTP-level auth tests (register / login / logout / me + gating)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server import db
from server.main import create_app

CREDS = {"email": "me@example.com", "password": "secret1"}


def fresh_client(tmp_path: Path) -> TestClient:
    """Client with NO authentication performed yet."""
    return TestClient(create_app(tmp_path / "trainer.db"), raise_server_exceptions=False)


def test_status_reflects_account_existence(tmp_path: Path) -> None:
    client = fresh_client(tmp_path)
    # Unauthenticated and allowed; first run has no accounts.
    assert client.get("/api/auth/status").json() == {"has_accounts": False}
    client.post("/api/auth/register", json=CREDS)
    assert client.get("/api/auth/status").json() == {"has_accounts": True}


def test_protected_route_401_without_session(tmp_path: Path) -> None:
    client = fresh_client(tmp_path)
    assert client.get("/api/user").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_register_sets_cookie_and_me_works(tmp_path: Path) -> None:
    client = fresh_client(tmp_path)
    reg = client.post("/api/auth/register", json=CREDS)
    assert reg.status_code == 200, reg.text
    assert reg.json()["email"] == "me@example.com"
    assert "chessmax_session" in reg.cookies or "chessmax_session" in client.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "me@example.com"
    # A protected app route now works on the same client (cookie carried).
    assert client.get("/api/user").status_code == 200


def test_logout_clears_session(tmp_path: Path) -> None:
    client = fresh_client(tmp_path)
    client.post("/api/auth/register", json=CREDS)
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/user").status_code == 401


def test_login_after_register(tmp_path: Path) -> None:
    client = fresh_client(tmp_path)
    client.post("/api/auth/register", json=CREDS)
    client.post("/api/auth/logout")

    bad = client.post("/api/auth/login", json={**CREDS, "password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/api/auth/login", json=CREDS)
    assert ok.status_code == 200
    assert client.get("/api/auth/me").json()["email"] == "me@example.com"


def test_duplicate_email_409(tmp_path: Path) -> None:
    client = fresh_client(tmp_path)
    client.post("/api/auth/register", json=CREDS)
    dup = client.post("/api/auth/register", json={**CREDS, "password": "other1"})
    assert dup.status_code == 409


def test_first_account_claims_default_history(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    # Seed the legacy singleton with a non-default rating.
    with db.connect(db_path) as conn:
        default = db.get_singleton_user(conn)
        conn.execute("UPDATE users SET rating = 1654 WHERE id = ?", (default["id"],))
        conn.commit()

    client = TestClient(create_app(db_path), raise_server_exceptions=False)
    client.post("/api/auth/register", json=CREDS)

    # The new account inherited the legacy rating, and there's still just one user.
    assert client.get("/api/user").json()["rating"] == 1654
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_two_accounts_have_isolated_data(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    app = create_app(db_path)
    a = TestClient(app, raise_server_exceptions=False)
    b = TestClient(app, raise_server_exceptions=False)

    a.post("/api/auth/register", json={"email": "a@x.com", "password": "secret1"})
    b.post("/api/auth/register", json={"email": "b@x.com", "password": "secret1"})

    # Each sees their own openings independently.
    a.put("/api/openings", json={"openings": ["london"]})
    assert a.get("/api/openings").json()["selected"] == ["london"]
    assert b.get("/api/openings").json()["selected"] == []
