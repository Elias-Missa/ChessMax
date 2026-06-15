"""Unit tests for :mod:`chess_vol.explain`.

These tests construct :class:`VolatilityResult` instances directly (no engine,
no Stockfish) and assert that the explainer produces the right pattern tags,
component breakdown, and summary structure.

The summary text itself is asserted only by *substring* — we check that the
right move name or descriptor appears, not the exact wording, so copy edits
to ``_summary`` won't churn the tests.
"""

from __future__ import annotations

import pytest

from chess_vol.explain import (
    PATTERN_CHECKMATE,
    PATTERN_DECIDED,
    PATTERN_DEFENSIVE_CRISIS,
    PATTERN_FEW_GOOD_MOVES,
    PATTERN_FORGIVING,
    PATTERN_KNIFE_EDGE,
    PATTERN_MATE_AVAILABLE,
    PATTERN_ONLY_MOVE,
    PATTERN_REPLY_DOMINATES,
    PATTERN_SCALE_DAMPENED,
    PATTERN_STALEMATE,
    Explanation,
    explain,
)
from chess_vol.volatility import TopLine, VolatilityResult


def _result(
    *,
    score: float | None = 50.0,
    raw_cp: float | None = 100.0,
    local_raw_cp: float | None = 100.0,
    best_cp: int = 0,
    alts_cp: list[int] | None = None,
    scale: float = 1.0,
    decided: bool = False,
    reason: str | None = None,
    recurse_depth_used: int = 0,
    top_lines: list[TopLine] | None = None,
) -> VolatilityResult:
    """Convenience constructor for VolatilityResult — keeps tests terse."""
    return VolatilityResult(
        score=score,
        raw_cp=raw_cp,
        local_raw_cp=local_raw_cp,
        best_eval_cp=best_cp,
        alt_evals_cp=alts_cp if alts_cp is not None else [],
        scale=scale,
        decided=decided,
        reason=reason,
        recurse_depth_used=recurse_depth_used,
        analyses=1,
        top_lines=top_lines if top_lines is not None else [],
    )


def _line(san: str, cp: int) -> TopLine:
    """Minimal TopLine — uci/pv_san aren't used by the explainer."""
    return TopLine(uci="a1a2", san=san, pv_san=[san], eval_cp=cp)


# --------------------------------------------------------------------------- #
# Terminal / undefined cases                                                   #
# --------------------------------------------------------------------------- #


class TestTerminalCases:
    def test_checkmate(self) -> None:
        e = explain(_result(score=None, raw_cp=None, local_raw_cp=None, reason="checkmate"))
        assert e.headline_pattern == PATTERN_CHECKMATE
        assert e.patterns == [PATTERN_CHECKMATE]
        assert "checkmate" in e.summary.lower()
        assert e.components == []

    def test_stalemate(self) -> None:
        e = explain(_result(score=None, raw_cp=None, local_raw_cp=None, reason="stalemate"))
        assert e.headline_pattern == PATTERN_STALEMATE
        assert "stalemate" in e.summary.lower() or "drawn" in e.summary.lower()
        assert e.components == []

    def test_only_move_with_san(self) -> None:
        e = explain(
            _result(
                score=None,
                raw_cp=None,
                local_raw_cp=None,
                reason="only_move",
                top_lines=[_line("Kg1", 0)],
            )
        )
        assert e.headline_pattern == PATTERN_ONLY_MOVE
        assert "Kg1" in e.summary
        assert e.components == []

    def test_only_move_without_san_does_not_crash(self) -> None:
        e = explain(_result(score=None, raw_cp=None, local_raw_cp=None, reason="only_move"))
        assert e.headline_pattern == PATTERN_ONLY_MOVE
        assert "one legal move" in e.summary.lower()


# --------------------------------------------------------------------------- #
# Decided positions                                                            #
# --------------------------------------------------------------------------- #


class TestDecided:
    def test_decided_short_circuits_other_patterns(self) -> None:
        # Even if alts look like a knife-edge by the eval gap, decided wins
        # the headline (the bar is dimmed; that's the dominant story).
        e = explain(
            _result(
                score=20.0,
                raw_cp=30.0,
                local_raw_cp=30.0,
                best_cp=900,
                alts_cp=[500, 100, -100, -300, -500],
                decided=True,
                top_lines=[_line("Rxd8", 900), _line("Rd1", 500)],
            )
        )
        assert e.headline_pattern == PATTERN_DECIDED
        assert PATTERN_DECIDED in e.patterns
        assert "decided" in e.summary.lower() or "dimmed" in e.summary.lower()


# --------------------------------------------------------------------------- #
# Knife-edge / defensive crisis                                                #
# --------------------------------------------------------------------------- #


