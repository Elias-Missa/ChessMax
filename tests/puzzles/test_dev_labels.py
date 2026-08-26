"""Dev tab calibration-labelling API (server/devlabels*.py). Engine-free."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from server import db
from server.main import create_app

FEN_1 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
FEN_2 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"


def client_for(app: Any, email: str = "a@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


def seed_review(
    db_path: Path,
    *,
    user_id: int = 1,
    review_id: str = "rev-1",
    game_id: str = "game-1",
    depth_tier: str = "full",
    moves: list[dict[str, Any]] | None = None,
) -> None:
    """Write one complete review with a couple of scored plies."""

    connection = db.connect(db_path)
    connection.execute(
        "INSERT OR REPLACE INTO games (game_id, source, pgn, white_name, black_name, "
        "result, played_at, opening_name) VALUES (?, 'pgn', ?, 'Alice', 'Bob', "
        "'1-0', '2026.01.01', 'Kings Pawn')",
        (game_id, "1. e4 e5 2. Nf3 *"),
    )
    connection.execute(
        "INSERT OR REPLACE INTO reviews (review_id, user_id, game_id, user_color, "
        "depth_tier, status, constants_version) VALUES (?, ?, ?, 'white', ?, 'complete', 'v1')",
        (review_id, user_id, game_id, depth_tier),
    )
    default_moves = [
        {
            "ply": 1,
            "san": "e4",
            "volatility": 12.5,
            "findability": 74,
            "detail": {
                "fen_before": FEN_1,
                "move_uci": "e2e4",
                "eval_cp": 25,
                "top_lines": [
                    {"uci": "g1f3", "san": "Nf3", "eval_cp": 30},
                    {"uci": "e2e4", "san": "e4", "eval_cp": 25},
                ],
            },
        },
        {
            "ply": 2,
            "san": "e5",
            "volatility": 41.0,
            "findability": None,
            "detail": {
                "fen_before": FEN_2,
                "move_uci": "e7e5",
                "eval_cp": -20,
                "top_lines": [{"uci": "b8c6", "san": "Nc6", "eval_cp": -15}],
            },
        },
    ]
    for move in moves if moves is not None else default_moves:
        connection.execute(
            "INSERT OR REPLACE INTO review_moves (review_id, ply, san, is_user_move, "
            "phase, classification, win_prob, delta_w, volatility, findability, detail) "
            "VALUES (?, ?, ?, 1, 'opening', ?, 0.55, 3.2, ?, ?, ?)",
            (
                review_id,
                move["ply"],
                move["san"],
                move.get("classification"),
                move.get("volatility"),
                move.get("findability"),
                json.dumps(move["detail"]),
            ),
        )
    connection.commit()
    connection.close()


def test_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "trainer.db"), raise_server_exceptions=False)
    assert client.get("/api/dev/positions").status_code == 401
    assert client.get("/api/dev/stats").status_code == 401
    assert client.get("/api/dev/export").status_code == 401
    assert client.post(
        "/api/dev/label", json={"review_id": "r", "ply": 1, "volatility_label": "lower"}
    ).status_code == 401


def test_positions_expose_the_stored_scores_and_the_best_move(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    data = client.get("/api/dev/positions").json()
    assert data["total"] == 2
    first = data["positions"][0]

    assert first["ply"] == 1
    assert first["san"] == "e4"
    assert first["fen"] == FEN_1
    assert first["volatility"] == 12.5
    assert first["findability"] == 74
    # The findability panel scores the BEST move, not the move played — the row
    # has to carry it or every label collected is about the wrong move.
    assert first["best_uci"] == "g1f3"
    assert first["best_san"] == "Nf3"
    assert first["move_uci"] == "e2e4"
    # The band is derived, never stored.
    assert first["findability_band"]
    assert first["white_name"] == "Alice"
    assert first["index"] == 0


def test_shallow_rows_carry_volatility_and_no_findability(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    second = client.get("/api/dev/positions").json()["positions"][1]
    assert second["volatility"] == 41.0
    assert second["findability"] is None
    assert second["findability_band"] is None


def test_label_roundtrip_and_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    res = client.post(
        "/api/dev/label",
        json={
            "review_id": "rev-1",
            "ply": 1,
            "volatility_label": "way_higher",
            "findability_label": "harder",
            "note": "  knight jump is easy to miss  ",
        },
    )
    assert res.status_code == 200
    assert res.json()["note"] == "knight jump is easy to miss"

    first = client.get("/api/dev/positions").json()["positions"][0]
    assert first["volatility_label"] == "way_higher"
    assert first["findability_label"] == "harder"

    exported = client.get("/api/dev/export").json()["labels"]
    assert len(exported) == 1
    row = exported[0]
    # The verdict is only interpretable against the number it was given, so the
    # scores are snapshotted onto the label row.
    assert row["volatility"] == 12.5
    assert row["findability"] == 74
    assert row["fen"] == FEN_1
    assert row["best_uci"] == "g1f3"
    assert row["volatility_delta"] == 2
    assert row["findability_delta"] == -1
    assert row["constants_version"] == "v1"


def test_relabelling_overwrites_rather_than_appends(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    for label in ("lower", "about_right"):
        client.post(
            "/api/dev/label",
            json={"review_id": "rev-1", "ply": 1, "volatility_label": label},
        )

    exported = client.get("/api/dev/export").json()["labels"]
    assert len(exported) == 1
    assert exported[0]["volatility_label"] == "about_right"


def test_clearing_both_labels_deletes_the_row(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    client.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 1, "volatility_label": "lower"},
    )
    assert client.get("/api/dev/positions?scope=labeled").json()["total"] == 1

    res = client.post("/api/dev/label", json={"review_id": "rev-1", "ply": 1})
    assert res.json()["cleared"] is True
    assert client.get("/api/dev/export").json()["labels"] == []
    # A tombstone row would make the unlabelled scope lie.
    assert client.get("/api/dev/positions?scope=unlabeled").json()["total"] == 2


def test_scope_and_require_filters_agree_with_their_count(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)
    client.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 2, "findability_label": "way_easier"},
    )

    for scope, require, expected in [
        ("all", "any", 2),
        ("unlabeled", "any", 1),
        ("labeled", "any", 1),
        ("all", "findability", 1),
        ("all", "volatility", 2),
    ]:
        data = client.get(f"/api/dev/positions?scope={scope}&require={require}").json()
        assert data["total"] == expected, (scope, require)
        # The listing and its COUNT must filter identically or the client's
        # "position N of M" counter drifts.
        assert len(data["positions"]) == expected, (scope, require)


def test_paging_is_stable_and_indexed_absolutely(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    page = client.get("/api/dev/positions?limit=1&offset=1").json()
    assert page["total"] == 2
    assert len(page["positions"]) == 1
    assert page["positions"][0]["ply"] == 2
    assert page["positions"][0]["index"] == 1


def test_unknown_label_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    assert client.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 1, "volatility_label": "much_bigger"},
    ).status_code == 422
    assert client.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 1, "findability_label": "lower"},
    ).status_code == 422


def test_labels_cannot_reach_another_users_review(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    app = create_app(db_path)
    client_for(app, "owner@x.com")
    connection = db.connect(db_path)
    owner_id = int(
        connection.execute(
            "SELECT id FROM users WHERE email = 'owner@x.com'"
        ).fetchone()["id"]
    )
    connection.close()
    seed_review(db_path, user_id=owner_id)

    intruder = TestClient(app, raise_server_exceptions=False)
    intruder.post("/api/auth/register", json={"email": "b@x.com", "password": "secret1"})

    assert intruder.get("/api/dev/positions").json()["total"] == 0
    assert intruder.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 1, "volatility_label": "lower"},
    ).status_code == 404


def test_stats_count_each_verdict(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)

    client.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 1, "volatility_label": "higher",
              "findability_label": "about_right"},
    )
    client.post(
        "/api/dev/label",
        json={"review_id": "rev-1", "ply": 2, "volatility_label": "higher"},
    )

    stats = client.get("/api/dev/stats").json()
    assert stats["labeled_positions"] == 2
    assert stats["volatility"]["higher"] == 2
    assert stats["findability"]["about_right"] == 1
    assert stats["volatility_total"] == 2
    assert stats["findability_total"] == 1
    assert stats["total_positions"] == 2


def test_incomplete_reviews_are_not_offered(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    client = client_for(create_app(db_path))
    seed_review(db_path)
    connection = db.connect(db_path)
    connection.execute("UPDATE reviews SET status = 'running' WHERE review_id = 'rev-1'")
    connection.commit()
    connection.close()

    assert client.get("/api/dev/positions").json()["total"] == 0


def test_dev_route_serves_the_spa(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "trainer.db"), raise_server_exceptions=False)
    res = client.get("/dev")
    assert res.status_code == 200
    assert "dev-root" in res.text
