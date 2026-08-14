"""Insights metrics over persisted ``review_moves`` rows (Tier 1–3 + advanced).

The professional layer on top of this catalogue — headline KPIs, move quality,
critical moments, opening tree, leak board — lives in :mod:`server.insights_pro`.
The dependency runs one way (this module imports that one) so the row helpers
below have exactly one definition.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from server.insights_narrative import build_narrative
from server.insights_pro import (
    castle_side as _castle_side,
    compute_pro_metrics,
    hour_from_played_at as _hour_from_played_at,
    parse_detail as _parse_detail,
    parse_dt as _parse_dt,
    user_drew as _user_drew,
    user_won as _user_won,
)

SESSION_GAP = timedelta(hours=2)
PRACTICE_DELTA_W = 15.0
PRACTICE_FINDABILITY_MIN = 60


def compute_tier1_metrics(
    connection: Any,
    *,
    review_ids: list[str],
) -> dict[str, Any]:
    """Aggregate Tier 1–3 metrics plus steering and practice flags."""

    empty = {
        "total_loss": 0.0,
        "fixable_loss": None,
        "fixable_sample_size": 0,
        "loss_taxonomy": {"counts": {}},
        "time_vs_criticality": {},
        "volatility_profile": {"buckets": []},
        "games": 0,
        "tier2": {},
        "tier3": {},
        "volatility_steering": {},
        "practice_flags": {"count": 0, "full_tier_sample": 0, "items": []},
        "missed_tactics": {},
        "time_scramble_decay": {"buckets": []},
        "maia_naturalness": {},
        "game_explorer": [],
        "ai_coach_takeaways": [],
        "narrative": {},
        "pro": {
            "headline": {},
            "move_quality": {},
            "critical_moments": {},
            "timeline": {"points": []},
            "openings": {"rows": []},
            "endgame": {},
            "resilience": {},
            "blunder_timing": {"buckets": []},
            "leaks": [],
            "strengths": [],
        },
    }
    if not review_ids:
        empty["narrative"] = build_narrative(empty)
        return empty

    placeholders = ",".join("?" for _ in review_ids)

    reviews = connection.execute(
        f"SELECT review_id, total_loss, fixable_loss, loss_type, depth_tier, game_id, "
        f"user_color, accuracy, created_at "
        f"FROM reviews WHERE review_id IN ({placeholders})",
        review_ids,
    ).fetchall()

    total_loss = sum(float(r["total_loss"] or 0) for r in reviews)
    full_reviews = [r for r in reviews if r["depth_tier"] == "full"]
    fixable_loss = None
    if full_reviews:
        fixable_loss = sum(float(r["fixable_loss"] or 0) for r in full_reviews)

    tax_counts: dict[str, int] = defaultdict(int)
    for r in reviews:
        tax_counts[str(r["loss_type"] or "bleed")] += 1

    moves = connection.execute(
        f"SELECT review_id, ply, san, phase, is_book, is_user_move, win_prob, delta_w, "
        f"volatility, time_spent, clock_remaining, classification, findability, detail, tactic_tags "
        f"FROM review_moves WHERE review_id IN ({placeholders}) ORDER BY review_id, ply",
        review_ids,
    ).fetchall()

    high_vol_times: list[float] = []
    low_vol_times: list[float] = []
    for m in moves:
        if not m["is_user_move"]:
            continue
        if m["time_spent"] is None or m["volatility"] is None:
            continue
        if float(m["volatility"]) >= 60:
            high_vol_times.append(float(m["time_spent"]))
        elif float(m["volatility"]) <= 30:
            low_vol_times.append(float(m["time_spent"]))

    def _avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    game_rows = connection.execute(
        f"""
        SELECT r.review_id, r.game_id, r.user_color, r.accuracy, r.loss_type, g.result,
               g.white_name, g.black_name, g.white_rating, g.black_rating,
               g.eco, g.opening_name, g.played_at, g.pgn,
               AVG(m.volatility) AS mean_vol,
               COUNT(m.ply) AS ply_count
        FROM reviews r
        JOIN games g ON g.game_id = r.game_id
        LEFT JOIN review_moves m ON m.review_id = r.review_id
        WHERE r.review_id IN ({placeholders})
        GROUP BY r.review_id
        """,
        review_ids,
    ).fetchall()

    buckets = {
        "low": {"label": "Low V (<35)", "wins": 0, "n": 0},
        "mid": {"label": "Mid V (35–60)", "wins": 0, "n": 0},
        "high": {"label": "High V (>60)", "wins": 0, "n": 0},
    }
    for row in game_rows:
        mv = float(row["mean_vol"] or 0)
        key = "low" if mv < 35 else "high" if mv > 60 else "mid"
        buckets[key]["n"] += 1
        if _user_won(row["result"], row["user_color"]):
            buckets[key]["wins"] += 1

    profile = []
    for key in ("low", "mid", "high"):
        b = buckets[key]
        n = b["n"]
        profile.append({
            "label": b["label"],
            "n": n,
            "win_rate": (b["wins"] / n) if n else 0.0,
        })

    moves_by_review: dict[str, list[Any]] = defaultdict(list)
    for m in moves:
        moves_by_review[str(m["review_id"])].append(m)

    tier2 = _compute_tier2(game_rows, moves_by_review)
    tier3 = _compute_tier3(game_rows, moves_by_review)
    steering = _compute_volatility_steering(moves_by_review)
    practice = _compute_practice_flags(game_rows, moves_by_review)
    missed_tactics = _compute_missed_tactics(moves_by_review)
    scramble_decay = _compute_time_scramble_decay(moves_by_review)
    maia_nat = _compute_maia_naturalness(moves_by_review)

    pro, game_facts = compute_pro_metrics(
        game_rows,
        moves_by_review,
        total_loss=total_loss,
        fixable_loss=fixable_loss,
        scramble=scramble_decay,
        tier3=tier3,
        missed_tactics=missed_tactics,
    )

    time_vs_crit = {
        "avg_time_high_vol": _avg(high_vol_times),
        "avg_time_low_vol": _avg(low_vol_times),
        "n_high": len(high_vol_times),
        "n_low": len(low_vol_times),
        "note": (
            "you rush critical positions"
            if (
                _avg(high_vol_times) is not None
                and _avg(low_vol_times) is not None
                and _avg(high_vol_times) < _avg(low_vol_times)  # type: ignore[operator]
            )
            else None
        ),
    }

    ai_takeaways = _compute_ai_coach_takeaways(
        leaks=pro["leaks"],
        total_loss=total_loss,
        fixable_loss=fixable_loss,
        tax_counts=dict(tax_counts),
        tier2=tier2,
        tier3=tier3,
        time_vs_criticality=time_vs_crit,
        scramble_decay=scramble_decay,
    )

    payload = {
        "total_loss": total_loss,
        "fixable_loss": fixable_loss,
        "fixable_sample_size": len(full_reviews),
        "loss_taxonomy": {"counts": dict(tax_counts)},
        "time_vs_criticality": time_vs_crit,
        "volatility_profile": {"buckets": profile},
        "volatility_steering": steering,
        "games": len(reviews),
        "tier2": tier2,
        "tier3": tier3,
        "practice_flags": practice,
        "missed_tactics": missed_tactics,
        "time_scramble_decay": scramble_decay,
        "maia_naturalness": maia_nat,
        # One per-game fact table, used both as the Game Explorer source and as
        # the client's substrate for re-aggregating the dashboard under filters.
        "game_explorer": game_facts,
        "ai_coach_takeaways": ai_takeaways,
        "pro": pro,
    }
    payload["narrative"] = build_narrative(payload)
    return payload


def _compute_time_scramble_decay(moves_by_review: dict[str, list[Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {
        "deep": {"label": ">60s clock", "loss_sum": 0.0, "moves": 0, "blunders": 0},
        "medium": {"label": "30–60s clock", "loss_sum": 0.0, "moves": 0, "blunders": 0},
        "low": {"label": "10–30s clock", "loss_sum": 0.0, "moves": 0, "blunders": 0},
        "scramble": {"label": "<10s clock", "loss_sum": 0.0, "moves": 0, "blunders": 0},
    }
    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"]:
                continue
            # sqlite3.Row has no ``.get`` — indexing is the only accessor, and
            # probing for one silently emptied every bucket.
            clock = m["clock_remaining"]
            if clock is None:
                continue
            try:
                clk = float(clock)
            except (TypeError, ValueError):
                continue
            dw = float(m["delta_w"] or 0)
            key = "scramble" if clk < 10 else "low" if clk < 30 else "medium" if clk < 60 else "deep"
            buckets[key]["loss_sum"] += dw
            buckets[key]["moves"] += 1
            if dw >= 25:
                buckets[key]["blunders"] += 1

    rows = []
    for key in ("deep", "medium", "low", "scramble"):
        b = buckets[key]
        n = b["moves"]
        rows.append({
            "key": key,
            "label": b["label"],
            "moves": n,
            "delta_w_per_move": (b["loss_sum"] / n) if n else 0.0,
            "blunder_rate": (b["blunders"] / n) if n else 0.0,
        })
    return {"buckets": rows}


def _compute_maia_naturalness(moves_by_review: dict[str, list[Any]]) -> dict[str, Any]:
    matched = 0
    total = 0
    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"]:
                continue
            findability = m["findability"]
            if findability is not None:
                total += 1
                if int(findability) >= 50:
                    matched += 1
    rate = (matched / total) if total else None
    return {
        "naturalness_rate": rate,
        "sample_size": total,
        "label": (
            "High intuition" if rate and rate >= 0.7
            else "Mixed intuition" if rate and rate >= 0.45
            else "Low intuition (engine-dependent / unorthodox)" if rate is not None
            else "Needs full tier review for Maia intuition"
        )
    }


def _compute_ai_coach_takeaways(
    leaks: list[dict[str, Any]],
    total_loss: float,
    fixable_loss: float | None,
    tax_counts: dict[str, int],
    tier2: dict[str, Any],
    tier3: dict[str, Any],
    time_vs_criticality: dict[str, Any],
    scramble_decay: dict[str, Any],
) -> list[str]:
    """Coach copy, ranked by the leak board rather than by hand-ordered rules.

    The leak board already scores every finding in win% lost per game, so the
    top three leaks *are* the three things worth saying. Older heuristics stay
    as a fallback for windows too small to score anything.
    """

    takeaways = [
        f"{leak['title']}. {leak['detail']} "
        f"(≈{leak['impact_win_pct_per_game']:.0f} win% per game)"
        for leak in leaks[:3]
    ]
    if takeaways:
        return takeaways

    top_tax = max(tax_counts.items(), key=lambda kv: kv[1]) if tax_counts else None
    if top_tax:
        takeaways.append(
            f"Primary loss mode: '{top_tax[0].replace('_', ' ').capitalize()}' "
            f"accounts for {top_tax[1]} of your analyzed games."
        )
    if time_vs_criticality.get("note"):
        high_t = time_vs_criticality.get("avg_time_high_vol")
        low_t = time_vs_criticality.get("avg_time_low_vol")
        if high_t is not None and low_t is not None:
            takeaways.append(
                f"You rush critical moves: {high_t:.0f}s on sharp positions versus "
                f"{low_t:.0f}s on quiet ones."
            )
    if not takeaways:
        takeaways.append(
            "No measurable leak stands out in this window — analyze more games "
            "for a sharper read."
        )
    return takeaways[:3]



def _compute_tier2(
    game_rows: list[Any],
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    phase_loss: dict[str, float] = defaultdict(float)
    phase_moves: dict[str, int] = defaultdict(int)

    leave_book_plies: list[int] = []
    post_book_deltas: list[float] = []

    conversion = {"wins": 0, "n": 0}
    comeback = {"wins": 0, "n": 0}

    castle_stats = {
        "kingside": {"wins": 0, "n": 0},
        "queenside": {"wins": 0, "n": 0},
        "never": {"wins": 0, "n": 0},
        "same_side": {"wins": 0, "n": 0},
        "opposite_side": {"wins": 0, "n": 0},
    }

    opp_bands: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "n": 0, "phase_loss": defaultdict(float), "phase_moves": defaultdict(int)}
    )

    missed_wins: list[dict[str, Any]] = []

    by_color = {"white": {"wins": 0, "n": 0}, "black": {"wins": 0, "n": 0}}
    by_eco: dict[str, dict[str, Any]] = defaultdict(lambda: {"wins": 0, "n": 0})
    length_buckets = {
        "short": {"label": "<40 plies", "n": 0, "wins": 0},
        "medium": {"label": "40–80 plies", "n": 0, "wins": 0},
        "long": {"label": ">80 plies", "n": 0, "wins": 0},
    }
    accuracy_over_time: list[dict[str, Any]] = []
    by_hour: dict[int, dict[str, Any]] = defaultdict(lambda: {"wins": 0, "n": 0})

    for row in game_rows:
        rid = str(row["review_id"])
        color = row["user_color"] or "white"
        won = _user_won(row["result"], color)
        result = row["result"]
        game_moves = moves_by_review.get(rid, [])
        user_moves = [m for m in game_moves if m["is_user_move"]]

        # Phase attribution
        for m in user_moves:
            phase = str(m["phase"] or "middlegame")
            phase_loss[phase] += float(m["delta_w"] or 0)
            phase_moves[phase] += 1

        # Repertoire depth — first non-book user move after any book
        book_seen = False
        leave_ply = None
        for m in user_moves:
            if m["is_book"]:
                book_seen = True
                continue
            if book_seen or (leave_ply is None and m["ply"] and int(m["ply"]) <= 20):
                leave_ply = int(m["ply"])
                break
        # Prefer first user move that isn't book
        if leave_ply is None:
            for m in user_moves:
                if not m["is_book"]:
                    leave_ply = int(m["ply"])
                    break
        if leave_ply is not None:
            leave_book_plies.append(leave_ply)
            following = [
                float(m["delta_w"] or 0)
                for m in user_moves
                if int(m["ply"]) >= leave_ply
            ][:5]
            if following:
                post_book_deltas.append(sum(following) / len(following))

        # Conversion / comeback
        if any((m["win_prob"] or 0) > 0.7 for m in user_moves):
            conversion["n"] += 1
            if won:
                conversion["wins"] += 1
        if any((m["win_prob"] or 0) < 0.3 for m in user_moves):
            comeback["n"] += 1
            if won:
                comeback["wins"] += 1

        # Castling
        user_castle = _castle_side(user_moves)
        opp_moves = [m for m in game_moves if not m["is_user_move"]]
        opp_castle = _castle_side(opp_moves)
        if user_castle is None:
            castle_stats["never"]["n"] += 1
            if won:
                castle_stats["never"]["wins"] += 1
        else:
            castle_stats[user_castle]["n"] += 1
            if won:
                castle_stats[user_castle]["wins"] += 1
            if opp_castle is not None:
                key = "opposite_side" if opp_castle != user_castle else "same_side"
                castle_stats[key]["n"] += 1
                if won:
                    castle_stats[key]["wins"] += 1

        # Opponent-relative (banded vs this game's own rating)
        opp_rating = (
            row["black_rating"] if color == "white" else row["white_rating"]
        )
        self_rating = (
            row["white_rating"] if color == "white" else row["black_rating"]
        )
        try:
            center = int(self_rating) if self_rating is not None else None
        except (TypeError, ValueError):
            center = None
        band = _rating_band(opp_rating, center=center)
        opp_bands[band]["n"] += 1
        if won:
            opp_bands[band]["wins"] += 1
        for m in user_moves:
            phase = str(m["phase"] or "middlegame")
            opp_bands[band]["phase_loss"][phase] += float(m["delta_w"] or 0)
            opp_bands[band]["phase_moves"][phase] += 1

        # Missed wins
        peak = None
        peak_ply = None
        for m in user_moves:
            wp = m["win_prob"]
            if wp is not None and float(wp) > 0.85:
                if peak is None or float(wp) > peak:
                    peak = float(wp)
                    peak_ply = int(m["ply"])
        if peak is not None and not won and result not in (None, "*", "1/2-1/2"):
            slip = None
            slip_san = None
            slip_dw = -1.0
            for m in user_moves:
                if peak_ply is not None and int(m["ply"]) < peak_ply:
                    continue
                dw = float(m["delta_w"] or 0)
                if dw > slip_dw:
                    slip_dw = dw
                    slip = int(m["ply"])
                    slip_san = m["san"]
            missed_wins.append({
                "game_id": row["game_id"],
                "peak_win_prob": peak,
                "peak_ply": peak_ply,
                "slip_ply": slip,
                "slip_san": slip_san,
                "slip_delta_w": slip_dw if slip_dw >= 0 else None,
                "opponent": row["black_name"] if color == "white" else row["white_name"],
            })

        # Standard set
        by_color[color]["n"] += 1
        if won:
            by_color[color]["wins"] += 1
        eco = (row["eco"] or "unknown").strip() or "unknown"
        by_eco[eco]["n"] += 1
        if won:
            by_eco[eco]["wins"] += 1
        if row["accuracy"] is not None:
            accuracy_over_time.append({
                "played_at": row["played_at"],
                "accuracy": float(row["accuracy"]),
                "game_id": row["game_id"],
            })
        plies = int(row["ply_count"] or 0)
        lk = "short" if plies < 40 else "long" if plies > 80 else "medium"
        length_buckets[lk]["n"] += 1
        if won:
            length_buckets[lk]["wins"] += 1
        hour = _hour_from_played_at(row["played_at"], row["pgn"])
        if hour is not None:
            by_hour[hour]["n"] += 1
            if won:
                by_hour[hour]["wins"] += 1

    phase_attribution = []
    for phase in ("opening", "middlegame", "endgame"):
        n = phase_moves.get(phase, 0)
        loss = phase_loss.get(phase, 0.0)
        phase_attribution.append({
            "phase": phase,
            "total_delta_w": loss,
            "moves": n,
            "delta_w_per_move": (loss / n) if n else 0.0,
        })

    def _rate(bucket: dict[str, Any]) -> dict[str, Any]:
        n = int(bucket["n"])
        return {
            "n": n,
            "wins": int(bucket["wins"]),
            "win_rate": (bucket["wins"] / n) if n else 0.0,
        }

    opp_relative = []
    for band in ("lower", "similar", "higher", "unknown"):
        b = opp_bands.get(band)
        if not b:
            continue
        phases = []
        for phase in ("opening", "middlegame", "endgame"):
            n = int(b["phase_moves"].get(phase, 0))
            loss = float(b["phase_loss"].get(phase, 0.0))
            phases.append({
                "phase": phase,
                "delta_w_per_move": (loss / n) if n else 0.0,
                "moves": n,
            })
        opp_relative.append({
            "band": band,
            **_rate(b),
            "phase_attribution": phases,
        })

    eco_rows = [
        {"eco": eco, **_rate(stats)}
        for eco, stats in sorted(by_eco.items(), key=lambda kv: -kv[1]["n"])[:12]
    ]

    hour_rows = [
        {"hour": h, **_rate(by_hour[h])}
        for h in sorted(by_hour)
    ]

    accuracy_over_time.sort(key=lambda x: x.get("played_at") or "")

    return {
        "phase_attribution": phase_attribution,
        "repertoire_depth": {
            "mean_leave_book_ply": (
                sum(leave_book_plies) / len(leave_book_plies) if leave_book_plies else None
            ),
            "mean_delta_w_next_5": (
                sum(post_book_deltas) / len(post_book_deltas) if post_book_deltas else None
            ),
            "n": len(leave_book_plies),
        },
        "conversion": _rate(conversion),
        "comeback": _rate(comeback),
        "castling": {k: _rate(v) for k, v in castle_stats.items()},
        "opponent_relative": opp_relative,
        "missed_wins": {
            "count": len(missed_wins),
            "examples": missed_wins[:10],
        },
        "standard": {
            "by_color": {k: _rate(v) for k, v in by_color.items()},
            "by_eco": eco_rows,
            "accuracy_over_time": accuracy_over_time[-40:],
            "by_hour": hour_rows,
            "game_length": [
                {"key": k, "label": v["label"], **_rate(v)}
                for k, v in length_buckets.items()
            ],
        },
    }


def _rating_band(opp_rating: Any, *, center: int | None = None) -> str:
    try:
        rating = int(opp_rating) if opp_rating is not None else None
    except (TypeError, ValueError):
        rating = None
    if rating is None:
        return "unknown"
    # Without a stable self-rating on the run, band relative to common gaps
    # using absolute tiers when center unknown.
    if center is None:
        if rating < 1400:
            return "lower"
        if rating > 1800:
            return "higher"
        return "similar"
    diff = rating - center
    if diff <= -100:
        return "lower"
    if diff >= 100:
        return "higher"
    return "similar"


def _compute_tier3(
    game_rows: list[Any],
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    """Tilt / session-index / post-loss / session-length effects."""

    enriched = []
    for row in game_rows:
        dt = _parse_dt(row["played_at"], row["pgn"])
        enriched.append({**dict(row), "_dt": dt})
    enriched.sort(key=lambda r: r["_dt"] or datetime.min.replace(tzinfo=timezone.utc))

    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev_dt: datetime | None = None
    for row in enriched:
        dt = row["_dt"]
        if current and prev_dt and dt and (dt - prev_dt) > SESSION_GAP:
            sessions.append(current)
            current = []
        current.append(row)
        if dt:
            prev_dt = dt
    if current:
        sessions.append(current)

    by_index: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "n": 0, "accuracy_sum": 0.0, "accuracy_n": 0}
    )
    post_loss = {"wins": 0, "n": 0, "accuracy_sum": 0.0, "accuracy_n": 0}
    session_len_buckets = {
        "short": {"label": "1–2 games", "wins": 0, "n": 0},
        "medium": {"label": "3–5 games", "wins": 0, "n": 0},
        "long": {"label": "6+ games", "wins": 0, "n": 0},
    }
    hour_acc: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"accuracy_sum": 0.0, "n": 0, "total_loss_sum": 0.0}
    )

    for session in sessions:
        slen = len(session)
        sk = "short" if slen <= 2 else "long" if slen >= 6 else "medium"
        prev_lost = False
        for idx, row in enumerate(session):
            color = row["user_color"] or "white"
            won = _user_won(row["result"], color)
            lost = (
                (row["result"] == "0-1" and color == "white")
                or (row["result"] == "1-0" and color == "black")
            )
            by_index[idx]["n"] += 1
            if won:
                by_index[idx]["wins"] += 1
            if row["accuracy"] is not None:
                by_index[idx]["accuracy_sum"] += float(row["accuracy"])
                by_index[idx]["accuracy_n"] += 1

            session_len_buckets[sk]["n"] += 1
            if won:
                session_len_buckets[sk]["wins"] += 1

            if prev_lost:
                post_loss["n"] += 1
                if won:
                    post_loss["wins"] += 1
                if row["accuracy"] is not None:
                    post_loss["accuracy_sum"] += float(row["accuracy"])
                    post_loss["accuracy_n"] += 1
            prev_lost = lost

            hour = _hour_from_played_at(row["played_at"], row["pgn"])
            if hour is not None and row["accuracy"] is not None:
                hour_acc[hour]["accuracy_sum"] += float(row["accuracy"])
                hour_acc[hour]["n"] += 1
            # total_loss lives on reviews — pull via moves sum if needed
            rid = str(row["review_id"])
            loss = sum(float(m["delta_w"] or 0) for m in moves_by_review.get(rid, []) if m["is_user_move"])
            if hour is not None:
                hour_acc[hour]["total_loss_sum"] += loss

    def _rate(bucket: dict[str, Any]) -> dict[str, Any]:
        n = int(bucket["n"])
        out: dict[str, Any] = {
            "n": n,
            "wins": int(bucket.get("wins", 0)),
            "win_rate": (bucket["wins"] / n) if n else 0.0,
        }
        if bucket.get("accuracy_n"):
            out["mean_accuracy"] = bucket["accuracy_sum"] / bucket["accuracy_n"]
        return out

    index_rows = []
    for idx in sorted(by_index):
        b = by_index[idx]
        index_rows.append({"game_index": idx + 1, **_rate(b)})

    hour_rows = []
    for h in sorted(hour_acc):
        b = hour_acc[h]
        n = int(b["n"])
        hour_rows.append({
            "hour": h,
            "n": n,
            "mean_accuracy": (b["accuracy_sum"] / n) if n else None,
            "mean_total_loss": (b["total_loss_sum"] / n) if n else None,
        })

    return {
        "sessions": len(sessions),
        "by_session_index": index_rows,
        "after_loss": _rate(post_loss),
        "by_session_length": {
            k: {"label": v["label"], **_rate(v)} for k, v in session_len_buckets.items()
        },
        "accuracy_by_hour": hour_rows,
        "note": (
            "tilt signal: game-2+ in a session underperforms game-1"
            if (
                len(index_rows) >= 2
                and index_rows[0]["n"]
                and index_rows[1]["n"]
                and index_rows[1]["win_rate"] + 0.08 < index_rows[0]["win_rate"]
            )
            else None
        ),
    }


def _compute_volatility_steering(
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    """Compare volatility when the user plays the engine's top move vs an alt."""

    best_vols: list[float] = []
    alt_vols: list[float] = []
    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"] or m["volatility"] is None:
                continue
            detail = _parse_detail(m["detail"])
            played = (detail.get("move_uci") or "").strip()
            lines = detail.get("top_lines") or []
            if not played or not lines:
                continue
            best_uci = (lines[0] or {}).get("uci")
            vol = float(m["volatility"])
            if best_uci and played == best_uci:
                best_vols.append(vol)
            else:
                # Confirm it's among reported alts when possible
                alt_ucis = {(ln or {}).get("uci") for ln in lines[1:]}
                if not alt_ucis or played in alt_ucis or best_uci:
                    alt_vols.append(vol)

    def _avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    mean_best = _avg(best_vols)
    mean_alt = _avg(alt_vols)
    note = None
    if mean_best is not None and mean_alt is not None:
        if mean_alt > mean_best + 8:
            note = "you steer into sharper positions when leaving the engine line"
        elif mean_best > mean_alt + 8:
            note = "you leave the engine line toward quieter positions"
    return {
        "mean_vol_played_best": mean_best,
        "mean_vol_played_alt": mean_alt,
        "n_best": len(best_vols),
        "n_alt": len(alt_vols),
        "note": note,
    }


