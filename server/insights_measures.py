"""Phases 5 and 11 — new measurements over data already stored.

Phase 5 is the cheap, high-value set the product was missing:

* **Punish rate** — the largest blind spot. The report measured the user's
  errors exhaustively and never asked whether they capitalize on the opponent's,
  which is roughly half of rating. The rows were always there; only
  ``is_user_move = 1`` was ever queried.
* **Offensive vs defensive blindness** — tactics you failed to *play* versus
  tactics you failed to *see coming*. Different skills, different fixes.
* **Fast vs slow blunders** — same Δw, opposite diagnosis: impulse versus a
  flawed evaluation.
* **Trade quality** — Δw restricted to captures.

Phase 11 is the behavioural layer: impulsivity, metacognition, risk conditioned
on game state, stubbornness, tilt significance, and the shape of the move-time
distribution.

Everything here consumes the enriched rows from
:func:`server.insights_evidence.enrich_moves`, so the database is read once.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean, median
from typing import Any, Sequence

from server.insights_evidence import support_query
from server.insights_pro import (
    BLUNDER_DELTA_W,
    CRITICAL_HANDLED_DELTA_W,
    INACCURACY_DELTA_W,
    MISTAKE_DELTA_W,
    SCRAMBLE_CLOCK_SECONDS,
)
from server.insights_stats import (
    bimodality_coefficient,
    binomial_tail_p,
    mean_result,
    pearson_correlation,
    proportion_result,
)

#: A move played under this many seconds is an impulse, not a decision.
IMPULSE_SECONDS = 3.0

#: Fast/slow blunder split, as multiples of the game's own median move time.
FAST_MULTIPLE = 0.5
SLOW_MULTIPLE = 1.5

#: Winning/losing thresholds for the risk-conditioning card.
WINNING_WIN_PROB = 0.70
LOSING_WIN_PROB = 0.30

#: How many of the user's own subsequent moves count as "persisting".
STUBBORNNESS_WINDOW = 3


def _by_review(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["review_id"])].append(row)
    for moves in grouped.values():
        moves.sort(key=lambda r: r["ply"])
    return grouped


def _rate(hits: float, total: float) -> float | None:
    return (hits / total) if total else None


# ── 5.1 Opponent error harvesting ─────────────────────────────────────────────


def compute_punish_rate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Did the user capitalize when the opponent went wrong?

    An opponent error is a non-user move losing more than ``MISTAKE_DELTA_W``.
    It counts as punished when the user's very next move both stays clean
    (``<= CRITICAL_HANDLED_DELTA_W``) and gives back less than half of what was
    handed over — "captured at least half the swing", expressed in the same Δw
    units the rest of the report uses.
    """

    opportunities: list[dict[str, Any]] = []
    for moves in _by_review(rows).values():
        for idx, move in enumerate(moves):
            if move["is_user_move"]:
                continue
            swing = float(move["delta_w"] or 0.0)
            if swing <= MISTAKE_DELTA_W:
                continue
            reply = moves[idx + 1] if idx + 1 < len(moves) else None
            if reply is None or not reply["is_user_move"]:
                continue
            given_back = float(reply["delta_w"] or 0.0)
            punished = (
                given_back <= CRITICAL_HANDLED_DELTA_W
                and given_back <= 0.5 * swing
            )
            opportunities.append({
                **reply,
                "swing": swing,
                "given_back": given_back,
                "punished": punished,
                "opponent_error_ply": move["ply"],
                "opponent_error_tactical": bool(move.get("tactic_tags")),
                "phase": move["phase"],
            })

    total = len(opportunities)
    punished = sum(1 for o in opportunities if o["punished"])
    result = proportion_result(
        punished,
        total,
        query=support_query(is_user_move=False, min_delta_w=MISTAKE_DELTA_W),
    )

    by_phase = {}
    for phase in ("opening", "middlegame", "endgame"):
        subset = [o for o in opportunities if o["phase"] == phase]
        if subset:
            by_phase[phase] = {
                "n": len(subset),
                "punish_rate": _rate(sum(1 for o in subset if o["punished"]), len(subset)),
            }

    tactical = [o for o in opportunities if o["opponent_error_tactical"]]
    positional = [o for o in opportunities if not o["opponent_error_tactical"]]

    return {
        "opportunities": total,
        "punished": punished,
        "punish_rate": result.value,
        "ci": list(result.ci) if result.ci else None,
        "swing_available": sum(o["swing"] for o in opportunities),
        "swing_kept": sum(o["swing"] - o["given_back"] for o in opportunities if o["punished"]),
        "by_phase": by_phase,
        "by_error_type": {
            "tactical": {
                "n": len(tactical),
                "punish_rate": _rate(sum(1 for o in tactical if o["punished"]), len(tactical)),
            },
            "positional": {
                "n": len(positional),
                "punish_rate": _rate(sum(1 for o in positional if o["punished"]), len(positional)),
            },
        },
        # Missed punishments are the evidence set worth showing.
        "missed": [o for o in opportunities if not o["punished"]],
        "taken": [o for o in opportunities if o["punished"]],
        "query": result.query,
    }


