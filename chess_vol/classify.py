"""Move classification using eval loss plus position volatility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from chess_vol.analyze import PlyResult

PrimaryLabel = Literal[
    "brilliant",
    "great",
    "best",
    "good",
    "inaccuracy",
    "mistake",
    "blunder",
]
SecondaryTag = Literal[
    "routine_miss",
    "critical_miss",
    "practical",
    "simplification",
    "defusal",
    "complication",
]


@dataclass(frozen=True)
class Classification:
    primary: PrimaryLabel
    secondary: SecondaryTag | None
    eval_drop_cp: float
    v_delta: float
    summary: str


def _score_or_zero(score: float | None) -> float:
    return float(score) if score is not None else 0.0


def _played_was_best(prev_ply: PlyResult) -> bool:
    lines = prev_ply.volatility.top_lines
    return bool(prev_ply.move_uci and lines and prev_ply.move_uci == lines[0].uci)


def _second_best_drop_cp(prev_ply: PlyResult) -> float:
    lines = prev_ply.volatility.top_lines
    if len(lines) >= 2:
        return float(lines[0].eval_cp - lines[1].eval_cp)
    if prev_ply.volatility.alt_evals_cp:
        return float(prev_ply.volatility.best_eval_cp - prev_ply.volatility.alt_evals_cp[0])
    return 0.0


def _played_line_eval_cp(prev_ply: PlyResult) -> int | None:
    for line in prev_ply.volatility.top_lines:
        if line.uci == prev_ply.move_uci:
            return line.eval_cp
    if _played_was_best(prev_ply):
        return prev_ply.volatility.best_eval_cp
    return None


def _eval_drop_cp(prev_ply: PlyResult, next_ply: PlyResult | None) -> float | None:
    if next_ply is not None and next_ply.volatility.reason is None:
        # Eval is side-to-move POV. The next ply's side-to-move is the opponent,
        # so negate it to recover the mover's post-move POV.
        return float(prev_ply.eval_cp + next_ply.eval_cp)

    played_eval = _played_line_eval_cp(prev_ply)
    if played_eval is None:
        return None
    return float(prev_ply.volatility.best_eval_cp - played_eval)


def _primary_label(
    *,
    played_was_best: bool,
    prev_v: float,
    second_best_drop_cp: float,
    eval_drop_cp: float,
) -> PrimaryLabel:
    if (
        played_was_best
        and prev_v >= 60
        and second_best_drop_cp >= 200
        and eval_drop_cp <= 5
    ):
        return "brilliant"
    if played_was_best and prev_v >= 25 and eval_drop_cp <= 5:
        return "great"
    if played_was_best and eval_drop_cp <= 5:
        return "best"
    if eval_drop_cp <= 30:
        return "good"
    if eval_drop_cp <= 90:
        return "inaccuracy"
    if eval_drop_cp <= 200:
        return "mistake"
    return "blunder"


def _secondary_tag(
    *,
    primary: PrimaryLabel,
    played_was_best: bool,
    prev_v: float,
    prev_eval_stm: int,
    v_delta: float | None,
    eval_drop_cp: float,
) -> SecondaryTag | None:
    if primary in {"mistake", "blunder"} and prev_v < 25:
        return "routine_miss"
    if primary in {"mistake", "blunder"} and prev_v >= 60:
        return "critical_miss"
    if v_delta is None:
        return None
    if not played_was_best and prev_eval_stm < -200 and v_delta >= 15:
        return "practical"
    if not played_was_best and prev_eval_stm > 200 and v_delta <= -15 and eval_drop_cp <= 50:
        return "simplification"
    if played_was_best and v_delta <= -25:
        return "defusal"
    if played_was_best and v_delta >= 25:
        return "complication"
    return None


def _summary(primary: PrimaryLabel, secondary: SecondaryTag | None) -> str:
    if secondary == "routine_miss":
        return "You missed something easy in a forgiving position."
    if secondary == "critical_miss":
        return "You missed something hard in a volatile position."
    if secondary == "practical":
        return "A practical try that raised the opponent's volatility from a losing position."
    if secondary == "simplification":
        return "A controlled simplification that calmed the position while preserving the win."
    if secondary == "defusal":
        return "Best move, and it navigated the minefield."
    if secondary == "complication":
        return "Best move, and it sharpened the game."

    summaries: dict[PrimaryLabel, str] = {
        "brilliant": "Brilliant best move in a sharp position.",
        "great": "Great best move in a volatile position.",
        "best": "Best move.",
        "good": "Good move with only a small eval loss.",
        "inaccuracy": "Inaccuracy: the eval slipped noticeably.",
        "mistake": "Mistake: the eval dropped by a lot.",
        "blunder": "Blunder: the eval collapsed.",
    }
    return summaries[primary]


def classify_move(prev_ply: PlyResult, next_ply: PlyResult | None) -> Classification | None:
    """Classify the move played on ``prev_ply``.

    Returns ``None`` only when the pre-move position itself is terminal or the
    played move cannot be evaluated from either the following ply or MultiPV.
    """
    if prev_ply.volatility.reason in {"checkmate", "stalemate", "only_move"}:
        return None

    eval_drop = _eval_drop_cp(prev_ply, next_ply)
    if eval_drop is None:
        return None

    played_was_best = _played_was_best(prev_ply)
    prev_v = _score_or_zero(prev_ply.volatility.score)
    primary = _primary_label(
        played_was_best=played_was_best,
        prev_v=prev_v,
        second_best_drop_cp=_second_best_drop_cp(prev_ply),
        eval_drop_cp=eval_drop,
    )

    if next_ply is None or next_ply.volatility.reason is not None:
        v_delta: float | None = None
        rendered_v_delta = 0.0
    else:
        v_delta = _score_or_zero(next_ply.volatility.score) - prev_v
        rendered_v_delta = v_delta

    secondary = _secondary_tag(
        primary=primary,
        played_was_best=played_was_best,
        prev_v=prev_v,
        prev_eval_stm=prev_ply.volatility.best_eval_cp,
        v_delta=v_delta,
        eval_drop_cp=eval_drop,
    )
    return Classification(
        primary=primary,
        secondary=secondary,
        eval_drop_cp=eval_drop,
        v_delta=rendered_v_delta,
        summary=_summary(primary, secondary),
    )


__all__ = [
    "Classification",
    "PrimaryLabel",
    "SecondaryTag",
    "classify_move",
]
