"""Phase 4 — reference corpus lookup, percentiles and the rating-implied profile.

"Your endgame Δw/move is 0.9" means nothing. "…the median 1500 is 0.62, you're
in the 18th percentile" is a diagnosis. This module turns any stored metric into
a percentile against the user's own rating band and time control, and then into
a **rating equivalent** by asking which band's median matches their value.

The corpus itself is a static, versioned artefact built offline by
``scripts/build_reference_corpus.py`` — never recomputed per run. Everything
here degrades to ``None`` when no corpus is loaded, so the report works without
one and simply omits the percentile badges.
"""

from __future__ import annotations

from typing import Any, Sequence

#: Rating bands are 100 points wide across the range the corpus covers.
BAND_WIDTH = 100
BAND_MIN = 800
BAND_MAX = 2400

#: Metrics where a *lower* value is better, so the percentile must be inverted.
LOWER_IS_BETTER = frozenset({
    "move_quality.middlegame.delta_w_per_move",
    "move_quality.opening.delta_w_per_move",
    "move_quality.endgame.delta_w_per_move",
    "headline.error_rates.blunders_per_100",
    "headline.error_rates.mistakes_per_100",
    "critical_moments.criticality_gap",
    "endgame.delta_w_per_move",
    "measures.impulsivity.impulse_rate",
    "headline.loss.total_per_game",
})


