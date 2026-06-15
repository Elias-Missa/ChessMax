"""Tests for :mod:`chess_vol.server`.

Mirrors the monkey-patching approach in :mod:`tests.test_cli`: we swap
:data:`chess_vol.server.ENGINE_FACTORY` for one yielding :class:`FakeEngine`,
so no real Stockfish is launched. The full SSE event stream is consumed
synchronously via :class:`httpx.Client` (which ``TestClient`` wraps).

Covers the Phase 3 endpoints (see README §8):

* ``GET /`` serves the frontend if it's on disk.
* ``POST /analyze/fen`` for shallow and deep.
* ``POST /analyze/pgn`` streams ``start`` -> N x ``ply`` -> ``done``.
* Bad input / invalid FEN paths.
* CORS allows ``https://www.chess.com``.
* Engine-not-found surfaces as 503 (fen) / SSE error (pgn).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import chess
import pytest
from fastapi.testclient import TestClient

from chess_vol import server as server_mod
from chess_vol.engine import EngineNotFoundError
from chess_vol.server import app

from .conftest import FakeEngine, evals_to_infos, load_pgn

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _flat_producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    evals = [50, 30, 10, -10, -30, -50][:multipv]
    moves = list(board.legal_moves)[:multipv]
    return evals_to_infos(evals, turn=board.turn, moves=moves)


@contextmanager
def _fake_factory(engine: FakeEngine) -> Iterator[FakeEngine]:
    yield engine


@pytest.fixture
def patch_engine(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Monkeypatch :data:`server.ENGINE_FACTORY` to yield a chosen ``FakeEngine``."""

    def _install(engine: FakeEngine) -> FakeEngine:
        def factory() -> Any:
            return _fake_factory(engine)

        monkeypatch.setattr(server_mod, "ENGINE_FACTORY", factory)
        return engine

    return _install


@pytest.fixture
def client() -> TestClient:
    # ``raise_server_exceptions=False`` lets us observe 5xx paths like the
    # engine-not-found case without the test runner rethrowing.
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# Helpers: SSE parsing                                                        #
# --------------------------------------------------------------------------- #


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a server-sent events body into ``[(event, json_payload), ...]``.

    Handles both ``\\n\\n`` and ``\\r\\n\\r\\n`` event delimiters — sse-starlette
    emits the latter, the spec allows either.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    normalized = text.replace("\r\n", "\n")
    for chunk in normalized.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in chunk.splitlines():
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        events.append((event, payload))
    return events


# --------------------------------------------------------------------------- #
# Static frontend                                                              #
# --------------------------------------------------------------------------- #


