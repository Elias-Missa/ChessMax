"""Insights 3.0 — evidence, expectation, shrinkage, new measurements, recurrence.

Engine-free throughout: every assertion runs off hand-built move rows or
hand-seeded ``review_moves``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from server import db
from server.insights_evidence import (
    build_caption,
    enrich_moves,
    load_evidence,
    persist_evidence,
    query_for_leak,
    run_query,
    select_counter_exemplars,
    select_exemplars,
    strip_support,
    support_query,
)
from server.insights_measures import (
    compute_blindness_split,
    compute_blunder_tempo,
    compute_impulsivity,
    compute_punish_rate,
    compute_state_conditioned_risk,
    compute_stubbornness,
    compute_tilt_significance,
)
from server.insights_signatures import (
    build_signatures,
    classify_geometry,
    compute_recurrence,
    error_signature,
    persist_signatures,
    record_practice_efficacy,
    signature_history,
)
from server.insights_stats import (
    DISPLAY_FLOOR_N,
    MetricResult,
    binomial_tail_p,
    bootstrap_interval,
    games_needed,
    performance_gap,
    pooling_constant,
    recency_weight,
    shrink,
    shrink_buckets,
    wilson_interval,
)

NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _move(**kwargs):
    """One enriched move row, as ``enrich_moves`` would produce."""

    base = {
        "review_id": "r1", "game_id": "g1", "ply": 1, "san": "e4",
        "is_user_move": True, "phase": "middlegame", "is_book": False,
        "classification": None, "win_prob": 0.5, "delta_w": 0.0,
        "volatility": 40.0, "findability": None, "time_spent": 10.0,
        "clock_remaining": 300.0, "tactic_tags": None, "fen_before": START_FEN,
        "move_uci": "e2e4", "best_uci": "d2d4", "san_best": "d4", "top_lines": [],
        "opponent": "rival", "opponent_rating": 1500,
        "played_at": "2026-08-08T12:00:00", "user_color": "white", "lead_in": [],
    }
    base.update(kwargs)
    return base


# ── Phase 3: statistics ───────────────────────────────────────────────────────


def test_wilson_interval_stays_inside_the_unit_range() -> None:
    """The reason for Wilson over the normal approximation: small, extreme samples."""

    lo, hi = wilson_interval(3, 3)
    assert 0.0 <= lo <= hi <= 1.0
    assert lo > 0.3  # a 3/3 record is not evidence of certainty
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0 and hi < 0.6
    assert wilson_interval(1, 0) is None


def test_wilson_interval_narrows_with_more_data() -> None:
    small = wilson_interval(5, 10)
    large = wilson_interval(50, 100)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_bootstrap_interval_is_deterministic_and_brackets_the_mean() -> None:
    """Immutable runs must not jitter when recomputed, so the seed is fixed."""

    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    first = bootstrap_interval(values)
    assert first == bootstrap_interval(values)
    assert first[0] <= 4.5 <= first[1]
    assert bootstrap_interval([1.0]) is None


def test_shrinkage_pulls_small_buckets_toward_the_baseline() -> None:
    """A 3-game opening barely moves off baseline; a 60-game one speaks for itself."""

    global_mean = 0.5
    tiny = shrink(1.0, n=3, global_mean=global_mean, k=20)
    large = shrink(1.0, n=200, global_mean=global_mean, k=20)
    assert abs(tiny - global_mean) < abs(large - global_mean)
    assert large > 0.9
    assert shrink(1.0, n=0, global_mean=global_mean, k=20) == global_mean


def test_pooling_constant_is_huge_when_buckets_do_not_differ() -> None:
    """No evidence that buckets differ → shrink everything to the mean."""

    identical = pooling_constant([0.5, 0.5, 0.5], [10, 10, 10], [0.25, 0.25, 0.25])
    spread = pooling_constant([0.2, 0.5, 0.8], [10, 10, 10], [0.25, 0.25, 0.25])
    assert identical > spread
    assert pooling_constant([0.5], [10], [0.25]) == 1e6


def test_shrink_buckets_flags_the_display_floor() -> None:
    result = shrink_buckets([
        {"key": "a", "value": 1.0, "n": 3, "variance": 0.25},
        {"key": "b", "value": 0.4, "n": 40, "variance": 0.24},
    ])
    by_key = {b["key"]: b for b in result["buckets"]}
    assert by_key["a"]["below_floor"] is True
    assert by_key["b"]["below_floor"] is False
    # The 3-game bucket is dragged much further from its raw value.
    assert abs(by_key["a"]["shrunk"] - 1.0) > abs(by_key["b"]["shrunk"] - 0.4)


def test_sample_size_guidance_is_silent_once_the_interval_is_tight() -> None:
    assert games_needed(10, (0.2, 0.8)) is not None
    assert games_needed(400, (0.48, 0.52)) is None
    assert games_needed(0, (0.2, 0.8)) is None


def test_metric_result_gates_the_leak_board_on_support() -> None:
    """Anti-goal: a metric with n < 8 never reaches the leak board."""

    assert MetricResult(value=0.5, n=DISPLAY_FLOOR_N - 1).leak_eligible is False
    assert MetricResult(value=0.5, n=DISPLAY_FLOOR_N).leak_eligible is True
    assert MetricResult(value=0.5, n=3).below_floor is True


# ── Phase 2: expectation ──────────────────────────────────────────────────────


def test_performance_gap_separates_result_from_opposition() -> None:
    """Two wins over much stronger players beat two wins over much weaker ones."""

    strong = [
        {"points": 1.0, "user_rating": 1500, "opponent_rating": 1900},
        {"points": 1.0, "user_rating": 1500, "opponent_rating": 1900},
    ]
    weak = [
        {"points": 1.0, "user_rating": 1500, "opponent_rating": 1100},
        {"points": 1.0, "user_rating": 1500, "opponent_rating": 1100},
    ]
    assert performance_gap(strong)["gap"] > performance_gap(weak)["gap"]
    # Same raw score, different verdict — the whole point of Phase 2.
    assert performance_gap(strong)["actual"] == performance_gap(weak)["actual"]


def test_performance_gap_reports_unrated_coverage() -> None:
    gap = performance_gap([
        {"points": 1.0, "user_rating": 1500, "opponent_rating": 1500},
        {"points": 0.0, "user_rating": None, "opponent_rating": 1500},
    ])
    assert gap["n"] == 1 and gap["n_total"] == 2


def test_recency_weight_decays_over_the_half_life() -> None:
    assert recency_weight(0) == pytest.approx(1.0)
    assert recency_weight(14) == pytest.approx(0.5, abs=0.02)
    assert recency_weight(28) < recency_weight(14)


# ── Phase 1: evidence ─────────────────────────────────────────────────────────


def test_exemplars_never_take_more_than_two_moves_from_one_game() -> None:
    """Acceptance criterion: one disastrous game must not fill the panel."""

    rows = [
        _move(game_id="blowout", ply=i, delta_w=60.0, played_at="2026-08-08T12:00:00")
        for i in range(1, 13, 2)
    ] + [
        _move(game_id="other", ply=3, delta_w=30.0, played_at="2026-08-08T12:00:00"),
        _move(game_id="third", ply=5, delta_w=28.0, played_at="2026-08-08T12:00:00"),
        _move(game_id="fourth", ply=7, delta_w=26.0, played_at="2026-08-08T12:00:00"),
    ]
    picked = select_exemplars(rows, now=NOW)

    assert len(picked) == 5
    from collections import Counter
    counts = Counter(e.game_id for e in picked)
    assert max(counts.values()) <= 2


def test_exemplar_score_prefers_typical_over_freakish() -> None:
    """Ranking by Δw alone surfaces the least representative move."""

    typical = [_move(game_id=f"g{i}", ply=i, delta_w=20.0, volatility=50.0) for i in range(1, 20, 2)]
    freak = _move(game_id="freak", ply=99, delta_w=25.0, volatility=99.0, time_spent=0.2)
    picked = select_exemplars(typical + [freak], now=NOW)

    # The outlier has the highest impact but should not lead on typicality.
    assert picked[0].game_id != "freak"


def test_counter_exemplars_come_from_clean_handling() -> None:
    """Spec 1.5: the report should not read as a prosecutor."""

    good = [_move(game_id=f"g{i}", ply=i, delta_w=1.0) for i in range(1, 12, 2)]
    picked = select_counter_exemplars(good, now=NOW)
    assert 0 < len(picked) <= 3
    assert len({e.game_id for e in picked}) == len(picked)  # one per game


def test_caption_drops_clauses_whose_data_is_missing() -> None:
    full = build_caption(_move(
        ply=35, san="Qh5", san_best="Nf3", delta_w=40.0, clock_remaining=12.0,
        findability=71, opponent="rival", opponent_rating=1620,
        played_at="2026-08-08T12:00:00",
    ))
    assert "Move 18" in full and "vs rival (1620)" in full
    assert "Qh5" in full and "12s left" in full
    assert "Nf3 was much better" in full and "Findability 71" in full

    sparse = build_caption({"ply": 4, "san": "e5", "delta_w": 3.0})
    assert "Findability" not in sparse and "left" not in sparse
    assert "Move 2" in sparse


def test_query_predicate_regenerates_the_support_set() -> None:
    """Storing the predicate instead of hundreds of plies (spec 1.1)."""

    rows = [
        _move(ply=1, volatility=80.0, delta_w=30.0),
        _move(ply=3, volatility=10.0, delta_w=30.0),
        _move(ply=5, volatility=80.0, delta_w=1.0, is_user_move=False),
    ]
    query = support_query(is_user_move=True, min_volatility=60.0)
    picked = run_query(rows, query)
    assert len(picked) == 1 and picked[0]["ply"] == 1


def test_leak_queries_resolve_for_move_level_leaks() -> None:
    assert query_for_leak({"id": "critical", "evidence": {}})["min_volatility"] == 60.0
    assert query_for_leak({"id": "clock", "evidence": {}})["max_clock"] == 10.0
    assert query_for_leak({"id": "phase", "evidence": {"phase": "endgame"}})["phase"] == "endgame"
    window = query_for_leak({"id": "game_window", "evidence": {"key": "21-30"}})
    assert window["min_ply"] == 41 and window["max_ply"] == 60
    # Game-level leaks have no move predicate and say so.
    assert query_for_leak({"id": "conversion", "evidence": {}}) is None


def test_strip_support_keeps_the_blob_lean() -> None:
    """Anti-goal: full support sets never reach ``insight_runs.metrics``."""

    payload = {
        "pro": {
            "measures": {
                "punish": {"punish_rate": 0.5, "missed": [{"ply": 1}], "taken": [{"ply": 3}]},
                "impulsivity": {"impulse_rate": 0.3, "moves": [{"ply": 5}]},
            }
        }
    }
    stripped = strip_support(payload)
    punish = stripped["pro"]["measures"]["punish"]
    assert punish["punish_rate"] == 0.5
    assert "missed" not in punish and "taken" not in punish
    assert "moves" not in stripped["pro"]["measures"]["impulsivity"]


def test_enrich_moves_attaches_lead_in_plies(connection: sqlite3.Connection) -> None:
    """Spec 1.6: animate into the position — the move before makes it legible."""

    rows = _fake_move_rows([
        {"ply": 1, "san": "e4"}, {"ply": 2, "san": "e5"},
        {"ply": 3, "san": "Nf3"}, {"ply": 4, "san": "Nc6"},
    ])
    enriched = enrich_moves(rows, {"r1": {"game_id": "g1", "user_color": "white"}})
    by_ply = {r["ply"]: r for r in enriched}
    assert by_ply[1]["lead_in"] == []
    assert len(by_ply[3]["lead_in"]) == 2
    assert [p["san"] for p in by_ply[4]["lead_in"]] == ["e5", "Nf3"]


def _fake_move_rows(entries):
    """Minimal stand-ins for ``review_moves`` rows."""

    out = []
    for e in entries:
        base = {
            "review_id": "r1", "ply": 1, "san": "e4", "is_user_move": 1,
            "phase": "opening", "is_book": 0, "classification": None,
            "win_prob": 0.5, "delta_w": 0.0, "volatility": 30.0,
            "findability": None, "time_spent": 5.0, "clock_remaining": 300.0,
            "tactic_tags": None,
            "detail": json.dumps({"fen_before": START_FEN, "move_uci": "e2e4", "top_lines": []}),
        }
        base.update(e)
        out.append(base)
    return out


# ── Phase 5: new measurements ─────────────────────────────────────────────────


def test_punish_rate_counts_only_conversions_that_kept_the_swing() -> None:
    rows = [
        # Opponent hands over 30 win%; the user keeps it.
        _move(ply=1, is_user_move=False, delta_w=30.0),
        _move(ply=2, is_user_move=True, delta_w=1.0),
        # Opponent errs again; the user gives it straight back.
        _move(ply=3, is_user_move=False, delta_w=30.0),
        _move(ply=4, is_user_move=True, delta_w=28.0),
        # A small opponent slip is below the threshold and never counted.
        _move(ply=5, is_user_move=False, delta_w=2.0),
        _move(ply=6, is_user_move=True, delta_w=0.0),
    ]
    result = compute_punish_rate(rows)
    assert result["opportunities"] == 2
    assert result["punished"] == 1
    assert result["punish_rate"] == pytest.approx(0.5)
    assert len(result["missed"]) == 1 and len(result["taken"]) == 1


def test_blindness_split_is_not_tautological() -> None:
    """A win-probability comparison classifies everything as defensive.

    The opponent gains almost exactly what the user lost by construction, so the
    discriminator has to be the shape of the moves instead.
    """

    rows = [
        # Punished by a forcing move the opponent then converted → defensive.
        _move(ply=1, delta_w=30.0, san_best="Rd1"),
        _move(ply=2, is_user_move=False, san="Qxf7+", delta_w=0.5),
        # A forcing move declined, and no punishment followed → offensive.
        _move(ply=3, delta_w=30.0, san_best="Nxe5+"),
        _move(ply=4, is_user_move=False, san="a6", delta_w=4.0),
        # Neither: a quiet slip.
        _move(ply=5, delta_w=15.0, san_best="Rc1"),
        _move(ply=6, is_user_move=False, san="h6", delta_w=1.0),
    ]
    result = compute_blindness_split(rows)
    assert result["defensive"] == 1
    assert result["offensive"] == 1
    assert result["positional"] == 1
    assert result["defensive_share"] == pytest.approx(0.5)


def test_blunder_tempo_splits_against_the_games_own_median() -> None:
    """A blitz long-think and a rapid long-think are different numbers."""

    rows = [_move(ply=i, delta_w=0.0, time_spent=10.0) for i in range(1, 20, 2)]
    rows += [_move(ply=101 + 2 * i, delta_w=40.0, time_spent=1.0) for i in range(3)]
    rows += [_move(ply=201 + 2 * i, delta_w=40.0, time_spent=40.0) for i in range(3)]
    rows += [_move(ply=301, delta_w=40.0, time_spent=10.0)]

    result = compute_blunder_tempo(rows)
    assert result["fast"] == 3 and result["slow"] == 3 and result["typical"] == 1
    assert result["verdict"] == "mixed"
    assert result["prescription"]


def test_blunder_tempo_withholds_a_verdict_on_thin_evidence() -> None:
    """Three blunders is not enough to diagnose impulse versus misjudgement."""

    rows = [_move(ply=i, delta_w=0.0, time_spent=10.0) for i in range(1, 20, 2)]
    rows += [_move(ply=101, delta_w=40.0, time_spent=1.0)]
    assert compute_blunder_tempo(rows)["verdict"] is None


def test_impulsivity_excludes_scrambles_and_matches_on_difficulty() -> None:
    """Playing fast with 6 seconds left is forced, not a fixable leak."""

    rows = [_move(ply=1, time_spent=0.5, clock_remaining=4.0)]  # scramble, excluded
    rows += [_move(ply=3 + 2 * i, time_spent=1.0, clock_remaining=200.0) for i in range(10)]
    rows += [_move(ply=101 + 2 * i, time_spent=20.0, clock_remaining=200.0) for i in range(10)]

    result = compute_impulsivity(rows)
    assert result["n"] == 20  # the scramble move is gone
    assert result["impulsive"] == 10
    assert result["impulse_rate"] == pytest.approx(0.5)


def test_state_conditioned_risk_names_the_converted_then_lost_mechanism() -> None:
    rows = [_move(ply=1 + 2 * i, win_prob=0.85, volatility=70.0, delta_w=6.0) for i in range(6)]
    rows += [_move(ply=101 + 2 * i, win_prob=0.2, volatility=20.0, delta_w=2.0) for i in range(6)]
    result = compute_state_conditioned_risk(rows)

    assert result["states"]["winning"]["mean_volatility"] > result["states"]["losing"]["mean_volatility"]
    assert any("already winning" in n for n in result["notes"])


def test_stubbornness_requires_the_persistence_to_keep_costing() -> None:
    """Moving the same piece again is ordinary chess; losing more is not."""

    refuted_then_free = [
        _move(ply=1, san="Nb5", delta_w=20.0),
        _move(ply=3, san="Nc7", delta_w=0.5),
    ]
    assert compute_stubbornness(refuted_then_free)["episodes"] == 0

    refuted_then_worse = [
        _move(ply=1, san="Nb5", delta_w=20.0),
        _move(ply=3, san="Nc7", delta_w=12.0),
    ]
    result = compute_stubbornness(refuted_then_worse)
    assert result["episodes"] == 1
    assert result["extra_delta_w"] == pytest.approx(12.0)


def test_tilt_significance_reports_a_null_result_too() -> None:
    """Spec 11.5: saying "not distinguishable from chance" builds trust."""

    null = compute_tilt_significance({"n": 20, "wins": 10}, 0.5)
    assert null["significant"] is False
    assert "not statistically distinguishable" in null["verdict"]

    real = compute_tilt_significance({"n": 40, "wins": 5}, 0.5)
    assert real["significant"] is True
    assert "significantly below" in real["verdict"]

    assert compute_tilt_significance({"n": 2, "wins": 0}, 0.5)["p_value"] is None


def test_binomial_tail_p_is_symmetric_at_the_null() -> None:
    assert binomial_tail_p(5, 10, 0.5) == pytest.approx(1.0, abs=0.01)
    assert binomial_tail_p(0, 20, 0.5) < 0.001


# ── Phase 6 / 10: signatures and geometry ─────────────────────────────────────


def test_geometry_classifies_direction_from_the_movers_point_of_view() -> None:
    """"Backward" must mean the same thing for both colours."""

    white_back = classify_geometry(
        "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R w KQkq - 0 1", "f3g1"
    )
    assert white_back["direction"] == "backward"
    assert white_back["piece"] == "knight"

    black_back = classify_geometry(
        "rnbqkb1r/pppppppp/5n2/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1", "f6g8"
    )
    assert black_back["direction"] == "backward"

    forward = classify_geometry(START_FEN, "e2e4")
    assert forward["direction"] == "forward" and forward["distance"] == "medium"
    assert classify_geometry(None, "e2e4") is None


def test_error_signature_is_stable_and_discriminating() -> None:
    """Stored and compared across processes, so it cannot use Python's hash()."""

    parts = {
        "motif": "fork", "piece_moved": "knight", "piece_lost": "rook",
        "phase": "middlegame", "geometry": "backward-medium-knight",
        "opponent_piece": "knight",
    }
    assert error_signature(parts) == error_signature(dict(parts))
    assert error_signature(parts) != error_signature({**parts, "piece_lost": "bishop"})
    # Missing parts are tolerated rather than crashing.
    assert error_signature({}) == error_signature({"motif": None})


