"""Calibration support for the volatility constants (README §7 / Phase 2.5).

The volatility algorithm has 8 coupled constants:

* ``K_SHALLOW`` — normalisation for one-ply mode
* ``EVAL_SCALE_GRACE`` — cp below which no dampening applies
* ``EVAL_SCALE_WIDTH`` — how fast dampening kicks in above the grace zone
* ``EVAL_SCALE_MAX`` — clamp on ``min(|e1|,|e2|)`` to keep mate scores from
  collapsing V to zero
* ``MATE_BASE`` — cp value of mate-in-1
* ``MATE_STEP`` — cp lost per additional mate-distance step
* ``DECIDED_BEST_CP`` / ``DECIDED_ALT_CP`` — thresholds for the "position is
  technically over" flag

This module separates the **slow** part (running Stockfish on a corpus to
collect raw multi-PV evaluations) from the **fast** part (re-computing V from
those cached evaluations under candidate constants and optimising to fit a
ground truth). That separation matters because tuning is iterative — you
want to try dozens of constant settings against the same engine output, not
relaunch Stockfish each time.

This module is **engine-free**. It defines:

* :class:`CorpusEntry` and :class:`CachedAnalysis` data classes
* :func:`recompute_v` — pure function from cached engine output + candidate
  constants → V score (mirrors :func:`chess_vol.volatility.compute_volatility`
  for ``recurse_depth=0``)
* loss functions for both calibration modes (expert-rating MSE and
  distributional KL divergence)
* :func:`tune_constants` — scipy.optimize wrapper that finds the best
  constant settings under a chosen loss

The CLI driver (which *does* call Stockfish) lives in ``scripts/calibrate.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from chess_vol.config import (
    DECIDED_ALT_CP,
    DECIDED_BEST_CP,
    DROP_CAP,
    EVAL_SCALE_GRACE,
    EVAL_SCALE_MAX,
    EVAL_SCALE_WIDTH,
    K_SHALLOW,
    MATE_BASE,
    MATE_MAX_N,
    MATE_STEP,
)
from chess_vol.volatility import default_weights

# --------------------------------------------------------------------------- #
# Data classes                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawScore:
    """One MultiPV line's raw engine output, **un-translated**.

    Exactly one of ``cp`` and ``mate`` is set. Storing the raw form means we
    can re-translate mate scores under different ``MATE_BASE`` / ``MATE_STEP``
    values without re-running Stockfish.
    """

    cp: int | None = None
    """Centipawn score from side-to-move POV. ``None`` for mate scores."""

    mate: int | None = None
    """Mate-in-N from side-to-move POV (``None`` for cp scores)."""

    def __post_init__(self) -> None:
        if (self.cp is None) == (self.mate is None):
            raise ValueError("Exactly one of cp / mate must be set")


@dataclass(frozen=True)
class CachedAnalysis:
    """Cached MultiPV result for one position. Engine-free input to tuning.

    ``lines`` is sorted best-first from STM POV under the *current* mate
    translation; calibration re-sorts after each retranslation.
    """

    fen: str
    lines: list[RawScore]
    legal_count: int
    """Number of legal moves at this position. Drives the only-move case."""

    is_terminal: bool = False
    """True if checkmate or stalemate at the root."""


@dataclass(frozen=True)
class CorpusEntry:
    """One labelled corpus position.

    ``label`` is the human-rated 0-100 sharpness (optional — pure-distributional
    calibration doesn't need it). ``category`` is a free-form tag used for
    distributional checks (``"quiet"``, ``"sharp"``, ``"endgame"``, etc.).
    """

    id: str
    fen: str
    label: float | None = None
    category: str | None = None


@dataclass(frozen=True)
class Constants:
    """The 8 tunable constants, packaged for easy substitution.

    All fields default to whatever's currently in :mod:`chess_vol.config` so
    you can tweak one at a time and see the impact. The optimiser treats this
    as a vector; :meth:`as_vector` and :meth:`from_vector` round-trip it.
    """

    k_shallow: float = K_SHALLOW
    eval_scale_grace: float = EVAL_SCALE_GRACE
    eval_scale_width: float = EVAL_SCALE_WIDTH
    eval_scale_max: float = EVAL_SCALE_MAX
    mate_base: float = MATE_BASE
    mate_step: float = MATE_STEP
    decided_best_cp: float = DECIDED_BEST_CP
    decided_alt_cp: float = DECIDED_ALT_CP

    def as_vector(self) -> list[float]:
        return [
            self.k_shallow,
            self.eval_scale_grace,
            self.eval_scale_width,
            self.eval_scale_max,
            self.mate_base,
            self.mate_step,
            self.decided_best_cp,
            self.decided_alt_cp,
        ]

    @classmethod
    def from_vector(cls, v: Sequence[float]) -> Constants:
        if len(v) != 8:
            raise ValueError(f"expected 8-element vector, got {len(v)}")
        return cls(
            k_shallow=v[0],
            eval_scale_grace=v[1],
            eval_scale_width=v[2],
            eval_scale_max=v[3],
            mate_base=v[4],
            mate_step=v[5],
            decided_best_cp=v[6],
            decided_alt_cp=v[7],
        )


@dataclass(frozen=True)
class TuningResult:
    """Output of :func:`tune_constants`."""

    constants: Constants
    """Best-fit constants under the chosen loss."""

    loss: float
    """Final loss value at ``constants``."""

    iterations: int
    """Number of optimiser iterations."""

    converged: bool


# --------------------------------------------------------------------------- #
# Pure recompute from cached engine output                                     #
# --------------------------------------------------------------------------- #


def _mate_to_cp_with(mate: int, base: float, step: float) -> float:
    """Same shape as :func:`chess_vol.volatility.mate_to_cp` but parametrised
    so calibration can sweep ``MATE_BASE`` / ``MATE_STEP`` without monkey-
    patching the config module."""
    if mate == 0:
        raise ValueError("mate=0 is undefined; the position is already mated")
    sign = 1 if mate > 0 else -1
    distance = min(abs(mate), MATE_MAX_N)
    return float(sign) * (base - step * distance)


def _line_to_cp(line: RawScore, constants: Constants) -> float:
    """Translate one raw line to a centipawn value under ``constants``."""
    if line.mate is not None:
        return _mate_to_cp_with(line.mate, constants.mate_base, constants.mate_step)
    assert line.cp is not None  # mypy: __post_init__ enforces the xor
    return float(line.cp)


def _scale_with(e1: float, e2: float, c: Constants) -> float:
    """Eval-aware dampening factor under candidate constants."""
    scale_eval = min(abs(e1), abs(e2), c.eval_scale_max)
    excess = max(0.0, scale_eval - c.eval_scale_grace)
    ratio = excess / c.eval_scale_width
    return 1.0 / (1.0 + ratio * ratio)


def _is_decided_with(e1: float, e2: float, c: Constants) -> bool:
    return (
        abs(e1) > c.decided_best_cp
        and (e1 > 0) == (e2 > 0)
        and abs(e2) > c.decided_alt_cp
    )


def recompute_v(analysis: CachedAnalysis, constants: Constants) -> float | None:
    """Recompute one-ply V for a cached analysis under candidate constants.

    Returns ``None`` for terminal / only-move positions (mirrors what
    :func:`chess_vol.volatility.compute_volatility` would return for the root).

    This is the inner loop of every calibration iteration — keep it cheap.
    """
    if analysis.is_terminal:
        return None
    if analysis.legal_count <= 1 or len(analysis.lines) < 2:
        return None

    cps = sorted((_line_to_cp(line, constants) for line in analysis.lines), reverse=True)
    e1 = cps[0]
    weights = default_weights(len(cps))
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError(f"degenerate weights for n={len(cps)}")

    drops = [min(e1 - e_i, DROP_CAP) for e_i in cps[1:]]
    weighted_sum = sum(w * d for w, d in zip(weights, drops, strict=True)) / weight_sum

    scale = _scale_with(e1, cps[1], constants)
    raw = scale * weighted_sum
    return 100.0 * (1.0 - math.exp(-raw / constants.k_shallow))


# --------------------------------------------------------------------------- #
# Loss functions                                                               #
# --------------------------------------------------------------------------- #


LossMode = Literal["expert", "distributional", "blended"]


@dataclass(frozen=True)
class CategoryTarget:
    """Target distribution shape for one category.

    Shape is given as `(low_share, med_share, high_share)` summing to 1.0
    using the same colour buckets as the UI (READMI §3.6). These are the
    buckets we tune the *distribution* to match in distributional mode.
    """

    name: str
    low: float
    medium: float
    high: float

    def as_vector(self) -> tuple[float, float, float]:
        return (self.low, self.medium, self.high)


#: Sensible default targets for the four canonical categories. These are the
#: numbers used for the initial calibration pass; refine against your corpus.
DEFAULT_TARGETS: Final[dict[str, CategoryTarget]] = {
    # Quiet openings / dead-drawn endgames: nearly all green.
    "quiet": CategoryTarget("quiet", low=0.95, medium=0.05, high=0.0),
    # Master middlegames: mostly green, a third yellow, occasional red.
    "middlegame": CategoryTarget("middlegame", low=0.55, medium=0.35, high=0.10),
    # Tactical / sacrificial games: more reds, a lot of yellow.
    "sharp": CategoryTarget("sharp", low=0.20, medium=0.45, high=0.35),
    # Forced-mate / only-move puzzles: almost entirely red (per-position).
    "tactical": CategoryTarget("tactical", low=0.05, medium=0.20, high=0.75),
}


def _bucket(score: float) -> int:
    """Return 0/1/2 for low/medium/high. Mirrors :func:`color_for`."""
    if score < 25.0:
        return 0
    if score < 60.0:
        return 1
    return 2


def expert_loss(
    corpus: Sequence[CorpusEntry],
    analyses: dict[str, CachedAnalysis],
    constants: Constants,
) -> float:
    """Mean squared error between V and human label, over labelled positions.

    Skips positions without a label, without a cached analysis, or where V is
    undefined (terminal / only-move). Returns ``0.0`` if no labelled positions
    survive — caller should warn separately, not via the loss value.
    """
    sq_errors: list[float] = []
    for entry in corpus:
        if entry.label is None:
            continue
        analysis = analyses.get(entry.id)
        if analysis is None:
            continue
        v = recompute_v(analysis, constants)
        if v is None:
            continue
        sq_errors.append((v - entry.label) ** 2)
    if not sq_errors:
        return 0.0
    return sum(sq_errors) / len(sq_errors)


def distributional_loss(
    corpus: Sequence[CorpusEntry],
    analyses: dict[str, CachedAnalysis],
    constants: Constants,
    targets: dict[str, CategoryTarget] | None = None,
) -> float:
    """KL-style divergence between observed bucket distribution and target,
    summed across categories present in the corpus.

    For each category in ``corpus``: bin the V scores into low/medium/high,
    compare to ``targets[category]``. Categories without a target are skipped
    (they don't contribute to the loss). Empty categories contribute 0.

    We use a smoothed KL-divergence to avoid log(0) explosions when a bucket
    is empty: every bucket gets a small ``epsilon`` floor before normalisation.
    """
    if targets is None:
        targets = DEFAULT_TARGETS

    by_cat: dict[str, list[float]] = {}
    for entry in corpus:
        if entry.category is None:
            continue
        analysis = analyses.get(entry.id)
        if analysis is None:
            continue
        v = recompute_v(analysis, constants)
        if v is None:
            continue
        by_cat.setdefault(entry.category, []).append(v)

    epsilon = 0.01
    total = 0.0
    matched_categories = 0
    for category, scores in by_cat.items():
        target = targets.get(category)
        if target is None or not scores:
            continue
        counts = [0, 0, 0]
        for s in scores:
            counts[_bucket(s)] += 1
        n = len(scores)
        observed = [c / n for c in counts]
        # Smooth both distributions to avoid log(0).
        smoothed_obs = [(o + epsilon) / (1.0 + 3 * epsilon) for o in observed]
        smoothed_tgt = [
            (t + epsilon) / (1.0 + 3 * epsilon) for t in target.as_vector()
        ]
        kl = sum(
            o * math.log(o / t) for o, t in zip(smoothed_obs, smoothed_tgt, strict=True)
        )
        total += kl
        matched_categories += 1
    if matched_categories == 0:
        return 0.0
    return total / matched_categories


def blended_loss(
    corpus: Sequence[CorpusEntry],
    analyses: dict[str, CachedAnalysis],
    constants: Constants,
    expert_weight: float = 1.0,
    distributional_weight: float = 50.0,
    targets: dict[str, CategoryTarget] | None = None,
) -> float:
    """Weighted sum of expert MSE and distributional KL.

    The default weights account for the very different scales: expert MSE on
    a 0-100 score easily reaches the hundreds, while KL divergence is
    typically <1. ``distributional_weight=50`` balances them so neither
    dominates the optimiser. Tune to your taste.
    """
    e = expert_loss(corpus, analyses, constants)
    d = distributional_loss(corpus, analyses, constants, targets=targets)
    return expert_weight * e + distributional_weight * d


# --------------------------------------------------------------------------- #
# Optimiser                                                                    #
# --------------------------------------------------------------------------- #


# Hard bounds for the optimiser. Wide enough to leave room for tuning, tight
# enough that a wandering optimiser can't produce nonsensical constants
# (e.g. negative width, MATE_BASE below the realistic eval ceiling).
DEFAULT_BOUNDS: Final[list[tuple[float, float]]] = [
    (50.0, 600.0),     # k_shallow
    (0.0, 800.0),      # eval_scale_grace
    (50.0, 1000.0),    # eval_scale_width
    (500.0, 5000.0),   # eval_scale_max
    (1500.0, 3000.0),  # mate_base
    (10.0, 200.0),     # mate_step
    (300.0, 1500.0),   # decided_best_cp
    (100.0, 1000.0),   # decided_alt_cp
]


def tune_constants(
    corpus: Sequence[CorpusEntry],
    analyses: dict[str, CachedAnalysis],
    *,
    mode: LossMode = "blended",
    initial: Constants | None = None,
    bounds: list[tuple[float, float]] | None = None,
    targets: dict[str, CategoryTarget] | None = None,
    max_iter: int = 200,
) -> TuningResult:
    """Find the best-fit constants under the chosen loss mode.

    Uses ``scipy.optimize.minimize`` with the L-BFGS-B method, which respects
    box bounds and handles the smooth-but-noisy losses we have well. Falls
    back to :class:`Constants` defaults if scipy isn't installed.

    Parameters
    ----------
    corpus, analyses:
        Same data both come from — see :func:`load_corpus` /
        :func:`load_analyses`. ``analyses`` is keyed by ``entry.id``.
    mode:
        Loss function — ``"expert"`` requires labelled corpus entries,
        ``"distributional"`` requires categorised entries, ``"blended"``
        uses both.
    initial:
        Starting point. Defaults to the current constants from
        :mod:`chess_vol.config`.
    bounds:
        Per-coordinate bounds. Defaults to :data:`DEFAULT_BOUNDS`.
    targets:
        Per-category target distributions for distributional / blended modes.
    """
    try:
        from scipy.optimize import minimize  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "scipy is required for tune_constants. Install with: pip install scipy"
        ) from exc

    if initial is None:
        initial = Constants()
    if bounds is None:
        bounds = DEFAULT_BOUNDS

    def loss_fn(vec: Sequence[float]) -> float:
        c = Constants.from_vector(vec)
        if mode == "expert":
            return expert_loss(corpus, analyses, c)
        if mode == "distributional":
            return distributional_loss(corpus, analyses, c, targets=targets)
        return blended_loss(corpus, analyses, c, targets=targets)

    result = minimize(
        loss_fn,
        x0=initial.as_vector(),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": max_iter},
    )
    return TuningResult(
        constants=Constants.from_vector(list(result.x)),
        loss=float(result.fun),
        iterations=int(result.nit),
        converged=bool(result.success),
    )


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CategoryReport:
    """Per-category summary of V scores under one set of constants."""

    name: str
    n: int
    mean_v: float
    low_share: float
    medium_share: float
    high_share: float
    target: CategoryTarget | None = None


@dataclass(frozen=True)
class CalibrationReport:
    """Top-level summary returned by :func:`build_report`."""

    constants: Constants
    n_total: int
    n_undefined: int
    """Positions where V was ``None`` (terminal / only-move)."""

    overall_mean_v: float
    overall_low_share: float
    overall_medium_share: float
    overall_high_share: float
    categories: list[CategoryReport] = field(default_factory=list)
    expert_mse: float | None = None
    """Mean-squared error against labels, or ``None`` if no labels in corpus."""

    distributional_kl: float | None = None
    """Mean KL divergence against targets, or ``None`` if no targeted
    categories in corpus."""


def build_report(
    corpus: Sequence[CorpusEntry],
    analyses: dict[str, CachedAnalysis],
    constants: Constants | None = None,
    targets: dict[str, CategoryTarget] | None = None,
) -> CalibrationReport:
    """Compute a structured report of how ``constants`` perform on ``corpus``.

    Useful as a `before` and `after` snapshot around a tuning run.
    """
    if constants is None:
        constants = Constants()
    if targets is None:
        targets = DEFAULT_TARGETS

    scores: list[float] = []
    by_cat: dict[str, list[float]] = {}
    n_undefined = 0
    label_pairs: list[tuple[float, float]] = []

    for entry in corpus:
        analysis = analyses.get(entry.id)
        if analysis is None:
            continue
        v = recompute_v(analysis, constants)
        if v is None:
            n_undefined += 1
            continue
        scores.append(v)
        if entry.category is not None:
            by_cat.setdefault(entry.category, []).append(v)
        if entry.label is not None:
            label_pairs.append((v, entry.label))

    def shares(arr: Iterable[float]) -> tuple[float, float, float]:
        items = list(arr)
        if not items:
            return (0.0, 0.0, 0.0)
        counts = [0, 0, 0]
        for v in items:
            counts[_bucket(v)] += 1
        n = len(items)
        return tuple(c / n for c in counts)  # type: ignore[return-value]

    overall_low, overall_med, overall_high = shares(scores)
    overall_mean = sum(scores) / len(scores) if scores else 0.0

    cat_reports: list[CategoryReport] = []
    for name, vs in by_cat.items():
        low, med, high = shares(vs)
        cat_reports.append(
            CategoryReport(
                name=name,
                n=len(vs),
                mean_v=sum(vs) / len(vs),
                low_share=low,
                medium_share=med,
                high_share=high,
                target=targets.get(name),
            )
        )
    cat_reports.sort(key=lambda r: r.name)

    expert_mse: float | None
    if label_pairs:
        expert_mse = sum((v - lab) ** 2 for v, lab in label_pairs) / len(label_pairs)
    else:
        expert_mse = None

    targeted_cats = [r for r in cat_reports if r.target is not None]
    if targeted_cats:
        distributional_kl = distributional_loss(corpus, analyses, constants, targets=targets)
    else:
        distributional_kl = None

    return CalibrationReport(
        constants=constants,
        n_total=len(scores) + n_undefined,
        n_undefined=n_undefined,
        overall_mean_v=overall_mean,
        overall_low_share=overall_low,
        overall_medium_share=overall_med,
        overall_high_share=overall_high,
        categories=cat_reports,
        expert_mse=expert_mse,
        distributional_kl=distributional_kl,
    )


# --------------------------------------------------------------------------- #
# I/O — corpus & analyses serialisation                                        #
# --------------------------------------------------------------------------- #


def corpus_to_json(corpus: Sequence[CorpusEntry]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in (
            ("id", e.id),
            ("fen", e.fen),
            ("label", e.label),
            ("category", e.category),
        ) if v is not None or k in {"id", "fen"}}
        for e in corpus
    ]


def corpus_from_json(payload: list[dict[str, Any]]) -> list[CorpusEntry]:
    out: list[CorpusEntry] = []
    for raw in payload:
        out.append(
            CorpusEntry(
                id=str(raw["id"]),
                fen=str(raw["fen"]),
                label=float(raw["label"]) if raw.get("label") is not None else None,
                category=str(raw["category"]) if raw.get("category") is not None else None,
            )
        )
    return out


def analyses_to_json(analyses: dict[str, CachedAnalysis]) -> dict[str, dict[str, Any]]:
    return {
        eid: {
            "fen": a.fen,
            "legal_count": a.legal_count,
            "is_terminal": a.is_terminal,
            "lines": [
                {"cp": line.cp, "mate": line.mate} for line in a.lines
            ],
        }
        for eid, a in analyses.items()
    }


def analyses_from_json(payload: dict[str, dict[str, Any]]) -> dict[str, CachedAnalysis]:
    out: dict[str, CachedAnalysis] = {}
    for eid, raw in payload.items():
        lines = [
            RawScore(cp=line.get("cp"), mate=line.get("mate"))
            for line in raw["lines"]
        ]
        out[eid] = CachedAnalysis(
            fen=str(raw["fen"]),
            lines=lines,
            legal_count=int(raw["legal_count"]),
            is_terminal=bool(raw.get("is_terminal", False)),
        )
    return out


__all__ = [
    "DEFAULT_BOUNDS",
    "DEFAULT_TARGETS",
    "CachedAnalysis",
    "CalibrationReport",
    "CategoryReport",
    "CategoryTarget",
    "Constants",
    "CorpusEntry",
    "LossMode",
    "RawScore",
    "TuningResult",
    "analyses_from_json",
    "analyses_to_json",
    "blended_loss",
    "build_report",
    "corpus_from_json",
    "corpus_to_json",
    "distributional_loss",
    "expert_loss",
    "recompute_v",
    "tune_constants",
]
