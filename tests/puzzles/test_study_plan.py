"""The study plan — engine-free, pure over a computed metrics blob."""

from __future__ import annotations

import json
import sqlite3

import chess
import pytest

from server import db
from server.game_shape import centre_type, endgame_type, game_shape
from server.insights_metrics import compute_tier1_metrics
from server.insights_narrative import BANNED_JARGON
from server.study_plan import (
    MIN_GAMES,
    PLAN_SCHEMA,
    build_study_plan,
    ensure_study_plan,
    plan_strings,
)

# A full game, so the shape helpers have something real to replay: a French
# Advance (closed centre) that trades into a minor-piece ending.
LINE = (
    "1. e4 e6 2. d4 d5 3. e5 c5 4. c3 Nc6 5. Nf3 Qb6 6. a3 Nh6 7. b4 cxd4 8. cxd4 Nf5 "
    "9. Bb2 Bd7 10. Be2 Rc8 11. O-O Be7 12. Nc3 Na5 13. Na4 Qa6 14. Nc5 Bxc5 15. bxc5 Nc4 "
    "16. Bxc4 dxc4 17. Qe2 b5 18. cxb6 axb6 19. Rfc1 O-O 20. Rxc4 Rxc4 21. Qxc4 Qxc4 "
    "22. Rc1 Qxc1+ 23. Bxc1 Rc8 24. Bd2 Nxd4 25. Nxd4 Rc1+ 26. Bxc1 b5 27. Kf1 g6 "
    "28. Ke2 h5 29. Kd3 f6 30. exf6 Kf7 31. f4 Ke8 32. f5 gxf5 33. Kd2 e5 34. Nxf5 e4 35. Ke3 h4 *"
)

OPENINGS = [
    ("Caro-Kann Defense", "B12", "black"),
    ("Italian Game", "C50", "white"),
    ("French Defense: Advance Variation", "C02", "black"),
    ("Queen's Gambit Declined", "D30", "white"),
]


def _connection(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "plan.db")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.commit()
    return conn


