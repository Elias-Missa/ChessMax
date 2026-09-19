"""Daily check-in + opening repertoire API. Engine-free: neither touches one."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import chess
import chess.pgn
from fastapi.testclient import TestClient

from core.repertoire import FIX, GAP, KEEP
from server import daily, db, repertoire
from server.main import create_app

DAY = "2026-09-18"


def client_for(app: Any, email: str = "a@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


def user_id_of(db_path: Path, email: str = "a@x.com") -> int:
    connection = db.connect(db_path)
    row = connection.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    connection.close()
    return int(row["id"])


def today_iso() -> str:
    """The real local day the routes will clamp a test's value to."""

    return datetime.now(timezone.utc).date().isoformat()


def pgn_of(sans: list[str], *, headers: dict[str, str] | None = None) -> str:
    game = chess.pgn.Game()
    for key, value in (headers or {}).items():
        game.headers[key] = value
    node = game
    board = chess.Board()
    for san in sans:
        move = board.parse_san(san)
        node = node.add_variation(move)
        board.push(move)
    return str(game)


LONDON = ["d4", "d5", "Bf4", "Nf6", "e3", "e6", "Nf3", "Be7"]
CARO = ["e4", "c6", "d4", "d5", "Nc3", "dxe4", "Nxe4", "Bf5"]


def seed_game(
    db_path: Path,
    user_id: int,
    game_id: str,
    sans: list[str],
    *,
    color: str = "white",
    result: str = "1-0",
    opening: str = "Queen's Pawn Game: London System",
    eco: str = "D02",
    moves: list[tuple[int, str, dict[str, Any], float | None]] | None = None,
) -> None:
    connection = db.connect(db_path)
    connection.execute(
        "INSERT OR REPLACE INTO games (game_id, source, pgn, result, opening_name, "
        "eco, white_name, black_name, played_at) VALUES (?,'chesscom',?,?,?,?,?,?,?)",
        (game_id, pgn_of(sans), result, opening, eco, "me", "them", "2026-09-01"),
    )
    review_id = f"rev-{game_id}"
    connection.execute(
        "INSERT OR REPLACE INTO reviews (review_id, user_id, game_id, user_color, "
        "depth_tier, status) VALUES (?,?,?,?,'full','complete')",
        (review_id, user_id, game_id, color),
    )
    for ply, san, detail, delta_w in moves or []:
        connection.execute(
            "INSERT OR REPLACE INTO review_moves (review_id, ply, san, is_user_move, "
            "phase, delta_w, detail) VALUES (?,?,?,1,'opening',?,?)",
            (review_id, ply, san, delta_w, json.dumps(detail)),
        )
    connection.commit()
    connection.close()


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #


def test_every_route_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "t.db"), raise_server_exceptions=False)
    assert client.get("/api/daily/today").status_code == 401
    assert client.get("/api/daily/calendar").status_code == 401
    assert client.post("/api/daily/phase/puzzles/start", json={}).status_code == 401
    assert client.get("/api/repertoire").status_code == 401
    assert client.get("/api/repertoire/drill").status_code == 401


def test_an_unknown_phase_is_a_404(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    assert client.post("/api/daily/phase/nope/start", json={}).status_code == 404


# --------------------------------------------------------------------------- #
# The session                                                                  #
# --------------------------------------------------------------------------- #


def test_today_creates_five_pending_phases_in_running_order(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))

    body = client.get(f"/api/daily/today?day={DAY}").json()

    assert [p["key"] for p in body["phases"]] == list(daily.PHASE_KEYS)
    assert {p["status"] for p in body["phases"]} == {"pending"}
    assert body["phases_done"] == 0
    assert body["complete"] is False
    assert body["next_phase"] == "openings"
    assert all(p["target_seconds"] == daily.TARGET_SECONDS for p in body["phases"])


def test_only_one_phase_is_ever_active(tmp_path: Path) -> None:
    """Two running clocks would both take heartbeats and the day's total is fiction."""

    client = client_for(create_app(tmp_path / "t.db"))
    client.post("/api/daily/phase/puzzles/start", json={"day": DAY})

    body = client.post("/api/daily/phase/endgame/start", json={"day": DAY}).json()

    active = [p["key"] for p in body["phases"] if p["status"] == "active"]
    assert active == ["endgame"]
    assert body["active_phase"] == "endgame"