def _compute_practice_flags(
    game_rows: list[Any],
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    """C.5: costly, learnable misses — prefer findability > 60 when present."""

    game_meta = {str(r["review_id"]): r for r in game_rows}
    items: list[dict[str, Any]] = []
    full_sample = 0
    for rid, moves in moves_by_review.items():
        meta = game_meta.get(rid)
        if meta is None:
            continue
        for m in moves:
            if not m["is_user_move"]:
                continue
            dw = float(m["delta_w"] or 0)
            if dw < PRACTICE_DELTA_W:
                continue
            findability = m["findability"]
            if findability is not None:
                full_sample += 1
                if int(findability) <= PRACTICE_FINDABILITY_MIN:
                    continue
                needs_full = False
            else:
                needs_full = True
            detail = _parse_detail(m["detail"])
            lines = detail.get("top_lines") or []
            best_uci = (lines[0] or {}).get("uci") if lines else None
            items.append({
                "review_id": rid,
                "game_id": meta["game_id"],
                "ply": int(m["ply"]),
                "san": m["san"],
                "delta_w": dw,
                "findability": int(findability) if findability is not None else None,
                "volatility": float(m["volatility"]) if m["volatility"] is not None else None,
                "fen": detail.get("fen_before"),
                "move_uci": detail.get("move_uci"),
                "best_uci": best_uci,
                "needs_full": needs_full,
                "opponent": (
                    meta["black_name"] if meta["user_color"] == "white" else meta["white_name"]
                ),
                "user_color": meta["user_color"],
            })

    items.sort(key=lambda x: -float(x["delta_w"]))
    # Cap the practice set size shown/stored per run
    items = items[:40]
    return {
        "count": len(items),
        "full_tier_sample": full_sample,
        "delta_w_threshold": PRACTICE_DELTA_W,
        "findability_min": PRACTICE_FINDABILITY_MIN,
        "items": items,
    }


def _compute_missed_tactics(
    moves_by_review: dict[str, list[Any]],
) -> dict[str, Any]:
    """Missed-tactic taxonomy × findability (Insights.md Tier 1 [F] cross)."""

    by_tag: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "high_find": 0, "delta_w_sum": 0.0}
    )
    for moves in moves_by_review.values():
        for m in moves:
            if not m["is_user_move"] or not m["tactic_tags"]:
                continue
            try:
                tags = json.loads(m["tactic_tags"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(tags, list):
                continue
            findability = m["findability"]
            dw = float(m["delta_w"] or 0)
            for tag in tags:
                by_tag[str(tag)]["n"] += 1
                by_tag[str(tag)]["delta_w_sum"] += dw
                if findability is not None and int(findability) > 60:
                    by_tag[str(tag)]["high_find"] += 1

    rows = []
    for tag, stats in sorted(by_tag.items(), key=lambda kv: -kv[1]["n"]):
        n = stats["n"]
        rows.append({
            "tag": tag,
            "n": n,
            "mean_delta_w": (stats["delta_w_sum"] / n) if n else 0.0,
            "high_findability_n": stats["high_find"],
            "high_findability_rate": (stats["high_find"] / n) if n else 0.0,
        })
    full_tier_n = sum(
        1
        for moves in moves_by_review.values()
        for m in moves
        if m["is_user_move"] and m["findability"] is not None
    )
    return {
        "tags": rows,
        "full_tier_sample_size": full_tier_n,
        "note": (
            f"you miss {rows[0]['tag'].replace('_', ' ')}s often "
            f"({int(rows[0]['high_findability_rate'] * 100)}% findability > 60)"
            if rows and rows[0]["n"] >= 2
            else None
        ),
    }


def compute_run_trend(
    connection: Any,
    *,
    user_id: int,
    run_id: str,
    handle: str,
    window_days: int,
    time_class: str,
    source: str = "chesscom",
    current_metrics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compare this run to the previous complete run with the same filters.

    Returns ``None`` when there is no prior run to compare against.
    """

    prior = connection.execute(
        """
        SELECT run_id, metrics, created_at, games_analyzed
        FROM insight_runs
        WHERE user_id = ? AND chesscom_handle = ? AND window_days = ?
          AND time_class = ? AND COALESCE(source, 'chesscom') = ?
          AND status = 'complete' AND run_id != ? AND metrics IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id, handle, window_days, time_class, source, run_id),
    ).fetchone()
    if prior is None:
        return None
    try:
        previous = json.loads(prior["metrics"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(previous, dict):
        return None
    current = current_metrics or {}

    def _num(metrics: dict[str, Any], *keys: str) -> float | None:
        cur: Any = metrics
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if cur is None:
            return None
        try:
            return float(cur)
        except (TypeError, ValueError):
            return None

    def _delta(cur: float | None, prev: float | None) -> dict[str, Any] | None:
        if cur is None or prev is None:
            return None
        change = cur - prev
        pct = None if prev == 0 else (change / abs(prev)) * 100.0
        return {
            "current": cur,
            "previous": prev,
            "delta": change,
            "pct": pct,
        }

    # Phase attribution: map phase → delta_w_per_move for endgame fixable story
    def _phase_map(metrics: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in (metrics.get("tier2") or {}).get("phase_attribution") or []:
            if isinstance(row, dict) and row.get("phase") is not None:
                try:
                    out[str(row["phase"])] = float(row.get("delta_w_per_move") or 0)
                except (TypeError, ValueError):
                    continue
        return out

    cur_phases = _phase_map(current)
    prev_phases = _phase_map(previous)
    phase_deltas = {}
    for phase in sorted(set(cur_phases) | set(prev_phases)):
        d = _delta(cur_phases.get(phase), prev_phases.get(phase))
        if d is not None:
            phase_deltas[phase] = d

    highlights: list[str] = []
    fixable = _delta(
        _num(current, "fixable_loss"),
        _num(previous, "fixable_loss"),
    )
    if fixable and fixable["pct"] is not None and abs(fixable["pct"]) >= 5:
        direction = "dropped" if fixable["delta"] < 0 else "rose"
        highlights.append(
            f"Fixable loss {direction} {abs(fixable['pct']):.0f}% versus last run"
        )
    endgame = phase_deltas.get("endgame")
    if endgame and endgame["pct"] is not None and abs(endgame["pct"]) >= 5:
        direction = "dropped" if endgame["delta"] < 0 else "rose"
        highlights.append(
            f"Endgame Δw/move {direction} {abs(endgame['pct']):.0f}% versus last run"
        )
    total = _delta(_num(current, "total_loss"), _num(previous, "total_loss"))
    if not highlights and total and total["pct"] is not None and abs(total["pct"]) >= 5:
        direction = "dropped" if total["delta"] < 0 else "rose"
        highlights.append(
            f"Total loss {direction} {abs(total['pct']):.0f}% versus last run"
        )

    return {
        "previous_run_id": prior["run_id"],
        "previous_created_at": prior["created_at"],
        "previous_games_analyzed": prior["games_analyzed"],
        "fixable_loss": fixable,
        "total_loss": total,
        "phase_delta_w_per_move": phase_deltas,
        "highlights": highlights,
    }


def persist_practice_flags(
    connection: Any,
    *,
    run_id: str,
    user_id: int,
    practice: dict[str, Any],
    mistake_run_id: int | None = None,
) -> int:
    """Write ``insight_flags`` and optionally bridge into ``mistake_puzzles``.

    Returns the number of flags persisted.
    """

    from server.mistakes import MistakePuzzle
    from server.mistakes_run import _insert_puzzle

    connection.execute("DELETE FROM insight_flags WHERE run_id = ?", (run_id,))
    inserted = 0
    for item in practice.get("items") or []:
        fen = item.get("fen")
        best = item.get("best_uci")
        played = item.get("move_uci")
        side = None
        if fen:
            parts = str(fen).split()
            side = parts[1] if len(parts) > 1 else None
        puzzle_id = None
        if (
            mistake_run_id is not None
            and fen
            and best
            and played
            and side
        ):
            color = item.get("user_color") or ("white" if side == "w" else "black")
            puzzle = MistakePuzzle(
                bucket="blunder" if float(item.get("delta_w") or 0) >= 25 else "missed_win",
                fen=fen,
                side_to_move=side,
                user_color=color[0] if color in ("white", "black") else side,
                best_move=best,
                solution_moves=best,
                user_actual_move=played,
                eval_before_cp=0,
                eval_best_cp=0,
                eval_played_cp=-int(float(item.get("delta_w") or 0) * 3),
                second_best_gap_cp=None,
                volatility=item.get("volatility"),
                maia_solution_rank=1 if item.get("findability") and item["findability"] > 60 else None,
                maia_best_in_top3=1 if item.get("findability") and item["findability"] > 60 else None,
                clock_seconds=None,
                ply_number=int(item.get("ply") or 0),
                game_url=None,
                game_id=item.get("game_id"),
                game_date=None,
                time_class=None,
                opponent=item.get("opponent"),
                caption=(
                    f"Insights flag: {item.get('san') or played} cost "
                    f"{float(item.get('delta_w') or 0):.0f} win% points"
                    + (
                        f" (findability {item['findability']})"
                        if item.get("findability") is not None
                        else " (shallow — open in Game Review for full findability)"
                    )
                ),
            )
            # mistakes table expects user_color as 'w'/'b' in some paths — check schema
            # mistake_puzzles.user_color is TEXT; detector uses 'white'/'black' in meta but
            # stores side letters in tests as 'w'. Keep letter for board side consistency.
            if puzzle.user_color in ("white", "black"):
                puzzle.user_color = "w" if puzzle.user_color == "white" else "b"
            puzzle_id = _insert_puzzle(connection, user_id, mistake_run_id, puzzle)

        connection.execute(
            """
            INSERT INTO insight_flags (
                run_id, game_id, review_id, ply, fen, side_to_move, move_uci, best_uci,
                san, delta_w, findability, volatility, needs_full, puzzle_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item.get("game_id"),
                item.get("review_id"),
                int(item.get("ply") or 0),
                fen,
                side,
                played,
                best,
                item.get("san"),
                item.get("delta_w"),
                item.get("findability"),
                item.get("volatility"),
                1 if item.get("needs_full") else 0,
                puzzle_id,
            ),
        )
        inserted += 1
    connection.commit()
    return inserted


def recompute_run_metrics(connection: Any, run_id: str) -> dict[str, Any] | None:
    """Re-compute metrics for an existing run and update insight_runs table."""

    row = connection.execute(
        "SELECT user_id, chesscom_handle, source, window_days, time_class FROM insight_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None

    game_rows = connection.execute(
        "SELECT game_id FROM insight_run_games WHERE run_id = ?", (run_id,)
    ).fetchall()
    if not game_rows:
        return None

    game_ids = [g["game_id"] for g in game_rows]
    placeholders = ",".join("?" for _ in game_ids)
    reviews = connection.execute(
        f"SELECT review_id, game_id, depth_tier, status FROM reviews "
        f"WHERE user_id = ? AND game_id IN ({placeholders})",
        [row["user_id"]] + game_ids,
    ).fetchall()
    # A game upgraded to full tier keeps its shallow review as well, so this
    # used to return more reviews than the run has games — double-counting
    # every upgraded game and listing the same miss twice in the practice set.
    # One review per game: complete beats pending, full beats shallow.
    def _rank(r: Any) -> tuple[int, int]:
        return (
            1 if str(r["status"]) == "complete" else 0,
            1 if str(r["depth_tier"]) == "full" else 0,
        )

    best: dict[str, Any] = {}
    for review in reviews:
        gid = str(review["game_id"])
        if gid not in best or _rank(review) > _rank(best[gid]):
            best[gid] = review
    review_ids = [str(r["review_id"]) for r in best.values()]
    if not review_ids:
        return None

    metrics = compute_tier1_metrics(connection, review_ids=review_ids)
    trend = compute_run_trend(
        connection,
        user_id=row["user_id"],
        run_id=run_id,
        handle=row["chesscom_handle"],
        window_days=row["window_days"],
        time_class=row["time_class"],
        source=row["source"] if "source" in row.keys() else "chesscom",
        current_metrics=metrics,
    )
    if trend is not None:
        metrics["trend"] = trend

    connection.execute(
        "UPDATE insight_runs SET metrics = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
        (json.dumps(metrics), run_id),
    )
    connection.commit()
    return metrics