def test_signatures_ignore_recaptures_when_attributing_a_hung_piece() -> None:
    """Without this every signature reads "hanging a pawn"."""

    exchange = [
        _move(ply=1, san="Nxe5", delta_w=20.0),
        _move(ply=2, is_user_move=False, san="dxe5", move_uci="d6e5",
              fen_before="rnbqkbnr/ppp1pppp/3p4/4N3/8/8/PPPPPPPP/RNBQKB1R b KQkq - 0 1"),
    ]
    signed = build_signatures(exchange)
    assert signed[0]["signature_parts"]["piece_lost"] is None

    hang = [
        _move(ply=1, san="Nf3", delta_w=20.0),
        _move(ply=2, is_user_move=False, san="dxe5", move_uci="d6e5",
              fen_before="rnbqkbnr/ppp1pppp/3p4/4N3/8/8/PPPPPPPP/RNBQKB1R b KQkq - 0 1"),
    ]
    assert build_signatures(hang)[0]["signature_parts"]["piece_lost"] == "knight"


def test_recurrence_flags_repeats_and_ignores_one_offs() -> None:
    """A mistake made once is noise; the same mistake three times is a leak."""

    repeated = [
        _move(game_id=f"g{i}", review_id=f"r{i}", ply=11, san="Nb5", delta_w=30.0,
              tactic_tags=json.dumps(["fork"]), played_at=f"2026-08-0{i}T12:00:00")
        for i in range(1, 5)
    ]
    one_off = [_move(game_id="gx", review_id="rx", ply=21, san="Qh5", delta_w=30.0,
                     tactic_tags=json.dumps(["pin"]))]

    result = compute_recurrence(build_signatures(repeated + one_off))
    assert len(result["recurring"]) == 1
    assert result["recurring"][0]["n"] == 4
    assert result["recurring"][0]["first_seen"] == "2026-08-01"
    assert result["singletons"] == 1


