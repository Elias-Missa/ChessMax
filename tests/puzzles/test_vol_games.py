"""Per-user saved-games API tests (server/vol_games_api.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from server.main import create_app


def client_for(app: Any, email: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


def sample_game(game_id: str = "g1") -> dict[str, Any]:
    return {
        "id": game_id,
        "imported_at": 1000,
        "source_name": "test.pgn#1",
        "pgn": '[White "a"]\n[Black "b"]\n\n1. e4 e5 *',
        "metadata": {"white": "a", "black": "b", "result": "*"},
        "report": {"plies": [{"ply": 1, "san": "e4"}]},
        "derived_stats": {"avgV": 12.3, "blunders": 0},
    }


def test_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "trainer.db"), raise_server_exceptions=False)
    assert client.get("/api/vol/games").status_code == 401
    assert client.post("/api/vol/games", json=sample_game()).status_code == 401


def test_save_list_get_delete_roundtrip(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"), "a@x.com")

    assert client.post("/api/vol/games", json=sample_game()).json() == {"id": "g1", "saved": True}

    listed = client.get("/api/vol/games").json()["games"]
    assert len(listed) == 1
    assert listed[0]["id"] == "g1"
    assert listed[0]["metadata"]["white"] == "a"
    assert listed[0]["derived_stats"]["avgV"] == 12.3
    assert "report" not in listed[0]  # list omits the heavy body

    full = client.get("/api/vol/games/g1").json()
    assert full["pgn"].startswith("[White")
    assert full["report"]["plies"][0]["san"] == "e4"

    assert client.delete("/api/vol/games/g1").json() == {"id": "g1", "deleted": True}
    assert client.get("/api/vol/games").json()["games"] == []
    assert client.get("/api/vol/games/g1").status_code == 404
    assert client.delete("/api/vol/games/g1").status_code == 404


def test_upsert_overwrites_same_id(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"), "a@x.com")
    client.post("/api/vol/games", json=sample_game())
    updated = sample_game()
    updated["source_name"] = "renamed.pgn"
    client.post("/api/vol/games", json=updated)

    games = client.get("/api/vol/games").json()["games"]
    assert len(games) == 1
    assert games[0]["source_name"] == "renamed.pgn"


def test_games_are_per_user(tmp_path: Path) -> None:
    app = create_app(tmp_path / "trainer.db")
    a = client_for(app, "a@x.com")
    b = client_for(app, "b@x.com")

    a.post("/api/vol/games", json=sample_game("shared-id"))

    # B sees nothing and cannot read or delete A's game.
    assert b.get("/api/vol/games").json()["games"] == []
    assert b.get("/api/vol/games/shared-id").status_code == 404
    assert b.delete("/api/vol/games/shared-id").status_code == 404

    # A still owns it.
    assert len(a.get("/api/vol/games").json()["games"]) == 1
