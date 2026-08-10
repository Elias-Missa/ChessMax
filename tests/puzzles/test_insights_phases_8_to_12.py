"""Insights 3.0 phases 8–12: IRT, Shapley, structure, exports.

Engine-free: every assertion runs off hand-built move rows or literal FENs.
"""

from __future__ import annotations

import random

import pytest

from server.insights_export import annotated_pgn, coach_memo, greatest_hits
from server.insights_irt import categorize, compute_skill_model, fit_theta
from server.insights_shapley import compute_attribution
from server.insights_structure import (
    classify_endgame,
    classify_structure,
    compute_blunder_clusters,
    compute_endgame_types,
    compute_structures,
)

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _move(**kwargs):
    base = {
        "review_id": "r1", "game_id": "g1", "ply": 21, "san": "Nf3",
        "is_user_move": True, "phase": "middlegame", "is_book": False,
        "classification": None, "win_prob": 0.5, "delta_w": 0.0,
        "volatility": 40.0, "findability": None, "r_find": None,
        "time_spent": 10.0, "clock_remaining": 300.0, "tactic_tags": None,
        "fen_before": START_FEN, "move_uci": "g1f3", "best_uci": "d2d4",
        "san_best": "d4", "opponent": "rival", "opponent_rating": 1500,
        "played_at": "2026-08-08T12:00:00", "user_color": "white",
    }
    base.update(kwargs)
    return base


# ── Phase 8: IRT ──────────────────────────────────────────────────────────────


def test_theta_lands_between_the_items_solved_and_missed() -> None:
    """Easy items right, hard items wrong — ability sits at the crossover."""

    items = [(1200.0, True), (1300.0, True), (1400.0, True),
             (1800.0, False), (1900.0, False), (2000.0, False)]
    theta, stderr = fit_theta(items, discrimination=1.0)

    assert theta is not None and 1400 < theta < 1800
    assert stderr is not None and stderr > 0


def test_theta_rises_when_harder_items_are_solved() -> None:
    """The whole point over an accuracy average: difficulty is accounted for."""

    easy = [(1200.0, True)] * 5 + [(1300.0, False)] * 5
    hard = [(1900.0, True)] * 5 + [(2000.0, False)] * 5
    assert fit_theta(hard)[0] > fit_theta(easy)[0]


def test_theta_is_undefined_on_all_right_or_all_wrong() -> None:
    """No interior maximum, so any point estimate is an artefact of the bounds."""

    assert fit_theta([(1500.0, True)] * 10) == (None, None)
    assert fit_theta([(1500.0, False)] * 10) == (None, None)
    assert fit_theta([]) == (None, None)


def test_categories_come_from_tags_phase_and_state() -> None:
    assert categorize(_move(tactic_tags='["fork"]')) == "tactics"
    assert categorize(_move(phase="endgame")) == "endgame"
    assert categorize(_move(win_prob=0.2)) == "defense"
    assert categorize(_move(volatility=80.0)) == "calculation"
    assert categorize(_move(volatility=20.0)) == "positional"


def test_skill_model_gates_itself_on_coverage() -> None:
    """Spec 8.5: wide error bars are worse than an honest "not yet"."""

    thin = compute_skill_model([
        _move(ply=1 + 2 * i, r_find=1500.0, delta_w=0.0 if i % 2 else 30.0)
        for i in range(10)
    ])
    assert thin["available"] is False
    assert thin["coverage_note"]
    assert all(c["below_floor"] for c in thin["categories"])

    rng = random.Random(5)
    rows = [
        _move(
            ply=1 + 2 * i,
            phase="endgame",
            r_find=1400.0 + rng.random() * 600,
            delta_w=0.0 if rng.random() < 0.5 else 30.0,
        )
        for i in range(120)
    ]
    thick = compute_skill_model(rows)
    assert thick["available"] is True
    endgame = next(c for c in thick["categories"] if c["category"] == "endgame")
    assert endgame["theta"] is not None and not endgame["below_floor"]


# ── Phase 9: Shapley ──────────────────────────────────────────────────────────


def _attribution_rows(n_games: int = 60):
    rng = random.Random(11)
    rows = []
    for g in range(n_games):
        for ply in range(1, 61, 2):
            phase = "opening" if ply <= 20 else "endgame" if ply > 40 else "middlegame"
            clock = max(1.0, 300 - ply * 4 + rng.gauss(0, 25))
            base = 1.5 * (3.0 if clock < 10 else 1.0)
            rows.append(_move(
                game_id=f"g{g}", ply=ply, phase=phase, clock_remaining=clock,
                volatility=max(5.0, min(95.0, rng.gauss(45, 20))),
                delta_w=max(0.0, rng.gauss(base, base)),
            ))
    return rows


