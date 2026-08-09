"""Professional-tier Insights metrics (server.insights_pro).

Engine-free: every assertion runs off hand-seeded ``review_moves`` rows.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from server import db
from server.insights_metrics import compute_tier1_metrics
from server.insights_pro import (
    accuracy_for_delta_w,
    compute_blunder_timing,
    compute_headline,
    compute_leaks,
    performance_rating,
    rating_difference,
)


def _headers(pgn_headers: str):
    import io

    import chess.pgn

    game = chess.pgn.read_game(io.StringIO(f"{pgn_headers}\n\n1. e4 *"))
    return game.headers


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # chess.com ships only ECOUrl; the slug tails off into the move continuation.
        ('[ECOUrl "https://www.chess.com/openings/Englund-Gambit-2.dxe5"]', "Englund Gambit"),
        (
            '[ECOUrl "https://www.chess.com/openings/Sicilian-Defense-Najdorf-Variation-6.Bg5"]',
            "Sicilian Defense Najdorf Variation",
        ),
        (
            '[ECOUrl "https://www.chess.com/openings/Kings-Pawn-Opening-Kings-Knight-Variation"]',
            "Kings Pawn Opening Kings Knight Variation",
        ),
        # lichess ships a real header, which wins.
        ('[Opening "Caro-Kann Defense: Advance"]', "Caro-Kann Defense: Advance"),
        ('[Opening "?"]', None),
        ('[Event "Live Chess"]', None),
    ],
)
def test_opening_name_from_headers(headers: str, expected: str | None) -> None:
    from server.reviews import opening_name

    assert opening_name(_headers(headers)) == expected


def test_eco_code_ignores_chesscom_url_metadata() -> None:
    """Chess.com's API ``eco`` field is a URL; the code lives in the PGN header."""

    from server.reviews import eco_code

    headers = _headers('[ECO "A40"]\n[ECOUrl "https://www.chess.com/openings/Englund-Gambit"]')
    url_meta = {"eco": "https://www.chess.com/openings/Englund-Gambit-2.dxe5"}

    assert eco_code(headers, url_meta) == "A40"
    assert eco_code(headers) == "A40"
    assert eco_code(_headers('[Event "x"]'), url_meta) is None
    # A genuine code in metadata still wins.
    assert eco_code(headers, {"eco": "B12"}) == "B12"


def test_opening_name_falls_back_to_url_shaped_metadata() -> None:
    from server.reviews import opening_name

    assert opening_name(
        _headers('[Event "x"]'),
        {"eco": "https://www.chess.com/openings/Englund-Gambit-2.dxe5"},
    ) == "Englund Gambit"


def test_opening_name_prefers_ingest_metadata() -> None:
    """Lichess's API nests the name; it should beat anything in the PGN."""

    from server.reviews import opening_name

    headers = _headers('[ECOUrl "https://www.chess.com/openings/Englund-Gambit-2.dxe5"]')
    assert opening_name(headers, {"opening": {"name": "Italian Game"}}) == "Italian Game"
    assert opening_name(headers, {"opening_name": "London System"}) == "London System"


