"""Statistical machinery for Insights 3.0 — expectation, shrinkage, intervals.

Three ideas, all of which exist to stop the report asserting things the data
does not support:

* **Expectation adjustment** (spec Phase 2). A raw win rate is partly a
  statement about who you happened to face. Every rate is reported alongside
  the points gained or lost against what the rating gaps predicted.
* **Partial pooling** (Phase 3.2). A bucket with 6 games is shrunk toward the
  player's own global mean; a bucket with 60 speaks for itself. This is the
  principled replacement for hand-tuned minimum-sample thresholds.
* **Intervals** (Phase 3.3). Wilson for proportions because the samples are
  small and the normal approximation is wrong at the edges; bootstrap for means.

Pure functions over plain numbers: no DB, no engine, no FastAPI.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

#: Recency half-life for every weighted aggregate, in days (spec Phase 2).
#: Surfaced in the UI as a note, never as a setting.
RECENCY_HALF_LIFE_DAYS = 14.0

#: Below this support a metric renders greyed and is barred from the leak board.
DISPLAY_FLOOR_N = 8

#: Bootstrap resamples for mean intervals (spec Phase 3.3).
BOOTSTRAP_RESAMPLES = 1000

#: Target half-width used by the sample-size guidance, in proportion points.
TARGET_CI_HALF_WIDTH = 0.08


# ── Expectation (Phase 2) ─────────────────────────────────────────────────────


def expected_score(own_rating: int | None, opp_rating: int | None) -> float | None:
    """Elo expectancy for a single game, or ``None`` when either side is unrated."""

    if own_rating is None or opp_rating is None:
        return None
    return 1.0 / (1.0 + 10 ** ((opp_rating - own_rating) / 400.0))


def performance_gap(games: Sequence[Any]) -> dict[str, Any]:
    """Points above/below what the rating differences predicted.

    ``games`` are per-game fact rows carrying ``points``, ``user_rating`` and
    ``opponent_rating``. Games missing either rating are excluded from the gap
    but still counted, so the caller can say how much of the sample was rated.
    """

    rated = [
        g for g in games
        if g.get("points") is not None
        and g.get("user_rating") is not None
        and g.get("opponent_rating") is not None
    ]
    if not rated:
        return {"gap": None, "expected": None, "actual": None, "n": 0, "n_total": len(games)}

    actual = sum(float(g["points"]) for g in rated)
    expected = sum(
        expected_score(g["user_rating"], g["opponent_rating"]) or 0.0 for g in rated
    )
    return {
        "gap": actual - expected,
        "gap_per_game": (actual - expected) / len(rated),
        "expected": expected,
        "actual": actual,
        "expected_pct": expected / len(rated),
        "actual_pct": actual / len(rated),
        "n": len(rated),
        "n_total": len(games),
    }


def recency_weight(days_ago: float, half_life: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """Exponential decay so a 30-day window estimates *current* ability.

    Uses ``exp(-ln2 * t / H)`` rather than ``exp(-t / H)``. The latter is a time
    constant — it gives ``1/e ≈ 0.37`` at ``t = H``, not a half — and the UI
    tells the user the number is a half-life, so it had better halve.
    """

    if days_ago is None or days_ago < 0:
        return 1.0
    return math.exp(-math.log(2) * float(days_ago) / half_life)


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    total = sum(weights)
    if not values or total <= 0:
        return None
    return sum(v * w for v, w in zip(values, weights)) / total


# ── Intervals (Phase 3.3) ─────────────────────────────────────────────────────


def wilson_interval(successes: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a proportion.

    Chosen over the normal approximation because insight windows are small and
    rates sit near 0 or 1 often enough that the naive interval leaves ``[0, 1]``.
    Accepts fractional successes so it works on points (a draw is half a win).
    """

    if n <= 0:
        return None
    p = max(0.0, min(1.0, successes / n))
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def bootstrap_interval(
    values: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    alpha: float = 0.05,
    seed: int = 12345,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval for a mean.

    Seeded so a run recomputed from identical stored moves produces identical
    intervals — insight runs are immutable snapshots and must not jitter.
    """

    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    pool = list(values)
    means = []
    for _ in range(resamples):
        means.append(sum(pool[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[max(0, int((alpha / 2) * resamples) - 1)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (lo, hi)


def excludes_null(ci: tuple[float, float] | None, null_value: float) -> bool:
    """Whether an interval clears a null — the leak-eligibility test (Phase 3.3)."""

    if ci is None:
        return False
    return ci[0] > null_value or ci[1] < null_value


# ── Partial pooling (Phase 3.2) ───────────────────────────────────────────────


def pooling_constant(
    bucket_means: Sequence[float],
    bucket_ns: Sequence[int],
    within_variances: Sequence[float],
) -> float:
    """``k = sigma2_within / sigma2_between``, fit across a metric family.

    Large ``k`` means the buckets barely differ relative to their own noise, so
    everything shrinks hard toward the global mean. Falls back to a large ``k``
    when between-bucket variance is unmeasurable, which is the conservative
    direction: with no evidence that buckets differ, assume they don't.
    """

    usable = [(m, n, v) for m, n, v in zip(bucket_means, bucket_ns, within_variances) if n > 0]
    if len(usable) < 2:
        return 1e6
    means = [m for m, _, _ in usable]
    grand = sum(means) / len(means)
    sigma2_between = sum((m - grand) ** 2 for m in means) / (len(means) - 1)
    total_n = sum(n for _, n, _ in usable)
    sigma2_within = (
        sum(v * n for _, n, v in usable) / total_n if total_n else 0.0
    )
    if sigma2_between <= 1e-12:
        return 1e6
    return max(0.0, sigma2_within / sigma2_between)


def shrink(observed: float, n: int, global_mean: float, k: float) -> float:
    """Pull a bucket estimate toward the player's own global mean."""

    if n <= 0:
        return global_mean
    weight = n / (n + k)
    return weight * observed + (1 - weight) * global_mean


def shrink_buckets(
    buckets: Sequence[dict[str, Any]],
    *,
    value_key: str = "value",
    n_key: str = "n",
    variance_key: str = "variance",
    global_mean: float | None = None,
) -> dict[str, Any]:
    """Shrink a family of buckets together, returning the fitted ``k``.

    Each bucket gains ``shrunk`` alongside its raw value, so the UI can show the
    honest estimate while the raw number stays available for the tooltip.
    """

    usable = [b for b in buckets if int(b.get(n_key) or 0) > 0]
    if not usable:
        return {"k": None, "global_mean": global_mean, "buckets": list(buckets)}

    total_n = sum(int(b[n_key]) for b in usable)
    grand = (
        global_mean
        if global_mean is not None
        else sum(float(b[value_key]) * int(b[n_key]) for b in usable) / total_n
    )
    k = pooling_constant(
        [float(b[value_key]) for b in usable],
        [int(b[n_key]) for b in usable],
        [float(b.get(variance_key) or 0.0) for b in usable],
    )
    out = []
    for b in buckets:
        n = int(b.get(n_key) or 0)
        row = dict(b)
        row["shrunk"] = (
            shrink(float(b[value_key]), n, grand, k) if n > 0 else grand
        )
        row["below_floor"] = n < DISPLAY_FLOOR_N
        out.append(row)
    return {"k": k, "global_mean": grand, "buckets": out}


# ── Sample-size guidance (Phase 3.4) ──────────────────────────────────────────


def games_needed(
    n: int,
    ci: tuple[float, float] | None,
    *,
    target_half_width: float = TARGET_CI_HALF_WIDTH,
) -> int | None:
    """Roughly how many more games before this number means anything.

    Interval width shrinks as ``1/sqrt(n)``, so the required ``n`` scales with
    the square of the ratio. Returns ``None`` once the interval is already tight
    enough, or when there is nothing to extrapolate from.
    """

    if not n or ci is None:
        return None
    half_width = (ci[1] - ci[0]) / 2.0
    if half_width <= target_half_width:
        return None
    needed = n * (half_width / target_half_width) ** 2
    return max(1, int(round(needed - n)))


# ── The result object every metric returns (Phase 1.1) ────────────────────────


@dataclass
class MetricResult:
    """A number plus everything needed to defend it.

    ``exemplars``/``counter_exemplars`` are filled by ``insights_evidence``;
    ``query`` is a serialized predicate that regenerates the full support set,
    so hundreds of plies never have to be stored per metric.
    """

    value: float | None
    n: int = 0
    ci: tuple[float, float] | None = None
    exemplars: list[Any] = field(default_factory=list)
    counter_exemplars: list[Any] = field(default_factory=list)
    query: dict[str, Any] = field(default_factory=dict)
    shrunk: float | None = None
    expectation: dict[str, Any] | None = None
    percentile: dict[str, Any] | None = None

    @property
    def below_floor(self) -> bool:
        return self.n < DISPLAY_FLOOR_N

    @property
    def leak_eligible(self) -> bool:
        """A metric may drive the leak board only with enough support."""

        return self.n >= DISPLAY_FLOOR_N

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "n": self.n,
            "ci": list(self.ci) if self.ci else None,
            "shrunk": self.shrunk,
            "below_floor": self.below_floor,
            "query": self.query,
            "expectation": self.expectation,
            "percentile": self.percentile,
            "exemplars": [
                e.to_dict() if hasattr(e, "to_dict") else e for e in self.exemplars
            ],
            "counter_exemplars": [
                e.to_dict() if hasattr(e, "to_dict") else e
                for e in self.counter_exemplars
            ],
            "sample_guidance": games_needed(self.n, self.ci),
        }