def rating_band(rating: int | None) -> int | None:
    """Lower bound of the 100-point band a rating falls in."""

    if rating is None:
        return None
    clamped = max(BAND_MIN, min(BAND_MAX - BAND_WIDTH, int(rating)))
    return (clamped // BAND_WIDTH) * BAND_WIDTH


def load_reference(
    connection: Any, *, time_class: str, version: str = "v1"
) -> dict[tuple[str, int], dict[str, Any]]:
    """Load the whole corpus for one time control, keyed by (metric, band)."""

    try:
        rows = connection.execute(
            "SELECT metric_key, rating_band, n_games, p10, p25, p50, p75, p90, mean, sd "
            "FROM reference_distribution WHERE time_class = ? AND version = ?",
            (time_class, version),
        ).fetchall()
    except Exception:  # noqa: BLE001 — table absent on an un-migrated DB
        return {}
    return {(r["metric_key"], int(r["rating_band"])): dict(r) for r in rows}


def percentile_of(value: float, row: dict[str, Any], *, lower_is_better: bool) -> float:
    """Interpolate a percentile from the five stored quantiles.

    Linear between breakpoints and clamped at the tails — the corpus stores
    quantiles rather than the full distribution, and pretending to more
    resolution than that would be false precision.
    """

    points = [(row["p10"], 0.10), (row["p25"], 0.25), (row["p50"], 0.50),
              (row["p75"], 0.75), (row["p90"], 0.90)]
    points = [(v, p) for v, p in points if v is not None]
    if not points:
        return 0.5
    points.sort()

    if value <= points[0][0]:
        pct = points[0][1]
    elif value >= points[-1][0]:
        pct = points[-1][1]
    else:
        pct = points[-1][1]
        for (v0, p0), (v1, p1) in zip(points, points[1:]):
            if v0 <= value <= v1:
                span = (v1 - v0) or 1e-9
                pct = p0 + (p1 - p0) * (value - v0) / span
                break
    return 1.0 - pct if lower_is_better else pct


def describe(
    metric_key: str,
    value: float | None,
    *,
    reference: dict[tuple[str, int], dict[str, Any]],
    band: int | None,
) -> dict[str, Any] | None:
    """Percentile badge for one metric, or ``None`` without corpus coverage."""

    if value is None or band is None:
        return None
    row = reference.get((metric_key, band))
    if row is None:
        return None
    lower_is_better = metric_key in LOWER_IS_BETTER
    pct = percentile_of(float(value), row, lower_is_better=lower_is_better)
    return {
        "percentile": pct,
        "band": band,
        "median": row["p50"],
        "n_games": row["n_games"],
        "lower_is_better": lower_is_better,
        "verdict": (
            "elite" if pct >= 0.9 else "strong" if pct >= 0.7
            else "typical" if pct >= 0.3 else "weak"
        ),
    }


def rating_equivalent(
    metric_key: str,
    value: float | None,
    *,
    reference: dict[tuple[str, int], dict[str, Any]],
    time_class: str,
) -> int | None:
    """Which band's median matches this value — the metric as a rating.

    Interpolates between adjacent band medians so the answer isn't quantized to
    100-point steps, and returns ``None`` when the value sits outside the
    corpus's range rather than extrapolating past the data.
    """

    medians = sorted(
        (band, row["p50"])
        for (key, band), row in reference.items()
        if key == metric_key and row.get("p50") is not None
    )
    if len(medians) < 2 or value is None:
        return None

    lower_is_better = metric_key in LOWER_IS_BETTER
    # Order bands so the median sequence is increasing in "goodness".
    series = [(band, med) for band, med in medians]
    if lower_is_better:
        series = [(band, -med) for band, med in series]
        target = -float(value)
    else:
        target = float(value)

    if target <= series[0][1] or target >= series[-1][1]:
        return series[0][0] if target <= series[0][1] else series[-1][0] + BAND_WIDTH

    for (b0, m0), (b1, m1) in zip(series, series[1:]):
        if m0 <= target <= m1:
            span = (m1 - m0) or 1e-9
            return int(round(b0 + (b1 - b0) * (target - m0) / span))
    return None


#: The vector shown on the rating-implied profile (spec 4.3).
PROFILE_METRICS = (
    ("tactics", "measures.punish.punish_rate"),
    ("endgame", "endgame.delta_w_per_move"),
    ("opening", "move_quality.opening.delta_w_per_move"),
    ("middlegame", "move_quality.middlegame.delta_w_per_move"),
    ("critical moments", "critical_moments.criticality_gap"),
    ("blunder control", "headline.error_rates.blunders_per_100"),
)


def rating_implied_profile(
    values: dict[str, float | None],
    *,
    reference: dict[tuple[str, int], dict[str, Any]],
    time_class: str,
    actual_rating: int | None,
) -> dict[str, Any]:
    """"You play tactics like a 1780 and endgames like a 1390."

    The screenshot-worthy output: each skill converted to the rating whose
    median matches it, drawn against the user's real rating.
    """

    rows = []
    for label, metric_key in PROFILE_METRICS:
        value = values.get(metric_key)
        equivalent = rating_equivalent(
            metric_key, value, reference=reference, time_class=time_class
        )
        if equivalent is None:
            continue
        rows.append({
            "skill": label,
            "metric_key": metric_key,
            "value": value,
            "rating_equivalent": equivalent,
            "delta_vs_actual": (
                equivalent - actual_rating if actual_rating is not None else None
            ),
        })
    rows.sort(key=lambda r: r["rating_equivalent"], reverse=True)

    note = None
    if len(rows) >= 2 and actual_rating is not None:
        best, worst = rows[0], rows[-1]
        if best["rating_equivalent"] - worst["rating_equivalent"] >= 150:
            note = (
                f"You play {best['skill']} like a {best['rating_equivalent']} and "
                f"{worst['skill']} like a {worst['rating_equivalent']}. Your rating is "
                f"{actual_rating} — {worst['skill']} is what's holding it down."
            )

    return {
        "rows": rows,
        "actual_rating": actual_rating,
        "note": note,
        "available": bool(rows),
    }


def attach_percentiles(
    metrics: dict[str, Any],
    *,
    connection: Any,
    time_class: str,
    user_rating: int | None,
    version: str = "v1",
) -> dict[str, Any]:
    """Add percentile badges and the rating-implied profile to a metrics payload.

    Returns a summary describing coverage; the payload is mutated in place under
    ``pro.reference``. A missing corpus is not an error — every field simply
    reports as unavailable.
    """

    reference = load_reference(connection, time_class=time_class, version=version)
    band = rating_band(user_rating)
    pro = metrics.setdefault("pro", {})

    values = _profile_values(metrics)
    badges = {}
    for metric_key, value in values.items():
        badge = describe(metric_key, value, reference=reference, band=band)
        if badge:
            badges[metric_key] = badge

    profile = rating_implied_profile(
        values, reference=reference, time_class=time_class, actual_rating=user_rating
    )
    pro["reference"] = {
        "version": version if reference else None,
        "available": bool(reference),
        "band": band,
        "time_class": time_class,
        "percentiles": badges,
        "profile": profile,
        "note": (
            None if reference
            else "No reference corpus loaded — run scripts/build_reference_corpus.py "
                 "to enable percentiles and the rating-implied profile."
        ),
    }
    return pro["reference"]


def _profile_values(metrics: dict[str, Any]) -> dict[str, float | None]:
    """Pull the metrics the corpus knows about out of a payload, by dotted key."""

    pro = metrics.get("pro") or {}
    out: dict[str, float | None] = {}

    for row in (pro.get("move_quality") or {}).get("by_phase") or []:
        out[f"move_quality.{row['phase']}.delta_w_per_move"] = row.get("delta_w_per_move")

    rates = (pro.get("headline") or {}).get("error_rates") or {}
    out["headline.error_rates.blunders_per_100"] = rates.get("blunders_per_100")
    out["headline.error_rates.mistakes_per_100"] = rates.get("mistakes_per_100")

    loss = (pro.get("headline") or {}).get("loss") or {}
    out["headline.loss.total_per_game"] = loss.get("total_per_game")

    crit = pro.get("critical_moments") or {}
    out["critical_moments.criticality_gap"] = crit.get("criticality_gap")

    out["endgame.delta_w_per_move"] = (pro.get("endgame") or {}).get("delta_w_per_move")

    measures = pro.get("measures") or {}
    out["measures.punish.punish_rate"] = (measures.get("punish") or {}).get("punish_rate")
    out["measures.impulsivity.impulse_rate"] = (
        measures.get("impulsivity") or {}
    ).get("impulse_rate")
    return out


def corpus_metric_keys() -> Sequence[str]:
    """Metric keys the offline builder must populate for full coverage."""

    return tuple(
        sorted(
            {
                "move_quality.opening.delta_w_per_move",
                "move_quality.middlegame.delta_w_per_move",
                "move_quality.endgame.delta_w_per_move",
                "headline.error_rates.blunders_per_100",
                "headline.error_rates.mistakes_per_100",
                "headline.loss.total_per_game",
                "critical_moments.criticality_gap",
                "endgame.delta_w_per_move",
                "measures.punish.punish_rate",
                "measures.impulsivity.impulse_rate",
            }
        )
    )