def test_shapley_budget_closes_on_the_observed_loss() -> None:
    """Acceptance criterion: the values must sum back to what was actually lost."""

    rows = _attribution_rows()
    result = compute_attribution(rows)

    assert result["available"] is True
    assert result["residual"] == pytest.approx(0.0, abs=1e-6)
    assert result["baseline"] + result["net_total"] == pytest.approx(result["total_loss"])
    # Each feature's gains and losses cancel, which is what makes it a budget.
    for row in result["features"]:
        assert row["net"] == pytest.approx(row["added"] + row["saved"])


def test_shapley_finds_the_seeded_condition() -> None:
    """Loss was seeded onto low clock, so time pressure must carry real weight."""

    result = compute_attribution(_attribution_rows())
    by_feature = {r["feature"]: r for r in result["features"]}

    assert by_feature["clock"]["added"] > 0
    # Exchanges were never seeded and every move here is the same SAN.
    assert by_feature["capture"]["added"] == pytest.approx(0.0, abs=1e-6)


def test_shapley_refuses_thin_data() -> None:
    result = compute_attribution([_move(ply=1)], min_moves=200)
    assert result["available"] is False
    assert "200" in result["reason"]


# ── Phase 10: structure, endgames, clustering ────────────────────────────────


def test_pawn_structure_classifier_names_the_skeletons() -> None:
    french = "rnbqkbnr/ppp2ppp/4p3/3pP3/3P4/8/PPP2PPP/RNBQKBNR b KQkq - 0 3"
    assert classify_structure(french) == "french_chain"

    # An isolated d-pawn with no c- or e-neighbour.
    iqp = "rnbqkbnr/pp3ppp/8/8/3P4/8/PP3PPP/RNBQKBNR w KQkq - 0 1"
    assert classify_structure(iqp) == "iqp"

    assert classify_structure("8/8/4k3/8/8/4K3/8/8 w - - 0 1") == "open_centre"
    assert classify_structure(None) is None
    assert classify_structure("not a fen") is None


def test_endgame_classifier_splits_by_material() -> None:
    assert classify_endgame("8/8/4k3/8/8/4K3/4R3/8 w - - 0 1") == "rook"
    assert classify_endgame("8/8/4k3/8/8/4K3/4Q3/8 w - - 0 1") == "queen"
    assert classify_endgame("8/5p2/4k3/8/8/4K3/4P3/8 w - - 0 1") == "pawn"
    assert classify_endgame("8/8/4k1b1/8/8/4K3/4N3/8 w - - 0 1") == "other"
    # Bishops on opposite colours is the classic drawing signature.
    assert classify_endgame("8/8/4kb2/8/8/4K3/4B3/8 w - - 0 1") in (
        "opposite_bishops", "same_bishops"
    )


def test_endgame_types_reports_missing_tablebases_rather_than_faking_them() -> None:
    rows = [
        _move(ply=61 + 2 * i, phase="endgame", delta_w=4.0,
              fen_before="8/8/4k3/8/8/4K3/4R3/8 w - - 0 1")
        for i in range(25)
    ]
    result = compute_endgame_types(rows)

    assert result["tablebase"] is False
    assert "CHESS_TRAINER_SYZYGY" in result["tablebase_note"]
    rook = next(r for r in result["rows"] if r["family"] == "rook")
    assert rook["moves"] == 25 and rook["dtz_optimal_rate"] is None


def test_structure_floor_requires_several_games() -> None:
    """Thirty moves from one game describes the game, not the structure."""

    one_game = [
        _move(ply=21 + 2 * i, game_id="g1", delta_w=8.0,
              fen_before="rnbqkbnr/ppp2ppp/4p3/3pP3/3P4/8/PPP2PPP/RNBQKBNR b KQkq - 0 3")
        for i in range(60)
    ]
    result = compute_structures(one_game)
    french = next(r for r in result["rows"] if r["family"] == "french_chain")
    assert french["moves"] == 60
    assert french["below_floor"] is True   # only one game
    assert result["worst"] is None