class TestStaticFrontend:
    def test_index_served_by_combined_app(self) -> None:
        # Static frontend serving moved to the combined ChessMax app
        # (server.main); the vol app is API-only. The merged index.html
        # contains both apps, including the vol markup.
        from server.main import create_app as create_combined_app

        combined = TestClient(create_combined_app(db_path=":memory:"))
        resp = combined.get("/")
        assert resp.status_code == 200
        assert "ChessMax" in resp.text
        assert "volBoard" in resp.text

    def test_healthz(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# /analyze/fen                                                                #
# --------------------------------------------------------------------------- #


class TestAnalyzeFen:
    def test_shallow_basic(self, client: TestClient, patch_engine: Any) -> None:
        engine = patch_engine(FakeEngine(producer=_flat_producer))
        resp = client.post("/analyze/fen", json={"fen": chess.STARTING_FEN})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["mode"] == "shallow"
        assert data["fen"].startswith("rnbqkbnr/")
        vol = data["volatility"]
        required = {
            "score",
            "raw_cp",
            "local_raw_cp",
            "best_eval_cp",
            "alt_evals_cp",
            "scale",
            "decided",
            "reason",
            "recurse_depth_used",
            "analyses",
            "color",
        }
        assert required <= set(vol.keys())
        assert vol["recurse_depth_used"] == 0
        assert vol["analyses"] == 1
        assert engine.call_count == 1

    def test_deep_sets_recurse_depth_two(self, client: TestClient, patch_engine: Any) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        resp = client.post(
            "/analyze/fen",
            json={"fen": chess.STARTING_FEN, "deep": True, "recurse_k": 2},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["mode"] == "deep"
        assert data["params"]["recurse_depth"] == 2
        assert data["volatility"]["recurse_depth_used"] == 2

    def test_invalid_fen_400(self, client: TestClient, patch_engine: Any) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        resp = client.post("/analyze/fen", json={"fen": "not-a-fen"})
        assert resp.status_code == 400
        assert "invalid FEN" in resp.json()["detail"]

    def test_deep_and_recurse_zero_conflict(self, client: TestClient, patch_engine: Any) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        resp = client.post(
            "/analyze/fen",
            json={"fen": chess.STARTING_FEN, "deep": True, "recurse_depth": 0},
        )
        assert resp.status_code == 400

    def test_engine_not_found_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @contextmanager
        def raising_factory() -> Iterator[Any]:
            raise EngineNotFoundError("no stockfish here")
            yield  # pragma: no cover  (unreachable; keeps type a generator)

        monkeypatch.setattr(server_mod, "ENGINE_FACTORY", raising_factory)
        resp = client.post("/analyze/fen", json={"fen": chess.STARTING_FEN})
        assert resp.status_code == 503
        assert "no stockfish here" in resp.json()["detail"]

    def test_schema_rejects_missing_fen(self, client: TestClient) -> None:
        resp = client.post("/analyze/fen", json={})
        # Pydantic validation → 422.
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /analyze/pgn (SSE)                                                           #
# --------------------------------------------------------------------------- #


class TestAnalyzePgn:
    def test_streams_start_ply_done(self, client: TestClient, patch_engine: Any) -> None:
        engine = patch_engine(FakeEngine(producer=_flat_producer))
        pgn = load_pgn("sample_game")
        resp = client.post(
            "/analyze/pgn",
            json={"pgn": pgn, "max_plies": 4, "depth": 8, "multipv": 4},
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [e for e, _ in events]
        assert kinds[0] == "start"
        assert kinds.count("ply") == 4
        assert kinds[-1] == "done"

        start_payload = events[0][1]
        assert start_payload["mode"] == "shallow"

        # Each ply payload matches the expected schema.
        ply_events = [p for k, p in events if k == "ply"]
        for i, ev in enumerate(ply_events, start=1):
            assert ev["done"] == i
            assert ev["total"] == 4
            ply_json = ev["ply"]
            assert ply_json["ply"] == i
            assert {"san", "fen_before", "fen_after", "eval_cp", "volatility"} <= set(
                ply_json.keys()
            )

        done_payload = events[-1][1]
        assert done_payload["plies_analysed"] == 4
        assert done_payload["total_analyses"] == engine.call_count

    def test_deep_mode_label(self, client: TestClient, patch_engine: Any) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn = load_pgn("sample_game")
        resp = client.post(
            "/analyze/pgn",
            json={
                "pgn": pgn,
                "max_plies": 1,
                "deep": True,
                "recurse_k": 2,
            },
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        start = next(p for k, p in events if k == "start")
        assert start["mode"] == "deep"
        assert start["params"]["recurse_depth"] == 2

        done = next(p for k, p in events if k == "done")
        # Phase 1.5 budget for 1 ply with k=2, recurse_depth=2: 1 + 2 + 4 = 7.
        assert done["plies_analysed"] == 1
        assert done["total_analyses"] == 7

    def test_empty_pgn_emits_error(self, client: TestClient, patch_engine: Any) -> None:
        # `chess.pgn.read_game` returns ``None`` for an empty stream; the
        # analyzer turns that into a ValueError, which we surface as an
        # ``event: error`` with kind=bad_input.
        patch_engine(FakeEngine(producer=_flat_producer))
        resp = client.post("/analyze/pgn", json={"pgn": ""})
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [e for e, _ in events]
        assert "error" in kinds
        err = next(p for k, p in events if k == "error")
        assert err["kind"] == "bad_input"

    def test_moveless_pgn_completes_with_zero_plies(
        self, client: TestClient, patch_engine: Any
    ) -> None:
        # A syntactically-valid PGN with no moves should complete cleanly,
        # not raise — ``analyze_pgn`` returns an empty list.
        patch_engine(FakeEngine(producer=_flat_producer))
        resp = client.post("/analyze/pgn", json={"pgn": "not a pgn at all"})
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [e for e, _ in events]
        assert kinds == ["start", "done"]
        done = events[-1][1]
        assert done["plies_analysed"] == 0

    def test_engine_not_found_emits_error(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @contextmanager
        def raising_factory() -> Iterator[Any]:
            raise EngineNotFoundError("no stockfish here")
            yield  # pragma: no cover

        monkeypatch.setattr(server_mod, "ENGINE_FACTORY", raising_factory)
        pgn = load_pgn("sample_game")
        resp = client.post("/analyze/pgn", json={"pgn": pgn, "max_plies": 2})
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        err_events = [p for k, p in events if k == "error"]
        assert err_events
        assert err_events[0]["kind"] == "engine_not_found"


# --------------------------------------------------------------------------- #
# CORS                                                                         #
# --------------------------------------------------------------------------- #


class TestCors:
    def test_chess_com_preflight_allowed(self, client: TestClient) -> None:
        resp = client.options(
            "/analyze/fen",
            headers={
                "Origin": "https://www.chess.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert resp.status_code == 200
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert allow_origin in ("https://www.chess.com", "*")

    def test_localhost_origin_allowed(self, client: TestClient) -> None:
        resp = client.options(
            "/analyze/fen",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert resp.status_code == 200
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert allow_origin in ("http://localhost:5173", "*")