def _seed(connection: sqlite3.Connection, *, games: int = 24) -> list[str]:
    """A window with a losing colour, a repeated motif and a clock problem."""

    uid = int(connection.execute("SELECT id FROM users").fetchone()["id"])
    for i in range(games):
        name, eco, color = OPENINGS[i % 4]
        lost = color == "black" and i % 4 != 3
        result = ("0-1" if color == "white" else "1-0") if lost else (
            "1-0" if color == "white" else "0-1"
        )
        gid, rid = f"g{i}", f"r{i}"
        pgn = (
            f'[Event "Live"]\n[White "p{i}"]\n[Black "q{i}"]\n[Result "{result}"]\n'
            f'[UTCTime "1{i % 9}:20:00"]\n\n{LINE}\n'
        )
        connection.execute(
            "INSERT INTO games (game_id, source, pgn, white_name, black_name, "
            "white_rating, black_rating, result, played_at, eco, opening_name) "
            "VALUES (?, 'chesscom', ?, ?, ?, 1480, 1500, ?, ?, ?, ?)",
            (gid, pgn, f"p{i}", f"q{i}", result,
             f"2026-08-{(i % 27) + 1:02d}T1{i % 9}:20:00Z", eco, name),
        )
        connection.execute(
            "INSERT INTO reviews (review_id, user_id, game_id, user_color, depth_tier, "
            "status, progress, accuracy, total_loss, fixable_loss, loss_type) "
            "VALUES (?, ?, ?, ?, 'full', 'complete', 1, ?, ?, ?, ?)",
            (rid, uid, gid, color, 64 if lost else 72, 40.0 if lost else 18.0,
             26.0 if lost else 8.0, "blunder" if lost else "bleed"),
        )
        plies = [
            (1, "e4", "opening", 1, 0.0, 15, None, None, 120, None),
            (11, "Qb6", "opening", 0, 2.0, 25, 70, 12, 90, None),
            (25, "Na5", "middlegame", 0, 9.0 if lost else 2.0, 50, 55, 8, 60, None),
            (31, "Nc4", "middlegame", 0, 32.0 if lost else 3.0, 72, 74, 4, 25, '["fork"]'),
            (41, "Rc1", "middlegame", 0, 12.0 if lost else 2.0, 65, 66, 3, 8, '["pin"]'),
            (63, "Kd3", "endgame", 0, 14.0 if lost else 1.0, 45, 58, 5, 40, None),
            (69, "f5", "endgame", 0, 6.0, 30, 40, 20, 55, None),
        ]
        detail = json.dumps({
            "fen_before": "8/8/8/8/8/8/8/8 w - - 0 1",
            "move_uci": "e2e4",
            "top_lines": [{"uci": "d8h4", "san": "Qh4+", "eval_cp": 40}],
        })
        for ply, san, phase, book, dw, vol, find, spent, clock, tags in plies:
            connection.execute(
                "INSERT INTO review_moves (review_id, ply, san, is_user_move, phase, "
                "is_book, classification, win_prob, delta_w, volatility, findability, "
                "time_spent, clock_remaining, detail, tactic_tags) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, ply, san, phase, book, "blunder" if dw > 25 else "good",
                 0.75 if ply < 30 else 0.35, dw, vol, find, spent, clock, detail, tags),
            )
            connection.execute(
                "INSERT INTO review_moves (review_id, ply, san, is_user_move, phase, "
                "is_book, classification, win_prob, delta_w, volatility, findability, "
                "time_spent, clock_remaining, detail, tactic_tags) "
                "VALUES (?, ?, 'Nf3', 0, ?, 0, 'good', 0.5, 2.0, 30, NULL, 5, ?, ?, NULL)",
                (rid, ply + 1, phase, clock, detail),
            )
    connection.commit()
    return [str(r["review_id"]) for r in connection.execute("SELECT review_id FROM reviews")]


@pytest.fixture
def metrics(tmp_path) -> dict:
    connection = _connection(tmp_path)
    return compute_tier1_metrics(connection, review_ids=_seed(connection))


# ── Game shape ───────────────────────────────────────────────────────────────


def test_centre_type_reads_the_pawn_structure() -> None:
    closed = chess.Board("rnbqkbnr/pp3ppp/4p3/2ppP3/3P4/8/PPP2PPP/RNBQKBNR w KQkq - 0 4")
    assert centre_type(closed) == "closed"
    bare = chess.Board("4r1k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1")
    assert centre_type(bare) == "open"


@pytest.mark.parametrize(
    "fen,expected",
    [
        ("8/5ppp/8/8/8/8/5PPP/8 w - - 0 1", "pawn"),
        ("4r1k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1", "rook"),
        ("4rbk1/5ppp/8/8/8/8/5PPP/4RBK1 w - - 0 1", "rook_minor"),
        ("6k1/5ppp/8/8/8/8/5PPP/3Q2K1 w - - 0 1", "queen"),
        ("5bk1/5ppp/8/8/8/8/5PPP/5BK1 w - - 0 1", "minor"),
    ],
)
def test_endgame_type_names_the_material(fen: str, expected: str) -> None:
    assert endgame_type(chess.Board(fen)) == expected


def test_game_shape_is_null_rather_than_guessed_when_the_pgn_will_not_replay() -> None:
    shape = game_shape("not a pgn at all", middlegame_ply=21, endgame_ply=61)
    assert shape == {"centre": None, "endgame_type": None}
    assert game_shape(f'[Result "*"]\n\n{LINE}\n', middlegame_ply=None, endgame_ply=None) == {
        "centre": None,
        "endgame_type": None,
    }


def test_game_facts_carry_the_shape(metrics: dict) -> None:
    facts = metrics["game_explorer"]
    assert facts
    # Every game here is the same French Advance walked to a minor-piece ending.
    assert {f["centre"] for f in facts} == {"closed"}
    assert {f["endgame_type"] for f in facts} == {"minor"}


