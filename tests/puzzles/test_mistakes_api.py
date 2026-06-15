"""TestClient coverage for the "Your Mistakes" API (server/mistakes_api.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import chess
from fastapi.testclient import TestClient

from server import db
from server.main import create_app
from server.mistakes_run import generate_mistakes

# Reuse the detector test's scripted-analysis builders.
from tests.puzzles.test_mistakes_detect import (
    FENS,
    MISTAKE_PLY,
    MOVES,
    neutral_white_nodes,
    scripted_analysis,
)


def make_client(tmp_path: Path) -> TestClient:
    path = str(tmp_path / "trainer.db")
    with db.connect(path) as connection:
        db.get_singleton_user(connection)  # claimed by the test account below
    client = TestClient(create_app(db_path=path), raise_server_exceptions=False)
    creds = {"email": "tester@example.com", "password": "testpass"}
    if client.post("/api/auth/register", json=creds).status_code >= 400:
        client.post("/api/auth/login", json=creds)
    return client


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event, data_lines = "message", []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


def seed_puzzle(db_path: Path, **over: Any) -> int:
    """Insert one mistake_puzzle directly and return its id."""

    fields = {
        "user_id": 1, "run_id": 1, "bucket": "missed_win",
        "fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 3 3",
        "side_to_move": "w", "user_color": "w",
        "best_move": "f3g5", "solution_moves": "f3g5 d7d6", "user_actual_move": "f1c4",
        "eval_before_cp": 300, "eval_best_cp": 300, "eval_played_cp": 120,
        "second_best_gap_cp": 180, "volatility": 61.0,
        "maia1900_p_solution": None, "maia_solution_rank": 1, "maia_best_in_top3": 1,
        "clock_seconds": 95.0, "ply_number": 4,
        "game_url": "https://chess.com/game/1", "game_id": "uuid-1",
        "game_date": "2026-06-01", "time_class": "blitz", "opponent": "bob",
        "caption": "In your game vs bob on 2026-06-01, you played Bc4. The win was Ng5.",
    }
    fields.update(over)
    with db.connect(db_path) as conn:
        db.get_singleton_user(conn)  # ensure user id 1 exists
        # ensure a run row exists for the FK
        conn.execute(
            "INSERT OR IGNORE INTO mistake_runs (id, user_id, chesscom_user, since_date) "
            "VALUES (1, 1, 'alice', '2026-03-01')"
        )
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO mistake_puzzles ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        conn.commit()
        return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# Username + run                                                               #
# --------------------------------------------------------------------------- #


def test_username_get_and_put_roundtrip(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/api/mistakes/username").json() == {"chesscom_username": None}

    put = client.put("/api/mistakes/username", json={"chesscom_username": "  alice  "})
    assert put.json() == {"chesscom_username": "alice"}
    assert client.get("/api/mistakes/username").json() == {"chesscom_username": "alice"}


def test_run_null_before_any_generation(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/api/mistakes/run").json() == {"run": None}


# --------------------------------------------------------------------------- #
# next + attempt                                                               #
# --------------------------------------------------------------------------- #


def test_next_404_when_empty(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/api/mistakes/next").status_code == 404


def test_next_returns_seeded_puzzle(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    pid = seed_puzzle(db_path)
    client = make_client(tmp_path)

    payload = client.get("/api/mistakes/next").json()
    assert payload["position_id"] == pid
    assert payload["bucket"] == "missed_win"
    assert payload["side_to_move"] == "w"
    assert payload["ply_count"] == 2  # "f3g5 d7d6"


def test_attempt_correct_marks_solved_and_returns_overlay(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    pid = seed_puzzle(db_path)
    client = make_client(tmp_path)

    resp = client.post(f"/api/mistakes/{pid}/attempt", json={"move": "f3g5"})
    body = resp.json()
    assert body["status"] == "solved" and body["solved"] is True
    assert body["best_move_uci"] == "f3g5" and body["best_move_san"] == "Ng5"
    assert body["user_actual_uci"] == "f1c4" and body["user_actual_san"] == "Bc4"
    assert body["bucket"] == "missed_win"
    assert body["game_url"] == "https://chess.com/game/1"
    assert "The win was Ng5" in body["caption"]

    # Solved persists; it no longer shows up as "next".
    assert client.get("/api/mistakes/next").status_code == 404


def test_attempt_wrong_is_failed_and_stays_unsolved(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    pid = seed_puzzle(db_path)
    client = make_client(tmp_path)

    resp = client.post(f"/api/mistakes/{pid}/attempt", json={"move": "f1c4"})
    body = resp.json()
    assert body["status"] == "failed" and body["solved"] is False
    # Overlay still reveals the answer even on a miss.
    assert body["best_move_san"] == "Ng5"
    assert client.get("/api/mistakes/next").json()["position_id"] == pid


def test_attempt_unknown_puzzle_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.post("/api/mistakes/999/attempt", json={"move": "e2e4"}).status_code == 404


# --------------------------------------------------------------------------- #
# generate (SSE) — engine-free via an injected generate fn                     #
# --------------------------------------------------------------------------- #


def _pgn_with_mistake() -> str:
    # 1.e4 e5 2.Nf3 Nc6 3.Bc4 (the mistake), clocks well above the 30s floor.
    return (
        '[White "alice"]\n[Black "bob"]\n\n'
        "1. e4 {[%clk 0:03:00]} e5 {[%clk 0:03:00]} "
        "2. Nf3 {[%clk 0:02:55]} Nc6 {[%clk 0:02:55]} "
        "3. Bc4 {[%clk 0:02:50]} *\n"
    )


def _fetch_stub(url: str, user_agent: str) -> dict[str, Any]:
    if url.endswith("/games/archives"):
        return {"archives": ["https://api.chess.com/pub/player/alice/games/2026/06"]}
    return {
        "games": [{
            "rules": "chess", "time_class": "blitz", "rated": True,
            "end_time": 4102444800,  # far-future so it's always within lookback
            "url": "https://chess.com/game/1", "uuid": "uuid-1",
            "pgn": _pgn_with_mistake(),
            "white": {"username": "alice", "rating": 1500, "result": "win"},
            "black": {"username": "bob", "rating": 1490, "result": "checkmated"},
        }]
    }


def _install_fake_generate(client: TestClient) -> None:
    script = neutral_white_nodes()
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 300, "pv": ["f3g5", "d7d6"]},
        {"move": "f1c4", "eval": 120},
    ]

    def fake_generate(connection, **kw: Any) -> dict[str, int]:
        return generate_mistakes(
            connection,
            user_id=kw["user_id"], username=kw["username"], since=kw["since"],
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["f3g5"],
            volatility_fn=lambda fen: 50.0,
            fetch_json=_fetch_stub,
            on_event=kw["on_event"], is_cancelled=kw["is_cancelled"],
        )

    client.app.state.mistakes_generate_fn = fake_generate


def test_generate_streams_events_and_inserts_puzzle(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    _install_fake_generate(client)

    resp = client.post("/api/mistakes/generate", json={"chesscom_username": "alice"})
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    names = [name for name, _ in events]

    assert names[0] == "start"
    assert "puzzle" in names
    assert names[-1] == "done"
    done = dict(events)["done"]
    assert done["puzzles_created"] == 1
    assert done["games_scanned"] == 1

    # The puzzle is now servable and the username was persisted.
    nxt = client.get("/api/mistakes/next").json()
    assert nxt["bucket"] == "missed_win"
    assert client.get("/api/mistakes/username").json() == {"chesscom_username": "alice"}

    run = client.get("/api/mistakes/run").json()["run"]
    assert run["status"] == "done" and run["puzzles_created"] == 1


def parse_sse_like_frontend(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse the raw SSE stream the way ``frontend/app.js:consumeEventStream`` does.

    Deliberately does NOT normalize ``\\r\\n`` first (unlike ``parse_sse``): it
    splits events on ``/\\r\\n\\r\\n|\\n\\n/`` and lines on ``/\\r?\\n/``, mirroring
    the browser parser exactly. This pins the wire contract — sse_starlette
    terminates events with CRLF, and a parser that only looked for ``\\n\\n``
    (the original bug) would recover zero events and freeze the UI on
    "Starting…".
    """

    events: list[tuple[str, dict[str, Any]]] = []
    for chunk in re.split(r"\r\n\r\n|\n\n", text):
        if not chunk.strip():
            continue
        event, data_lines = "message", []
        for line in re.split(r"\r?\n", chunk):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


def test_generate_stream_uses_crlf_and_parses_frontend_side(tmp_path: Path) -> None:
    """Regression: the stream must be parseable by the browser's CRLF-aware parser.

    sse_starlette emits ``\\r\\n\\r\\n`` event terminators; the frontend once split
    only on ``\\n\\n`` (which never matches), so no event was ever delivered and the
    status stayed on "Starting…" forever.
    """

    client = make_client(tmp_path)
    _install_fake_generate(client)

    resp = client.post("/api/mistakes/generate", json={"chesscom_username": "alice"})
    assert resp.status_code == 200, resp.text

    # Pin the wire format: events are CRLF-terminated, never bare-LF-terminated.
    assert "\r\n\r\n" in resp.text
    assert "\n\n" not in resp.text.replace("\r\n", "")

    # A frontend-faithful parser (no CRLF normalization) must recover the events.
    events = parse_sse_like_frontend(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert dict(events)["done"]["puzzles_created"] == 1


def test_generate_400_without_username(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    _install_fake_generate(client)
    resp = client.post("/api/mistakes/generate", json={})
    assert resp.status_code == 400