# ── 5.2 Offensive vs defensive blindness ──────────────────────────────────────


def _is_forcing(san: Any) -> bool:
    """Capture, check or promotion — the shape of a concrete tactical resource."""

    text = str(san or "")
    return "x" in text or "+" in text or "#" in text or "=" in text


def compute_blindness_split(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Was the refutation the opponent's resource, or the user's forgone chance?

    Comparing win-probability swings cannot separate these: by construction the
    opponent gains almost exactly what the user lost, so every error classifies
    as defensive. The discriminator has to be the *shape* of the moves.

    * The opponent answers with a forcing move they then convert — a concrete
      resource existed and the user did not see it coming: **defensive**.
    * The move the user declined was itself forcing, and the opponent's reply was
      quiet — the user simply failed to take their own chance: **offensive**.
    * Neither: a quiet positional slip, counted separately rather than forced
      into one of the two buckets.
    """

    defensive: list[dict[str, Any]] = []
    offensive: list[dict[str, Any]] = []
    positional: list[dict[str, Any]] = []

    for moves in _by_review(rows).values():
        for idx, move in enumerate(moves):
            if not move["is_user_move"]:
                continue
            if float(move["delta_w"] or 0.0) < MISTAKE_DELTA_W:
                continue
            reply = moves[idx + 1] if idx + 1 < len(moves) else None
            punished = (
                reply is not None
                and not reply["is_user_move"]
                and _is_forcing(reply.get("san"))
                and float(reply["delta_w"] or 0.0) <= CRITICAL_HANDLED_DELTA_W
            )
            forgone_tactic = _is_forcing(move.get("san_best"))

            if punished:
                defensive.append(move)
            elif forgone_tactic:
                offensive.append(move)
            else:
                positional.append(move)

    total = len(defensive) + len(offensive)
    note = None
    if total >= 10:
        share = len(defensive) / total
        if share >= 0.65:
            note = (
                "You mostly get punished for what you didn't see coming — "
                "train defensive alertness and opponent threats."
            )
        elif share <= 0.35:
            note = (
                "You mostly lose value by not taking your own chances — "
                "train tactical opportunity spotting."
            )

    return {
        "defensive": len(defensive),
        "offensive": len(offensive),
        "positional": len(positional),
        "total": total,
        "defensive_share": _rate(len(defensive), total),
        "defensive_delta_w": sum(float(m["delta_w"] or 0) for m in defensive),
        "offensive_delta_w": sum(float(m["delta_w"] or 0) for m in offensive),
        "note": note,
        "defensive_moves": defensive,
        "offensive_moves": offensive,
    }


# ── 5.3 Fast vs slow blunders ─────────────────────────────────────────────────


def compute_blunder_tempo(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Impulse or misjudgement? Split blunders against each game's own tempo.

    Measured relative to the game's median move time rather than an absolute
    threshold, because a blitz "long think" and a rapid one are different
    numbers describing the same behaviour.
    """

    fast: list[dict[str, Any]] = []
    slow: list[dict[str, Any]] = []
    typical: list[dict[str, Any]] = []

    for moves in _by_review(rows).values():
        times = [
            float(m["time_spent"])
            for m in moves
            if m["is_user_move"] and m["time_spent"] is not None
        ]
        if not times:
            continue
        game_median = median(times)
        if game_median <= 0:
            continue
        for move in moves:
            if not move["is_user_move"] or move["time_spent"] is None:
                continue
            if float(move["delta_w"] or 0.0) < BLUNDER_DELTA_W:
                continue
            ratio = float(move["time_spent"]) / game_median
            row = {**move, "time_ratio": ratio, "game_median_time": game_median}
            if ratio < FAST_MULTIPLE:
                fast.append(row)
            elif ratio > SLOW_MULTIPLE:
                slow.append(row)
            else:
                typical.append(row)

    total = len(fast) + len(slow) + len(typical)
    verdict = None
    if total >= 6:
        if len(fast) >= 2 * max(1, len(slow)):
            verdict = "impulse"
        elif len(slow) >= 2 * max(1, len(fast)):
            verdict = "evaluation"
        else:
            verdict = "mixed"

    return {
        "fast": len(fast),
        "slow": len(slow),
        "typical": len(typical),
        "total": total,
        "fast_share": _rate(len(fast), total),
        "verdict": verdict,
        "prescription": {
            "impulse": "You blunder before you look. A forced pre-move checklist beats more tactics.",
            "evaluation": "You look and judge wrong. Calculation and candidate-move work is the fix.",
            "mixed": "Both impulse and misjudgement contribute; the fast half is the cheaper one to fix.",
        }.get(verdict or ""),
        "fast_moves": fast,
        "slow_moves": slow,
        "query_fast": support_query(is_user_move=True, min_delta_w=BLUNDER_DELTA_W),
    }


# ── 5.4 Trade quality ─────────────────────────────────────────────────────────


def _is_capture(row: dict[str, Any]) -> bool:
    return "x" in str(row.get("san") or "")


def compute_trade_quality(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Δw restricted to captures — bad trading is common and rarely diagnosed."""

    captures = [
        r for r in rows if r["is_user_move"] and not r["is_book"] and _is_capture(r)
    ]
    others = [
        r for r in rows if r["is_user_move"] and not r["is_book"] and not _is_capture(r)
    ]

    cap_result = mean_result(
        [float(r["delta_w"] or 0.0) for r in captures],
        query=support_query(is_user_move=True),
    )
    other_mean = fmean([float(r["delta_w"] or 0.0) for r in others]) if others else None

    gap = (
        cap_result.value - other_mean
        if cap_result.value is not None and other_mean is not None
        else None
    )
    return {
        "captures": len(captures),
        "capture_delta_w_per_move": cap_result.value,
        "ci": list(cap_result.ci) if cap_result.ci else None,
        "other_delta_w_per_move": other_mean,
        "gap": gap,
        "note": (
            "Your exchanges leak more than your other moves — trading decisions "
            "are a distinct weakness."
            if gap is not None and gap > 1.0
            else None
        ),
        "worst": sorted(captures, key=lambda r: -float(r["delta_w"] or 0))[:20],
    }


# ── 11.1 Impulsivity ──────────────────────────────────────────────────────────


def compute_impulsivity(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Moves played on reflex when there was no clock pressure to excuse it.

    Scramble moves are excluded deliberately: playing fast with 6 seconds left
    is forced, and lumping the two together hides the leak that is actually fixable.
    """

    unhurried = [
        r for r in rows
        if r["is_user_move"]
        and not r["is_book"]
        and r["time_spent"] is not None
        and (r["clock_remaining"] is None or float(r["clock_remaining"]) >= SCRAMBLE_CLOCK_SECONDS)
    ]
    if not unhurried:
        return {"n": 0, "impulse_rate": None}

    impulsive = [r for r in unhurried if float(r["time_spent"]) < IMPULSE_SECONDS]
    deliberate = [r for r in unhurried if float(r["time_spent"]) >= IMPULSE_SECONDS]

    def blunder_rate(subset: Sequence[dict[str, Any]]) -> float | None:
        return _rate(
            sum(1 for r in subset if float(r["delta_w"] or 0) >= BLUNDER_DELTA_W),
            len(subset),
        )

    # Matching on a min/max volatility band is no control at all — the band ends
    # up spanning nearly every move. Compare *within volatility deciles* and
    # average, so quick recaptures are never weighed against hard middlegames.
    scored = [r for r in unhurried if r["volatility"] is not None]
    scored.sort(key=lambda r: float(r["volatility"]))
    decile_size = max(1, len(scored) // 10)
    deciles = [scored[i:i + decile_size] for i in range(0, len(scored), decile_size)]

    fast_rates: list[float] = []
    slow_rates: list[float] = []
    matched_n = 0
    for decile in deciles:
        quick = [r for r in decile if float(r["time_spent"]) < IMPULSE_SECONDS]
        slow = [r for r in decile if float(r["time_spent"]) >= IMPULSE_SECONDS]
        if len(quick) < 3 or len(slow) < 3:
            continue
        fast_rates.append(blunder_rate(quick) or 0.0)
        slow_rates.append(blunder_rate(slow) or 0.0)
        matched_n += len(quick) + len(slow)

    result = proportion_result(
        len(impulsive),
        len(unhurried),
        query=support_query(is_user_move=True, min_clock=SCRAMBLE_CLOCK_SECONDS, max_time_spent=IMPULSE_SECONDS),
    )
    return {
        "n": len(unhurried),
        "impulsive": len(impulsive),
        "impulse_rate": result.value,
        "ci": list(result.ci) if result.ci else None,
        "blunder_rate_impulsive": fmean(fast_rates) if fast_rates else blunder_rate(impulsive),
        "blunder_rate_deliberate": fmean(slow_rates) if slow_rates else blunder_rate(deliberate),
        "matched_on_volatility": bool(fast_rates),
        "matched_deciles": len(fast_rates),
        "matched_n": matched_n,
        "threshold_seconds": IMPULSE_SECONDS,
        "moves": impulsive,
        "query": result.query,
    }


# ── 11.2 Metacognition ────────────────────────────────────────────────────────


def compute_metacognition(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Do you know which positions are hard?

    The continuous version of the inverted-time-budget leak: correlation between
    time spent and actual difficulty. A real, trainable skill.
    """

    pairs = [
        (float(r["volatility"]), float(r["time_spent"]))
        for r in rows
        if r["is_user_move"]
        and not r["is_book"]
        and r["volatility"] is not None
        and r["time_spent"] is not None
    ]
    find_pairs = [
        (100.0 - float(r["findability"]), float(r["time_spent"]))
        for r in rows
        if r["is_user_move"]
        and r["findability"] is not None
        and r["time_spent"] is not None
    ]

    r_vol = pearson_correlation([p[0] for p in pairs], [p[1] for p in pairs])
    r_find = pearson_correlation([p[0] for p in find_pairs], [p[1] for p in find_pairs])

    label = None
    if r_vol is not None:
        if r_vol >= 0.25:
            label = "Good — you spend your time where the position demands it"
        elif r_vol >= 0.05:
            label = "Weak — your clock only loosely tracks difficulty"
        else:
            label = "Poor — your time allocation is unrelated to how hard the position is"

    # Sub-sampled scatter: enough to see the cloud, small enough to ship.
    step = max(1, len(pairs) // 400)
    return {
        "n": len(pairs),
        "r_volatility": r_vol,
        "r_findability": r_find,
        "n_findability": len(find_pairs),
        "label": label,
        "scatter": [
            {"volatility": round(v, 1), "time_spent": round(t, 1)}
            for v, t in pairs[::step]
        ],
    }


# ── 11.3 Risk conditioned on game state ───────────────────────────────────────


def compute_state_conditioned_risk(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Volatility steering is unconditional today. Condition it on the score.

    Sharpening when losing is correct; sharpening when winning is usually fatal.
    Going passive when winning is the mechanism behind the "converted then lost"
    taxonomy label, which currently only names the symptom.
    """

    states = {
        "winning": {"label": "Winning (>70%)", "vol": [], "loss": [], "time": []},
        "level": {"label": "Level", "vol": [], "loss": [], "time": []},
        "losing": {"label": "Losing (<30%)", "vol": [], "loss": [], "time": []},
    }
    for row in rows:
        if not row["is_user_move"] or row["win_prob"] is None:
            continue
        wp = float(row["win_prob"])
        key = (
            "winning" if wp > WINNING_WIN_PROB
            else "losing" if wp < LOSING_WIN_PROB
            else "level"
        )
        bucket = states[key]
        if row["volatility"] is not None:
            bucket["vol"].append(float(row["volatility"]))
        bucket["loss"].append(float(row["delta_w"] or 0.0))
        if row["time_spent"] is not None:
            bucket["time"].append(float(row["time_spent"]))

    out = {}
    for key, bucket in states.items():
        out[key] = {
            "label": bucket["label"],
            "moves": len(bucket["loss"]),
            "mean_volatility": fmean(bucket["vol"]) if bucket["vol"] else None,
            "delta_w_per_move": fmean(bucket["loss"]) if bucket["loss"] else None,
            "mean_time": fmean(bucket["time"]) if bucket["time"] else None,
        }

    notes = []
    win, lose = out["winning"], out["losing"]
    if win["mean_volatility"] is not None and lose["mean_volatility"] is not None:
        if win["mean_volatility"] > lose["mean_volatility"] + 5:
            notes.append(
                "You steer into sharper positions when you are already winning — "
                "the wrong direction, and the mechanism behind converted-then-lost."
            )
        elif lose["mean_volatility"] > win["mean_volatility"] + 5:
            notes.append("You sharpen when losing and simplify when winning — correct instincts.")
    if win["mean_time"] is not None and lose["mean_time"] is not None:
        if win["mean_time"] < lose["mean_time"] * 0.7:
            notes.append("You speed up when winning, which is where conversion slips away.")
    if (
        win["delta_w_per_move"] is not None
        and out["level"]["delta_w_per_move"] is not None
        and win["delta_w_per_move"] > out["level"]["delta_w_per_move"] * 1.4
    ):
        notes.append("Your accuracy drops specifically in winning positions — passivity, not difficulty.")

    return {"states": out, "notes": notes}


# ── 11.4 Stubbornness ─────────────────────────────────────────────────────────


def _piece_of(san: Any) -> str:
    text = str(san or "")
    if not text:
        return "?"
    if text.startswith("O-O"):
        return "K"
    return text[0] if text[0].isupper() else "P"


def compute_stubbornness(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Sunk-cost behaviour at the board.

    After a plan is refuted, does the user keep pushing the same piece? Detected
    as the same piece type moving again within the next few of their own moves,
    and the extra Δw that follows.
    """

    episodes: list[dict[str, Any]] = []
    for moves in _by_review(rows).values():
        user_moves = [m for m in moves if m["is_user_move"]]
        for idx, move in enumerate(user_moves):
            if float(move["delta_w"] or 0.0) < MISTAKE_DELTA_W:
                continue
            piece = _piece_of(move["san"])
            if piece in ("?", "P"):
                continue  # pawn pushes and unknowns are too common to mean anything
            following = user_moves[idx + 1 : idx + 1 + STUBBORNNESS_WINDOW]
            # Persisting only counts when it keeps costing: moving the same piece
            # again is ordinary chess, moving it again and losing more is not.
            persisted = [
                m for m in following
                if _piece_of(m["san"]) == piece
                and float(m["delta_w"] or 0.0) >= INACCURACY_DELTA_W
            ]
            if not persisted:
                continue
            episodes.append({
                **move,
                "piece": piece,
                "persisted_moves": len(persisted),
                "extra_delta_w": sum(float(m["delta_w"] or 0.0) for m in persisted),
            })

    refuted = sum(
        1
        for moves in _by_review(rows).values()
        for m in moves
        if m["is_user_move"]
        and float(m["delta_w"] or 0.0) >= MISTAKE_DELTA_W
        and _piece_of(m["san"]) not in ("?", "P")
    )
    result = proportion_result(len(episodes), refuted)
    return {
        "episodes": len(episodes),
        "refuted_plans": refuted,
        "persistence_rate": result.value,
        "ci": list(result.ci) if result.ci else None,
        "extra_delta_w": sum(e["extra_delta_w"] for e in episodes),
        "window": STUBBORNNESS_WINDOW,
        "note": (
            "After a plan is refuted you tend to keep pushing the same piece — "
            "sunk-cost behaviour that compounds the original error."
            if result.value is not None and result.value > 0.4 and refuted >= 8
            else None
        ),
        "moves": episodes,
    }


# ── 11.5 Tilt significance ────────────────────────────────────────────────────


def compute_tilt_significance(
    after_loss: dict[str, Any], overall_win_rate: float | None
) -> dict[str, Any]:
    """Put the existing tilt number through a proper test.

    Saying "your post-loss drop is not distinguishable from chance" builds more
    trust than confirming every folk belief, so it is reported either way.
    """

    n = int(after_loss.get("n") or 0)
    wins = int(after_loss.get("wins") or 0)
    if n < 5 or overall_win_rate is None or not (0.0 < overall_win_rate < 1.0):
        return {"n": n, "p_value": None, "significant": None, "verdict": "Not enough post-loss games to test."}

    p = binomial_tail_p(wins, n, overall_win_rate)
    significant = p is not None and p < 0.05
    observed = wins / n
    if significant:
        direction = "below" if observed < overall_win_rate else "above"
        verdict = (
            f"Your post-loss win rate is significantly {direction} baseline "
            f"(p = {p:.3f}, n = {n})."
        )
    else:
        verdict = (
            f"Your post-loss drop is not statistically distinguishable from chance "
            f"(p = {p:.2f}, n = {n})."
        )
    return {
        "n": n,
        "wins": wins,
        "observed": observed,
        "baseline": overall_win_rate,
        "p_value": p,
        "significant": significant,
        "verdict": verdict,
    }


# ── 11.6 Move-time distribution shape ─────────────────────────────────────────


def compute_move_time_shape(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The fingerprint behind the fast/slow blunder split.

    A bimodal distribution means an impulsive-plus-deliberate mix rather than a
    consistent tempo — which is exactly what 5.3 then splits blunders along.
    """

    times = [
        float(r["time_spent"])
        for r in rows
        if r["is_user_move"] and r["time_spent"] is not None
    ]
    if len(times) < 20:
        return {"n": len(times), "histogram": [], "bimodality": None}

    edges = [0, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 90, 10_000]
    labels = ["<1s", "1–2s", "2–3s", "3–5s", "5–8s", "8–12s", "12–20s", "20–30s",
              "30–45s", "45–60s", "60–90s", ">90s"]
    counts = [0] * (len(edges) - 1)
    for t in times:
        for i in range(len(edges) - 1):
            if edges[i] <= t < edges[i + 1]:
                counts[i] += 1
                break

    bc = bimodality_coefficient(times)
    return {
        "n": len(times),
        "median": median(times),
        "mean": fmean(times),
        "histogram": [
            {"label": label, "n": count, "share": count / len(times)}
            for label, count in zip(labels, counts)
        ],
        "bimodality": bc,
        "shape": (
            "bimodal" if bc is not None and bc > 0.555
            else "unimodal" if bc is not None
            else None
        ),
        "note": (
            "Your move times are bimodal — you either blitz or think hard, with "
            "little in between. The fast half is where the cheap blunders live."
            if bc is not None and bc > 0.555
            else None
        ),
    }


# ── Entry point ───────────────────────────────────────────────────────────────


def compute_measures(
    rows: Sequence[dict[str, Any]],
    *,
    after_loss: dict[str, Any] | None = None,
    overall_win_rate: float | None = None,
) -> dict[str, Any]:
    """Everything in Phases 5 and 11, over one pass of enriched move rows."""

    return {
        "punish": compute_punish_rate(rows),
        "blindness": compute_blindness_split(rows),
        "blunder_tempo": compute_blunder_tempo(rows),
        "trades": compute_trade_quality(rows),
        "impulsivity": compute_impulsivity(rows),
        "metacognition": compute_metacognition(rows),
        "state_risk": compute_state_conditioned_risk(rows),
        "stubbornness": compute_stubbornness(rows),
        "tilt_test": compute_tilt_significance(after_loss or {}, overall_win_rate),
        "move_time_shape": compute_move_time_shape(rows),
    }