# ── Persistence ───────────────────────────────────────────────────────────────


@pytest.fixture
def connection(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "i30.db")
    conn.execute(
        "INSERT INTO users (username, email, password_hash, password_salt) "
        "VALUES ('u', 'u@ex.com', 'x', 'y')"
    )
    conn.execute(
        "INSERT INTO insight_runs (run_id, user_id, chesscom_handle, window_days, "
        "time_class, status) VALUES ('run1', 1, 'alice', 7, 'blitz', 'complete')"
    )
    conn.commit()
    return conn


def test_evidence_round_trips_by_metric_key(connection: sqlite3.Connection) -> None:
    """The dotted key is the contract that keeps the evidence UI generic."""

    rows = [_move(game_id=f"g{i}", ply=i, delta_w=30.0) for i in range(1, 8, 2)]
    bundles = {
        "pro.leaks.critical": {
            "exemplar": select_exemplars(rows, now=NOW),
            "counter": select_counter_exemplars(
                [_move(game_id="clean", ply=9, delta_w=0.5)], now=NOW
            ),
        }
    }
    written = persist_evidence(connection, run_id="run1", bundles=bundles)
    assert written > 0

    loaded = load_evidence(connection, run_id="run1")
    assert "pro.leaks.critical" in loaded
    assert len(loaded["pro.leaks.critical"]["exemplar"]) == len(bundles["pro.leaks.critical"]["exemplar"])
    assert loaded["pro.leaks.critical"]["counter"][0]["caption"]

    scoped = load_evidence(connection, run_id="run1", metric_key="pro.leaks.critical")
    assert set(scoped) == {"pro.leaks.critical"}
    assert load_evidence(connection, run_id="run1", metric_key="nope") == {}


