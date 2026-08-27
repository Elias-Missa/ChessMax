"""Dev-tab piece-value lab API (server/piecevalues_api.py). Engine-free."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import chess
import chess.engine
from fastapi.testclient import TestClient

from server import piecevalues_api
from server.main import create_app

START = chess.Board().fen()


class ScriptedEngine:
    """Flat evaluation for every position — enough to exercise the route."""

    def __init__(self, cp: int = 30) -> None:
        self.cp = cp
        self.calls = 0

    def analyse(
        self, board: chess.Board, depth: int = 18, multipv: int = 6
    ) -> list[dict[str, Any]]:
        self.calls += 1
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(self.cp), chess.WHITE),
                "multipv": 1,
                "pv": [],
            }
        ]


def client_for(app: Any, email: str = "a@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


def use_engine(monkeypatch, engine: Any) -> None:
    @contextmanager
    def factory():
        yield engine

    monkeypatch.setattr(piecevalues_api, "ENGINE_FACTORY", factory)


def test_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "trainer.db"), raise_server_exceptions=False)
    assert client.post("/api/dev/piece-values", json={"fen": START}).status_code == 401


def test_returns_a_value_for_every_non_king_piece(tmp_path: Path, monkeypatch) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"))
    use_engine(monkeypatch, ScriptedEngine(cp=30))

    body = client.post("/api/dev/piece-values", json={"fen": START, "depth": 6}).json()
    assert len(body["pieces"]) == 30
    assert body["eval_cp"] == 30
    assert body["material_cp"] == 0
    assert "elapsed_ms" in body and "shrinkage" in body
    first = body["pieces"][0]
    assert {"square", "raw_cp", "anchored_cp", "premium_cp", "shrunk_cp", "tags"} <= set(first)


def test_the_reconciliation_identity_survives_the_json_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"))
    use_engine(monkeypatch, ScriptedEngine(cp=30))

    body = client.post("/api/dev/piece-values", json={"fen": START, "depth": 6}).json()
    signed = sum(
        (1 if p["color"] == "white" else -1) * p["anchored_cp"] for p in body["pieces"]
    )
    # Values are rounded to 0.1cp for transport, so 30 pieces admit ~1.5cp of
    # rounding drift. The exact identity is pinned in tests/core.
    assert abs(signed - body["eval_material_cp"]) < 2.0


def test_an_unparseable_fen_is_a_400(tmp_path: Path, monkeypatch) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"))
    use_engine(monkeypatch, ScriptedEngine())
    res = client.post("/api/dev/piece-values", json={"fen": "not a fen at all"})
    assert res.status_code == 400
    assert "FEN" in res.json()["detail"]


def test_an_illegal_position_is_a_400_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"))
    use_engine(monkeypatch, ScriptedEngine())
    # Kings adjacent — a position that yields plausible-looking nonsense.
    res = client.post(
        "/api/dev/piece-values", json={"fen": "8/1P6/P7/8/8/8/6k1/6K1 w - - 0 1"}
    )
    assert res.status_code == 400
    assert "not legal" in res.json()["detail"]


def test_a_missing_engine_is_a_503_with_an_actionable_message(
    tmp_path: Path, monkeypatch
) -> None:
    from chess_vol.engine import EngineNotFoundError

    @contextmanager
    def factory():
        raise EngineNotFoundError("no stockfish here")
        yield  # pragma: no cover

    monkeypatch.setattr(piecevalues_api, "ENGINE_FACTORY", factory)
    client = client_for(create_app(tmp_path / "trainer.db"))

    res = client.post("/api/dev/piece-values", json={"fen": START})
    assert res.status_code == 503
    assert "STOCKFISH_PATH" in res.json()["detail"]


def test_depth_is_bounded(tmp_path: Path, monkeypatch) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"))
    use_engine(monkeypatch, ScriptedEngine())
    assert client.post(
        "/api/dev/piece-values", json={"fen": START, "depth": 99}
    ).status_code == 422
    assert client.post(
        "/api/dev/piece-values", json={"fen": START, "depth": 1}
    ).status_code == 422


def test_relocation_is_off_by_default_and_bounded(tmp_path: Path, monkeypatch) -> None:
    client = client_for(create_app(tmp_path / "trainer.db"))
    engine = ScriptedEngine()
    use_engine(monkeypatch, engine)

    body = client.post("/api/dev/piece-values", json={"fen": START, "depth": 6}).json()
    assert all(p["relocation"] is None for p in body["pieces"])
    assert client.post(
        "/api/dev/piece-values", json={"fen": START, "relocate_top_k": 99}
    ).status_code == 422
