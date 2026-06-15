"""Tests for move classification rules."""

from __future__ import annotations

import chess

from chess_vol.analyze import PlyResult
from chess_vol.classify import classify_move
from chess_vol.volatility import TopLine, VolatilityResult


def _vol(
    *,
    score: float | None,
    best_eval_cp: int,
    reason: str | None = None,
    top_lines: list[TopLine] | None = None,
) -> VolatilityResult:
    return VolatilityResult(
        score=score,
        raw_cp=score,
        local_raw_cp=score,
        best_eval_cp=best_eval_cp,
        alt_evals_cp=[line.eval_cp for line in (top_lines or [])[1:]],
        scale=1.0,
        decided=False,
        reason=reason,
        recurse_depth_used=0,
        analyses=1,
        top_lines=top_lines or [],
    )


def _lines(best: int, second: int = 0, played: int | None = None) -> list[TopLine]:
    lines = [
        TopLine("best", "Best", ["Best"], best),
        TopLine("second", "Second", ["Second"], second),
    ]
    if played is not None:
        lines.append(TopLine("played", "Played", ["Played"], played))
    return lines


def _ply(
    *,
    move_uci: str,
    eval_cp: int,
    score: float | None,
    best_eval_cp: int | None = None,
    reason: str | None = None,
    top_lines: list[TopLine] | None = None,
) -> PlyResult:
    return PlyResult(
        ply=1,
        san="a3",
        fen_before=chess.STARTING_FEN,
        fen_after=chess.STARTING_FEN,
        eval_cp=eval_cp,
        volatility=_vol(
            score=score,
            best_eval_cp=eval_cp if best_eval_cp is None else best_eval_cp,
            reason=reason,
            top_lines=top_lines,
        ),
        move_uci=move_uci,
    )


def _pair(
    *,
    move_uci: str,
    prev_eval: int = 100,
    after_eval_for_mover: int = 100,
    prev_v: float = 10,
    next_v: float = 10,
    best_eval_cp: int | None = None,
    top_lines: list[TopLine] | None = None,
) -> tuple[PlyResult, PlyResult]:
    prev = _ply(
        move_uci=move_uci,
        eval_cp=prev_eval,
        score=prev_v,
        best_eval_cp=best_eval_cp,
        top_lines=top_lines or _lines(prev_eval),
    )
    next_ply = _ply(
        move_uci="reply",
        eval_cp=-after_eval_for_mover,
        score=next_v,
        top_lines=_lines(-after_eval_for_mover),
    )
    return prev, next_ply


class TestPrimaryLabels:
    def test_brilliant(self) -> None:
        prev, next_ply = _pair(
            move_uci="best",
            prev_v=70,
            next_v=50,
            top_lines=_lines(100, -120),
        )
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "brilliant"
        assert classification.secondary is None

    def test_great(self) -> None:
        prev, next_ply = _pair(move_uci="best", prev_v=30, top_lines=_lines(100, 50))
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "great"

    def test_best(self) -> None:
        prev, next_ply = _pair(move_uci="best", prev_v=10, top_lines=_lines(100, 50))
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "best"

    def test_good(self) -> None:
        prev, next_ply = _pair(move_uci="played", after_eval_for_mover=80)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "good"

    def test_inaccuracy(self) -> None:
        prev, next_ply = _pair(move_uci="played", after_eval_for_mover=20)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "inaccuracy"

    def test_mistake(self) -> None:
        prev, next_ply = _pair(move_uci="played", after_eval_for_mover=-50)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "mistake"

    def test_blunder(self) -> None:
        prev, next_ply = _pair(move_uci="played", after_eval_for_mover=-150)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "blunder"


class TestSecondaryTags:
    def test_routine_miss(self) -> None:
        prev, next_ply = _pair(move_uci="played", after_eval_for_mover=-50, prev_v=10)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.secondary == "routine_miss"

    def test_critical_miss(self) -> None:
        prev, next_ply = _pair(move_uci="played", after_eval_for_mover=-50, prev_v=65)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.secondary == "critical_miss"

    def test_practical(self) -> None:
        prev, next_ply = _pair(
            move_uci="played",
            prev_eval=-300,
            after_eval_for_mover=-320,
            prev_v=20,
            next_v=40,
            best_eval_cp=-300,
        )
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "good"
        assert classification.secondary == "practical"

    def test_simplification(self) -> None:
        prev, next_ply = _pair(
            move_uci="played",
            prev_eval=300,
            after_eval_for_mover=260,
            prev_v=50,
            next_v=25,
            best_eval_cp=300,
        )
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "inaccuracy"
        assert classification.secondary == "simplification"

    def test_defusal(self) -> None:
        prev, next_ply = _pair(move_uci="best", prev_v=40, next_v=10)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.secondary == "defusal"

    def test_complication(self) -> None:
        prev, next_ply = _pair(move_uci="best", prev_v=40, next_v=70)
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.secondary == "complication"


class TestEdges:
    def test_last_ply_uses_played_line_and_zero_v_delta(self) -> None:
        prev = _ply(
            move_uci="played",
            eval_cp=100,
            score=40,
            top_lines=_lines(100, 90, played=80),
        )
        classification = classify_move(prev, None)
        assert classification is not None
        assert classification.primary == "good"
        assert classification.secondary is None
        assert classification.eval_drop_cp == 20
        assert classification.v_delta == 0.0

    def test_terminal_next_position_uses_played_line_without_v_delta(self) -> None:
        prev = _ply(
            move_uci="played",
            eval_cp=100,
            score=40,
            top_lines=_lines(100, 90, played=20),
        )
        next_ply = _ply(move_uci="reply", eval_cp=0, score=None, reason="checkmate")
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.primary == "inaccuracy"
        assert classification.secondary is None
        assert classification.v_delta == 0.0

    def test_terminal_prev_position_is_undefined(self) -> None:
        prev = _ply(move_uci="best", eval_cp=0, score=None, reason="checkmate")
        assert classify_move(prev, None) is None

    def test_mate_scores_are_already_centipawns(self) -> None:
        prev, next_ply = _pair(
            move_uci="played",
            prev_eval=1900,
            after_eval_for_mover=1700,
            prev_v=30,
            next_v=30,
        )
        classification = classify_move(prev, next_ply)
        assert classification is not None
        assert classification.eval_drop_cp == 200
        assert classification.primary == "mistake"
