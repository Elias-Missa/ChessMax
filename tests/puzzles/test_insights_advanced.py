"""Advanced Insights: Tier 3, volatility steering, C.5 practice flags."""

from __future__ import annotations

import json
import sqlite3

import pytest

from server import db
from server.insights_metrics import (
    compute_run_trend,
    compute_tier1_metrics,
    persist_practice_flags,
)
from server.mistakes_run import _start_run
from datetime import datetime, timezone


@pytest.fixture
def connection(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "adv.db")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.commit()
    return conn


def _seed(
    connection: sqlite3.Connection,
    *,
    game_id: str,
    review_id: str,
    result: str,
    played_at: str,
    moves: list[dict],
    accuracy: float = 70.0,
) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    pgn = (
        f'[Event "Live"]\n[White "alice"]\n[Black "bob"]\n[Result "{result}"]\n'
        f'[UTCTime "{played_at[11:19]}"]\n\n1. e4 e5 *\n'
    )
    connection.execute(
        "INSERT INTO games (game_id, source, pgn, white_name, black_name, "
        "white_rating, black_rating, result, played_at) "
        "VALUES (?, 'chesscom', ?, 'alice', 'bob', 1500, 1500, ?, ?)",
        (game_id, pgn, result, played_at),
    )
    connection.execute(
        "INSERT INTO reviews (review_id, user_id, game_id, user_color, depth_tier, "
        "status, progress, accuracy, total_loss, loss_type) "
        "VALUES (?, ?, ?, 'white', 'shallow', 'complete', 1, ?, 30, 'cliff')",
        (review_id, user_id, game_id, accuracy),
    )
    for m in moves:
        detail = {
            "fen_before": m.get("fen", "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"),
            "fen_after": "x",
            "move_uci": m["uci"],
            "eval_cp": 20,
            "top_lines": m.get(
                "lines",
                [
                    {"uci": "e2e4", "san": "e4", "eval_cp": 40},
                    {"uci": "d2d4", "san": "d4", "eval_cp": 30},
                ],
            ),
        }
        connection.execute(
            "INSERT INTO review_moves ("
            "review_id, ply, san, is_user_move, phase, is_book, win_prob, delta_w, "
            "volatility, findability, detail"
            ") VALUES (?, ?, ?, 1, 'middlegame', 0, ?, ?, ?, ?, ?)",
            (
                review_id,
                m["ply"],
                m["san"],
                m.get("wp", 0.5),
                m.get("dw", 5),
                m.get("vol", 40),
                m.get("findability"),
                json.dumps(detail),
            ),
        )
    connection.commit()


def test_tier3_tilt_and_after_loss(connection: sqlite3.Connection) -> None:
    # Session: win, loss, then another game shortly after (post-loss)
    _seed(
        connection,
        game_id="g1",
        review_id="r1",
        result="1-0",
        played_at="2026-06-01T12:00:00",
        moves=[{"ply": 1, "san": "e4", "uci": "e2e4", "vol": 20}],
        accuracy=80,
    )
    _seed(
        connection,
        game_id="g2",
        review_id="r2",
        result="0-1",
        played_at="2026-06-01T12:30:00",
        moves=[{"ply": 1, "san": "e4", "uci": "e2e4", "vol": 30}],
        accuracy=50,
    )
    _seed(
        connection,
        game_id="g3",
        review_id="r3",
        result="0-1",
        played_at="2026-06-01T13:00:00",
        moves=[{"ply": 1, "san": "e4", "uci": "e2e4", "vol": 25}],
        accuracy=55,
    )
    metrics = compute_tier1_metrics(connection, review_ids=["r1", "r2", "r3"])
    t3 = metrics["tier3"]
    assert t3["sessions"] == 1
    assert t3["after_loss"]["n"] == 1
    assert len(t3["by_session_index"]) >= 2


def test_volatility_steering(connection: sqlite3.Connection) -> None:
    _seed(
        connection,
        game_id="gs",
        review_id="rs",
        result="1-0",
        played_at="2026-06-01T15:00:00",
        moves=[
            {
                "ply": 1,
                "san": "e4",
                "uci": "e2e4",
                "vol": 20,
                "lines": [
                    {"uci": "e2e4", "san": "e4", "eval_cp": 40},
                    {"uci": "d2d4", "san": "d4", "eval_cp": 35},
                ],
            },
            {
                "ply": 3,
                "san": "d4",
                "uci": "d2d4",
                "vol": 70,
                "lines": [
                    {"uci": "e2e4", "san": "e4", "eval_cp": 40},
                    {"uci": "d2d4", "san": "d4", "eval_cp": 35},
                ],
            },
        ],
    )
    metrics = compute_tier1_metrics(connection, review_ids=["rs"])
    steer = metrics["volatility_steering"]
    assert steer["n_best"] == 1
    assert steer["n_alt"] == 1
    assert steer["mean_vol_played_best"] == 20
    assert steer["mean_vol_played_alt"] == 70
    assert steer["note"]