def test_heartbeats_accumulate_onto_the_phase_and_the_day(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    client.post("/api/daily/phase/puzzles/start", json={"day": DAY})

    client.post("/api/daily/phase/puzzles/heartbeat", json={"day": DAY, "seconds": 30})
    body = client.post(
        "/api/daily/phase/puzzles/heartbeat", json={"day": DAY, "seconds": 45}
    ).json()

    assert body["seconds_spent"] == 75
    assert client.get(f"/api/daily/today?day={DAY}").json()["seconds_total"] == 75


def test_one_heartbeat_cannot_credit_a_whole_night(tmp_path: Path) -> None:
    """A tab left open is not practice; the server caps what one jump may add."""

    client = client_for(create_app(tmp_path / "t.db"))
    client.post("/api/daily/phase/puzzles/start", json={"day": DAY})

    body = client.post(
        "/api/daily/phase/puzzles/heartbeat", json={"day": DAY, "seconds": 3600}
    ).json()

    assert body["seconds_spent"] == daily.MAX_HEARTBEAT_SECONDS


def test_finishing_every_phase_completes_the_day(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))

    body: dict[str, Any] = {}
    for key in daily.PHASE_KEYS:
        body = client.post(
            f"/api/daily/phase/{key}/finish", json={"day": DAY, "seconds": 10}
        ).json()

    assert body["phases_done"] == 5
    assert body["complete"] is True
    assert body["seconds_total"] == 50


