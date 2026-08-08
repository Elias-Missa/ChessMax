"""Zobrist position_cache hit/miss around CachingEngine."""

from __future__ import annotations

from typing import Any

import chess
import pytest

from server import db
from server.position_cache import CachingEngine, get_features, put_features
from tests.vol.conftest import FakeEngine, make_info


def _producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    moves = list(board.legal_moves)[: max(1, multipv)]
    return [
        make_info(50 - i * 10, multipv=i + 1, pv=[m], turn=board.turn)
        for i, m in enumerate(moves)
    ]


def test_put_get_roundtrip(tmp_path) -> None:
    conn = db.connect(tmp_path / "c.db")
    board = chess.Board()
    features = {
        "lines": [
            {"multipv": 1, "cp": 20, "mate": None, "pv": ["e2e4"]},
        ]
    }
    put_features(conn, board, depth=12, multipv=3, features=features)
    got = get_features(conn, board, depth=12, multipv=3)
    assert got == features
    assert get_features(conn, board, depth=14, multipv=3) is None


def test_caching_engine_hit_miss(tmp_path) -> None:
    conn = db.connect(tmp_path / "c.db")
    inner = FakeEngine(producer=_producer)
    cached = CachingEngine(inner, conn)
    board = chess.Board()

    first = cached.analyse(board, depth=10, multipv=2)
    assert cached.misses == 1
    assert cached.hits == 0
    assert len(first) >= 1
    calls = len(inner.calls)

    second = cached.analyse(board, depth=10, multipv=2)
    assert cached.hits == 1
    assert cached.misses == 1
    assert len(inner.calls) == calls  # no second engine call
    assert second[0]["pv"][0].uci() == first[0]["pv"][0].uci()
