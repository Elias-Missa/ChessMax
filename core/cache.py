"""Zobrist-keyed SQLite cache for findability feature vectors (spec §3.5).

MultiPV-8 at fixed nodes across ~70 positions is the review's bottleneck; Maia
forward passes are cheap. So we cache the **feature vector** (``d_star`` per
move, ``forc``, ``narr``, ``q``, ``delta_w``, and ``pi_r`` for every rating
band), *not just the score* — Phase 3 refits the constants repeatedly and must
never pay the engine cost twice for the same position.

Keys are ``(zobrist_hash, params_fingerprint)``: the position identity plus an
opaque fingerprint of the engine/search parameters, so a cache built at one
``multipv``/``nodes`` setting is never mistaken for another.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import chess
import chess.polyglot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findability_features (
    zobrist    TEXT NOT NULL,
    params     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (zobrist, params)
);
"""


def zobrist_key(board: chess.Board) -> str:
    """Stable string key for a position (128-bit Zobrist hash as text).

    Text rather than INTEGER because the hash can exceed SQLite's 64-bit signed
    INTEGER range.
    """
    return str(chess.polyglot.zobrist_hash(board))


class FeatureCache:
    """Thin SQLite key-value store for feature vectors.

    Use as a context manager, or call :meth:`close` yourself::

        with FeatureCache("data/findability_cache.db") as cache:
            hit = cache.get(board, params_key)
            if hit is None:
                hit = expensive_extract(board)
                cache.put(board, params_key, hit)
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self) -> "FeatureCache":
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def get(self, board: chess.Board, params_key: str) -> dict[str, Any] | None:
        """Return the cached feature vector for ``board`` under ``params_key``, or ``None``."""
        row = self._conn.execute(
            "SELECT payload FROM findability_features WHERE zobrist = ? AND params = ?",
            (zobrist_key(board), params_key),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def put(self, board: chess.Board, params_key: str, payload: dict[str, Any]) -> None:
        """Insert or replace the feature vector for ``board`` under ``params_key``."""
        self._conn.execute(
            "INSERT OR REPLACE INTO findability_features (zobrist, params, payload) VALUES (?, ?, ?)",
            (zobrist_key(board), params_key, json.dumps(payload, separators=(",", ":"))),
        )
        self._conn.commit()

    def __len__(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM findability_features").fetchone()[0]
        )


__all__ = ["FeatureCache", "zobrist_key"]
