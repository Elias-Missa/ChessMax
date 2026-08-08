"""B.3: feature-vector persistence + constants_version recompute (no engine)."""

from __future__ import annotations

import json
import sqlite3

import chess
import pytest

from core.features import MoveEval
from core.findability import FindabilityConstants
from server import db
from server.findability_features import (
    build_feature_payload,
    constants_version,
    recompute_review_findability,
    score_from_payload,
)


def _uniform_policy(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
    n = max(1, len(moves))
    return {m: 1.0 / n for m in moves}


def _payload() -> dict:
    board = chess.Board()
    moves = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")]
    evals = [
        MoveEval(move=moves[0], cp=30, pv=[moves[0]], d_star=1),
        MoveEval(move=moves[1], cp=20, pv=[moves[1]], d_star=2),
    ]
    consts = FindabilityConstants.load()
    return build_feature_payload(
        board.fen(),
        evals,
        _uniform_policy,
        consts,
        volatility=40.0,
        m_star=moves[0],
    )


def test_constants_version_stable() -> None:
    a = constants_version()
    b = constants_version()
    assert a == b
    assert len(a) == 12


def test_score_from_payload_matches_roundtrip() -> None:
    payload = _payload()
    consts = FindabilityConstants.load()
    result = score_from_payload(payload, consts, user_rating=1500)
    assert result is not None
    assert 0 <= result.score <= 100
    assert "pi_r" in payload
    assert "1100" in payload["pi_r"] or str(consts.rating_grid[0]) in payload["pi_r"]


def test_recompute_updates_when_version_stale(tmp_path) -> None:
    conn = db.connect(tmp_path / "f.db")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.execute(
        "INSERT INTO games (game_id, source, pgn) VALUES ('g1', 'pgn', '1. e4 e5')"
    )
    conn.execute(
        """
        INSERT INTO reviews (
            review_id, user_id, game_id, user_color, user_rating, depth_tier,
            status, progress, constants_version, fixable_loss
        ) VALUES ('r1', 1, 'g1', 'white', 1500, 'full', 'complete', 1, 'stale-version', 0)
        """
    )
    payload = _payload()
    detail = json.dumps({"findability_features": payload})
    conn.execute(
        """
        INSERT INTO review_moves (
            review_id, ply, san, is_user_move, phase, delta_w, findability, detail
        ) VALUES ('r1', 1, 'e4', 1, 'opening', 20.0, 10, ?)
        """,
        (detail,),
    )
    conn.commit()

    changed = recompute_review_findability(conn, "r1")
    assert changed is True
    row = conn.execute(
        "SELECT findability, findability_personal, r_find FROM review_moves WHERE review_id='r1'"
    ).fetchone()
    assert row["findability"] is not None
    assert row["findability"] != 10  # refreshed from features
    rev = conn.execute(
        "SELECT constants_version, fixable_loss FROM reviews WHERE review_id='r1'"
    ).fetchone()
    assert rev["constants_version"] == constants_version()
    # Second call is a no-op
    assert recompute_review_findability(conn, "r1") is False
