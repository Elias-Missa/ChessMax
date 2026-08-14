"""Shared Zobrist position cache for review engine MultiPV (Insights.md B.3)."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

import chess
import chess.engine
import chess.polyglot

from core.cache import zobrist_key

ENGINE_VERSION = "stockfish"
DEFAULT_MAIA_VERSION = ""


def params_nodes(depth: int, multipv: int) -> int:
    """Pack depth/multipv into the ``nodes`` column key space.

    Uses ``depth * 100 + multipv`` so different MultiPV budgets never collide.
    """

    return int(depth) * 100 + int(multipv)


def get_features(
    connection: sqlite3.Connection,
    board: chess.Board,
    *,
    depth: int,
    multipv: int,
    engine_version: str = ENGINE_VERSION,
    maia_version: str = DEFAULT_MAIA_VERSION,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT features FROM position_cache "
        "WHERE zobrist = ? AND engine_version = ? AND nodes = ? AND maia_version = ?",
        (zobrist_key(board), engine_version, params_nodes(depth, multipv), maia_version),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["features"])
    except (TypeError, json.JSONDecodeError):
        return None


def put_features(
    connection: sqlite3.Connection,
    board: chess.Board,
    *,
    depth: int,
    multipv: int,
    features: dict[str, Any],
    engine_version: str = ENGINE_VERSION,
    maia_version: str = DEFAULT_MAIA_VERSION,
) -> None:
    connection.execute(
        """
        INSERT INTO position_cache (zobrist, fen, engine_version, maia_version, nodes, features)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(zobrist, engine_version, maia_version, nodes) DO UPDATE SET
            fen=excluded.fen,
            features=excluded.features,
            created_at=CURRENT_TIMESTAMP
        """,
        (
            zobrist_key(board),
            board.fen(),
            engine_version,
            maia_version,
            params_nodes(depth, multipv),
            json.dumps(features, separators=(",", ":")),
        ),
    )
    connection.commit()


def infos_to_features(infos: list[dict[str, Any]], turn: chess.Color) -> dict[str, Any]:
    """Serialize python-chess analyse infos into a JSON-safe feature payload."""

    lines = []
    for info in infos:
        score = info.get("score")
        cp = None
        mate = None
        if score is not None:
            pov = score.pov(turn)
            if pov.is_mate():
                mate = pov.mate()
            else:
                cp = pov.score()
        pv = info.get("pv") or []
        lines.append({
            "multipv": int(info.get("multipv", 1)),
            "cp": cp,
            "mate": mate,
            "pv": [m.uci() if isinstance(m, chess.Move) else str(m) for m in pv],
        })
    return {"lines": lines}


def features_to_infos(features: dict[str, Any], turn: chess.Color) -> list[dict[str, Any]]:
    """Rebuild analyse-style info dicts from a cached feature payload."""

    infos: list[dict[str, Any]] = []
    for line in features.get("lines") or []:
        cp = line.get("cp")
        mate = line.get("mate")
        if mate is not None:
            # PovScore from side-to-move mate
            white_mate = mate if turn == chess.WHITE else -mate
            score = chess.engine.PovScore(chess.engine.Mate(white_mate), chess.WHITE)
        elif cp is not None:
            white_cp = cp if turn == chess.WHITE else -cp
            score = chess.engine.PovScore(chess.engine.Cp(white_cp), chess.WHITE)
        else:
            continue
        pv_moves = []
        for uci in line.get("pv") or []:
            try:
                pv_moves.append(chess.Move.from_uci(uci))
            except ValueError:
                break
        infos.append({
            "score": score,
            "multipv": int(line.get("multipv", 1)),
            "pv": pv_moves,
        })
    infos.sort(key=lambda info: int(info.get("multipv", 1)))
    return infos


class CachingEngine:
    """Engine proxy that reads/writes ``position_cache`` around ``analyse``."""

    def __init__(
        self,
        inner: Any,
        connection: sqlite3.Connection,
        *,
        engine_version: str = ENGINE_VERSION,
        maia_version: str = DEFAULT_MAIA_VERSION,
        lock: threading.Lock | None = None,
    ) -> None:
        self._inner = inner
        self._connection = connection
        self._engine_version = engine_version
        self._maia_version = maia_version
        # Reviews may walk a game on several engines at once; one SQLite
        # connection is shared, so cache reads and writes are serialised. The
        # engine call itself stays outside the lock — that is the slow part.
        # Proxies sharing a connection must be handed the *same* lock.
        self._lock = lock or threading.Lock()
        self.hits = 0
        self.misses = 0

    def analyse(
        self,
        board: chess.Board,
        depth: int = 18,
        multipv: int = 6,
    ) -> list[dict[str, Any]]:
        with self._lock:
            cached = get_features(
                self._connection,
                board,
                depth=depth,
                multipv=multipv,
                engine_version=self._engine_version,
                maia_version=self._maia_version,
            )
        if cached is not None:
            infos = features_to_infos(cached, board.turn)
            if infos:
                with self._lock:
                    self.hits += 1
                return infos
        with self._lock:
            self.misses += 1
        infos = self._inner.analyse(board, depth=depth, multipv=multipv)
        with self._lock:
            put_features(
                self._connection,
                board,
                depth=depth,
                multipv=multipv,
                features=infos_to_features(infos, board.turn),
                engine_version=self._engine_version,
                maia_version=self._maia_version,
            )
        return infos

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