def test_evidence_is_replaced_not_appended(connection: sqlite3.Connection) -> None:
    rows = [_move(game_id="g1", ply=1, delta_w=30.0)]
    bundle = {"pro.leaks.clock": {"exemplar": select_exemplars(rows, now=NOW), "counter": []}}
    persist_evidence(connection, run_id="run1", bundles=bundle)
    persist_evidence(connection, run_id="run1", bundles=bundle)
    total = connection.execute(
        "SELECT COUNT(*) c FROM metric_evidence WHERE run_id = 'run1'"
    ).fetchone()["c"]
    assert total == 1


def test_signatures_persist_across_runs(connection: sqlite3.Connection) -> None:
    """Recurrence is a cross-run property, so it cannot live in the run blob."""

    first = build_signatures([
        _move(game_id="g1", review_id="r1", ply=11, san="Nb5", delta_w=30.0,
              tactic_tags=json.dumps(["fork"]), played_at="2026-07-01T12:00:00"),
    ])
    second = build_signatures([
        _move(game_id="g2", review_id="r2", ply=11, san="Nb5", delta_w=30.0,
              tactic_tags=json.dumps(["fork"]), played_at="2026-08-01T12:00:00"),
    ])
    persist_signatures(connection, user_id=1, signed=first)
    persist_signatures(connection, user_id=1, signed=second)

    signature = first[0]["signature"]
    assert second[0]["signature"] == signature
    history = signature_history(connection, user_id=1, signature=signature)
    assert len(history) == 2  # both runs, one shared signature


def test_practice_efficacy_records_both_directions(connection: sqlite3.Connection) -> None:
    """If the rate doesn't drop, that is more valuable than any metric."""

    solved = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for offset in (-20, -10, -5):
        connection.execute(
            "INSERT INTO error_signatures (user_id, signature, game_id, ply, delta_w, played_at) "
            "VALUES (1, 'sig', ?, 1, 30.0, ?)",
            (f"before{offset}", (solved + timedelta(days=offset)).isoformat()),
        )
    connection.execute(
        "INSERT INTO error_signatures (user_id, signature, game_id, ply, delta_w, played_at) "
        "VALUES (1, 'sig', 'after', 1, 30.0, ?)",
        ((solved + timedelta(days=5)).isoformat(),),
    )
    connection.commit()

    result = record_practice_efficacy(connection, user_id=1, signature="sig", solved_at=solved)
    assert result["n_before"] == 3 and result["n_after"] == 1
    assert result["improved"] is True

    stored = connection.execute(
        "SELECT n_before, n_after FROM practice_efficacy WHERE user_id = 1"
    ).fetchone()
    assert stored["n_before"] == 3 and stored["n_after"] == 1