def test_practice_flags_and_persist(connection: sqlite3.Connection) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    _seed(
        connection,
        game_id="gp",
        review_id="rp",
        result="0-1",
        played_at="2026-06-01T16:00:00",
        moves=[
            {
                "ply": 5,
                "san": "Qh5",
                "uci": "d1h5",
                "dw": 30,
                "vol": 65,
                "findability": 75,
                "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                "lines": [
                    {"uci": "g1f3", "san": "Nf3", "eval_cp": 40},
                    {"uci": "d1h5", "san": "Qh5", "eval_cp": -80},
                ],
            },
            {
                "ply": 7,
                "san": "a3",
                "uci": "a2a3",
                "dw": 5,
                "vol": 20,
                "findability": 80,
            },
        ],
    )
    metrics = compute_tier1_metrics(connection, review_ids=["rp"])
    pf = metrics["practice_flags"]
    assert pf["count"] == 1
    assert pf["items"][0]["san"] == "Qh5"
    assert pf["items"][0]["findability"] == 75

    connection.execute(
        "INSERT INTO insight_runs (run_id, user_id, chesscom_handle, window_days, "
        "time_class, status) VALUES ('run1', ?, 'alice', 7, 'blitz', 'complete')",
        (user_id,),
    )
    connection.commit()
    mistake_run = _start_run(
        connection, user_id, "alice", datetime(2026, 5, 1, tzinfo=timezone.utc)
    )
    n = persist_practice_flags(
        connection,
        run_id="run1",
        user_id=user_id,
        practice=pf,
        mistake_run_id=mistake_run,
    )
    assert n == 1
    row = connection.execute(
        "SELECT * FROM insight_flags WHERE run_id = 'run1'"
    ).fetchone()
    assert row is not None
    assert row["puzzle_id"] is not None
    puzzle = connection.execute(
        "SELECT * FROM mistake_puzzles WHERE id = ?", (row["puzzle_id"],)
    ).fetchone()
    assert puzzle is not None
    assert puzzle["best_move"] == "g1f3"


def test_compute_run_trend_highlights_fixable_drop(connection: sqlite3.Connection) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    prior_metrics = {
        "total_loss": 400.0,
        "fixable_loss": 120.0,
        "tier2": {
            "phase_attribution": [
                {"phase": "endgame", "delta_w_per_move": 4.0, "moves": 10},
            ]
        },
    }
    connection.execute(
        """
        INSERT INTO insight_runs (
            run_id, user_id, chesscom_handle, source, window_days, time_class,
            status, games_analyzed, metrics, created_at
        ) VALUES (
            'old', ?, 'alice', 'chesscom', 30, 'blitz', 'complete', 40, ?,
            '2026-05-01 00:00:00'
        )
        """,
        (user_id, json.dumps(prior_metrics)),
    )
    connection.execute(
        """
        INSERT INTO insight_runs (
            run_id, user_id, chesscom_handle, source, window_days, time_class,
            status, games_analyzed, created_at
        ) VALUES (
            'new', ?, 'alice', 'chesscom', 30, 'blitz', 'running', 42,
            '2026-06-01 00:00:00'
        )
        """,
        (user_id,),
    )
    connection.commit()

    current = {
        "total_loss": 350.0,
        "fixable_loss": 90.0,
        "tier2": {
            "phase_attribution": [
                {"phase": "endgame", "delta_w_per_move": 2.5, "moves": 12},
            ]
        },
    }
    trend = compute_run_trend(
        connection,
        user_id=user_id,
        run_id="new",
        handle="alice",
        window_days=30,
        time_class="blitz",
        source="chesscom",
        current_metrics=current,
    )
    assert trend is not None
    assert trend["previous_run_id"] == "old"
    assert trend["fixable_loss"]["delta"] == pytest.approx(-30.0)
    assert any("Fixable loss dropped" in h for h in trend["highlights"])
    assert "endgame" in trend["phase_delta_w_per_move"]


def test_compute_run_trend_none_without_prior(connection: sqlite3.Connection) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    assert (
        compute_run_trend(
            connection,
            user_id=user_id,
            run_id="solo",
            handle="alice",
            window_days=7,
            time_class="blitz",
            current_metrics={"fixable_loss": 10},
        )
        is None
    )
