"""Narrative layer over stored Insights metrics — engine-free."""

from __future__ import annotations

import json
import sqlite3

from server import db
from server.insights_metrics import compute_tier1_metrics
from server.insights_narrative import (
    BANNED_JARGON,
    build_narrative,
    contains_jargon,
    ensure_narrative,
    _short_opening,
)


def _seed_game(
    connection: sqlite3.Connection,
    *,
    game_id: str,
    review_id: str,
    result: str,
    played_at: str,
    moves: list[dict],
    user_color: str = "white",
    accuracy: float = 70.0,
    opening_name: str = "Caro-Kann Defense",
    eco: str = "B12",
    loss_type: str = "bleed",
    white_rating: int = 1500,
    black_rating: int = 1500,
) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    pgn = (
        f'[Event "Live"]\n[White "alice"]\n[Black "bob"]\n[Result "{result}"]\n'
        f'[UTCTime "{played_at[11:19]}"]\n\n1. e4 e5 *\n'
    )
    connection.execute(
        "INSERT INTO games (game_id, source, pgn, white_name, black_name, "
        "white_rating, black_rating, result, played_at, eco, opening_name) "
        "VALUES (?, 'chesscom', ?, 'alice', 'bob', ?, ?, ?, ?, ?, ?)",
        (game_id, pgn, white_rating, black_rating, result, played_at, eco, opening_name),
    )
    connection.execute(
        "INSERT INTO reviews (review_id, user_id, game_id, user_color, depth_tier, "
        "status, progress, accuracy, total_loss, loss_type) "
        "VALUES (?, ?, ?, ?, 'shallow', 'complete', 1, ?, 30, ?)",
        (review_id, user_id, game_id, user_color, accuracy, loss_type),
    )
    for m in moves:
        detail = {
            "fen_before": m.get(
                "fen",
                "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            ),
            "move_uci": m.get("uci", "e2e4"),
            "top_lines": m.get("lines", [{"uci": "d8h4", "san": "Qh4+", "eval_cp": 40}]),
        }
        connection.execute(
            "INSERT INTO review_moves ("
            "review_id, ply, san, is_user_move, phase, is_book, classification, "
            "win_prob, delta_w, volatility, findability, time_spent, "
            "clock_remaining, detail, tactic_tags"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review_id,
                m["ply"],
                m.get("san", "e4"),
                1 if m.get("is_user", True) else 0,
                m.get("phase", "middlegame"),
                1 if m.get("is_book") else 0,
                m.get("classification"),
                m.get("wp", 0.5),
                m.get("dw", 2.0),
                m.get("vol", 40),
                m.get("findability"),
                m.get("time_spent"),
                m.get("clock"),
                json.dumps(detail),
                json.dumps(m["tags"]) if m.get("tags") else None,
            ),
        )
    connection.commit()


def _connection(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "narr.db")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.commit()
    return conn


def _window(connection: sqlite3.Connection) -> None:
    """Ten games: 4 wins, 1 draw, 5 losses with a clear conversion+clock story."""

    def moves(*, peak: float, clock: float | None, tags: list[str] | None = None, dw: float = 30.0):
        return [
            {"ply": 1, "san": "e4", "dw": 0.0, "wp": 0.52, "phase": "opening",
             "is_book": True, "classification": "book", "vol": 20},
            {"ply": 11, "san": "Qh5", "dw": 4.0, "wp": peak, "phase": "middlegame",
             "classification": "good", "vol": 45, "clock": 90},
            {"ply": 31, "san": "Qxf7", "dw": dw, "wp": 0.22, "phase": "middlegame",
             "classification": "blunder", "vol": 70, "clock": clock,
             "tags": tags or ["fork"], "san": "Qxf7"},
        ]

    # Wins as White in the London.
    for i in range(4):
        _seed_game(
            connection,
            game_id=f"w-{i}",
            review_id=f"wr-{i}",
            result="1-0",
            user_color="white",
            played_at=f"2026-06-0{i + 1}T12:00:00",
            opening_name="London System",
            eco="D02",
            loss_type="bleed",
            moves=moves(peak=0.72, clock=80, dw=2.0),
        )
    # Draw.
    _seed_game(
        connection,
        game_id="d-0",
        review_id="dr-0",
        result="1/2-1/2",
        user_color="white",
        played_at="2026-06-05T12:00:00",
        opening_name="London System",
        eco="D02",
        loss_type="bleed",
        moves=moves(peak=0.55, clock=40, dw=8.0),
    )
    # Losses as Black in the Caro: were winning, then flagged.
    for i in range(5):
        _seed_game(
            connection,
            game_id=f"l-{i}",
            review_id=f"lr-{i}",
            result="1-0",  # user is black → loss
            user_color="black",
            played_at=f"2026-06-1{i}T12:00:00",
            opening_name="Caro-Kann Defense",
            eco="B12",
            loss_type="converted_then_lost",
            moves=moves(peak=0.88, clock=6, dw=32.0, tags=["fork"]),
        )