def test_a_skipped_phase_does_not_count_as_done(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    for key in daily.PHASE_KEYS[:-1]:
        client.post(f"/api/daily/phase/{key}/finish", json={"day": DAY})

    body = client.post(
        "/api/daily/phase/review/finish", json={"day": DAY, "status": "skipped"}
    ).json()

    assert body["phases_done"] == 4
    assert body["complete"] is False


def test_reopening_a_phase_undoes_it(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    client.post("/api/daily/phase/puzzles/finish", json={"day": DAY, "seconds": 20})

    body = client.post("/api/daily/phase/puzzles/reopen", json={"day": DAY}).json()

    phase = next(p for p in body["phases"] if p["key"] == "puzzles")
    assert phase["status"] == "pending"
    assert body["phases_done"] == 0
    # The time already spent is not thrown away with the verdict.
    assert phase["seconds_spent"] == 20


def test_re_entering_a_finished_phase_keeps_counting(tmp_path: Path) -> None:
    """Five minutes is a floor. Extra practice after "done" is still practice."""

    client = client_for(create_app(tmp_path / "t.db"))
    client.post("/api/daily/phase/puzzles/finish", json={"day": DAY, "seconds": 20})

    client.post("/api/daily/phase/puzzles/start", json={"day": DAY})
    body = client.post(
        "/api/daily/phase/puzzles/heartbeat", json={"day": DAY, "seconds": 15}
    ).json()

    assert body["seconds_spent"] == 35


# --------------------------------------------------------------------------- #
# Days, calendar, streak                                                       #
# --------------------------------------------------------------------------- #


def test_the_client_day_is_accepted_but_clamped_to_reality() -> None:
    today = datetime.now(timezone.utc).date()

    assert daily.normalize_day((today - timedelta(days=1)).isoformat()) == (
        today - timedelta(days=1)
    ).isoformat()
    assert daily.normalize_day("2099-01-01") == (today + timedelta(days=1)).isoformat()
    assert daily.normalize_day("1999-01-01") == (today - timedelta(days=1)).isoformat()
    assert daily.normalize_day("not-a-date") == today.isoformat()
    assert daily.normalize_day(None) == today.isoformat()


def test_the_calendar_reports_which_phases_a_day_actually_finished(
    tmp_path: Path,
) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    day = today_iso()
    client.post("/api/daily/phase/puzzles/finish", json={"day": day, "seconds": 60})
    client.post("/api/daily/phase/endgame/finish", json={"day": day, "seconds": 60})

    body = client.get(f"/api/daily/calendar?month={day[:7]}&day={day}").json()

    cell = next(d for d in body["days"] if d["day"] == day)
    assert cell["phases_done"] == 2
    assert cell["done_phases"] == ["puzzles", "endgame"]  # running order, not insert order
    assert cell["complete"] is False
    assert [p["key"] for p in body["phases"]] == list(daily.PHASE_KEYS)


def test_month_bounds_handles_december(tmp_path: Path) -> None:
    label, first, last = daily.month_bounds("2026-12")

    assert (label, first, last) == ("2026-12", "2026-12-01", "2026-12-31")


def complete_day(db_path: Path, user_id: int, day: str) -> None:
    connection = db.connect(db_path)
    daily.get_or_create_session(connection, user_id, day)
    session = connection.execute(
        "SELECT id FROM daily_sessions WHERE user_id = ? AND day = ?", (user_id, day)
    ).fetchone()
    for key in daily.PHASE_KEYS:
        daily.finish_phase(connection, int(session["id"]), key)
    connection.close()


def test_the_streak_counts_back_from_yesterday_when_today_is_unfinished(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    today = date.fromisoformat(today_iso())
    for back in (1, 2, 3):
        complete_day(db_path, uid, (today - timedelta(days=back)).isoformat())

    connection = db.connect(db_path)
    streak = daily.streaks(connection, uid, today.isoformat())
    connection.close()

    # Today is not over, so it has not been missed.
    assert streak["current"] == 3
    assert streak["best"] == 3
    assert streak["total_days"] == 3


def test_a_missed_day_breaks_the_streak_but_not_the_best(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    today = date.fromisoformat(today_iso())
    for back in (1, 3, 4, 5):  # yesterday, then a gap at 2
        complete_day(db_path, uid, (today - timedelta(days=back)).isoformat())

    connection = db.connect(db_path)
    streak = daily.streaks(connection, uid, today.isoformat())
    connection.close()

    assert streak["current"] == 1
    assert streak["best"] == 3


# --------------------------------------------------------------------------- #
# The review phase's pick                                                      #
# --------------------------------------------------------------------------- #


def test_the_review_phase_picks_a_loss_and_opens_it_at_the_worst_move(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    seed_game(
        db_path,
        uid,
        "lost",
        LONDON,
        color="white",
        result="0-1",
        moves=[
            (3, "Bf4", {"fen_before": chess.STARTING_FEN, "move_uci": "c1f4"}, 4.0),
            (7, "Nf3", {"fen_before": chess.STARTING_FEN, "move_uci": "g1f3"}, 31.5),
        ],
    )

    body = client.post("/api/daily/phase/review/start", json={"day": DAY}).json()

    payload = next(p for p in body["phases"] if p["key"] == "review")["payload"]
    assert payload["game_id"] == "lost"
    assert payload["ply"] == 7
    assert payload["san"] == "Nf3"
    assert payload["delta_w"] == 31.5


def test_a_won_game_is_never_the_loss_of_the_day(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    seed_game(db_path, uid, "won", LONDON, color="white", result="1-0")

    body = client.post("/api/daily/phase/review/start", json={"day": DAY}).json()

    assert next(p for p in body["phases"] if p["key"] == "review")["payload"] is None


def test_the_same_loss_comes_back_when_the_phase_is_re_entered(tmp_path: Path) -> None:
    """"One loss, reviewed" must not be a slot machine between page loads."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(6):
        seed_game(db_path, uid, f"lost{i}", LONDON, color="white", result="0-1")

    first = client.post("/api/daily/phase/review/start", json={"day": DAY}).json()
    client.post("/api/daily/phase/puzzles/start", json={"day": DAY})
    second = client.post("/api/daily/phase/review/start", json={"day": DAY}).json()

    def picked(body: dict[str, Any]) -> str:
        return next(p for p in body["phases"] if p["key"] == "review")["payload"]["game_id"]

    assert picked(first) == picked(second)


def test_a_loss_whose_review_found_the_moment_is_preferred(tmp_path: Path) -> None:
    """The phase is "opened at the move it turned" — so prefer one we can locate."""

    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(8):
        seed_game(db_path, uid, f"blank{i}", LONDON, color="white", result="0-1")
    seed_game(
        db_path, uid, "located", CARO, color="black", result="1-0",
        opening="Caro-Kann Defense", eco="B18",
        moves=[(6, "dxe4", {"fen_before": chess.STARTING_FEN, "move_uci": "d5e4"}, 22.0)],
    )

    connection = db.connect(db_path)
    picks = {daily.pick_loss(connection, uid)["game_id"] for _ in range(12)}
    connection.close()

    assert picks == {"located"}


def test_a_thin_history_repeats_a_loss_rather_than_running_out(tmp_path: Path) -> None:
    """Recently-shown is a preference, not a filter — the Endgame Arena rule."""

    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    seed_game(db_path, uid, "only", LONDON, color="black", result="1-0")

    connection = db.connect(db_path)
    session = daily.get_or_create_session(connection, uid, DAY)
    connection.execute(
        "UPDATE daily_phase_runs SET payload = ? WHERE session_id = ? AND phase = 'review'",
        (json.dumps({"game_id": "only"}), int(session["id"])),
    )
    connection.commit()
    pick = daily.pick_loss(connection, uid)
    connection.close()

    assert pick is not None
    assert pick["game_id"] == "only"
    assert pick["repeat"] is True


# --------------------------------------------------------------------------- #
# Repertoire                                                                   #
# --------------------------------------------------------------------------- #


def test_a_cold_account_gets_a_reason_not_an_empty_page(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))

    body = client.get("/api/repertoire").json()

    assert body["available"] is False
    assert "Review a few of your games" in body["reason"]
    assert body["lines"] == {"white": [], "black": []}


def test_the_repertoire_is_built_from_the_users_own_games(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(5):
        seed_game(db_path, uid, f"w{i}", LONDON, color="white")
    for i in range(4):
        seed_game(
            db_path, uid, f"b{i}", CARO, color="black",
            opening="Caro-Kann Defense", eco="B18",
        )

    body = client.get("/api/repertoire").json()

    assert body["available"] is True
    assert body["games"] == 9
    assert body["games_by_color"] == {"white": 5, "black": 4}
    white = body["lines"]["white"][0]
    assert white["opening"] == "Queen's Pawn Game: London System"
    assert [m["san"] for m in white["moves"]] == LONDON
    assert all(m["verdict"] == KEEP for m in white["moves"] if m["user_to_move"])
    assert all(m["verdict"] is None for m in white["moves"] if not m["user_to_move"])


def test_a_game_reviewed_twice_counts_once(tmp_path: Path) -> None:
    """Two depth tiers are two reviews of one game, not two games played."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(4):
        seed_game(db_path, uid, f"w{i}", LONDON, color="white")
    connection = db.connect(db_path)
    connection.execute(
        "INSERT INTO reviews (review_id, user_id, game_id, user_color, depth_tier, "
        "status) VALUES ('rev-w0-shallow', ?, 'w0', 'white', 'shallow', 'complete')",
        (uid,),
    )
    connection.commit()
    connection.close()

    assert client.get("/api/repertoire").json()["games"] == 4


def test_review_evidence_promotes_a_habit_to_a_fix(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    board = chess.Board()
    for san in CARO[:7]:
        board.push_san(san)
    fen_before_bf5 = board.fen()
    for i in range(4):
        seed_game(
            db_path, uid, f"b{i}", CARO, color="black", result="1-0",
            opening="Caro-Kann Defense", eco="B18",
            moves=[(
                8,
                "Bf5",
                {
                    "fen_before": fen_before_bf5,
                    "move_uci": "c8f5",
                    "top_lines": [{"uci": "b8d7", "san": "Nd7", "eval_cp": 20}],
                },
                8.0,
            )],
        )

    body = client.get("/api/repertoire").json()

    line = body["lines"]["black"][0]
    move = next(m for m in line["moves"] if m["san"] == "Bf5")
    assert move["verdict"] == FIX
    assert move["recommended_san"] == "Nd7"
    assert move["mean_loss"] == 8.0
    assert body["counts"]["fix"] == 1


def test_a_pgn_that_starts_from_a_set_up_position_is_not_an_opening(
    tmp_path: Path,
) -> None:
    """An arena game's first move is not a first move."""

    fen = "8/5pk1/8/8/8/8/5PK1/8 w - - 0 1"
    assert repertoire.mainline_uci(pgn_of([], headers={"FEN": fen, "SetUp": "1"}), 16) == []
    assert repertoire.mainline_uci(pgn_of(LONDON), 16) == [
        "d2d4", "d7d5", "c1f4", "g8f6", "e2e3", "e7e6", "g1f3", "f8e7",
    ]
    assert repertoire.mainline_uci("not a pgn at all", 16) == []


def test_the_drill_queue_leads_with_the_fix(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    board = chess.Board()
    for san in CARO[:7]:
        board.push_san(san)
    for i in range(4):
        seed_game(db_path, uid, f"w{i}", LONDON, color="white")
        seed_game(
            db_path, uid, f"b{i}", CARO, color="black", result="1-0",
            opening="Caro-Kann Defense", eco="B18",
            moves=[(
                8,
                "Bf5",
                {
                    "fen_before": board.fen(),
                    "move_uci": "c8f5",
                    "top_lines": [{"uci": "b8d7", "san": "Nd7", "eval_cp": 20}],
                },
                9.0,
            )],
        )

    body = client.get("/api/repertoire/drill?limit=20").json()

    assert body["available"] is True
    first = body["cards"][0]
    assert first["verdict"] == FIX
    assert first["expected_uci"] == "b8d7"
    assert first["played_uci"] == "c8f5"
    assert first["color"] == "black"
    # The card carries the line that reached it, so the drill can show it.
    assert first["line_san"] == CARO[:7]


def test_the_drill_can_be_narrowed_to_one_colour(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(4):
        seed_game(db_path, uid, f"w{i}", LONDON, color="white")
        seed_game(db_path, uid, f"b{i}", CARO, color="black")

    body = client.get("/api/repertoire/drill?color=white").json()

    assert body["cards"]
    assert {c["color"] for c in body["cards"]} == {"white"}
    assert client.get("/api/repertoire/drill?color=green").status_code == 422


def test_an_attempt_is_graded_and_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(4):
        seed_game(db_path, uid, f"w{i}", LONDON, color="white")
    card = client.get("/api/repertoire/drill").json()["cards"][0]

    hit = client.post(
        "/api/repertoire/attempt",
        json={
            "card_id": card["card_id"],
            "fen": card["fen"],
            "expected_uci": card["expected_uci"],
            "played_uci": card["expected_uci"],
            "verdict": card["verdict"],
        },
    ).json()
    miss = client.post(
        "/api/repertoire/attempt",
        json={
            "card_id": card["card_id"],
            "fen": card["fen"],
            "expected_uci": card["expected_uci"],
            "played_uci": "e2e4",
            "verdict": card["verdict"],
        },
    ).json()

    assert hit["correct"] is True
    assert miss["correct"] is False
    assert miss["played_san"] == "e4"
    assert miss["attempts"] == 2 and miss["hits"] == 1


def test_an_illegal_answer_is_rejected_rather_than_logged(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))

    res = client.post(
        "/api/repertoire/attempt",
        json={
            "card_id": "x",
            "fen": chess.STARTING_FEN,
            "expected_uci": "d2d4",
            "played_uci": "a1a8",
            "verdict": "keep",
        },
    )

    assert res.status_code == 422
    assert "not legal" in res.json()["detail"]


def test_a_failed_card_comes_back_before_one_answered_correctly(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    for i in range(4):
        seed_game(db_path, uid, f"w{i}", LONDON, color="white")
    cards = client.get("/api/repertoire/drill").json()["cards"]
    hit, missed = cards[0], cards[1]

    for card, played in ((hit, hit["expected_uci"]), (missed, None)):
        client.post(
            "/api/repertoire/attempt",
            json={
                "card_id": card["card_id"],
                "fen": card["fen"],
                "expected_uci": card["expected_uci"],
                "played_uci": played or wrong_move(card["fen"], card["expected_uci"]),
                "verdict": card["verdict"],
            },
        )

    after = client.get("/api/repertoire/drill").json()["cards"]
    order = [c["card_id"] for c in after]
    assert order.index(missed["card_id"]) < order.index(hit["card_id"])


def wrong_move(fen: str, expected: str) -> str:
    board = chess.Board(fen)
    for move in board.legal_moves:
        if move.uci() != expected:
            return move.uci()
    raise AssertionError("position has only one legal move")


def test_opening_roi_from_the_latest_insights_run_orders_the_lines(
    tmp_path: Path,
) -> None:
    """The drill and the Insights dashboard must not recommend different openings."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    queens_gambit = ["d4", "d5", "c4", "e6", "Nc3", "Nf6"]
    for i in range(6):
        seed_game(db_path, uid, f"l{i}", LONDON, color="white")
    for i in range(4):
        seed_game(
            db_path, uid, f"q{i}", queens_gambit, color="white",
            opening="Queen's Gambit Declined", eco="D30",
        )
    connection = db.connect(db_path)
    connection.execute(
        "INSERT INTO insight_runs (run_id, user_id, chesscom_handle, window_days, "
        "time_class, status, metrics) VALUES ('r1', ?, 'me', 30, 'blitz', "
        "'complete', ?)",
        (
            uid,
            json.dumps(
                {
                    "pro": {
                        "openings": {
                            "roi": {
                                "rows": [
                                    {
                                        "color": "white",
                                        "opening": "Queen's Gambit Declined",
                                        "roi_score": 7.5,
                                    }
                                ]
                            }
                        }
                    }
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    body = client.get("/api/repertoire").json()

    # The London is played more often, but the QGD is where the points are.
    assert body["lines"]["white"][0]["opening"] == "Queen's Gambit Declined"
    assert body["lines"]["white"][0]["roi_score"] == 7.5
    assert body["roi_openings"] == 1


def test_evidence_matches_a_position_reached_by_a_different_move_order(
    tmp_path: Path,
) -> None:
    """Transpositions carry different clocks; the key must ignore them."""

    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    stale_clocks = chess.STARTING_FEN.replace(" 0 1", " 4 12")
    seed_game(
        db_path, uid, "g1", LONDON, color="white",
        moves=[(
            1,
            "d4",
            {
                "fen_before": stale_clocks,
                "move_uci": "d2d4",
                "top_lines": [{"uci": "e2e4", "san": "e4"}],
            },
            6.0,
        )],
    )

    connection = db.connect(db_path)
    evidence = repertoire.load_evidence(connection, uid)
    connection.close()

    from core.repertoire import fen_key

    record = evidence[fen_key(chess.STARTING_FEN)]
    assert record.best_uci == "e2e4"
    assert record.loss_by_uci["d2d4"] == 6.0


def test_two_habits_at_one_position_average_independently(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client_for(create_app(db_path))
    uid = user_id_of(db_path)
    seed_game(
        db_path, uid, "g1", LONDON, color="white",
        moves=[
            (1, "d4", {"fen_before": chess.STARTING_FEN, "move_uci": "d2d4"}, 2.0),
            (3, "d4", {"fen_before": chess.STARTING_FEN, "move_uci": "d2d4"}, 8.0),
            (5, "e4", {"fen_before": chess.STARTING_FEN, "move_uci": "e2e4"}, 1.0),
        ],
    )

    connection = db.connect(db_path)
    evidence = repertoire.load_evidence(connection, uid)
    connection.close()

    from core.repertoire import fen_key

    record = evidence[fen_key(chess.STARTING_FEN)]
    assert record.loss_by_uci == {"d2d4": 5.0, "e2e4": 1.0}
    assert record.n_by_uci == {"d2d4": 2, "e2e4": 1}


def test_an_inconsistent_node_reads_as_a_gap_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    uid = user_id_of(db_path)
    good = ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]
    bad = ["d4", "d5", "c4", "Nf6", "Nc3", "e6"]
    for i in range(5):
        seed_game(db_path, uid, f"g{i}", good, color="white", result="1-0")
        seed_game(db_path, uid, f"b{i}", bad, color="white", result="0-1")

    body = client.get("/api/repertoire").json()

    verdicts = {
        m["san"]: m["verdict"]
        for line in body["lines"]["white"]
        for m in line["moves"]
        if m["user_to_move"]
    }
    assert verdicts["Bf4"] == GAP
    assert verdicts["c4"] == GAP
    assert body["counts"]["gap"] >= 2
