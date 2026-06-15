import sqlite3
from pathlib import Path
from typing import Any

import chess
from fastapi.testclient import TestClient

from server import db
from server.main import create_app


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def test_get_user_auto_provisions_singleton(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    first = client.get("/api/user")
    second = client.get("/api/user")

    assert first.status_code == 200
    assert first.json() == {"rating": 1500, "selected_openings": []}
    assert second.json() == first.json()

    with db.connect(tmp_path / "trainer.db") as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 1


def test_openings_get_and_put_roundtrip(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    initial = client.get("/api/openings").json()
    assert initial == {"selected": [], "available": ["london", "caro-kann"]}

    update = client.put("/api/openings", json={"openings": ["london"]})
    assert update.status_code == 200
    assert update.json() == {"selected": ["london"]}

    user = client.get("/api/user").json()
    assert user["selected_openings"] == ["london"]


def test_openings_put_rejects_unknown(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.put("/api/openings", json={"openings": ["sicilian"]})

    assert response.status_code == 422
    assert "sicilian" in response.json()["detail"]


def test_next_puzzle_hides_classification(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    insert_position(db_path, classification="quiet")
    client = make_client(tmp_path, db_path)

    response = client.get("/api/puzzle/next")

    assert response.status_code == 200
    payload = response.json()
    assert payload["fen"] == START_FEN
    assert payload["side_to_move"] == "w"
    assert "classification" not in payload
    assert "position_classification" not in payload


def test_tactical_one_move_puzzle_solved(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(
        db_path,
        classification="tactical",
        solution_moves="e2e4",
    )
    client = make_client(tmp_path, db_path)
    client.app.state.analyze_fn = ShouldNotBeCalled()

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "e2e4", "step": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "solved"
    assert payload["solved"] is True
    assert payload["grade"] == "best"
    assert payload["best_move"] == "e4"
    assert payload["solution_line"] == "e4"
    assert payload["opponent_move"] is None
    assert payload["opponent_move_uci"] is None
    assert payload["user_rating_after"] == 1510


def test_attempt_exposes_puzzle_rating_and_opening(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(
        db_path,
        classification="tactical",
        solution_moves="e2e4",
        opening_tag="london",
    )
    client = make_client(tmp_path, db_path)
    client.app.state.analyze_fn = ShouldNotBeCalled()

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "e2e4", "step": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    # position_rating reflects the puzzle's difficulty at attempt time (pre-drift).
    assert payload["position_rating"] == 1500
    assert payload["opening_tag"] == "london"


def test_tactical_two_move_puzzle_solved_with_opponent_reply(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(
        db_path,
        classification="tactical",
        solution_moves="e2e4 e7e5",
    )
    client = make_client(tmp_path, db_path)
    client.app.state.analyze_fn = ShouldNotBeCalled()

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "e2e4", "step": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "solved"
    assert payload["solved"] is True
    assert payload["best_move"] == "e4"
    assert payload["solution_line"] == "e4 e5"
    assert payload["opponent_move"] == "e5"
    assert payload["opponent_move_uci"] == "e7e5"
    assert payload["user_rating_after"] == 1510

    with db.connect(db_path) as connection:
        attempts = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert attempts == 1


def test_tactical_three_move_puzzle_continue_then_solved(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(
        db_path,
        classification="tactical",
        solution_moves="e2e4 e7e5 g1f3",
    )
    client = make_client(tmp_path, db_path)
    client.app.state.analyze_fn = ShouldNotBeCalled()

    first = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "e2e4", "step": 0},
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["status"] == "continue"
    assert first_payload["opponent_move"] == "e5"
    assert first_payload["opponent_move_uci"] == "e7e5"
    assert first_payload["next_step"] == 2
    assert first_payload["solved"] is False
    assert first_payload["user_rating_after"] == 1500  # unchanged on continue

    # No DB writes on continue
    with db.connect(db_path) as connection:
        mid_attempts = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        mid_user = connection.execute(
            "SELECT * FROM users WHERE username = ?", (db.SINGLETON_USERNAME,)
        ).fetchone()
    assert mid_attempts == 0
    assert mid_user["rating"] == 1500

    second = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "g1f3", "step": 2},
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["status"] == "solved"
    assert second_payload["solved"] is True
    assert second_payload["best_move"] == "Nf3"
    assert second_payload["solution_line"] == "e4 e5 Nf3"
    assert second_payload["user_rating_after"] == 1510

    with db.connect(db_path) as connection:
        final_attempts = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert final_attempts == 1  # exactly one row for the whole puzzle


def test_tactical_wrong_move_fails_immediately(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(
        db_path,
        classification="tactical",
        solution_moves="e2e4 e7e5 g1f3",
    )
    client = make_client(tmp_path, db_path)
    # Tactical FAILED runs one short refutation analysis so the UI can show the
    # engine's response to the bad move.
    client.app.state.analyze_fn = FakeAnalyzer([
        {"top_moves": [{"move": "g8f6", "eval": -25, "pv": ["g8f6"]}]},
    ])

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "d2d4", "step": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["solved"] is False
    assert payload["grade"] == "blunder"
    assert payload["best_move"] == "e4"
    assert payload["solution_line"] == "e4 e5 Nf3"  # full intended line
    assert payload["user_rating_after"] == 1490
    assert payload["refutation"] == "Nf6"
    assert payload["refutation_uci"] == "g8f6"


def test_quiet_attempt_returns_top_lines_with_evals(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(db_path, classification="quiet")
    client = make_client(tmp_path, db_path)
    client.app.state.analyze_fn = FakeAnalyzer([
        # First call (multipv=3) — top 3 candidates on the original position
        {
            "top_moves": [
                {"move": "e2e4", "eval": 60, "pv": ["e2e4", "e7e5", "g1f3"]},
                {"move": "d2d4", "eval": 40, "pv": ["d2d4", "d7d5"]},
                {"move": "g1f3", "eval": 25, "pv": ["g1f3", "g8f6"]},
            ]
        },
        # Second call (multipv=1) — resulting position eval after user move
        {"top_moves": [{"move": "e7e5", "eval": -35, "pv": ["e7e5"]}]},
    ])

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "e2e4", "step": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "graded"
    assert payload["solved"] is True
    assert payload["grade"] == "good"
    assert payload["eval_loss"] == 25.0
    assert payload["best_move"] == "e4"
    assert payload["position_classification"] == "quiet"

    top_lines = payload["top_lines"]
    assert len(top_lines) == 3
    assert top_lines[0] == {
        "move_san": "e4",
        "eval_cp": 60,
        "pv_san": "e4 e5 Nf3",
    }
    assert top_lines[1]["move_san"] == "d4"
    assert top_lines[1]["eval_cp"] == 40
    assert top_lines[2]["move_san"] == "Nf3"


def test_quiet_blunder_returns_refutation(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(db_path, classification="quiet")
    client = make_client(tmp_path, db_path)
    # Best move scores +60; user's move leaves opponent at +200 (= -200 for us).
    # eval_loss = 60 - (-200) = 260 → "mistake", not solved → refutation expected.
    client.app.state.analyze_fn = FakeAnalyzer([
        {
            "top_moves": [
                {"move": "e2e4", "eval": 60, "pv": ["e2e4"]},
                {"move": "d2d4", "eval": 40, "pv": ["d2d4"]},
                {"move": "g1f3", "eval": 25, "pv": ["g1f3"]},
            ]
        },
        {"top_moves": [{"move": "e7e5", "eval": 200, "pv": ["e7e5"]}]},
    ])

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "a2a3", "step": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "graded"
    assert payload["solved"] is False
    assert payload["refutation"] == "e5"
    assert payload["refutation_uci"] == "e7e5"


def test_attempt_rejects_illegal_move(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(db_path)
    client = make_client(tmp_path, db_path)

    response = client.post(
        f"/api/puzzle/{position_id}/attempt",
        json={"move": "e2e5", "step": 0},
    )

    assert response.status_code == 400
    with db.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert count == 0


def test_root_serves_frontend(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    # The merged ChessMax index contains the trainer markup.
    assert "ChessMax" in response.text
    assert "view-train" in response.text


def test_playout_capabilities_endpoint(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/api/playout/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["fallback_engine"] == "stockfish"
    assert payload["supported_ratings"] == [1100, 1300, 1500, 1700, 1900]


def test_playout_start_move_end_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(db_path, classification="quiet")
    client = make_client(tmp_path, db_path)
    client.app.state.playout_move_fn = FakePlayoutMover(["e7e5", "b8c6"])
    client.app.state.analyze_fn = FlatAnalyzer()

    board = chess.Board(START_FEN)
    board.push(chess.Move.from_uci("e2e4"))

    start = client.post(
        "/api/playout/start",
        json={
            "position_id": position_id,
            "maia_rating": 1520,
            "fen": board.fen(),
            "user_color": "w",
        },
    )
    assert start.status_code == 200
    start_payload = start.json()
    assert start_payload["status"] == "active"
    assert start_payload["maia_move"] == "e7e5"
    assert start_payload["engine"] == "stockfish"
    assert start_payload["move_list"] == ["e7e5"]

    move = client.post(
        f"/api/playout/{start_payload['playout_id']}/move",
        json={"move": "g1f3"},
    )
    assert move.status_code == 200
    move_payload = move.json()
    assert move_payload["status"] == "active"
    assert move_payload["maia_move"] == "b8c6"
    assert move_payload["move_list"] == ["e7e5", "g1f3", "b8c6"]

    end = client.post(f"/api/playout/{start_payload['playout_id']}/end")
    assert end.status_code == 200
    end_payload = end.json()
    assert "Chess Trainer Playout" in end_payload["final_pgn"]
    assert end_payload["result"] in {"win", "loss", "draw"}

    with db.connect(db_path) as connection:
        archived = connection.execute("SELECT COUNT(*) FROM playouts").fetchone()[0]
        active = connection.execute("SELECT COUNT(*) FROM playout_sessions").fetchone()[0]
    assert archived == 1
    assert active == 0


def test_playout_takeback_undoes_last_turn(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(db_path, classification="quiet")
    client = make_client(tmp_path, db_path)
    client.app.state.playout_move_fn = FakePlayoutMover(["e7e5", "b8c6"])

    board = chess.Board(START_FEN)
    board.push(chess.Move.from_uci("e2e4"))
    start = client.post(
        "/api/playout/start",
        json={
            "position_id": position_id,
            "maia_rating": 1500,
            "fen": board.fen(),
            "user_color": "w",
        },
    )
    assert start.status_code == 200
    playout_id = start.json()["playout_id"]

    move = client.post(f"/api/playout/{playout_id}/move", json={"move": "g1f3"})
    assert move.status_code == 200
    assert move.json()["move_list"] == ["e7e5", "g1f3", "b8c6"]

    takeback = client.post(f"/api/playout/{playout_id}/takeback")
    assert takeback.status_code == 200
    payload = takeback.json()
    assert payload["status"] == "active"
    assert payload["undone_plies"] == 2
    assert payload["move_list"] == ["e7e5"]


def test_recent_playouts_includes_replay_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(db_path, classification="quiet")
    client = make_client(tmp_path, db_path)

    with db.connect(db_path) as connection:
        user = db.get_singleton_user(connection)
        pgn = """[Event "Chess Trainer Playout"]
[Site "Local"]
[Result "1-0"]
[FEN "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"]

1. e4 e5 2. Nf3 Nc6 1-0
"""
        connection.execute(
            """
            INSERT INTO playouts (user_id, position_id, maia_rating, result, pgn, engine)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user["id"], position_id, 1500, "win", pgn, "maia"),
        )
        connection.commit()

    response = client.get("/api/playout/recent")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["playouts"]) == 1
    first = payload["playouts"][0]
    assert first["engine"] == "maia"
    assert first["move_list"] == ["e2e4", "e7e5", "g1f3", "b8c6"]
    assert first["initial_fen"] == START_FEN


def test_stats_endpoint_returns_breakdowns(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    tactical_id = insert_position(
        db_path,
        classification="tactical",
        solution_moves="e2e4",
        opening_tag="london",
        themes="fork,pin",
    )
    quiet_id = insert_position(
        db_path,
        classification="quiet",
        opening_tag=None,
        themes=None,
    )
    client = make_client(tmp_path, db_path)

    with db.connect(db_path) as connection:
        user = db.get_singleton_user(connection)
        connection.execute(
            """
            INSERT INTO attempts (
                user_id, position_id, user_move, eval_loss, grade,
                user_rating_before, user_rating_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], tactical_id, "e2e4", 0.0, "best", 1500, 1510),
        )
        connection.execute(
            """
            INSERT INTO attempts (
                user_id, position_id, user_move, eval_loss, grade,
                user_rating_before, user_rating_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], quiet_id, "a2a3", 220.0, "mistake", 1510, 1499),
        )
        connection.execute(
            """
            INSERT INTO playouts (user_id, position_id, maia_rating, result, pgn, engine)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user["id"], quiet_id, 1500, "win", "[Event \"x\"]", "stockfish"),
        )
        connection.commit()

    response = client.get("/api/stats")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"]["attempts"] == 2
    assert payload["overall"]["solved"] == 1
    assert payload["quiet"]["attempts"] == 1
    assert payload["tactical"]["attempts"] == 1
    assert payload["theme_accuracy"][0]["theme"] == "fork"
    assert payload["opening_accuracy"][0]["opening"] == "london"
    assert payload["playouts"]["wins"] == 1
    assert len(payload["rating_history"]) == 3


class FakeAnalyzer:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        fen: str,
        depth: int = 18,
        multipv: int = 1,
    ) -> dict[str, Any]:
        assert fen
        self.calls.append({"fen": fen, "depth": depth, "multipv": multipv})
        return self.responses.pop(0)


class ShouldNotBeCalled:
    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("Tactical attempts must not invoke Stockfish")


class FlatAnalyzer:
    def __call__(
        self,
        fen: str,
        depth: int = 18,
        multipv: int = 1,
    ) -> dict[str, Any]:
        assert fen
        return {"top_moves": [{"move": "a2a3", "eval": 0, "pv": ["a2a3"]}]}


class FakePlayoutMover:
    def __init__(self, moves: list[str]) -> None:
        self.moves = moves

    def __call__(self, fen: str, maia_rating: int) -> tuple[str | None, str]:
        assert fen
        assert maia_rating
        if not self.moves:
            return None, "stockfish"
        return self.moves.pop(0), "stockfish"


def make_client(tmp_path: Path, db_path: Path | None = None) -> TestClient:
    path = db_path or tmp_path / "trainer.db"
    # Provision the singleton 'default' row first so the test account claims it —
    # this keeps the logged-in user and any directly-seeded data on the same id.
    with db.connect(path) as connection:
        db.get_singleton_user(connection)
    client = TestClient(create_app(path))
    _authenticate(client)
    return client


def _authenticate(client: TestClient) -> None:
    """Register (or log in) a test account so the session cookie authenticates
    subsequent /api/* calls. TestClient persists cookies across requests."""
    creds = {"email": "tester@example.com", "password": "testpass"}
    if client.post("/api/auth/register", json=creds).status_code >= 400:
        client.post("/api/auth/login", json=creds)


def insert_position(
    db_path: Path,
    classification: str = "quiet",
    solution_moves: str | None = None,
    opening_tag: str | None = None,
    themes: str | None = None,
) -> int:
    with db.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO positions (
                fen,
                side_to_move,
                source,
                classification,
                opening_tag,
                best_move,
                best_eval,
                solution_moves,
                themes,
                rating,
                rating_deviation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                START_FEN,
                "w",
                "test",
                classification,
                opening_tag,
                "e2e4",
                60.0,
                solution_moves,
                themes,
                1500,
                None,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
