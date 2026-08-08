"""Findability integration over an analyzed game (reuse mode + JSON shape)."""

from __future__ import annotations

import json
from typing import Any

import chess
import pytest
from fastapi.testclient import TestClient

import chess_vol.server as server_mod
from chess_vol.analyze import analyze_pgn
from chess_vol.cli_report import ply_to_json
from chess_vol.findability_review import attach_findability, move_evals_from_ply
from core.findability import FindabilityConstants

from .conftest import FakeEngine, evals_to_infos, load_pgn

CONSTANTS = FindabilityConstants()


def _producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    evals = [50, 30, 10, -10, -30, -50][:multipv]
    moves = list(board.legal_moves)[:multipv]
    return evals_to_infos(evals, turn=board.turn, moves=moves)


def _ramp_policy(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
    if not moves:
        return {}
    frac = (rating - 800) / (2600 - 800)
    out: dict[chess.Move, float] = {moves[0]: 0.10 + 0.70 * frac}
    for move in moves[1:]:
        out[move] = 0.15
    return out


def _analyze() -> list:
    pgn = load_pgn("sample_game")
    return analyze_pgn(pgn, FakeEngine(producer=_producer))


def test_json_findability_is_null_before_attach() -> None:
    results = _analyze()
    payloads = [ply_to_json(ply) for ply in results]
    assert all("findability" in p for p in payloads)
    assert all(p["findability"] is None for p in payloads)


def test_move_evals_from_ply_preserves_best_first() -> None:
    results = _analyze()
    # first non-book, non-terminal ply with >=2 lines
    ply = next(p for p in results if len(p.volatility.top_lines) >= 2)
    evals = move_evals_from_ply(ply)
    assert evals[0].move.uci() == ply.volatility.top_lines[0].uci
    assert all(e.pv for e in evals)  # each carries a parsed PV prefix


def test_attach_populates_scorable_plies() -> None:
    results = _analyze()
    attach_findability(results, _ramp_policy, CONSTANTS, user_rating=1200)

    scored = [p for p in results if p.findability is not None]
    assert scored, "expected at least one scorable ply"
    for ply in scored:
        f = ply.findability
        assert 0 <= f.score <= 100
        assert isinstance(f.band, str) and f.band
        c_a = [v for _, v in f.curve]
        assert all(b >= a - 1e-9 for a, b in zip(c_a, c_a[1:]))  # monotone
        assert f.r_find is None or 600 <= f.r_find <= 2600
        assert f.personal is not None  # user_rating supplied


def test_book_moves_are_gated_out() -> None:
    results = _analyze()
    attach_findability(results, _ramp_policy, CONSTANTS)
    # The Opera Game opens 1.e4 e5 2.Nf3 Nc6 3.Bb5 — flagged book, so skipped.
    book = [p for p in results if p.review is not None and p.review.classification == "book"]
    assert book
    assert all(p.findability is None for p in book)


def test_json_round_trips_findability() -> None:
    results = _analyze()
    attach_findability(results, _ramp_policy, CONSTANTS, user_rating=1500)
    scored = next(p for p in results if p.findability is not None)
    payload = ply_to_json(scored)["findability"]
    assert payload is not None
    assert set(payload) == {
        "score",
        "r_find",
        "band",
        "personal",
        "personal_star",
        "curve",
        "star_curve",
        "alternate",
        "forced",
    }
    assert isinstance(payload["curve"], list)
    assert all(len(pair) == 2 for pair in payload["curve"])


# --------------------------------------------------------------------------- #
# Server seam: opt-in findability via POLICY_FACTORY                            #
# --------------------------------------------------------------------------- #


def _parse_done(text: str) -> dict[str, Any]:
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in chunk.strip().splitlines()]
        if any(ln == "event: done" for ln in lines):
            data = "\n".join(ln[len("data:") :].strip() for ln in lines if ln.startswith("data:"))
            return json.loads(data)
    raise AssertionError("no done event in SSE stream")


def test_sse_endpoint_attaches_findability_when_policy_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /analyze/pgn seam populates findability when POLICY_FACTORY yields one."""

    def producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
        return _producer(board, depth, multipv)

    from contextlib import contextmanager

    @contextmanager
    def engine_factory():
        yield FakeEngine(producer=producer)

    monkeypatch.setattr(server_mod, "ENGINE_FACTORY", engine_factory)
    # Opt findability back in (the vol autouse fixture disables it by default).
    monkeypatch.setattr(server_mod, "POLICY_FACTORY", lambda: _ramp_policy)

    client = TestClient(server_mod.app)
    resp = client.post(
        "/analyze/pgn",
        json={"pgn": load_pgn("sample_game"), "max_plies": 10, "multipv": 4, "user_rating": 1300},
    )
    assert resp.status_code == 200
    done = _parse_done(resp.text)
    plies = done["plies"]
    scored = [p for p in plies if p["findability"] is not None]
    assert scored, "expected findability on at least one non-book ply"
    sample = scored[0]["findability"]
    assert 0 <= sample["score"] <= 100
    assert sample["personal"] is not None  # user_rating was supplied