@pytest.fixture
def connection(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "pro.db")
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
    result: str,
    played_at: str,
    moves: list[dict],
    user_color: str = "white",
    accuracy: float = 70.0,
    white_rating: int = 1500,
    black_rating: int = 1500,
    eco: str = "B10",
    opening_name: str = "Caro-Kann Defense",
) -> None:
    """Insert one game + review + its user moves.

    ``moves`` entries accept: ply, san, uci, dw, wp, vol, findability, phase,
    classification, clock, time_spent, is_book, is_user.
    """

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
        "VALUES (?, ?, ?, ?, 'shallow', 'complete', 1, ?, 30, 'cliff')",
        (review_id, user_id, game_id, user_color, accuracy),
    )
    for m in moves:
        detail = {
            "fen_before": m.get("fen", "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"),
            "move_uci": m.get("uci", "e2e4"),
            "top_lines": m.get("lines", [{"uci": "e2e4", "san": "e4", "eval_cp": 40}]),
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


# ── Pure maths ────────────────────────────────────────────────────────────────


def test_rating_difference_matches_elo_curve() -> None:
    assert rating_difference(0.5) == pytest.approx(0.0, abs=1e-9)
    # A 76% score is the classic ~+200 rating gap.
    assert rating_difference(0.76) == pytest.approx(200.0, abs=3.0)
    assert rating_difference(0.24) == pytest.approx(-200.0, abs=3.0)


def test_rating_difference_clamps_a_clean_sweep() -> None:
    """A 100% score implies an infinite gap; the clamp keeps it printable."""

    assert rating_difference(1.0) == 800.0
    assert rating_difference(0.0) == -800.0


def test_performance_rating_uses_average_opponent() -> None:
    # 3/4 against an average of 1600 is roughly 1600 + 191.
    perf = performance_rating([1600, 1600, 1600, 1600], score=3.0, games=4)
    assert perf is not None
    assert 1770 <= perf <= 1810
    assert performance_rating([], score=1.0, games=1) is None


def test_accuracy_for_delta_w_is_monotonic_and_book_is_free() -> None:
    assert accuracy_for_delta_w(0) == pytest.approx(100.0, abs=0.5)
    assert accuracy_for_delta_w(5) > accuracy_for_delta_w(25)
    assert accuracy_for_delta_w(80, is_book=True) == 100.0


# ── Headline ──────────────────────────────────────────────────────────────────


def _facts(**overrides):
    base = {
        "user_rating": 1500,
        "opponent_rating": 1500,
        "points": 1.0,
        "accuracy": 80.0,
        "user_moves": 30,
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 0,
        "classification_counts": {"best": 10},
        "findable_delta_w": None,
        "findable_moves": 0,
        "expected_points": 0.5,
        "outcome": "win",
    }
    base.update(overrides)
    return base


def test_headline_record_rating_and_expectancy() -> None:
    facts = [
        _facts(points=1.0, user_rating=1520, outcome="win"),
        _facts(points=0.0, user_rating=1510, outcome="loss"),
        _facts(points=0.5, user_rating=1500, outcome="draw"),
    ]
    head = compute_headline(facts, total_loss=300.0, fixable_loss=None)

    assert head["record"] == {
        "games": 3, "decided": 3, "wins": 1, "draws": 1, "losses": 1,
        "score": 1.5, "score_pct": 0.5,
    }
    # ``facts`` is newest-first, so the timeline start is the last entry.
    assert head["rating"]["start"] == 1500
    assert head["rating"]["end"] == 1520
    assert head["rating"]["delta"] == 20
    assert head["performance_rating"] == 1500
    assert head["expectancy"]["delta"] == pytest.approx(0.0)


def test_headline_error_rates_and_clean_games() -> None:
    facts = [
        _facts(blunders=2, mistakes=1, user_moves=40),
        _facts(blunders=0, mistakes=0, user_moves=60),
    ]
    head = compute_headline(facts, total_loss=200.0, fixable_loss=None)
    rates = head["error_rates"]

    assert rates["blunders"] == 2
    assert rates["blunders_per_100"] == pytest.approx(2.0)  # 2 per 100 moves
    assert rates["moves_per_blunder"] == pytest.approx(50.0)
    assert rates["clean_game_rate"] == pytest.approx(0.5)


def test_elo_left_on_board_is_capped_by_the_result_actually_dropped() -> None:
    """A game already won cannot have points recovered in it."""

    facts = [
        # Won despite a huge findable leak — nothing to recover.
        _facts(points=1.0, findable_delta_w=200.0, findable_moves=4),
        # Lost with a findable 50 win% leak — half a point recoverable.
        _facts(points=0.0, findable_delta_w=50.0, findable_moves=2, outcome="loss"),
    ]
    elo = compute_headline(facts, total_loss=400.0, fixable_loss=250.0)["elo_left_on_board"]

    assert elo["basis"] == "findability"
    assert elo["recoverable_score"] == pytest.approx(0.5)
    assert elo["actual_score_pct"] == pytest.approx(0.5)
    assert elo["potential_score_pct"] == pytest.approx(0.75)
    assert elo["points"] > 0


def test_elo_left_on_board_falls_back_to_blunders_without_full_tier() -> None:
    facts = [_facts(points=0.0, blunders=2, findable_delta_w=None, outcome="loss")]
    elo = compute_headline(facts, total_loss=100.0, fixable_loss=None)["elo_left_on_board"]

    assert elo["basis"] == "blunders"
    # 2 blunders × 25 win% = 0.5 of a game point, and a full point was dropped.
    assert elo["recoverable_score"] == pytest.approx(0.5)


# ── Blunder timing ────────────────────────────────────────────────────────────


def test_blunder_timing_buckets_by_move_number() -> None:
    moves = {
        "r1": [
            _row(ply=5, delta_w=2.0),     # move 3
            _row(ply=41, delta_w=40.0),   # move 21 — a blunder
            _row(ply=45, delta_w=30.0),   # move 23 — a blunder
        ]
    }
    facts = [{"first_error_move": 21}]
    timing = compute_blunder_timing(facts, moves)
    by_key = {b["key"]: b for b in timing["buckets"]}

    assert by_key["1-10"]["moves"] == 1
    assert by_key["1-10"]["blunders"] == 0
    assert by_key["21-30"]["moves"] == 2
    assert by_key["21-30"]["blunders"] == 2
    assert by_key["21-30"]["blunder_rate"] == pytest.approx(1.0)
    assert timing["mean_first_error_move"] == pytest.approx(21.0)


def _row(**kwargs):
    """A dict standing in for a sqlite3.Row of ``review_moves``."""

    base = {
        "ply": 1, "san": "e4", "is_user_move": 1, "phase": "middlegame",
        "is_book": 0, "classification": None, "win_prob": 0.5, "delta_w": 0.0,
        "volatility": 40.0, "findability": None, "time_spent": None,
        "clock_remaining": None, "detail": None, "tactic_tags": None,
    }
    base.update(kwargs)
    return base


# ── Leak board ────────────────────────────────────────────────────────────────


def test_leaks_rank_by_impact_and_carry_a_practice_route() -> None:
    move_quality = {
        "by_phase": [
            {"phase": "opening", "moves": 100, "accuracy": 92.0, "delta_w_per_move": 1.0,
             "total_delta_w": 100.0, "blunder_rate": 0.0},
            {"phase": "endgame", "moves": 100, "accuracy": 60.0, "delta_w_per_move": 9.0,
             "total_delta_w": 900.0, "blunder_rate": 0.2},
        ],
    }
    critical = {
        "buckets": [
            {"key": "critical", "moves": 40, "accuracy": 55.0, "delta_w_per_move": 8.0,
             "handled_rate": 0.4, "mean_time": 4.0},
            {"key": "tense", "moves": 40, "accuracy": 70.0, "delta_w_per_move": 4.0,
             "handled_rate": 0.6, "mean_time": 8.0},
            {"key": "quiet", "moves": 40, "accuracy": 90.0, "delta_w_per_move": 1.0,
             "handled_rate": 0.9, "mean_time": 12.0},
        ],
        "criticality_gap": 35.0,
        "critical_conversion": 0.4,
        "time_note": "inverted budget",
    }
    leaks = compute_leaks(
        [{"outcome": "win"}] * 10,
        move_quality=move_quality,
        critical=critical,
        resilience={"conversion": {"n": 5, "score_pct": 0.5, "points_dropped": 2.5}},
        openings={"worst": None},
        blunder_timing={"worst_window": None, "buckets": []},
        scramble={"buckets": []},
        tier3={"after_loss": {}},
        missed_tactics={"tags": []},
    )

    ids = [l["id"] for l in leaks]
    assert "phase" in ids and "critical" in ids and "conversion" in ids
    # Ranked by cost, descending.
    impacts = [l["impact_win_pct_per_game"] for l in leaks]
    assert impacts == sorted(impacts, reverse=True)
    # Every leak is actionable.
    assert all(l["practice"] for l in leaks)
    assert all(l["severity"] in ("high", "medium", "low") for l in leaks)


def test_leaks_stay_silent_when_nothing_is_measurable() -> None:
    leaks = compute_leaks(
        [{"outcome": "win"}],
        move_quality={"by_phase": []},
        critical={"buckets": [], "criticality_gap": None},
        resilience={"conversion": {"n": 0}},
        openings={"worst": None},
        blunder_timing={"worst_window": None, "buckets": []},
        scramble={"buckets": []},
        tier3={"after_loss": {}},
        missed_tactics={"tags": []},
    )
    assert leaks == []


# ── End-to-end through compute_tier1_metrics ──────────────────────────────────


def test_pro_layer_is_attached_end_to_end(connection: sqlite3.Connection) -> None:
    _seed_game(
        connection,
        game_id="g1",
        review_id="r1",
        result="1-0",
        played_at="2026-06-01T12:00:00",
        white_rating=1600,
        black_rating=1700,
        accuracy=85.0,
        moves=[
            {"ply": 1, "san": "e4", "dw": 0.0, "phase": "opening", "is_book": True,
             "classification": "book", "vol": 20, "clock": 300, "time_spent": 1.0},
            {"ply": 3, "san": "Nf3", "dw": 1.0, "phase": "opening",
             "classification": "best", "vol": 25, "clock": 280, "time_spent": 5.0},
            {"ply": 41, "san": "Qxh7", "dw": 40.0, "phase": "endgame",
             "classification": "blunder", "vol": 75, "clock": 6, "time_spent": 2.0,
             "wp": 0.9},
        ],
    )
    _seed_game(
        connection,
        game_id="g2",
        review_id="r2",
        result="0-1",
        played_at="2026-06-01T13:00:00",
        white_rating=1590,
        black_rating=1500,
        accuracy=61.0,
        opening_name="Sicilian Defense",
        eco="B20",
        moves=[
            {"ply": 1, "san": "e4", "dw": 0.0, "phase": "opening", "is_book": True,
             "classification": "book", "vol": 20, "clock": 300},
            {"ply": 21, "san": "Nd5", "dw": 30.0, "phase": "middlegame",
             "classification": "blunder", "vol": 80, "clock": 8, "time_spent": 3.0,
             "wp": 0.8},
        ],
    )

    metrics = compute_tier1_metrics(connection, review_ids=["r1", "r2"])
    pro = metrics["pro"]

    # Headline
    head = pro["headline"]
    assert head["record"]["games"] == 2
    assert head["record"]["wins"] == 1 and head["record"]["losses"] == 1
    assert head["performance_rating"] is not None
    assert head["accuracy"]["mean"] == pytest.approx(73.0)
    assert head["error_rates"]["blunders"] == 2

    # Move quality carries a phase split with per-move accuracy.
    phases = {p["phase"]: p for p in pro["move_quality"]["by_phase"]}
    assert set(phases) == {"opening", "middlegame", "endgame"}
    assert phases["opening"]["accuracy"] > phases["endgame"]["accuracy"]
    assert pro["move_quality"]["weakest_phase"] in ("endgame", "middlegame")

    # Critical moments split by volatility, not by centipawns.
    crit = {b["key"]: b for b in pro["critical_moments"]["buckets"]}
    assert crit["critical"]["moves"] == 2  # vol 75 and 80
    assert crit["quiet"]["moves"] == 3     # vol 20, 25 and 20
    assert pro["critical_moments"]["criticality_gap"] > 0

    # Per-game facts drive the client-side filters.
    facts = {f["game_id"]: f for f in metrics["game_explorer"]}
    assert facts["g1"]["outcome"] == "win"
    assert facts["g1"]["rating_band"] == "higher"   # 1700 vs 1600
    assert facts["g2"]["outcome"] == "loss"
    assert facts["g2"]["scramble_moves"] == 1       # clock 8 < 10
    assert facts["g1"]["biggest_miss"]["san"] == "Qxh7"

    # The openings tree groups by colour.
    openings = {r["opening"] for r in pro["openings"]["rows"]}
    assert openings == {"Caro-Kann Defense", "Sicilian Defense"}

    # Coach copy is derived from the ranked leak board.
    assert metrics["ai_coach_takeaways"]
    assert len(metrics["ai_coach_takeaways"]) <= 3


def test_game_facts_reaggregate_to_the_server_totals(connection: sqlite3.Connection) -> None:
    """The dashboard's filters re-derive its panels from ``game_explorer`` rows.

    That is only honest if summing the per-game facts reproduces the aggregates
    the server computed over the raw moves. This pins that contract.
    """

    for idx, (result, color) in enumerate([("1-0", "white"), ("0-1", "white"), ("1/2-1/2", "black")]):
        _seed_game(
            connection,
            game_id=f"agg-{idx}",
            review_id=f"aggr-{idx}",
            result=result,
            user_color=color,
            played_at=f"2026-06-0{idx + 1}T12:00:00",
            accuracy=70.0 + idx,
            moves=[
                {"ply": 1, "san": "e4", "dw": 0.0, "phase": "opening", "is_book": True,
                 "classification": "book", "vol": 20},
                {"ply": 3, "san": "Nf3", "dw": 3.0, "phase": "opening",
                 "classification": "good", "vol": 30},
                {"ply": 21, "san": "Nd5", "dw": 12.0 + idx, "phase": "middlegame",
                 "classification": "mistake", "vol": 70},
                {"ply": 61, "san": "Kf2", "dw": 30.0, "phase": "endgame",
                 "classification": "blunder", "vol": 65},
            ],
        )

    metrics = compute_tier1_metrics(connection, review_ids=["aggr-0", "aggr-1", "aggr-2"])
    facts = metrics["game_explorer"]
    pro = metrics["pro"]

    assert len(facts) == 3

    # Error counts.
    assert sum(f["blunders"] for f in facts) == pro["headline"]["error_rates"]["blunders"]
    assert sum(f["user_moves"] for f in facts) == pro["move_quality"]["total_moves"]

    # Phase moves, loss and weighted accuracy, per phase.
    for phase_row in pro["move_quality"]["by_phase"]:
        phase = phase_row["phase"]
        moves = sum(f["phase_moves"].get(phase, 0) for f in facts)
        loss = sum(f["phase_delta_w"].get(phase, 0.0) for f in facts)
        weighted = sum(
            f["phase_accuracy"][phase] * f["phase_moves"][phase]
            for f in facts
            if phase in f["phase_accuracy"]
        )
        assert moves == phase_row["moves"]
        assert loss == pytest.approx(phase_row["total_delta_w"])
        assert weighted / moves == pytest.approx(phase_row["accuracy"])

    # Critical/quiet buckets, weighted the same way the client does.
    crit = next(b for b in pro["critical_moments"]["buckets"] if b["key"] == "critical")
    assert sum(f["critical_moves"] for f in facts) == crit["moves"]
    weighted_crit = sum(
        f["critical_accuracy"] * f["critical_moves"]
        for f in facts
        if f["critical_accuracy"] is not None
    )
    assert weighted_crit / crit["moves"] == pytest.approx(crit["accuracy"])

    # Score, straight from the per-game points.
    assert sum(f["points"] for f in facts) == pytest.approx(pro["headline"]["record"]["score"])


def test_opening_tree_splits_by_colour(connection: sqlite3.Connection) -> None:
    for idx in range(3):
        _seed_game(
            connection,
            game_id=f"op-{idx}",
            review_id=f"opr-{idx}",
            result="1-0",
            user_color="white" if idx < 2 else "black",
            played_at=f"2026-06-1{idx}T12:00:00",
            opening_name="London System",
            eco="D02",
            moves=[{"ply": 1, "san": "d4", "dw": 1.0, "phase": "opening", "is_book": True}],
        )

    metrics = compute_tier1_metrics(
        connection, review_ids=["opr-0", "opr-1", "opr-2"]
    )
    tree = metrics["pro"]["openings"]

    assert len(tree["rows"]) == 2  # same opening, two colours
    as_white = next(r for r in tree["rows"] if r["color"] == "white")
    as_black = next(r for r in tree["rows"] if r["color"] == "black")
    assert as_white["n"] == 2 and as_white["score_pct"] == pytest.approx(1.0)
    assert as_black["n"] == 1 and as_black["score_pct"] == pytest.approx(0.0)
    assert as_white["eco"] == "D02"


def test_scramble_decay_buckets_populate_from_clock(connection: sqlite3.Connection) -> None:
    """Regression: sqlite3.Row has no ``.get``, which silently emptied every bucket."""

    _seed_game(
        connection,
        game_id="gs",
        review_id="rs",
        result="0-1",
        played_at="2026-06-02T12:00:00",
        moves=[
            {"ply": 1, "san": "e4", "dw": 1.0, "clock": 300.0},
            {"ply": 3, "san": "d4", "dw": 2.0, "clock": 45.0},
            {"ply": 5, "san": "Nf3", "dw": 4.0, "clock": 20.0},
            {"ply": 7, "san": "Qh5", "dw": 40.0, "clock": 5.0},
        ],
    )
    metrics = compute_tier1_metrics(connection, review_ids=["rs"])
    buckets = {b["key"]: b for b in metrics["time_scramble_decay"]["buckets"]}

    assert buckets["deep"]["moves"] == 1
    assert buckets["medium"]["moves"] == 1
    assert buckets["low"]["moves"] == 1
    assert buckets["scramble"]["moves"] == 1
    assert buckets["scramble"]["delta_w_per_move"] == pytest.approx(40.0)
    assert buckets["scramble"]["blunder_rate"] == pytest.approx(1.0)
    # And the decay is visible: loss per move climbs as the clock drains.
    assert (
        buckets["scramble"]["delta_w_per_move"] > buckets["deep"]["delta_w_per_move"]
    )
