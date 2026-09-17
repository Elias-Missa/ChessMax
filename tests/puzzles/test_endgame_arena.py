"""Endgame Arena (server/endgame*.py). Engine-free — Maia and Stockfish injected."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import chess
from fastapi.testclient import TestClient

from core.endgame import DRAWN, LOSING, WINNING
from server import db, endgame
from server.main import create_app

# A rook-and-pawn ending, White to move, dead level.
LEVEL_FEN = "8/5pk1/8/8/8/8/5PK1/8 w - - 0 1"
# White a clear rook up.
WON_FEN = "8/6k1/8/8/8/8/6K1/4R3 w - - 0 1"
# White a clear rook down.
LOST_FEN = "4r3/6k1/8/8/8/8/6K1/8 w - - 0 1"


def client_for(app: Any, email: str = "a@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


def user_id_of(db_path: Path, email: str = "a@x.com") -> int:
    connection = db.connect(db_path)
    row = connection.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    connection.close()
    return int(row["id"])


def seed_review_endgame(db_path: Path, user_id: int, fen: str, eval_cp: int) -> None:
    """One completed review carrying a single endgame ply."""

    connection = db.connect(db_path)
    connection.execute(
        "INSERT OR REPLACE INTO games (game_id, source, pgn) VALUES ('g1','pgn','*')"
    )
    connection.execute(
        "INSERT OR REPLACE INTO reviews (review_id, user_id, game_id, user_color, "
        "depth_tier, status) VALUES ('rev-1', ?, 'g1', 'white', 'full', 'complete')",
        (user_id,),
    )
    connection.execute(
        "INSERT OR REPLACE INTO review_moves (review_id, ply, san, is_user_move, "
        "phase, detail) VALUES ('rev-1', 1, 'Kg2', 1, 'endgame', ?)",
        (json.dumps({"fen_before": fen, "eval_cp": eval_cp, "top_lines": []}),),
    )
    connection.commit()
    connection.close()


def seed_puzzle_endgame(db_path: Path, fen: str, best_eval: float) -> None:
    connection = db.connect(db_path)
    connection.execute(
        "INSERT INTO positions (fen, side_to_move, source, classification, "
        "best_move, best_eval, themes, rating) "
        "VALUES (?, 'w', 'lichess', 'tactical', 'g2g3', ?, 'rookEndgame endgame', 1500)",
        (fen, best_eval),
    )
    connection.commit()
    connection.close()


def always(uci: str):
    return lambda fen, rating: uci


def eval_of(cp: int):
    """An `analyze_fn` returning a fixed side-to-move evaluation."""

    return lambda fen, **kw: {"top_moves": [{"move": "g2g3", "eval": cp, "pv": []}]}


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #


def test_every_route_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "t.db"), raise_server_exceptions=False)
    assert client.post("/api/endgame/start", json={}).status_code == 401
    assert client.get("/api/endgame/summary").status_code == 401
    assert client.post("/api/endgame/hint", json={"fen": LEVEL_FEN}).status_code == 401


def test_start_without_any_positions_explains_itself(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    res = client.post("/api/endgame/start", json={})
    assert res.status_code == 404
    assert "Review a game" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# Sourcing — 50/50 across two corpora                                          #
# --------------------------------------------------------------------------- #


def test_own_games_supply_endgames_with_the_user_to_move(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    seed_review_endgame(db_path, uid, LEVEL_FEN, 0)

    connection = db.connect(db_path)
    found = endgame.candidates_from_own_games(connection, uid)
    connection.close()
    assert len(found) == 1
    assert found[0].source == endgame.OWN_GAMES
    assert found[0].bucket == DRAWN


def test_a_stored_eval_is_normalised_from_side_to_move_to_the_user(tmp_path: Path) -> None:
    """Black to move at +300 for Black is −300 for White, and the user is White."""

    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    black_to_move = "4r3/6k1/8/8/8/8/6K1/8 b - - 0 1"
    seed_review_endgame(db_path, uid, black_to_move, 300)

    connection = db.connect(db_path)
    found = endgame.candidates_from_own_games(connection, uid)
    connection.close()
    # The user is White and it is Black to move, so the position is skipped
    # rather than handed over on someone else's turn.
    assert found == []


def test_puzzles_supply_endgames_when_there_are_no_reviews(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    seed_puzzle_endgame(db_path, WON_FEN, 5.0)

    connection = db.connect(db_path)
    found = endgame.candidates_from_puzzles(connection)
    connection.close()
    assert len(found) == 1
    assert found[0].source == endgame.PUZZLES
    assert found[0].bucket == WINNING


def test_the_picker_falls_back_when_one_corpus_is_empty(tmp_path: Path) -> None:
    """A cold account still gets a position; the 50/50 is a target, not a quota."""

    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    connection = db.connect(db_path)
    uid = user_id_of(db_path)
    for seed in range(6):
        picked = endgame.pick_position(connection, uid, rng=random.Random(seed))
        assert picked is not None
        assert picked.source == endgame.PUZZLES
    connection.close()


# --------------------------------------------------------------------------- #
# The Maia ladder, end to end                                                  #
# --------------------------------------------------------------------------- #


def test_a_winning_start_faces_a_stronger_maia(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    client = client_for(app)
    seed_puzzle_endgame(db_path, WON_FEN, 5.0)

    body = client.post("/api/endgame/start", json={"bucket": "winning"}).json()
    assert body["bucket"] == WINNING
    assert body["maia_rating"] == 1700  # one rung above the default 1500


def test_a_losing_start_faces_a_weaker_maia(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    client = client_for(app)
    seed_puzzle_endgame(db_path, LOST_FEN, -5.0)

    body = client.post("/api/endgame/start", json={"bucket": "losing"}).json()
    assert body["bucket"] == LOSING
    assert body["maia_rating"] == 1300


# --------------------------------------------------------------------------- #
# Playing                                                                      #
# --------------------------------------------------------------------------- #


def start_level_game(tmp_path: Path, maia_move: str = "g7g8"):
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always(maia_move)
    client = client_for(app)
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    state = client.post("/api/endgame/start", json={}).json()
    return client, app, db_path, state


def test_a_move_is_answered_by_maia(tmp_path: Path) -> None:
    client, _, _, state = start_level_game(tmp_path)
    body = client.post(f"/api/endgame/{state['session_id']}/move",
                       json={"move": "g2g3"}).json()
    assert body["last_move_uci"] == "g2g3"
    assert body["maia_move_uci"] == "g7g8"
    assert len(body["moves"]) == 2
    assert body["status"] == "active"


def test_an_illegal_move_is_refused_without_ending_the_game(tmp_path: Path) -> None:
    client, _, _, state = start_level_game(tmp_path)
    res = client.post(f"/api/endgame/{state['session_id']}/move",
                      json={"move": "a1a8"})
    assert res.status_code == 400
    assert client.get("/api/endgame/active").json()["session"] is not None


def test_takeback_returns_the_board_to_the_users_turn(tmp_path: Path) -> None:
    client, _, _, state = start_level_game(tmp_path)
    sid = state["session_id"]
    client.post(f"/api/endgame/{sid}/move", json={"move": "g2g3"})
    body = client.post(f"/api/endgame/{sid}/takeback", json={}).json()

    assert body["moves"] == []
    assert body["fen"] == LEVEL_FEN
    assert body["takebacks"] == 1
    # It is the user's move again, which is the whole point of the button.
    assert chess.Board(body["fen"]).turn == chess.WHITE


def test_takeback_on_a_fresh_board_is_refused(tmp_path: Path) -> None:
    client, _, _, state = start_level_game(tmp_path)
    res = client.post(f"/api/endgame/{state['session_id']}/takeback", json={})
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# The draw offer — the mode's teaching mechanism                               #
# --------------------------------------------------------------------------- #


def play_some_moves(client: TestClient, sid: int, count: int = 4) -> None:
    board = chess.Board(LEVEL_FEN)
    for _ in range(count):
        legal = [m for m in board.legal_moves if board.piece_at(m.from_square).color]
        if not legal:
            break
        move = legal[0]
        res = client.post(f"/api/endgame/{sid}/move", json={"move": move.uci()})
        if res.status_code != 200 or res.json()["status"] != "active":
            break
        board = chess.Board(res.json()["fen"])


def test_maia_accepts_a_draw_in_a_position_it_is_not_winning(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    app.state.analyze_fn = eval_of(0)  # dead level
    client = client_for(app)
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    sid = client.post("/api/endgame/start", json={}).json()["session_id"]
    play_some_moves(client, sid, 4)

    body = client.post(f"/api/endgame/{sid}/draw", json={}).json()
    assert body["status"] == "finished"
    assert body["result"] == "draw"
    assert body["outcome"] == "held"
    assert body["passed"] is True


def test_maia_declines_and_plays_on_when_it_is_winning(tmp_path: Path) -> None:
    """You cannot offer your way out of a position you threw away."""

    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    # White (the user) to move at −800 => Maia is +800 and has no reason to agree.
    app.state.analyze_fn = eval_of(-800)
    client = client_for(app)
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    sid = client.post("/api/endgame/start", json={}).json()["session_id"]
    play_some_moves(client, sid, 4)

    body = client.post(f"/api/endgame/{sid}/draw", json={}).json()
    assert body["status"] == "active"
    assert body["result"] is None
    assert "hold the position you made" in body["message"]


def test_a_draw_cannot_be_claimed_before_playing(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.analyze_fn = eval_of(0)
    client = client_for(app)
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    sid = client.post("/api/endgame/start", json={}).json()["session_id"]

    body = client.post(f"/api/endgame/{sid}/draw", json={}).json()
    assert body["status"] == "active"
    assert "Play it out" in body["message"]


# --------------------------------------------------------------------------- #
# Finishing + record                                                           #
# --------------------------------------------------------------------------- #


def test_resigning_a_winning_start_is_graded_as_throwing_it(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    client = client_for(app)
    seed_puzzle_endgame(db_path, WON_FEN, 5.0)
    sid = client.post("/api/endgame/start", json={"bucket": "winning"}).json()["session_id"]

    body = client.post(f"/api/endgame/{sid}/resign", json={}).json()
    assert body["result"] == "loss"
    assert body["outcome"] == "threw_it"
    assert body["passed"] is False


def test_a_finished_game_lands_in_the_summary_and_offers_a_pgn(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    client = client_for(app)
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    sid = client.post("/api/endgame/start", json={}).json()["session_id"]
    client.post(f"/api/endgame/{sid}/resign", json={})

    summary = client.get("/api/endgame/summary").json()
    assert summary["played"] == 1
    assert summary["buckets"][DRAWN]["played"] == 1
    assert summary["buckets"][DRAWN]["loss"] == 1
    assert summary["buckets"][DRAWN]["pass_rate"] == 0.0
    assert len(summary["recent"]) == 1

    pgn = client.get(f"/api/endgame/{sid}/pgn").json()
    assert "[FEN " in pgn["pgn"]
    assert pgn["outcome"] == "lost_a_draw"


def test_a_finished_session_refuses_further_moves(tmp_path: Path) -> None:
    client, _, _, state = start_level_game(tmp_path)
    sid = state["session_id"]
    client.post(f"/api/endgame/{sid}/resign", json={})
    res = client.post(f"/api/endgame/{sid}/move", json={"move": "g2g3"})
    assert res.status_code == 400
    assert "already finished" in res.json()["detail"]


def test_one_user_cannot_touch_anothers_session(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    owner = client_for(app, "owner@x.com")
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)
    sid = owner.post("/api/endgame/start", json={}).json()["session_id"]

    intruder = TestClient(app, raise_server_exceptions=False)
    intruder.post("/api/auth/register", json={"email": "b@x.com", "password": "secret1"})
    assert intruder.post(f"/api/endgame/{sid}/move",
                        json={"move": "g2g3"}).status_code == 400


# --------------------------------------------------------------------------- #
# The live-analysis arrows                                                     #
# --------------------------------------------------------------------------- #


def test_the_hint_returns_both_arrows(tmp_path: Path) -> None:
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = eval_of(30)
    app.state.maia_topk_fn = lambda fen, net, k: ["f2f4"]
    client = client_for(app)

    body = client.post("/api/endgame/hint",
                       json={"fen": LEVEL_FEN, "maia_rating": 1500}).json()
    assert body["best"]["uci"] == "g2g3"
    assert body["human"]["uci"] == "f2f4"
    assert body["human"]["rating"] == 1500


def test_the_hint_degrades_to_whichever_engine_is_present(tmp_path: Path) -> None:
    """A missing Maia must not take the Stockfish arrow down with it."""

    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = eval_of(30)
    app.state.maia_topk_fn = None
    client = client_for(app)

    body = client.post("/api/endgame/hint", json={"fen": LEVEL_FEN}).json()
    assert body["best"]["uci"] == "g2g3"
    assert body["human"] is None


def test_the_hint_refuses_an_illegal_position(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    res = client.post("/api/endgame/hint",
                      json={"fen": "8/1P6/P7/8/8/8/6k1/6K1 w - - 0 1"})
    assert res.status_code == 400


def test_a_small_corpus_repeats_rather_than_running_dry(tmp_path: Path) -> None:
    """The recent-position exclusion is a preference, not a hard filter.

    With one endgame seeded, the window would otherwise swallow the pool and
    the arena would refuse to deal a second game.
    """

    db_path = tmp_path / "t.db"
    app = create_app(db_path)
    app.state.playout_move_fn = always("g7g8")
    client = client_for(app)
    seed_puzzle_endgame(db_path, LEVEL_FEN, 0.0)

    for _ in range(3):
        res = client.post("/api/endgame/start", json={})
        assert res.status_code == 200, res.text
        client.post(f"/api/endgame/{res.json()['session_id']}/resign", json={})