# ── The plan ─────────────────────────────────────────────────────────────────


def test_plan_is_attached_to_metrics_and_names_the_losing_opening(metrics: dict) -> None:
    plan = metrics["study_plan"]
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["available"] is True

    openings = next(b for b in plan["blocks"] if b["id"] == "openings")
    lines = openings["evidence"]["lines"]
    # Black is losing three lines in four; the plan must name one, not "openings".
    assert lines[0]["color"] == "black"
    assert lines[0]["games"] >= 3
    assert lines[0]["score_pct"] < 0.45
    assert lines[0]["opening"] in {name for name, _, _ in OPENINGS}


def test_every_block_carries_evidence_and_a_way_to_act_on_it(metrics: dict) -> None:
    plan = metrics["study_plan"]
    for block in plan["blocks"] + plan["habits"]:
        assert block["why"].strip(), block["id"]
        assert block["evidence"], block["id"]
        assert block["drills"], block["id"]
        assert block["measure"].strip(), block["id"]
        for drill in block["drills"]:
            assert drill["kind"] in {"review", "trainer", "study", "rule"}
            if drill["kind"] == "review":
                assert drill["game_id"], block["id"]
            if drill["kind"] == "trainer":
                assert str(drill["route"]).startswith("/"), block["id"]


def test_review_drills_land_on_a_ply_so_the_user_does_not_hunt(metrics: dict) -> None:
    reviews = [
        d
        for b in metrics["study_plan"]["blocks"]
        for d in b["drills"]
        if d["kind"] == "review"
    ]
    assert reviews
    assert all(d["ply"] for d in reviews)


def test_a_block_never_lists_the_same_game_twice(metrics: dict) -> None:
    for block in metrics["study_plan"]["blocks"]:
        ids = [d["game_id"] for d in block["drills"] if d["kind"] == "review"]
        assert len(ids) == len(set(ids)), block["id"]


# ── Budget ───────────────────────────────────────────────────────────────────


def test_hours_respect_the_budget_and_no_block_eats_the_week(metrics: dict) -> None:
    for hours in (1, 2, 5, 12):
        plan = build_study_plan(metrics, hours_per_week=hours, weeks=4)
        spent = sum(b["hours_per_week"] for b in plan["blocks"])
        # Quarter-hour rounding is the only slack allowed.
        assert spent <= hours + 0.25, hours
        assert spent >= min(hours, 0.5) - 0.25, hours
        for block in plan["blocks"]:
            assert block["hours_per_week"] >= 0.5, (hours, block["id"])
            assert block["hours_per_week"] <= max(0.5, hours * 0.45) + 0.25


def test_a_small_budget_drops_blocks_rather_than_shaving_all_of_them(metrics: dict) -> None:
    tight = build_study_plan(metrics, hours_per_week=1, weeks=4)
    roomy = build_study_plan(metrics, hours_per_week=12, weeks=4)
    assert len(tight["blocks"]) < len(roomy["blocks"])
    # The blocks that survive are the ones that cost the player most.
    assert tight["blocks"][0]["impact"] >= tight["blocks"][-1]["impact"]


def test_habits_cost_no_study_time_so_a_tiny_budget_still_gets_them(metrics: dict) -> None:
    plan = build_study_plan(metrics, hours_per_week=1, weeks=4)
    assert plan["habits"]
    assert all(b["hours_per_week"] == 0.0 for b in plan["habits"])


def test_the_schedule_spends_exactly_the_allocated_hours(metrics: dict) -> None:
    plan = build_study_plan(metrics, hours_per_week=5, weeks=4)
    scheduled = sum(s["minutes"] for s in plan["schedule"])
    allocated = round(sum(b["hours_per_week"] for b in plan["blocks"]) * 60)
    assert scheduled == allocated
    assert all(s["items"] for s in plan["schedule"])