def test_narrative_attaches_to_metrics(tmp_path) -> None:
    connection = _connection(tmp_path)
    _window(connection)
    ids = [r["review_id"] for r in connection.execute("SELECT review_id FROM reviews")]
    metrics = compute_tier1_metrics(connection, review_ids=ids)
    narr = metrics["narrative"]
    assert narr["sufficiency"]["ok"] is True
    assert narr["verdict"]["record"]["losses"] == 5
    assert narr["verdict"]["record"]["games"] == 10
    assert "lost 5" in narr["verdict"]["headline"]
    assert narr["why_you_lose"]["funnel"][0]["id"] == "games"
    assert narr["why_you_lose"]["funnel"][1]["id"] == "losses"
    assert any(s["id"] == "were_winning" for s in narr["why_you_lose"]["funnel"])
    assert narr["spine"]["all"]
    assert len(narr["spine"]["all"]) == 20
    assert narr["how_you_win"]["fixes"]


def test_no_jargon_reaches_the_narrative_tabs(tmp_path) -> None:
    connection = _connection(tmp_path)
    _window(connection)
    ids = [r["review_id"] for r in connection.execute("SELECT review_id FROM reviews")]
    metrics = compute_tier1_metrics(connection, review_ids=ids)
    hits = contains_jargon(metrics["narrative"])
    assert hits == [], hits
    blob = json.dumps(metrics["narrative"]["verdict"]) + json.dumps(
        metrics["narrative"]["why_you_lose"]
    ) + json.dumps(metrics["narrative"]["how_you_win"])
    for token in BANNED_JARGON:
        assert token.lower() not in blob.lower(), token


def test_opening_matrix_splits_by_colour(tmp_path) -> None:
    connection = _connection(tmp_path)
    _window(connection)
    ids = [r["review_id"] for r in connection.execute("SELECT review_id FROM reviews")]
    narr = compute_tier1_metrics(connection, review_ids=ids)["narrative"]
    black = narr["why_you_lose"]["openings"]["black"]
    assert black
    caro = next(r for r in black if "Caro" in r["opening"])
    assert caro["losses"] == 5
    assert caro["loss_pct"] == 1.0


def test_loss_type_lands_on_game_facts(tmp_path) -> None:
    connection = _connection(tmp_path)
    _window(connection)
    ids = [r["review_id"] for r in connection.execute("SELECT review_id FROM reviews")]
    facts = compute_tier1_metrics(connection, review_ids=ids)["game_explorer"]
    lost = [f for f in facts if f["outcome"] == "loss"]
    assert lost
    assert all(f.get("loss_type") == "converted_then_lost" for f in lost)


def test_ensure_narrative_is_idempotent() -> None:
    empty = ensure_narrative({"pro": {}, "game_explorer": []})
    again = ensure_narrative(empty)
    assert again is empty
    assert empty["narrative"]["sufficiency"]["ok"] is False


def test_ensure_narrative_rebuilds_old_schema() -> None:
    metrics = {
        "pro": {"headline": {"record": {"games": 0, "wins": 0, "draws": 0, "losses": 0}}},
        "game_explorer": [],
        "narrative": {
            "verdict": {"headline": {"sentence": "old 4.0 shape", "losses": 46}},
            "why_you_lose": {"funnel": {"available": True, "records": []}},
        },
    }
    out = ensure_narrative(metrics)
    assert out["narrative"]["schema"] == "story-3"
    assert isinstance(out["narrative"]["verdict"]["headline"], str)


def test_empty_metrics_still_have_a_story() -> None:
    narr = build_narrative({})
    assert narr["verdict"]["headline"]
    assert narr["schema"] == "story-3"
    assert narr["sufficiency"]["ok"] is False
    assert contains_jargon(narr) == []


def test_short_opening_drops_generic_prefix() -> None:
    assert (
        _short_opening("Queens Pawn Opening Zukertort Chigorin Variation")
        == "Zukertort Chigorin Variation"
    )
    long_name = "Queens Pawn Opening Accelerated London System with an extra clause"
    out = _short_opening(long_name, limit=28)
    assert "Queens Pawn Opening" not in out
    assert len(out) <= 29  # 28 + ellipsis
    assert _short_opening("") == "Unknown opening"


def test_conversion_diagnosis_does_not_name_an_opening(tmp_path) -> None:
    connection = _connection(tmp_path)
    _window(connection)
    ids = [r["review_id"] for r in connection.execute("SELECT review_id FROM reviews")]
    diagnosis = compute_tier1_metrics(connection, review_ids=ids)["narrative"]["verdict"]["diagnosis"]
    assert "especially as" not in diagnosis
    assert "Zukertort" not in diagnosis
    assert "London" not in diagnosis
