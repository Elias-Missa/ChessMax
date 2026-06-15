"""Tests for the four training modes + shared utilities (no real engine)."""

from pathlib import Path
from typing import Any

import chess
from fastapi.testclient import TestClient

from server import db
from server.evalcheck import check_eval_drop, position_eval_cp
from server.main import create_app
from server.modes import validate_forced_line
from server.replies import engine_reply


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class StubAnalyzer:
    """Returns canned {fen -> top_moves} responses; falls back to a default."""

    def __init__(
        self,
        by_fen: dict[str, list[dict[str, Any]]] | None = None,
        default: list[dict[str, Any]] | None = None,
    ) -> None:
        self.by_fen = by_fen or {}
        self.default = default or [{"move": "a2a3", "eval": 0, "pv": ["a2a3"]}]
        self.calls: list[str] = []

    def __call__(self, fen: str, depth: int = 18, multipv: int = 1) -> dict[str, Any]:
        self.calls.append(fen)
        return {"top_moves": self.by_fen.get(fen, self.default)}


def fen_after(fen: str, *moves: str) -> str:
    board = chess.Board(fen)
    for uci in moves:
        board.push(chess.Move.from_uci(uci))
    return board.fen()


# --------------------------------------------------------------------------- #
# Shared utilities                                                            #
# --------------------------------------------------------------------------- #


def test_engine_reply_best_style() -> None:
    analyzer = StubAnalyzer(default=[{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}])
    move, engine = engine_reply(START_FEN, style="best", analyzer=analyzer)
    assert move == "e2e4"
    assert engine == "stockfish"


def test_engine_reply_topn_samples_within_window() -> None:
    analyzer = StubAnalyzer(
        default=[
            {"move": "e2e4", "eval": 30, "pv": ["e2e4"]},
            {"move": "d2d4", "eval": 25, "pv": ["d2d4"]},
            {"move": "g2g4", "eval": -300, "pv": ["g2g4"]},  # outside window
        ]
    )
    seen = {engine_reply(START_FEN, style="topn", analyzer=analyzer)[0] for _ in range(40)}
    assert "g2g4" not in seen
    assert seen <= {"e2e4", "d2d4"}


def test_engine_reply_game_over_returns_none() -> None:
    mate_fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    move, _ = engine_reply(mate_fen, style="best", analyzer=StubAnalyzer())
    assert move is None


def test_check_eval_drop_within_threshold() -> None:
    after = fen_after(START_FEN, "e2e4")
    analyzer = StubAnalyzer(
        by_fen={
            START_FEN: [{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}],
            after: [{"move": "e7e5", "eval": -20, "pv": ["e7e5"]}],
        }
    )
    check = check_eval_drop(START_FEN, "e2e4", threshold_cp=100, analyzer=analyzer)
    assert check.ok
    assert check.drop_cp == 10  # 30 - (-(-20))
    assert check.best_move_uci == "e2e4"


def test_check_eval_drop_exceeding_threshold_fails() -> None:
    after = fen_after(START_FEN, "g2g4")
    analyzer = StubAnalyzer(
        by_fen={
            START_FEN: [{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}],
            after: [{"move": "e7e5", "eval": 250, "pv": ["e7e5"]}],
        }
    )
    check = check_eval_drop(START_FEN, "g2g4", threshold_cp=100, analyzer=analyzer)
    assert not check.ok
    assert check.drop_cp == 280


def test_check_eval_drop_mate_delivered_is_ok() -> None:
    mate_fen = "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq g3 0 2"
    analyzer = StubAnalyzer(
        by_fen={mate_fen: [{"move": "d8h4", "eval": 99000, "pv": ["d8h4"]}]}
    )
    check = check_eval_drop(mate_fen, "d8h4", threshold_cp=100, analyzer=analyzer)
    assert check.ok
    assert check.game_over and check.mate_delivered