def test_a_session_never_lists_the_same_block_twice(metrics: dict) -> None:
    for hours in (2, 5, 8, 12, 20):
        plan = build_study_plan(metrics, hours_per_week=hours, weeks=4)
        for session in plan["schedule"]:
            blocks = [i["block"] for i in session["items"]]
            assert len(blocks) == len(set(blocks)), (hours, session["label"])


def test_inputs_are_clamped_not_trusted(metrics: dict) -> None:
    silly = build_study_plan(metrics, hours_per_week=10_000, weeks=999)
    assert silly["hours_per_week"] <= 20
    assert silly["weeks"] <= 12
    tiny = build_study_plan(metrics, hours_per_week=-4, weeks=0)
    assert tiny["hours_per_week"] >= 1
    assert tiny["weeks"] >= 1


# ── Projection ───────────────────────────────────────────────────────────────


def test_projection_never_exceeds_the_rating_actually_left_on_the_board(metrics: dict) -> None:
    ceiling = metrics["pro"]["headline"]["elo_left_on_board"]["points"]
    for hours in (1, 5, 20):
        for weeks in (1, 4, 12):
            projection = build_study_plan(
                metrics, hours_per_week=hours, weeks=weeks
            )["projection"]
            assert 0 <= projection["rating_gain"] <= ceiling, (hours, weeks)


def test_projection_is_bounded_by_a_plausible_pace(metrics: dict) -> None:
    projection = build_study_plan(metrics, hours_per_week=5, weeks=4)["projection"]
    # The measured pool here is enormous; a four-week plan must not claim it.
    assert projection["ceiling"] > projection["rating_gain"]
    assert projection["limited_by"] == "pace"
    assert projection["rating_gain"] <= projection["pace_ceiling"]


def test_more_work_projects_more_rating_with_diminishing_returns(metrics: dict) -> None:
    gains = [
        build_study_plan(metrics, hours_per_week=h, weeks=4)["projection"]["rating_gain"]
        for h in (1, 3, 5, 10, 20)
    ]
    assert gains == sorted(gains)
    assert gains[-1] < gains[0] * 20  # never linear in hours


def test_projection_lands_on_a_new_rating(metrics: dict) -> None:
    projection = build_study_plan(metrics)["projection"]
    assert projection["current_rating"] == 1480
    assert projection["projected_rating"] == 1480 + projection["rating_gain"]
    assert projection["confidence"] in {"low", "medium", "high"}


def test_block_gains_sum_to_the_projection(metrics: dict) -> None:
    plan = build_study_plan(metrics, hours_per_week=5, weeks=4)
    total = sum(b["rating_gain"] for b in plan["blocks"] + plan["habits"])
    # Per-block figures are rounded independently, so allow rounding drift only.
    assert abs(total - plan["projection"]["rating_gain"]) <= len(plan["blocks"]) + len(plan["habits"])


# ── Thin data, voice, and the read path ──────────────────────────────────────


def test_a_thin_window_says_so_instead_of_inventing_a_plan(tmp_path) -> None:
    connection = _connection(tmp_path)
    metrics = compute_tier1_metrics(connection, review_ids=_seed(connection, games=3))
    plan = metrics["study_plan"]
    assert plan["available"] is False
    assert plan["blocks"] == [] and plan["habits"] == []
    assert plan["projection"] is None
    assert str(MIN_GAMES) in plan["reason"]


def test_empty_metrics_still_produce_the_full_shape() -> None:
    plan = build_study_plan({})
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["available"] is False
    assert plan["reason"]
    for key in ("blocks", "habits", "schedule", "arc"):
        assert plan[key] == []
    assert build_study_plan(None)["available"] is False


def test_no_jargon_reaches_the_plan(metrics: dict) -> None:
    for hours in (1, 5, 20):
        strings = plan_strings(build_study_plan(metrics, hours_per_week=hours))
        assert strings
        for text in strings:
            for token in BANNED_JARGON:
                assert token.lower() not in text.lower(), (token, text)


