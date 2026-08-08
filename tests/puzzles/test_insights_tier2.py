"""Unit tests for Tier 2 Insights metrics."""

from __future__ import annotations

import sqlite3

import pytest

from server import db
from server.insights_metrics import compute_tier1_metrics


@pytest.fixture
def connection(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "t2.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.commit()
    return conn


def _seed_game(
    connection: sqlite3.Connection,
    *,
    game_id: str,
    review_id: str,
    user_color: str = "white",
    result: str = "0-1",
    white_rating: int = 1500,
    black_rating: int = 1700,
    eco: str = "B20",
    played_at: str = "2026-06-01T18:30:00",
    moves: list[tuple],
) -> None:
    user_id = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    pgn = (
        '[Event "Live"]\n[White "alice"]\n[Black "bob"]\n'
        f'[Result "{result}"]\n[UTCTime "18:30:00"]\n\n1. e4 e5 *\n'
    )
    connection.execute(
        "INSERT INTO games (game_id, source, pgn, white_name, black_name, "
        "white_rating, black_rating, result, eco, played_at) "
        "VALUES (?, 'chesscom', ?, 'alice', 'bob', ?, ?, ?, ?, ?)",
        (game_id, pgn, white_rating, black_rating, result, eco, played_at),
    )
    connection.execute(
        "INSERT INTO reviews (review_id, user_id, game_id, user_color, depth_tier, "
        "status, progress, accuracy, total_loss, loss_type) "
        "VALUES (?, ?, ?, ?, 'shallow', 'complete', 1, 72.5, 40, 'cliff')",
        (review_id, user_id, game_id, user_color),
    )
    for ply, san, is_user, phase, is_book, wp, dw, vol in moves:
        connection.execute(
            "INSERT INTO review_moves ("
            "review_id, ply, san, is_user_move, phase, is_book, win_prob, delta_w, volatility"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, ply, san, is_user, phase, is_book, wp, dw, vol),
        )
    connection.commit()


def test_tier2_phase_conversion_castling_missed(connection: sqlite3.Connection) -> None:
    # Game: reached winning, castled kingside, then blundered — lost. Opponent higher-rated.
    _seed_game(
        connection,
        game_id="g1",
        review_id="r1",
        result="0-1",
        moves=[
            (1, "e4", 1, "opening", 1, 0.55, 2, 20),
            (2, "e5", 0, "opening", 1, 0.45, 0, 20),
            (3, "Nf3", 1, "opening", 1, 0.56, 1, 25),
            (4, "Nc6", 0, "opening", 1, 0.44, 0, 25),
            (5, "O-O", 1, "opening", 0, 0.58, 1, 30),
            (6, "O-O-O", 0, "middlegame", 0, 0.42, 0, 40),
            (11, "Qh5", 1, "middlegame", 0, 0.90, 0, 55),  # peak win
            (12, "g6", 0, "middlegame", 0, 0.10, 0, 55),
            (13, "Qxf7", 1, "middlegame", 0, 0.20, 55, 70),  # cliff slip
            (21, "Ke2", 1, "endgame", 0, 0.15, 8, 35),
        ],
    )
    # Second game: similar opponent, drew conversion chance
    _seed_game(
        connection,
        game_id="g2",
        review_id="r2",
        result="1-0",
        white_rating=1500,
        black_rating=1450,
        eco="C00",
        played_at="2026-06-02T10:00:00",
        moves=[
            (1, "e4", 1, "opening", 1, 0.52, 1, 15),
            (2, "e6", 0, "opening", 1, 0.48, 0, 15),
            (3, "d4", 1, "opening", 0, 0.55, 2, 18),
            (5, "O-O", 1, "middlegame", 0, 0.75, 1, 40),
            (7, "Qd2", 1, "middlegame", 0, 0.80, 2, 45),
        ],
    )

    metrics = compute_tier1_metrics(connection, review_ids=["r1", "r2"])
    t2 = metrics["tier2"]

    phases = {p["phase"]: p for p in t2["phase_attribution"]}
    assert phases["middlegame"]["total_delta_w"] >= 50
    assert phases["middlegame"]["delta_w_per_move"] > 0

    assert t2["conversion"]["n"] == 2
    assert t2["conversion"]["wins"] == 1

    assert t2["castling"]["kingside"]["n"] == 2
    assert t2["castling"]["opposite_side"]["n"] == 1

    assert t2["missed_wins"]["count"] == 1
    assert t2["missed_wins"]["examples"][0]["slip_san"] == "Qxf7"

    bands = {b["band"]: b for b in t2["opponent_relative"]}
    assert "higher" in bands
    assert bands["higher"]["n"] >= 1

    assert t2["standard"]["by_color"]["white"]["n"] == 2
    ecos = {e["eco"] for e in t2["standard"]["by_eco"]}
    assert "B20" in ecos and "C00" in ecos
    assert t2["repertoire_depth"]["n"] >= 1
    assert any(h["hour"] == 18 for h in t2["standard"]["by_hour"]) or any(
        h["hour"] == 10 for h in t2["standard"]["by_hour"]
    )
