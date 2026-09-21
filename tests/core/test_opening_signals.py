"""The three dots (core/opening_signals.py). Pure — no engine, no network.

The property these tests exist to protect is the tri-state: "we asked and the
answer is no" must never render as the same thing as "we never asked". Every
degradation path in this repo (findability, human eval, piece values) draws that
line, and the dots are the most visually collapsible place to lose it.
"""

from __future__ import annotations

from core.opening_signals import (
    MoveSignals,
    compute_signals,
    engine_best,
    human_best,
    results_best,
    row_games,
    row_score,
    signals_for_position,
)

E4, D4, NF3 = "e2e4", "d2d4", "g1f3"


def rows(*specs: tuple[str, int, int, int]) -> list[dict[str, object]]:
    return [
        {"uci": uci, "white": w, "draws": d, "black": b} for uci, w, d, b in specs
    ]


# ── the three sources, read individually ──────────────────────────────────── #


def test_engine_best_reads_the_first_multipv_line() -> None:
    assert engine_best([{"move": E4, "eval": 30}, {"move": D4, "eval": 20}]) == E4


def test_engine_best_is_unknown_without_analysis() -> None:
    assert engine_best(None) is None
    assert engine_best([]) is None


def test_human_best_reads_maia_policy_order() -> None:
    assert human_best([D4, E4]) == D4
    assert human_best(None) is None
    assert human_best([]) is None


def test_results_best_counts_a_draw_as_half() -> None:
    # all-draws (0.5) beats win-a-third-lose-two-thirds (0.333)
    best = results_best(
        rows((E4, 0, 100, 0), (D4, 34, 0, 66)), min_games=10, for_white=True
    )
    assert best == E4


def test_results_best_flips_perspective_for_black() -> None:
    """The explorer always reports W/D/L from White. Reading it without
    flipping hands Black the move White scores best with — the single most
    damaging way this feature could be quietly wrong."""

    data = rows(("e7e5", 700, 50, 250), ("c7c5", 300, 60, 640))
    assert results_best(data, min_games=10, for_white=True) == "e7e5"
    assert results_best(data, min_games=10, for_white=False) == "c7c5"


def test_results_best_ignores_rows_under_the_sample_floor() -> None:
    # A 3-game 100% must not take the dot from a 500-game 55%.
    data = rows((E4, 3, 0, 0), (D4, 275, 0, 225))
    assert results_best(data, min_games=20, for_white=True) == D4


def test_results_best_is_unknown_when_nothing_clears_the_floor() -> None:
    assert results_best(rows((E4, 2, 0, 1)), min_games=20, for_white=True) is None
    assert results_best(None, for_white=True) is None


def test_row_helpers() -> None:
    row = {"uci": E4, "white": 3, "draws": 2, "black": 5}
    assert row_games(row) == 10
    assert row_score(row, for_white=True) == 0.4
    assert row_score(row, for_white=False) == 0.6
    assert row_score({"uci": E4}, for_white=True) is None


# ── the tri-state ─────────────────────────────────────────────────────────── #


def test_a_source_that_was_not_asked_is_unknown_for_every_move() -> None:
    out = compute_signals(
        [E4, D4], engine_top=E4, human_top=None, results_top=None
    )
    assert out[E4].engine is True
    assert out[D4].engine is False
    for uci in (E4, D4):
        assert out[uci].human is None, "no Maia must be unknown, never False"
        assert out[uci].results is None


def test_a_source_that_answered_off_list_is_known_and_unlit() -> None:
    """Stockfish's best is a move we are not showing. Every listed move has
    genuinely failed the engine test — that is False, not unknown."""

    out = compute_signals([E4, D4], engine_top=NF3, human_top=None, results_top=None)
    assert out[E4].engine is False
    assert out[D4].engine is False


def test_known_override_separates_silence_from_a_negative_answer() -> None:
    out = compute_signals(
        [E4, D4], engine_top=None, human_top=None, results_top=None,
        results_known=True,
    )
    assert out[E4].results is False, "explorer answered 'nothing scores best here'"
    assert out[E4].engine is None, "engine was never asked"


def test_explorer_rows_below_the_floor_are_known_and_unlit() -> None:
    """A position the explorer has, but with only a handful of games, is an
    answer: no move scores best. Distinct from an unreachable explorer."""

    out = signals_for_position(
        [E4, D4], explorer_rows=rows((E4, 2, 0, 1)), for_white=True
    )
    assert out[E4].results is False
    assert out[D4].results is False


def test_no_explorer_at_all_is_unknown() -> None:
    out = signals_for_position([E4, D4], explorer_rows=None, for_white=True)
    assert out[E4].results is None


def test_a_box_with_nothing_installed_still_answers() -> None:
    """No Stockfish, no Maia, no network — the real state of the container this
    was written in. Three grey dots is the correct output, not an error."""

    out = signals_for_position([E4, D4], for_white=True)
    assert out[E4].as_dict() == {"engine": None, "human": None, "results": None}
    assert out[E4].known == 0
    assert out[E4].lit == 0
    assert out[E4].unanimous is False


# ── the aggregate properties the UI sorts and badges on ───────────────────── #


def test_unanimous_requires_all_three_known_and_lit() -> None:
    assert MoveSignals(True, True, True).unanimous is True
    assert MoveSignals(True, True, None).unanimous is False, "unknown is not agreement"
    assert MoveSignals(True, True, False).unanimous is False


def test_lit_counts_only_positives() -> None:
    assert MoveSignals(True, False, None).lit == 1
    assert MoveSignals(True, False, None).known == 2


def test_all_three_light_up_when_the_sources_agree() -> None:
    out = signals_for_position(
        [E4, D4],
        engine_top_moves=[{"move": E4, "eval": 30}],
        maia_top_moves=[E4],
        explorer_rows=rows((E4, 600, 100, 300), (D4, 200, 50, 250)),
        for_white=True,
    )
    assert out[E4].unanimous is True
    assert out[D4].lit == 0
    assert out[D4].known == 3, "D4 failed all three — known, not unknown"
