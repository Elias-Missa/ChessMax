from __future__ import annotations

import chess

from core.cache import FeatureCache, zobrist_key


def test_zobrist_key_stable_and_distinct() -> None:
    a = chess.Board()
    b = chess.Board()
    assert zobrist_key(a) == zobrist_key(b)
    b.push_san("e4")
    assert zobrist_key(a) != zobrist_key(b)


def test_put_get_roundtrip() -> None:
    with FeatureCache() as cache:
        board = chess.Board()
        payload = {"d_star": {"e2e4": 1}, "delta_w": {"e2e4": 0.0}}
        assert cache.get(board, "p1") is None
        cache.put(board, "p1", payload)
        assert cache.get(board, "p1") == payload
        assert len(cache) == 1


def test_params_key_isolates_entries() -> None:
    with FeatureCache() as cache:
        board = chess.Board()
        cache.put(board, "multipv8-nodes2p5M", {"v": 1})
        cache.put(board, "multipv6-nodes1M", {"v": 2})
        assert cache.get(board, "multipv8-nodes2p5M") == {"v": 1}
        assert cache.get(board, "multipv6-nodes1M") == {"v": 2}
        assert len(cache) == 2


def test_replace_overwrites() -> None:
    with FeatureCache() as cache:
        board = chess.Board()
        cache.put(board, "p", {"v": 1})
        cache.put(board, "p", {"v": 2})
        assert cache.get(board, "p") == {"v": 2}
        assert len(cache) == 1