def test_blunder_clustering_is_deterministic_and_gated() -> None:
    """Immutable runs must cluster identically when recomputed."""

    thin = compute_blunder_clusters([_move(delta_w=40.0) for _ in range(5)])
    assert thin["available"] is False

    rng = random.Random(2)
    rows = []
    for i in range(40):
        rows.append(_move(
            game_id=f"g{i}", ply=21 + 2 * (i % 10), delta_w=30.0 + rng.random() * 40,
            phase="endgame" if i % 2 else "middlegame",
            san="Nxe5" if i % 3 == 0 else "Nf3",
            volatility=20.0 + rng.random() * 60,
        ))
    first = compute_blunder_clusters(rows)
    second = compute_blunder_clusters(rows)

    assert first["available"] is True
    assert [c["n"] for c in first["clusters"]] == [c["n"] for c in second["clusters"]]
    assert sum(c["n"] for c in first["clusters"]) == first["n"]
    assert all(len(c["montage"]) <= 6 for c in first["clusters"])


# ── Phase 12: exports ─────────────────────────────────────────────────────────


def test_greatest_hits_needs_hard_and_well_played() -> None:
    """A hard-to-find move that changes nothing is trivia, not a hit."""

    found = _move(findability=20, delta_w=0.5, move_uci="g1f3", best_uci="g1f3",
                  volatility=60.0)
    missed = _move(findability=20, delta_w=30.0, move_uci="g1f3", best_uci="d2d4")
    obvious = _move(findability=95, delta_w=0.0, move_uci="g1f3", best_uci="g1f3",
                    volatility=60.0)
    trivial = _move(findability=20, delta_w=0.0, move_uci="g1f3", best_uci="g1f3",
                    volatility=1.0)

    hits = greatest_hits([found, missed, obvious, trivial])
    assert len(hits) == 1
    assert hits[0]["findability"] == 20
    assert "found it" in hits[0]["caption"]


def test_coach_memo_is_readable_markdown_with_the_headline_numbers() -> None:
    metrics = {
        "pro": {
            "headline": {
                "record": {"games": 40, "wins": 20, "draws": 4, "losses": 16, "score_pct": 0.55},
                "accuracy": {"mean": 78.5, "stdev": 12.0, "consistency": "Streaky"},
                "error_rates": {"blunders_per_100": 2.4, "clean_game_rate": 0.5},
                "elo_left_on_board": {"points": 90, "basis": "mixed"},
                "performance_rating": 1740,
                "opponents": {"mean_rating": 1700},
            },
            "leaks": [{"title": "Endgame is your weakest phase", "detail": "You leak 4.8 win% per move.",
                       "impact_win_pct_per_game": 12.3}],
            "strengths": [{"title": "Hard to put away", "detail": "You rescued 19 points."}],
            "attribution": {"available": True, "total_loss": 4000,
                            "features": [{"label": "Time pressure", "added": 900, "share_of_excess": 0.3}]},
            "skill_model": {"available": False},
            "structure": {"endgame_types": {"note": "Pawn endings are your weakest."}},
        },
        "practice_flags": {"count": 24, "delta_w_threshold": 15},
    }
    memo = coach_memo(metrics, {"handle": "alice", "window_days": 30, "time_class": "blitz"})

    assert memo.startswith("# Coaching brief — alice")
    assert "20W–4D–16L" in memo
    assert "1740" in memo
    assert "Endgame is your weakest phase" in memo
    assert "Time pressure" in memo
    assert "Pawn endings" in memo
    assert "24 positions" in memo
    # A category with no data must not be invented.
    assert "## Ability by skill" not in memo


def test_annotated_pgn_carries_nags_and_per_move_numbers() -> None:
    pgn = '[Event "Test"]\n[White "a"]\n[Black "b"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 *\n'
    moves = [
        {"ply": 1, "classification": "best", "delta_w": 0.0, "volatility": 12.0, "findability": None},
        {"ply": 2, "classification": "blunder", "delta_w": 41.5, "volatility": 78.0, "findability": 63},
    ]
    rows = [_Row(m) for m in moves]

    out = annotated_pgn(pgn, rows, user_color="white")
    assert "Annotator" in out and "ChessMax Insights" in out
    assert "$4" in out          # blunder NAG
    assert "41.5" in out and "find 63" in out
    assert annotated_pgn("not a pgn", rows) == "not a pgn"


class _Row(dict):
    """A dict that also answers ``keys()`` the way a sqlite3.Row does."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)