def test_position_eval_cp_uses_mover_pov() -> None:
    analyzer = StubAnalyzer(default=[{"move": "e2e4", "eval": 42, "pv": ["e2e4"]}])
    assert position_eval_cp(START_FEN, analyzer=analyzer) == 42


# --------------------------------------------------------------------------- #
# Eval-hold API                                                               #
# --------------------------------------------------------------------------- #


def make_client(tmp_path: Path) -> TestClient:
    path = tmp_path / "trainer.db"
    with db.connect(path) as connection:
        db.get_singleton_user(connection)  # claimed by the test account below
    client = TestClient(create_app(path))
    creds = {"email": "tester@example.com", "password": "testpass"}
    if client.post("/api/auth/register", json=creds).status_code >= 400:
        client.post("/api/auth/login", json=creds)
    return client


def insert_position(
    db_path: Path,
    fen: str = START_FEN,
    side_to_move: str = "w",
    classification: str = "quiet",
    solution_moves: str | None = None,
) -> int:
    with db.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO positions (
                fen, side_to_move, source, classification, opening_tag,
                best_move, best_eval, solution_moves, themes, rating, rating_deviation
            ) VALUES (?, ?, 'test', ?, NULL, 'e2e4', 0.0, ?, NULL, 1500, NULL)
            """,
            (fen, side_to_move, classification, solution_moves),
        )
        connection.commit()
        return int(cursor.lastrowid)


def test_evalhold_full_pass_flow(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    insert_position(db_path, classification="quiet")
    client = make_client(tmp_path)

    after_e4 = fen_after(START_FEN, "e2e4")
    after_e4_e5 = fen_after(START_FEN, "e2e4", "e7e5")
    after_3 = fen_after(START_FEN, "e2e4", "e7e5", "g1f3")
    client.app.state.analyze_fn = StubAnalyzer(
        by_fen={
            START_FEN: [{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}],
            after_e4: [{"move": "e7e5", "eval": -25, "pv": ["e7e5"]}],
            after_e4_e5: [{"move": "g1f3", "eval": 28, "pv": ["g1f3"]}],
            after_3: [{"move": "b8c6", "eval": -20, "pv": ["b8c6"]}],
        }
    )
    client.app.state.reply_fn = lambda fen, **kw: ("e7e5", "stockfish")

    start = client.post(
        "/api/evalhold/start",
        json={"target_moves": 2, "threshold_cp": 100, "maia_rating": 1500},
    )
    assert start.status_code == 200
    payload = start.json()
    assert payload["baseline_eval_cp"] == 30
    assert payload["target_moves"] == 2
    session_id = payload["session_id"]

    first = client.post(f"/api/evalhold/{session_id}/move", json={"move": "e2e4"})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "active"
    assert body["moves_survived"] == 1
    assert body["reply_uci"] == "e7e5"

    second = client.post(f"/api/evalhold/{session_id}/move", json={"move": "g1f3"})
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "passed"
    assert body["moves_survived"] == 2
    assert body["streak"] == 1

    summary = client.get("/api/evalhold/summary").json()
    assert summary == {"total": 1, "passed": 1, "streak": 1, "best_streak": 1}


def test_evalhold_fails_on_big_drop_and_resets_streak(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    insert_position(db_path, classification="quiet")
    client = make_client(tmp_path)

    after_blunder = fen_after(START_FEN, "f2f3")
    client.app.state.analyze_fn = StubAnalyzer(
        by_fen={
            START_FEN: [{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}],
            after_blunder: [{"move": "e7e5", "eval": 200, "pv": ["e7e5"]}],
        }
    )
    client.app.state.reply_fn = lambda fen, **kw: (None, "stockfish")

    session_id = client.post(
        "/api/evalhold/start",
        json={"target_moves": 3, "threshold_cp": 100, "maia_rating": 1500},
    ).json()["session_id"]

    result = client.post(f"/api/evalhold/{session_id}/move", json={"move": "f2f3"})
    body = result.json()
    assert body["status"] == "failed"
    assert body["drop_cp"] == 230
    assert body["best_move_san"] == "e4"
    assert body["streak"] == 0

    # Session is gone after a terminal result.
    again = client.post(f"/api/evalhold/{session_id}/move", json={"move": "e2e4"})
    assert again.status_code == 400


def test_evalhold_abandon_counts_as_fail(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    insert_position(db_path, classification="quiet")
    client = make_client(tmp_path)
    client.app.state.analyze_fn = StubAnalyzer(
        by_fen={START_FEN: [{"move": "e2e4", "eval": 10, "pv": ["e2e4"]}]}
    )
    session_id = client.post(
        "/api/evalhold/start", json={"maia_rating": 1500}
    ).json()["session_id"]

    end = client.post(f"/api/evalhold/{session_id}/end")
    assert end.status_code == 200
    assert end.json()["status"] == "failed"
    summary = client.get("/api/evalhold/summary").json()
    assert summary["total"] == 1
    assert summary["passed"] == 0


# --------------------------------------------------------------------------- #
# Defense gym API                                                             #
# --------------------------------------------------------------------------- #


def test_defense_start_picks_band_position_and_caches_eval(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    bad_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"  # pawn down
    insert_position(db_path, fen=bad_fen, classification="quiet")
    client = make_client(tmp_path)
    client.app.state.analyze_fn = StubAnalyzer(
        by_fen={bad_fen: [{"move": "e2e4", "eval": -150, "pv": ["e2e4"]}]}
    )

    start = client.post("/api/defense/start", json={"maia_rating": 1500})
    assert start.status_code == 200
    payload = start.json()
    assert payload["baseline_eval_cp"] == -150
    assert payload["mode"] == "defense"

    with db.connect(db_path) as connection:
        cached = connection.execute("SELECT eval_cp FROM position_evals").fetchone()
    assert cached["eval_cp"] == -150


def test_defense_holding_baseline_passes(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    bad_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"
    insert_position(db_path, fen=bad_fen, classification="quiet")
    client = make_client(tmp_path)

    after_move = fen_after(bad_fen, "d2d4")
    client.app.state.analyze_fn = StubAnalyzer(
        by_fen={
            bad_fen: [{"move": "d2d4", "eval": -150, "pv": ["d2d4"]}],
            # Opponent's POV +160 => mover POV -160: a small slip, above floor.
            after_move: [{"move": "e7e5", "eval": 160, "pv": ["e7e5"]}],
        }
    )
    client.app.state.reply_fn = lambda fen, **kw: (None, "stockfish")

    session_id = client.post(
        "/api/defense/start",
        json={"target_moves": 1, "threshold_cp": 100, "maia_rating": 1500},
    ).json()["session_id"]

    result = client.post(f"/api/defense/{session_id}/move", json={"move": "d2d4"}).json()
    assert result["status"] == "passed"
    assert result["played_eval_cp"] == -160


def test_defense_collapse_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    bad_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"
    insert_position(db_path, fen=bad_fen, classification="quiet")
    client = make_client(tmp_path)

    after_move = fen_after(bad_fen, "g2g4")
    client.app.state.analyze_fn = StubAnalyzer(
        by_fen={
            bad_fen: [{"move": "e2e4", "eval": -150, "pv": ["e2e4"]}],
            after_move: [{"move": "d8h4", "eval": 450, "pv": ["d8h4"]}],
        }
    )

    session_id = client.post(
        "/api/defense/start",
        json={"target_moves": 4, "threshold_cp": 100, "maia_rating": 1500},
    ).json()["session_id"]

    result = client.post(f"/api/defense/{session_id}/move", json={"move": "g2g4"}).json()
    assert result["status"] == "failed"
    assert result["played_eval_cp"] == -450


# --------------------------------------------------------------------------- #
# Guess API                                                                   #
# --------------------------------------------------------------------------- #


def test_guess_flow_and_history(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    insert_position(db_path)
    client = make_client(tmp_path)
    client.app.state.guess_actuals_fn = lambda fen: {
        "actual_eval_cp": 120,
        "actual_sharpness": 62.5,
        "decided": False,
        "reason": None,
    }

    nxt = client.get("/api/guess/next")
    assert nxt.status_code == 200
    payload = nxt.json()
    assert payload["fen"] == START_FEN

    submit = client.post(
        "/api/guess/submit",
        json={
            "position_id": payload["position_id"],
            "fen": payload["fen"],
            "guessed_eval_cp": 50,
            "guessed_sharpness": 40,
        },
    )
    assert submit.status_code == 200
    body = submit.json()
    assert body["actual_eval_cp"] == 120
    assert body["eval_error_cp"] == 70
    assert body["sharpness_error"] == 22.5

    history = client.get("/api/guess/history").json()["attempts"]
    assert len(history) == 1
    assert history[0]["guessed_eval_cp"] == 50
    assert history[0]["actual_sharpness"] == 62.5


def test_guess_submit_rejects_bad_fen(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/guess/submit",
        json={"fen": "not a fen", "guessed_eval_cp": 0, "guessed_sharpness": 0},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Forced-line validation                                                      #
# --------------------------------------------------------------------------- #


def test_validate_forced_line_exact_match_passes_without_engine() -> None:
    outcome = validate_forced_line(
        START_FEN,
        ["e2e4", "e7e5"],
        ["e2e4", "e7e5"],
        analyzer=StubAnalyzer(default=[]),
    )
    assert outcome.passed
    assert outcome.matched_plies == 2
    assert [v.verdict for v in outcome.verdicts] == ["match", "match"]


def test_validate_forced_line_wrong_move_fails_with_expected_san() -> None:
    after_wrong = fen_after(START_FEN, "a2a3")
    analyzer = StubAnalyzer(
        by_fen={
            START_FEN: [{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}],
            after_wrong: [{"move": "e7e5", "eval": 120, "pv": ["e7e5"]}],
        }
    )
    outcome = validate_forced_line(
        START_FEN, ["a2a3", "e7e5"], ["e2e4", "e7e5"], analyzer=analyzer
    )
    assert not outcome.passed
    assert outcome.verdicts[0].verdict == "wrong"
    assert outcome.verdicts[0].expected_san == "e4"
    assert outcome.verdicts[1].verdict == "not_reached"


def test_validate_forced_line_acceptable_deviation_within_tolerance() -> None:
    after_alt = fen_after(START_FEN, "d2d4")
    analyzer = StubAnalyzer(
        by_fen={
            START_FEN: [{"move": "e2e4", "eval": 30, "pv": ["e2e4"]}],
            # Opponent POV -5 => mover POV +5: only a 25cp drop, acceptable.
            after_alt: [{"move": "g8f6", "eval": -5, "pv": ["g8f6"]}],
        }
    )
    outcome = validate_forced_line(
        START_FEN, ["d2d4", "e7e5"], ["e2e4", "e7e5"], analyzer=analyzer
    )
    assert outcome.verdicts[0].verdict == "acceptable"
    assert outcome.verdicts[1].verdict == "match"
    assert outcome.passed


def test_forced_api_flow(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    position_id = insert_position(
        db_path,
        fen=START_FEN,
        side_to_move="w",
        classification="tactical",
        solution_moves="e2e4 e7e5",
    )
    client = make_client(tmp_path)
    client.app.state.analyze_fn = StubAnalyzer(default=[])

    nxt = client.get("/api/forced/next").json()
    assert nxt["position_id"] == position_id
    assert nxt["ply_count"] == 2

    submit = client.post(
        f"/api/forced/{position_id}/submit",
        json={"line": ["e2e4", "e7e5"]},
    )
    assert submit.status_code == 200
    body = submit.json()
    assert body["passed"] is True
    assert body["matched_plies"] == 2
    assert body["summary"]["total"] == 1
    assert body["summary"]["streak"] == 1