def test_ensure_study_plan_backfills_a_run_that_predates_it(metrics: dict) -> None:
    stored = json.loads(json.dumps(metrics))
    stored.pop("study_plan")
    filled = ensure_study_plan(stored)
    assert filled["study_plan"]["schema"] == PLAN_SCHEMA
    assert filled["study_plan"]["available"] is True

    # A current plan is left alone rather than rebuilt.
    filled["study_plan"]["blocks"] = []
    assert ensure_study_plan(filled)["study_plan"]["blocks"] == []
    assert ensure_study_plan(None) is None


def test_a_stale_schema_is_rebuilt(metrics: dict) -> None:
    stored = json.loads(json.dumps(metrics))
    stored["study_plan"] = {"schema": "plan-0", "blocks": []}
    assert ensure_study_plan(stored)["study_plan"]["schema"] == PLAN_SCHEMA
    assert ensure_study_plan(stored)["study_plan"]["blocks"]


def test_the_plan_is_json_serialisable(metrics: dict) -> None:
    # It is stored in `insight_runs.metrics`, so anything exotic in a block
    # breaks the whole run rather than just this tab.
    assert json.loads(json.dumps(metrics["study_plan"]))["available"] is True


# ── The endpoint ─────────────────────────────────────────────────────────────


def _client(tmp_path):
    from fastapi.testclient import TestClient
    from server.main import create_app

    path = str(tmp_path / "api.db")
    with db.connect(path) as connection:
        db.get_singleton_user(connection)
    client = TestClient(create_app(db_path=path), raise_server_exceptions=False)
    client.post("/api/auth/register", json={"email": "t@ex.com", "password": "testpass"})
    return client, path


def _stored_run(path: str) -> str:
    """A completed run whose metrics are seeded the same way as the fixture."""

    with db.connect(path) as connection:
        review_ids = _seed(connection)
        metrics = compute_tier1_metrics(connection, review_ids=review_ids)
        uid = int(connection.execute("SELECT id FROM users").fetchone()["id"])
        connection.execute(
            "INSERT INTO insight_runs (run_id, user_id, chesscom_handle, source, "
            "window_days, time_class, status, progress, metrics) "
            "VALUES ('run-1', ?, 'me', 'chesscom', 30, 'blitz', 'complete', 1, ?)",
            (uid, json.dumps(metrics)),
        )
        connection.commit()
    return "run-1"


def test_study_plan_endpoint_rebuilds_for_a_custom_budget(tmp_path) -> None:
    client, path = _client(tmp_path)
    run_id = _stored_run(path)

    default = client.get(f"/api/insights/{run_id}/study-plan")
    assert default.status_code == 200
    assert default.json()["study_plan"]["hours_per_week"] == 5.0

    custom = client.get(
        f"/api/insights/{run_id}/study-plan",
        params={"hours_per_week": 12, "weeks": 8},
    )
    assert custom.status_code == 200
    plan = custom.json()["study_plan"]
    assert plan["hours_per_week"] == 12.0
    assert plan["weeks"] == 8
    assert plan["projection"]["rating_gain"] > default.json()["study_plan"]["projection"]["rating_gain"]


def test_study_plan_endpoint_is_scoped_to_the_caller(tmp_path) -> None:
    client, path = _client(tmp_path)
    run_id = _stored_run(path)
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"email": "other@ex.com", "password": "testpass"})
    assert client.get(f"/api/insights/{run_id}/study-plan").status_code == 404


def test_run_get_carries_the_plan(tmp_path) -> None:
    client, path = _client(tmp_path)
    run_id = _stored_run(path)
    body = client.get(f"/api/insights/{run_id}").json()
    assert body["metrics"]["study_plan"]["available"] is True
    assert body["metrics"]["study_plan"]["blocks"]