class TestKnifeEdge:
    def test_winning_knife_edge_uses_san(self) -> None:
        # Best wins, every alt loses meaningfully — classic knife-edge.
        e = explain(
            _result(
                score=85.0,
                best_cp=300,
                alts_cp=[-100, -300, -500, -800, -1200],
                top_lines=[_line("Nxe5", 300), _line("Bxe5", -100)],
            )
        )
        assert e.headline_pattern == PATTERN_KNIFE_EDGE
        assert PATTERN_KNIFE_EDGE in e.patterns
        # Mention both the saving move and the alternative
        assert "Nxe5" in e.summary
        assert "Bxe5" in e.summary

    def test_defensive_crisis_when_best_is_zero_and_all_others_lose(self) -> None:
        # Best move only holds (~0); every alt is losing. Defensive crisis
        # is more specific than knife-edge and should headline.
        e = explain(
            _result(
                score=92.0,
                best_cp=0,
                alts_cp=[-300, -500, -700, -900, -1200],
                top_lines=[_line("Qxg7+", 0), _line("Kg1", -300)],
            )
        )
        assert e.headline_pattern == PATTERN_DEFENSIVE_CRISIS
        assert PATTERN_DEFENSIVE_CRISIS in e.patterns
        assert PATTERN_KNIFE_EDGE in e.patterns  # superset
        assert "Qxg7+" in e.summary


# --------------------------------------------------------------------------- #
# Few-good-moves vs forgiving                                                  #
# --------------------------------------------------------------------------- #


class TestMoveDistribution:
    def test_forgiving_when_three_or_more_alts_are_credible(self) -> None:
        # 4 alts within 50cp of best → forgiving (green-bar narrative).
        e = explain(
            _result(
                score=12.0,
                best_cp=20,
                alts_cp=[10, 0, -10, -20, -30],
                top_lines=[_line("e4", 20)],
            )
        )
        assert e.headline_pattern == PATTERN_FORGIVING
        assert PATTERN_FORGIVING in e.patterns
        assert "plays itself" in e.summary or "forgiving" in e.summary

    def test_few_good_moves_intermediate(self) -> None:
        # 2 credible alts, others losing — between forgiving and knife-edge.
        e = explain(
            _result(
                score=55.0,
                best_cp=80,
                alts_cp=[60, 30, -250, -400, -600],
                top_lines=[_line("Bd3", 80)],
            )
        )
        assert e.headline_pattern == PATTERN_FEW_GOOD_MOVES
        assert PATTERN_FEW_GOOD_MOVES in e.patterns


# --------------------------------------------------------------------------- #
# Reply-dominates (deep mode)                                                  #
# --------------------------------------------------------------------------- #


class TestReplyDominates:
    def test_reply_dominates_when_local_share_below_threshold(self) -> None:
        # local=20, total=100 → local share is 20% → reply dominates.
        e = explain(
            _result(
                score=70.0,
                raw_cp=100.0,
                local_raw_cp=20.0,
                best_cp=50,
                alts_cp=[30, 10, -10, -30, -50],
                recurse_depth_used=2,
                top_lines=[_line("Qd2", 50)],
            )
        )
        assert e.headline_pattern == PATTERN_REPLY_DOMINATES
        assert PATTERN_REPLY_DOMINATES in e.patterns
        assert "Qd2" in e.summary

    def test_no_reply_pattern_in_shallow_mode(self) -> None:
        # Same numbers but recurse_depth_used=0 → reply pattern can't fire.
        e = explain(
            _result(
                score=70.0,
                raw_cp=100.0,
                local_raw_cp=20.0,
                best_cp=50,
                alts_cp=[30, 10, -10, -30, -50],
                recurse_depth_used=0,
                top_lines=[_line("Qd2", 50)],
            )
        )
        assert PATTERN_REPLY_DOMINATES not in e.patterns


# --------------------------------------------------------------------------- #
# Mate-available                                                               #
# --------------------------------------------------------------------------- #


class TestMateAvailable:
    def test_mate_with_sharp_alts_is_knife_edge(self) -> None:
        # M1 best, all alts losing — knife-edge wins the headline because
        # the *decision* is the story, not the mere existence of the mate.
        e = explain(
            _result(
                score=98.0,
                best_cp=1950,
                alts_cp=[200, 0, -200, -400, -600],
                top_lines=[_line("Qxh7#", 1950), _line("Qe4", 200)],
            )
        )
        assert PATTERN_MATE_AVAILABLE in e.patterns
        assert e.headline_pattern == PATTERN_KNIFE_EDGE

    def test_mate_with_redundant_options_is_decided(self) -> None:
        # Multiple mates / huge winning lines → decided, mate is mentioned.
        e = explain(
            _result(
                score=10.0,
                best_cp=1950,
                alts_cp=[1850, 400, 200, 0, -100],
                decided=True,
                top_lines=[_line("Qh7#", 1950)],
            )
        )
        assert PATTERN_MATE_AVAILABLE in e.patterns
        assert e.headline_pattern == PATTERN_DECIDED


# --------------------------------------------------------------------------- #
# Eval-scale dampening                                                         #
# --------------------------------------------------------------------------- #


