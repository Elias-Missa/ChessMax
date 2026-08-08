"""Pure helpers that convert volatility results into JSON-serializable dicts.

Kept separate from :mod:`chess_vol.cli` so the schema is directly unit-testable
without instantiating the Typer app or an engine.

See README §7 for the CLI specification; the dict produced here is what
``chess-vol ... --output report.json`` writes to disk.
"""

from __future__ import annotations

from typing import TypedDict

from chess_vol.analyze import PlyResult
from chess_vol.classify import Classification, PrimaryLabel, SecondaryTag
from chess_vol.config import color_for
from chess_vol.explain import Component, Explanation, explain
from chess_vol.game_review import MoveReview, ReviewLabel
from core.findability import Alternate, PositionFindability
from core.volatility import TopLine, VolatilityResult


class TopLineJson(TypedDict):
    """JSON shape for a single engine line (one MultiPV entry)."""

    uci: str
    san: str
    pv_san: list[str]
    eval_cp: int


class ComponentJson(TypedDict):
    """JSON shape for one :class:`Component` of an explanation."""

    name: str
    label: str
    value: float
    direction: str
    detail: str


class ExplanationJson(TypedDict):
    """JSON shape for an :class:`Explanation`."""

    summary: str
    components: list[ComponentJson]
    patterns: list[str]
    headline_pattern: str | None


class ClassificationJson(TypedDict):
    """JSON shape for a move :class:`Classification`."""

    primary: PrimaryLabel
    secondary: SecondaryTag | None
    eval_drop_cp: float
    v_delta: float
    summary: str


class MoveReviewJson(TypedDict):
    classification: ReviewLabel
    symbol: str
    color: str
    expected_points_before: float
    expected_points_after: float
    expected_points_loss: float
    accuracy: float
    best_move_uci: str | None
    best_move_san: str | None
    best_line_san: list[str]
    eval_after_cp_white: int
    coach: str


class VolatilityJson(TypedDict):
    """JSON shape for a single :class:`VolatilityResult`."""

    score: float | None
    raw_cp: float | None
    local_raw_cp: float | None
    best_eval_cp: int
    alt_evals_cp: list[int]
    scale: float
    decided: bool
    recapture: bool
    reason: str | None
    recurse_depth_used: int
    analyses: int
    color: str | None
    top_lines: list[TopLineJson]
    explanation: ExplanationJson


class AlternateJson(TypedDict):
    """JSON shape for a findability alternate-move recommendation."""

    uci: str
    san: str
    delta_w: float
    pi: float


class FindabilityJson(TypedDict):
    """JSON shape for a :class:`PositionFindability` (spec §7)."""

    score: int
    r_find: int | None
    band: str
    personal: float | None
    personal_star: float | None
    curve: list[list[float]]
    star_curve: list[list[float]]
    alternate: AlternateJson | None
    forced: bool


class PlyJson(TypedDict):
    """JSON shape for a single :class:`PlyResult`."""

    ply: int
    san: str
    fen_before: str
    fen_after: str
    eval_cp: int
    move_uci: str
    volatility: VolatilityJson
    classification: ClassificationJson | None
    review: MoveReviewJson | None
    findability: FindabilityJson | None


class ParamsJson(TypedDict, total=False):
    """Parameters echoed back into the report for reproducibility."""

    depth: int
    multipv: int
    recurse_depth: int
    recurse_k: int
    recurse_alpha: float
    child_depth: int
    max_plies: int | None


class AnalyzeReportJson(TypedDict):
    """Top-level JSON for ``chess-vol analyze``."""

    mode: str
    params: ParamsJson
    plies: list[PlyJson]


class FenReportJson(TypedDict):
    """Top-level JSON for ``chess-vol fen``."""

    mode: str
    fen: str
    params: ParamsJson
    volatility: VolatilityJson


def mode_label(recurse_depth: int) -> str:
    """Return ``"shallow"`` for ``recurse_depth == 0`` else ``"deep"``."""
    return "shallow" if recurse_depth == 0 else "deep"


def _top_line_to_json(line: TopLine) -> TopLineJson:
    return TopLineJson(
        uci=line.uci,
        san=line.san,
        pv_san=list(line.pv_san),
        eval_cp=line.eval_cp,
    )


def _component_to_json(component: Component) -> ComponentJson:
    return ComponentJson(
        name=component.name,
        label=component.label,
        value=component.value,
        direction=component.direction,
        detail=component.detail,
    )


def explanation_to_json(explanation: Explanation) -> ExplanationJson:
    """Convert an :class:`Explanation` to a JSON-serializable dict."""
    return ExplanationJson(
        summary=explanation.summary,
        components=[_component_to_json(c) for c in explanation.components],
        patterns=list(explanation.patterns),
        headline_pattern=explanation.headline_pattern,
    )


def classification_to_json(classification: Classification) -> ClassificationJson:
    """Convert a :class:`Classification` to a JSON-serializable dict."""
    return ClassificationJson(
        primary=classification.primary,
        secondary=classification.secondary,
        eval_drop_cp=classification.eval_drop_cp,
        v_delta=classification.v_delta,
        summary=classification.summary,
    )