def proportion_result(
    successes: float,
    n: int,
    *,
    query: dict[str, Any] | None = None,
    expectation: dict[str, Any] | None = None,
) -> MetricResult:
    """A rate with its Wilson interval attached."""

    return MetricResult(
        value=(successes / n) if n else None,
        n=n,
        ci=wilson_interval(successes, n),
        query=query or {},
        expectation=expectation,
    )


def mean_result(
    values: Sequence[float],
    *,
    query: dict[str, Any] | None = None,
    seed: int = 12345,
) -> MetricResult:
    """A mean with its bootstrap interval attached."""

    values = list(values)
    return MetricResult(
        value=(sum(values) / len(values)) if values else None,
        n=len(values),
        ci=bootstrap_interval(values, seed=seed),
        query=query or {},
    )


# ── Significance testing (Phase 11.5) ─────────────────────────────────────────


def binomial_tail_p(successes: int, n: int, p0: float) -> float | None:
    """Two-sided binomial test against a baseline rate.

    Used to say "your post-loss drop is not distinguishable from chance" when
    that is what the data shows — reported either way, which is the point.
    """

    if n <= 0 or not (0.0 < p0 < 1.0):
        return None

    def pmf(k: int) -> float:
        return math.comb(n, k) * (p0 ** k) * ((1 - p0) ** (n - k))

    observed = pmf(successes)
    # Sum every outcome at least as unlikely as the one observed.
    total = sum(pmf(k) for k in range(n + 1) if pmf(k) <= observed + 1e-12)
    return min(1.0, total)


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Plain Pearson r — the metacognition score is a correlation (Phase 11.2)."""

    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 1e-12 or dy <= 1e-12:
        return None
    return num / (dx * dy)


def bimodality_coefficient(values: Sequence[float]) -> float | None:
    """Sarle's bimodality coefficient — >0.555 suggests two modes (Phase 11.6).

    A bimodal move-time distribution is the fingerprint of an impulsive-plus-
    deliberate mix, which is exactly the fast/slow blunder split.
    """

    n = len(values)
    if n < 4:
        return None
    mean = sum(values) / n
    m2 = sum((v - mean) ** 2 for v in values) / n
    if m2 <= 1e-12:
        return None
    m3 = sum((v - mean) ** 3 for v in values) / n
    m4 = sum((v - mean) ** 4 for v in values) / n
    skew = m3 / (m2 ** 1.5)
    kurt = m4 / (m2 ** 2) - 3.0
    denom = kurt + 3.0 * ((n - 1) ** 2) / ((n - 2) * (n - 3))
    if abs(denom) <= 1e-12:
        return None
    return (skew ** 2 + 1.0) / denom
