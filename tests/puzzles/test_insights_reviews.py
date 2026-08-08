"""Engine-free tests for Insights persistence, metrics, and review API."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import chess
import pytest

from server import db
from server.game_identity import pgn_san_hash, resolve_game_id
from server.insights_metrics import compute_tier1_metrics
from server.insights_run import run_insights
from server.reviews import (
    analyze_and_store,
    create_pending_review,
    find_review,
    upsert_game,
)
from tests.vol.conftest import FakeEngine, make_info

SHORT_PGN = """
[Event "Live Chess"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[WhiteElo "1500"]
[BlackElo "1480"]

1. e4 {[%clk 0:03:00]} e5 {[%clk 0:03:00]} 2. Nf3 {[%clk 0:02:50]} Nc6 {[%clk 0:02:55]} *
"""


def _producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    moves = list(board.legal_moves)[: max(1, multipv)]
    infos = []
    for i, move in enumerate(moves):
        infos.append(
            make_info(
                40 - i * 15,
                multipv=i + 1,
                pv=[move],
                turn=board.turn,
            )
        )
    return infos


@pytest.fixture
def connection(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "t.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.commit()
    return conn


def test_schema_has_insights_tables(connection: sqlite3.Connection) -> None:
    names = {
        r["name"]
        for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "games" in names
    assert "reviews" in names
    assert "review_moves" in names
    assert "insight_runs" in names
    assert "position_cache" in names
    insight_cols = {
        r["name"] for r in connection.execute("PRAGMA table_info(insight_runs)")
    }
    assert "source" in insight_cols


def test_upsert_and_shallow_review(connection: sqlite3.Connection) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    game_id = pgn_san_hash(SHORT_PGN)
    upsert_game(connection, game_id=game_id, source="pgn", pgn=SHORT_PGN, meta={})
    review_id = create_pending_review(
        connection,
        user_id=user_id,
        game_id=game_id,
        user_color="white",
        depth_tier="shallow",
        user_rating=1500,
    )
    engine = FakeEngine(producer=_producer)
    out = analyze_and_store(
        connection,
        review_id=review_id,
        pgn=SHORT_PGN,
        user_color="white",
        depth_tier="shallow",
        engine=engine,
    )
    assert out["review_id"] == review_id
    row = find_review(
        connection, user_id=user_id, game_id=game_id, depth_tier="shallow"
    )
    assert row is not None
    assert row["status"] == "complete"
    moves = connection.execute(
        "SELECT COUNT(*) AS n FROM review_moves WHERE review_id = ?", (review_id,)
    ).fetchone()
    assert moves["n"] == 4
    # Cache hit
    again = create_pending_review(
        connection,
        user_id=user_id,
        game_id=game_id,
        user_color="white",
        depth_tier="shallow",
    )
    assert again == review_id


def test_tier1_metrics_taxonomy(connection: sqlite3.Connection) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    game_id = "chesscom:test-metric"
    upsert_game(
        connection,
        game_id=game_id,
        source="chesscom",
        pgn=SHORT_PGN,
        meta={"white_result": "win", "black_result": "checkmated", "date": "2026-06-01"},
    )
    connection.execute(
        """
        INSERT INTO reviews (
            review_id, user_id, game_id, user_color, depth_tier, status,
            total_loss, loss_type, progress
        ) VALUES ('r1', ?, ?, 'white', 'shallow', 'complete', 40, 'cliff', 1)
        """,
        (user_id, game_id),
    )
    connection.execute(
        """
        INSERT INTO review_moves (
            review_id, ply, san, is_user_move, phase, win_prob, delta_w, volatility, time_spent
        ) VALUES
            ('r1', 1, 'e4', 1, 'opening', 0.55, 5, 20, 10),
            ('r1', 3, 'Nf3', 1, 'opening', 0.50, 30, 70, 3)
        """
    )
    connection.commit()
    metrics = compute_tier1_metrics(connection, review_ids=["r1"])
    assert metrics["total_loss"] == 40
    assert metrics["loss_taxonomy"]["counts"]["cliff"] == 1
    assert metrics["fixable_sample_size"] == 0
    assert metrics["time_vs_criticality"]["avg_time_high_vol"] == 3


def test_insights_run_incremental(connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    meta = {
        "user_color": "white",
        "opponent": "bob",
        "user_rating": 1500,
        "white_username": "alice",
        "black_username": "bob",
        "white_rating": 1500,
        "black_rating": 1480,
        "white_result": "win",
        "black_result": "checkmated",
        "url": "https://chess.com/game/live/99",
        "game_id": "uuid-99",
        "date": "2026-06-01",
        "time_class": "blitz",
        "end_time": int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()),
    }

    def fake_collect(*_a, **_k):
        return [(SHORT_PGN, meta)], False

    monkeypatch.setattr("server.insights_run.chesscom.collect_games", fake_collect)

    engine = FakeEngine(producer=_producer)
    out1 = run_insights(
        connection,
        user_id=user_id,
        username="alice",
        window_days=7,
        time_class="blitz",
        engine=engine,
        analysis_fn=None,
        run_id="run-a",
    )
    assert out1["status"] == "complete"
    assert out1["newly_analyzed"] == 1
    calls_after_first = len(engine.calls)

    out2 = run_insights(
        connection,
        user_id=user_id,
        username="alice",
        window_days=7,
        time_class="blitz",
        engine=engine,
        analysis_fn=None,
        run_id="run-b",
    )
    assert out2["cached"] == 1
    assert out2["newly_analyzed"] == 0
    assert len(engine.calls) == calls_after_first  # no re-analysis


def test_insights_run_lichess_source(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    meta = {
        "user_color": "white",
        "opponent": "bob",
        "user_rating": 1500,
        "url": "https://lichess.org/abcdefgh",
        "game_id": "abcdefgh",
        "date": "2026-06-01",
        "time_class": "blitz",
        "end_time": int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()),
    }

    def fake_collect(*_a, **_k):
        return [(SHORT_PGN, meta)], False

    monkeypatch.setattr("server.insights_run.lichess.collect_games", fake_collect)

    engine = FakeEngine(producer=_producer)
    out = run_insights(
        connection,
        user_id=user_id,
        username="alice",
        window_days=7,
        time_class="blitz",
        engine=engine,
        analysis_fn=None,
        run_id="run-lich",
        source="lichess",
    )
    assert out["status"] == "complete"
    row = connection.execute(
        "SELECT source, chesscom_handle FROM insight_runs WHERE run_id = ?",
        ("run-lich",),
    ).fetchone()
    assert row["source"] == "lichess"
    assert row["chesscom_handle"] == "alice"
    game = connection.execute("SELECT source, game_id FROM games").fetchone()
    assert game["source"] == "lichess"
    assert game["game_id"].startswith("lichess:")


def test_review_api_cache_hit(tmp_path) -> None:
    from tests.puzzles.test_mistakes_api import make_client
    import time

    client = make_client(tmp_path)
    analyzed: list[str] = []

    def fake_analyze(connection, **kwargs):
        analyzed.append(kwargs["review_id"])
        connection.execute(
            "UPDATE reviews SET status = 'complete', progress = 1 WHERE review_id = ?",
            (kwargs["review_id"],),
        )
        connection.commit()

    client.app.state.review_analyze_fn = fake_analyze

    first = client.post(
        "/api/review",
        json={"pgn": SHORT_PGN, "source": "pgn", "user_color": "white", "depth_tier": "full"},
    )
    assert first.status_code == 200, first.text
    rid = first.json()["review_id"]
    got = None
    for _ in range(40):
        got = client.get(f"/api/review/{rid}")
        if got.json().get("status") == "complete":
            break
        time.sleep(0.05)
    assert got is not None and got.json()["status"] == "complete"

    second = client.post(
        "/api/review",
        json={"pgn": SHORT_PGN, "source": "pgn", "user_color": "white", "depth_tier": "full"},
    )
    assert second.json()["cached"] is True
    assert second.json()["review_id"] == rid
    assert len(analyzed) == 1


def test_review_get_resumes_orphaned_pending(tmp_path) -> None:
    """GET on a stuck pending row (no live worker) re-queues analysis."""

    from tests.puzzles.test_mistakes_api import make_client
    import time

    client = make_client(tmp_path)
    analyzed: list[str] = []

    def fake_analyze(connection, **kwargs):
        analyzed.append(kwargs["review_id"])
        connection.execute(
            "UPDATE reviews SET status = 'complete', progress = 1 WHERE review_id = ?",
            (kwargs["review_id"],),
        )
        connection.commit()

    client.app.state.review_analyze_fn = fake_analyze

    # Seed an orphaned pending review without going through POST's worker.
    conn = db.connect(client.app.state.db_path)
    user_id = int(conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"])
    game_id = pgn_san_hash(SHORT_PGN)
    upsert_game(conn, game_id=game_id, source="pgn", pgn=SHORT_PGN, meta={})
    rid = create_pending_review(
        conn,
        user_id=user_id,
        game_id=game_id,
        user_color="white",
        depth_tier="shallow",
    )
    conn.close()

    got = None
    for _ in range(40):
        got = client.get(f"/api/review/{rid}")
        assert got.status_code == 200
        if got.json().get("status") == "complete":
            break
        time.sleep(0.05)
    assert got is not None and got.json()["status"] == "complete"
    assert rid in analyzed


def test_resolve_game_id_shared() -> None:
    a = resolve_game_id(
        source="chesscom",
        pgn=SHORT_PGN,
        meta={"game_id": "abc", "url": "https://chess.com/game/live/1"},
    )
    b = resolve_game_id(
        source="chesscom",
        pgn=SHORT_PGN,
        meta={"game_id": "abc", "url": "https://chess.com/game/live/1"},
    )
    assert a == b


def test_reviews_list_includes_pgn_and_get_has_moves(tmp_path) -> None:
    from tests.puzzles.test_mistakes_api import make_client
    import time

    client = make_client(tmp_path)

    def fake_analyze(connection, **kwargs):
        rid = kwargs["review_id"]
        # Minimal complete row + one move so Library/UI have something to show.
        connection.execute(
            "UPDATE reviews SET status = 'complete', progress = 1, accuracy = 80 "
            "WHERE review_id = ?",
            (rid,),
        )
        connection.execute(
            "INSERT OR REPLACE INTO review_moves ("
            "review_id, ply, san, is_user_move, phase, classification, win_prob, "
            "delta_w, volatility, detail"
            ") VALUES (?, 1, 'e4', 1, 'opening', 'best', 0.55, 0, 20, ?)",
            (rid, '{"fen_before":"start","fen_after":"x","move_uci":"e2e4","eval_cp":20}'),
        )
        connection.commit()

    client.app.state.review_analyze_fn = fake_analyze
    started = client.post(
        "/api/review",
        json={"pgn": SHORT_PGN, "source": "pgn", "user_color": "white", "depth_tier": "shallow"},
    )
    assert started.status_code == 200, started.text
    rid = started.json()["review_id"]
    for _ in range(40):
        got = client.get(f"/api/review/{rid}")
        if got.json().get("status") == "complete":
            break
        time.sleep(0.05)
    body = got.json()
    assert body["pgn"]
    assert body["moves"]
    listed = client.get("/api/reviews")
    assert listed.status_code == 200
    reviews = listed.json()["reviews"]
    assert any(r["review_id"] == rid for r in reviews)
    assert any(r.get("pgn") for r in reviews)