def move_review_to_json(review: MoveReview) -> MoveReviewJson:
    return MoveReviewJson(
        classification=review.classification,
        symbol=review.symbol,
        color=review.color,
        expected_points_before=review.expected_points_before,
        expected_points_after=review.expected_points_after,
        expected_points_loss=review.expected_points_loss,
        accuracy=review.accuracy,
        best_move_uci=review.best_move_uci,
        best_move_san=review.best_move_san,
        best_line_san=list(review.best_line_san),
        eval_after_cp_white=review.eval_after_cp_white,
        coach=review.coach,
    )


def volatility_to_json(result: VolatilityResult) -> VolatilityJson:
    """Convert a :class:`VolatilityResult` to a JSON-serializable dict."""
    color = color_for(result.score) if result.score is not None else None
    return VolatilityJson(
        score=result.score,
        raw_cp=result.raw_cp,
        local_raw_cp=result.local_raw_cp,
        best_eval_cp=result.best_eval_cp,
        alt_evals_cp=list(result.alt_evals_cp),
        scale=result.scale,
        decided=result.decided,
        recapture=result.recapture,
        reason=result.reason,
        recurse_depth_used=result.recurse_depth_used,
        analyses=result.analyses,
        color=color,
        top_lines=[_top_line_to_json(line) for line in result.top_lines],
        explanation=explanation_to_json(explain(result)),
    )


def _alternate_to_json(alternate: Alternate) -> AlternateJson:
    return AlternateJson(
        uci=alternate.uci,
        san=alternate.san,
        delta_w=alternate.delta_w,
        pi=alternate.pi,
    )


def findability_to_json(findability: PositionFindability) -> FindabilityJson:
    """Convert a :class:`PositionFindability` to a JSON-serializable dict."""
    return FindabilityJson(
        score=findability.score,
        r_find=findability.r_find,
        band=findability.band,
        personal=findability.personal,
        personal_star=findability.personal_star,
        curve=[[r, v] for r, v in findability.curve],
        star_curve=[[r, v] for r, v in findability.star_curve],
        alternate=(
            _alternate_to_json(findability.alternate)
            if findability.alternate is not None
            else None
        ),
        forced=findability.forced,
    )


def ply_to_json(ply: PlyResult) -> PlyJson:
    """Convert a :class:`PlyResult` to a JSON-serializable dict."""
    return PlyJson(
        ply=ply.ply,
        san=ply.san,
        fen_before=ply.fen_before,
        fen_after=ply.fen_after,
        eval_cp=ply.eval_cp,
        move_uci=ply.move_uci,
        volatility=volatility_to_json(ply.volatility),
        classification=(
            classification_to_json(ply.classification)
            if ply.classification is not None
            else None
        ),
        review=move_review_to_json(ply.review) if ply.review is not None else None,
        findability=(
            findability_to_json(ply.findability) if ply.findability is not None else None
        ),
    )


def build_params(
    *,
    depth: int,
    multipv: int,
    recurse_depth: int,
    recurse_k: int,
    recurse_alpha: float,
    child_depth: int,
    max_plies: int | None = None,
) -> ParamsJson:
    """Assemble a ``params`` dict for the JSON report."""
    params: ParamsJson = {
        "depth": depth,
        "multipv": multipv,
        "recurse_depth": recurse_depth,
        "recurse_k": recurse_k,
        "recurse_alpha": recurse_alpha,
        "child_depth": child_depth,
    }
    if max_plies is not None:
        params["max_plies"] = max_plies
    return params


def build_analyze_report(
    plies: list[PlyResult],
    *,
    params: ParamsJson,
) -> AnalyzeReportJson:
    """Build the top-level report for ``chess-vol analyze``."""
    return AnalyzeReportJson(
        mode=mode_label(params.get("recurse_depth", 0)),
        params=params,
        plies=[ply_to_json(p) for p in plies],
    )


def build_fen_report(
    fen: str,
    result: VolatilityResult,
    *,
    params: ParamsJson,
) -> FenReportJson:
    """Build the top-level report for ``chess-vol fen``."""
    return FenReportJson(
        mode=mode_label(params.get("recurse_depth", 0)),
        fen=fen,
        params=params,
        volatility=volatility_to_json(result),
    )


__all__: list[str] = [
    "AlternateJson",
    "AnalyzeReportJson",
    "ClassificationJson",
    "ComponentJson",
    "ExplanationJson",
    "FenReportJson",
    "FindabilityJson",
    "ParamsJson",
    "PlyJson",
    "MoveReviewJson",
    "TopLineJson",
    "VolatilityJson",
    "build_analyze_report",
    "build_fen_report",
    "build_params",
    "classification_to_json",
    "explanation_to_json",
    "findability_to_json",
    "mode_label",
    "move_review_to_json",
    "ply_to_json",
    "volatility_to_json",
]