class TestScaleDampening:
    def test_scale_dampened_pattern_when_scale_low(self) -> None:
        # Winning with options — scale very low. No knife-edge alt distribution.
        e = explain(
            _result(
                score=10.0,
                raw_cp=15.0,
                local_raw_cp=15.0,
                best_cp=450,
                alts_cp=[420, 400, 380, 350, 300],
                scale=0.2,
                top_lines=[_line("Rxd5", 450)],
            )
        )
        assert PATTERN_SCALE_DAMPENED in e.patterns

    def test_dampening_component_appears_when_scale_below_threshold(self) -> None:
        e = explain(
            _result(
                score=10.0,
                raw_cp=15.0,
                local_raw_cp=15.0,
                best_cp=450,
                alts_cp=[420, 400, 380, 350, 300],
                scale=0.2,
                top_lines=[_line("Rxd5", 450)],
            )
        )
        names = [c.name for c in e.components]
        assert "eval_scale" in names
        scale_comp = next(c for c in e.components if c.name == "eval_scale")
        assert scale_comp.direction == "removes"
        assert scale_comp.value > 0  # something was removed

    def test_no_dampening_component_when_scale_normal(self) -> None:
        e = explain(
            _result(
                score=40.0,
                raw_cp=80.0,
                local_raw_cp=80.0,
                best_cp=80,
                alts_cp=[30, 10, -5, -20, -40],
                scale=1.0,
                top_lines=[_line("e4", 80)],
            )
        )
        names = [c.name for c in e.components]
        assert "eval_scale" not in names


# --------------------------------------------------------------------------- #
# Components                                                                   #
# --------------------------------------------------------------------------- #


class TestComponents:
    def test_shallow_mode_has_only_move_choice_component(self) -> None:
        e = explain(
            _result(
                score=40.0,
                raw_cp=80.0,
                local_raw_cp=80.0,
                best_cp=80,
                alts_cp=[30, 10, -5, -20, -40],
                recurse_depth_used=0,
                top_lines=[_line("e4", 80)],
            )
        )
        names = [c.name for c in e.components]
        assert names == ["move_choice"]

    def test_deep_mode_has_both_move_choice_and_reply(self) -> None:
        e = explain(
            _result(
                score=60.0,
                raw_cp=100.0,
                local_raw_cp=60.0,
                best_cp=80,
                alts_cp=[30, 10, -5, -20, -40],
                recurse_depth_used=2,
                top_lines=[_line("e4", 80)],
            )
        )
        names = [c.name for c in e.components]
        assert "move_choice" in names
        assert "reply" in names
        # Move-choice value should be larger than reply (60% local share)
        mc = next(c for c in e.components if c.name == "move_choice").value
        rep = next(c for c in e.components if c.name == "reply").value
        assert mc > rep

    def test_components_split_proportionally(self) -> None:
        # local=40, total=100 → 40% local / 60% reply.
        e = explain(
            _result(
                score=80.0,
                raw_cp=100.0,
                local_raw_cp=40.0,
                best_cp=50,
                alts_cp=[20, 0, -20, -40, -60],
                recurse_depth_used=2,
                top_lines=[_line("Nf3", 50)],
            )
        )
        mc = next(c for c in e.components if c.name == "move_choice").value
        rep = next(c for c in e.components if c.name == "reply").value
        # Allow rounding noise from .value being rounded to one decimal.
        assert mc == pytest.approx(80.0 * 0.4, abs=0.2)
        assert rep == pytest.approx(80.0 * 0.6, abs=0.2)


# --------------------------------------------------------------------------- #
# Generic-bucket fallback                                                      #
# --------------------------------------------------------------------------- #


class TestFallbackBuckets:
    def test_low_score_no_pattern_falls_back_to_quiet_summary(self) -> None:
        # No alts at all → no pattern fires; summary should be score-bucket.
        e = explain(
            _result(
                score=10.0,
                raw_cp=20.0,
                local_raw_cp=20.0,
                best_cp=20,
                alts_cp=[],
                top_lines=[_line("e4", 20)],
            )
        )
        assert e.headline_pattern is None
        assert "quiet" in e.summary.lower() or "keep" in e.summary.lower()

    def test_high_score_no_pattern_falls_back_to_sharp_summary(self) -> None:
        e = explain(
            _result(
                score=80.0,
                raw_cp=180.0,
                local_raw_cp=180.0,
                best_cp=20,
                alts_cp=[],
                top_lines=[_line("Nf3", 20)],
            )
        )
        # No alts means no pattern can fire — but summary still informs.
        assert "sharp" in e.summary.lower() or "punish" in e.summary.lower()


# --------------------------------------------------------------------------- #
# Returned shape                                                               #
# --------------------------------------------------------------------------- #


class TestReturnShape:
    def test_explain_returns_explanation(self) -> None:
        e = explain(_result())
        assert isinstance(e, Explanation)
        assert isinstance(e.summary, str)
        assert isinstance(e.patterns, list)
        assert isinstance(e.components, list)

    def test_summary_is_never_empty(self) -> None:
        e = explain(_result(score=None, raw_cp=None, local_raw_cp=None, reason="checkmate"))
        assert e.summary.strip() != ""
