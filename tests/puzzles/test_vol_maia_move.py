"""Phase 5: stateless 'play vs Maia from this position' endpoint.

Unauthenticated, DB-free, and driven by the ``playout_move_fn`` seam, so it is
tested with a fake mover — no lc0/Stockfish required.
"""

from __future__ import annotations

import chess
from fastapi.testclient import TestClient

from server.main import create_app


def _client() -> TestClient:
    return TestClient(create_app(":memory:"))


class FakeMover:
    def __init__(self, move: str | None, engine: str = "maia") -> None:
        self.move = move
        self.engine = engine
        self.calls: list[tuple[str, int]] = []

    def __call__(self, fen: str, rating: int) -> tuple[str | None, str]:
        self.calls.append((fen, rating))
        return self.move, self.engine


def _after_e4() -> str:
    board = chess.Board()
    board.push_uci("e2e4")
    return board.fen()


def test_returns_maia_reply_and_advances_fen() -> None:
    client = _client()
    mover = FakeMover("e7e5", "maia")
    client.app.state.playout_move_fn = mover

    resp = client.post("/api/vol/play/maia-move", json={"fen": _after_e4(), "rating": 1450})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["move"] == "e7e5"
    assert data["san"] == "e5"
    assert data["engine"] == "maia"
    assert data["game_over"] is False
    assert chess.Board(data["fen"]).turn == chess.WHITE  # advanced past black's reply
    # rating snapped to the nearest Maia bucket before the mover was called
    assert mover.calls[0][1] == 1500


def test_invalid_fen_is_400() -> None:
    client = _client()
    resp = client.post("/api/vol/play/maia-move", json={"fen": "not-a-fen"})
    assert resp.status_code == 400


def test_terminal_position_returns_no_move() -> None:
    client = _client()
    client.app.state.playout_move_fn = FakeMover("a2a3")  # must not be used
    board = chess.Board()
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):  # fool's mate
        board.push_uci(uci)
    assert board.is_checkmate()

    resp = client.post("/api/vol/play/maia-move", json={"fen": board.fen()})
    assert resp.status_code == 200
    data = resp.json()
    assert data["move"] is None
    assert data["game_over"] is True
    assert data["result"] == "0-1"


def test_engine_no_move_is_503() -> None:
    client = _client()
    client.app.state.playout_move_fn = FakeMover(None)
    resp = client.post("/api/vol/play/maia-move", json={"fen": _after_e4()})
    assert resp.status_code == 503


def test_illegal_engine_move_is_502() -> None:
    client = _client()
    # After 1.e4 it is black to move; "e2e4" is illegal (e2 is empty).
    client.app.state.playout_move_fn = FakeMover("e2e4")
    resp = client.post("/api/vol/play/maia-move", json={"fen": _after_e4()})
    assert resp.status_code == 502
