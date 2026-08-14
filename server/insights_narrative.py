"""Insights narrative layer — the story a player reads first.

The catalogue in ``insights_metrics`` / ``insights_pro`` is a dashboard.
This module turns that same blob into ``metrics.narrative``: a verdict,
a why-you-lose argument, and a short list of fixes. The frontend renders
it; it does not invent the story.

Voice is enforced here. Verdict / Why / How copy may not contain Δw,
volatility, findability, or "expectation-adjusted" — those belong on the
Deep Dive. Captions are rebuilt from structured fields, never copied from
leak ``detail`` strings (those are written for the dashboard).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from server.insights_pro import WINNING_WIN_PROB

MIN_GAMES = 8
MIN_LOSSES = 3
MIN_STAGE_N = 3
SPINE_POINTS = 20
NARRATIVE_SCHEMA = "story-3"

_GENERIC_OPENING_PREFIXES = (
    "Queens Pawn Opening ",
    "Queen's Pawn Opening ",
    "Queens Pawn Game ",
    "Queen's Pawn Game ",
    "Kings Pawn Opening ",
    "King's Pawn Opening ",
    "Kings Pawn Game ",
    "King's Pawn Game ",
)


def _short_opening(name: Any, *, limit: int = 36) -> str:
    """Display form: drop the generic ECO prefix, then cap the rest."""

    s = " ".join(str(name or "").split())
    if not s:
        return "Unknown opening"
    for prefix in _GENERIC_OPENING_PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix) + 3:
            s = s[len(prefix):]
            break
    if len(s) <= limit:
        return s
    cut = s.rfind(" ", 0, limit)
    stem = s[:cut] if cut >= 14 else s[:limit]
    return stem.rstrip(" -:,") + "…"


def _narrative_is_current(narrative: Any) -> bool:
    """Older runs stored a different shape; the story UI cannot render it."""

    return isinstance(narrative, dict) and narrative.get("schema") == NARRATIVE_SCHEMA


def ensure_narrative(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """Attach ``narrative`` when missing or from an older schema. Pure; does not persist."""

    if metrics is None:
        return None
    if not _narrative_is_current(metrics.get("narrative")):
        metrics["narrative"] = build_narrative(metrics)
    return metrics

SHAPE_LABELS = {
    "converted_then_lost": "Winning, then blundered",
    "cliff": "Even, then one blunder",
    "scramble": "Lost on the clock",
    "never_in_it": "Never in the game",
    "bleed": "Lost slowly",
}

SHAPE_CAPTIONS = {
    "converted_then_lost": "You had the game, then gave it back.",
    "cliff": "The position was fine until a single move ended it.",
    "scramble": "The clock finished the game, not the opponent's ideas.",
    "never_in_it": "You were worse before the middlegame started.",
    "bleed": "No explosion — just a slow slide you never reversed.",
}

TACTIC_LABELS = {
    "fork": "forks",
    "pin": "pins",
    "skewer": "skewers",
    "deflection": "deflections",
    "discovered_attack": "discovered attacks",
    "back_rank": "back-rank mates",
    "overloaded_defender": "overloaded defenders",
    "zwischenzug": "in-between moves",
    "hanging_piece": "hanging pieces",
    "mate_threat": "mate threats",
    "trapped_piece": "trapped pieces",
}

PHASE_FIX = {
    "opening": "mistakes",
    "middlegame": "mistakes",
    "endgame": "defense",
}

BANNED_JARGON = (
    "Δw",
    "delta_w",
    "volatility",
    "findability",
    "expectation-adjusted",
    "win% / game",
    "win%/move",
    "win% per",
)


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{round(float(value) * 100)}%"


def build_narrative(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Compose the story payload from an already-computed metrics blob."""

    metrics = metrics or {}
    facts = list(metrics.get("game_explorer") or [])
    pro = metrics.get("pro") or {}
    headline = pro.get("headline") or {}
    record = headline.get("record") or {}
    leaks = list(pro.get("leaks") or [])
    strengths = list(pro.get("strengths") or [])
    openings = pro.get("openings") or {}
    endgame = pro.get("endgame") or {}
    resilience = pro.get("resilience") or {}
    move_quality = pro.get("move_quality") or {}
    critical = pro.get("critical_moments") or {}
    scramble = metrics.get("time_scramble_decay") or {}
    missed = metrics.get("missed_tactics") or {}
    practice = metrics.get("practice_flags") or {}
    tax = (metrics.get("loss_taxonomy") or {}).get("counts") or {}

    games = int(record.get("games") or len(facts) or metrics.get("games") or 0)
    wins = int(record.get("wins") or 0)
    draws = int(record.get("draws") or 0)
    losses_n = int(record.get("losses") or 0)
    if not losses_n and facts:
        losses_n = sum(1 for f in facts if f.get("outcome") == "loss")
        wins = wins or sum(1 for f in facts if f.get("outcome") == "win")
        draws = draws or sum(1 for f in facts if f.get("outcome") == "draw")

    losses = [f for f in facts if f.get("outcome") == "loss"]
    sufficiency = _sufficiency(games, losses_n)

    shapes = _loss_shapes(losses, tax)
    funnel = _funnel(games, losses, shapes, leaks, openings)
    opening_split = _opening_split(openings, facts)
    tactics = _tactics(missed, facts, practice)
    habits = _habits(
        leaks,
        scramble=scramble,
        critical=critical,
        endgame=endgame,
        resilience=resilience,
        move_quality=move_quality,
        facts=facts,
    )
    phase = _phase(move_quality, endgame)
    moments = _moments(facts, practice, losses)
    twins = _twin_games(facts)
    spine = _spine(facts)
    not_the_reason = _not_the_reason(
        facts, openings, move_quality, endgame, resilience, leaks, shapes
    )
    verdict = _verdict(
        games=games,
        wins=wins,
        draws=draws,
        losses_n=losses_n,
        losses=losses,
        facts=facts,
        leaks=leaks,
        shapes=shapes,
        openings=openings,
        headline=headline,
        not_the_reason=not_the_reason,
        sufficiency=sufficiency,
    )
    how = _how_you_win(leaks, strengths, moments, practice, phase, tactics, habits)

    return {
        "schema": NARRATIVE_SCHEMA,
        "sufficiency": sufficiency,
        "verdict": verdict,
        "why_you_lose": {
            "funnel": funnel,
            "shapes": shapes,
            "openings": opening_split,
            "tactics": tactics,
            "habits": habits,
            "phase": phase,
            "moments": moments[:8],
            "twins": twins,
        },
        "how_you_win": how,
        "spine": spine,
        "ordering": ["verdict", "why", "how", "deep-dive"],
    }


def _sufficiency(games: int, losses: int) -> dict[str, Any]:
    if games < MIN_GAMES:
        return {
            "ok": False,
            "games": games,
            "losses": losses,
            "reason": (
                f"Need at least {MIN_GAMES} analyzed games to tell this story "
                f"({games} so far)."
            ),
        }
    if losses < MIN_LOSSES:
        return {
            "ok": False,
            "games": games,
            "losses": losses,
            "reason": (
                f"Only {losses} loss{'es' if losses != 1 else ''} in this window — "
                "not enough to name a pattern yet."
            ),
        }
    return {"ok": True, "games": games, "losses": losses, "reason": None}


def _shape_of(fact: dict[str, Any]) -> str:
    labelled = fact.get("loss_type")
    if labelled in SHAPE_LABELS:
        return str(labelled)
    peak = fact.get("peak_win_prob")
    total = float(fact.get("total_delta_w") or 0.0) or 1.0
    scramble_share = float(fact.get("scramble_delta_w") or 0.0) / total
    if peak is not None and peak > 0.8:
        return "converted_then_lost"
    if scramble_share > 0.5:
        return "scramble"
    if peak is not None and peak < 0.35:
        return "never_in_it"
    if int(fact.get("blunders") or 0) >= 1 and (peak or 0.5) >= 0.35:
        return "cliff"
    return "bleed"


def _loss_shapes(
    losses: list[dict[str, Any]], tax: dict[str, Any]
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    if losses:
        for f in losses:
            counts[_shape_of(f)] += 1
    elif tax:
        for key, n in tax.items():
            if key in SHAPE_LABELS:
                counts[str(key)] += int(n or 0)
    total = sum(counts.values()) or 1
    rows = []
    for key, label in SHAPE_LABELS.items():
        n = int(counts.get(key) or 0)
        if n <= 0:
            continue
        rows.append({
            "id": key,
            "label": label,
            "caption": SHAPE_CAPTIONS[key],
            "n": n,
            "share": round(n / total, 3),
        })
    rows.sort(key=lambda r: -r["n"])
    return rows


def _funnel(
    games: int,
    losses: list[dict[str, Any]],
    shapes: list[dict[str, Any]],
    leaks: list[dict[str, Any]],
    openings: dict[str, Any],
) -> list[dict[str, Any]]:
    stages = [{
        "id": "games",
        "label": "Games",
        "n": games,
        "caption": f"{games} analyzed in this window.",
    }]
    n_loss = len(losses)
    if n_loss:
        stages.append({
            "id": "losses",
            "label": "Losses",
            "n": n_loss,
            "caption": f"{n_loss} of {games} games ended as losses.",
        })

    were_winning = [
        f for f in losses
        if (f.get("peak_win_prob") or 0) > WINNING_WIN_PROB
    ]
    top_shape = shapes[0] if shapes else None

    if len(were_winning) >= MIN_STAGE_N and len(were_winning) >= 0.35 * max(1, n_loss):
        stages.append({
            "id": "were_winning",
            "label": "Were winning",
            "n": len(were_winning),
            "caption": (
                f"{len(were_winning)} of those losses reached a winning position "
                "and still went the other way."
            ),
        })
        died = _mechanism_among(were_winning)
        if died and died["n"] >= MIN_STAGE_N:
            stages.append(died)
    elif top_shape and top_shape["n"] >= MIN_STAGE_N:
        stages.append({
            "id": top_shape["id"],
            "label": top_shape["label"],
            "n": top_shape["n"],
            "caption": top_shape["caption"],
        })

    # A funnel that widens is a chart of nothing. Only keep a narrower claim.
    last_n = stages[-1]["n"]
    extra: dict[str, Any] | None = None
    worst = openings.get("worst") or {}
    if (
        worst.get("n", 0) >= MIN_STAGE_N
        and worst.get("score_pct") is not None
        and worst["score_pct"] < 0.4
        and stages[-1]["id"] != "opening"
        and int(worst["n"]) < last_n
    ):
        color = "White" if worst.get("color") == "white" else "Black"
        extra = {
            "id": "opening",
            "label": _short_opening(worst.get("opening") or "One opening", limit=28),
            "n": int(worst["n"]),
            "caption": (
                f"{_pct(1 - worst['score_pct'])} of your {color} games in "
                f"{_short_opening(worst['opening'])} did not end in a win."
            ),
        }
    if extra:
        stages.append(extra)

    # Stop narrowing to anecdotes.
    trimmed: list[dict[str, Any]] = []
    for stage in stages:
        if trimmed and stage["n"] < MIN_STAGE_N and stage["id"] not in ("games", "losses"):
            break
        trimmed.append(stage)
    return trimmed


def _mechanism_among(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    counts: dict[str, int] = defaultdict(int)
    for f in facts:
        counts[_shape_of(f)] += 1
    if not counts:
        return None
    key = max(counts, key=lambda k: counts[k])
    n = counts[key]
    return {
        "id": key,
        "label": SHAPE_LABELS.get(key, key),
        "n": n,
        "caption": SHAPE_CAPTIONS.get(key, ""),
    }


def _opening_split(
    openings: dict[str, Any], facts: list[dict[str, Any]]
) -> dict[str, Any]:
    def _rows(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in raw:
            n = int(r.get("n") or 0)
            if n < 2:
                continue
            score = r.get("score_pct")
            losses = _opening_losses(facts, r.get("opening"), r.get("color"))
            out.append({
                "opening": r.get("opening") or "Unknown opening",
                "eco": r.get("eco") or "",
                "color": r.get("color"),
                "n": n,
                "losses": losses,
                "loss_pct": round(losses / n, 3) if n else 0.0,
                "score_pct": score,
            })
        out.sort(key=lambda r: (-r["loss_pct"], -r["n"]))
        return out[:8]

    white = _rows(list(openings.get("as_white") or []))
    black = _rows(list(openings.get("as_black") or []))
    worst = None
    pool = white + black
    if pool:
        worst = max(pool, key=lambda r: (r["loss_pct"], r["n"]))
    return {"white": white, "black": black, "worst": worst}


def _opening_losses(facts: list[dict[str, Any]], name: Any, color: Any) -> int:
    return sum(
        1
        for f in facts
        if f.get("opening_name") == name
        and f.get("user_color") == color
        and f.get("outcome") == "loss"
    )


def _tactics(
    missed: dict[str, Any],
    facts: list[dict[str, Any]],
    practice: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for tag_row in list(missed.get("tags") or [])[:5]:
        n = int(tag_row.get("n") or 0)
        if n < 2:
            continue
        tag = str(tag_row.get("tag") or "")
        label = TACTIC_LABELS.get(tag, tag.replace("_", " ") + "s")
        moment = _moment_matching(facts, practice, tag=tag)
        rows.append({
            "id": tag,
            "label": label,
            "n": n,
            "caption": f"You missed {label} {n} times in this window.",
            "moment": moment,
        })
    return rows


def _habits(
    leaks: list[dict[str, Any]],
    *,
    scramble: dict[str, Any],
    critical: dict[str, Any],
    endgame: dict[str, Any],
    resilience: dict[str, Any],
    move_quality: dict[str, Any],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    habits: list[dict[str, Any]] = []
    leak_ids = {l.get("id") for l in leaks}

    if "clock" in leak_ids or "time_allocation" in leak_ids:
        buckets = scramble.get("buckets") or []
        scr = next((b for b in buckets if b.get("key") == "scramble"), None)
        n = int((scr or {}).get("moves") or 0)
        habits.append({
            "id": "clock",
            "title": "Time pressure",
            "caption": (
                "With under ten seconds you start dropping games you had already won."
                if "clock" in leak_ids
                else "You spend your thinking time on quiet moves and rush the ones that decide the game."
            ),
            "n": n,
        })
    if "critical" in leak_ids:
        bucket = next(
            (b for b in (critical.get("buckets") or []) if b.get("key") == "critical"),
            None,
        )
        habits.append({
            "id": "sharp",
            "title": "Sharp positions",
            "caption": "When the position gets sharp, your accuracy falls off.",
            "n": int((bucket or {}).get("moves") or 0),
        })
    if "conversion" in leak_ids:
        conv = (resilience.get("conversion") or {})
        habits.append({
            "id": "conversion",
            "title": "Winning positions slip",
            "caption": (
                f"You reached a winning position in {conv.get('n') or 0} games "
                "and did not convert enough of them."
            ),
            "n": int(conv.get("n") or 0),
        })
    if "tilt" in leak_ids:
        habits.append({
            "id": "tilt",
            "title": "The game after a loss",
            "caption": "The game immediately after a defeat is where the next one leaks.",
            "n": None,
        })
    if "phase" in leak_ids:
        phases = move_quality.get("by_phase") or []
        if phases:
            worst = max(phases, key=lambda p: p.get("delta_w_per_move") or 0)
            name = str(worst.get("phase") or "middlegame")
            habits.append({
                "id": "phase",
                "title": f"{name.capitalize()} is the weak phase",
                "caption": _phase_habit_caption(name, facts),
                "n": int(worst.get("moves") or 0),
            })

    reached = int(endgame.get("reached") or 0)
    entry = endgame.get("entry") or []
    losing_entry = next((e for e in entry if e.get("key") == "losing"), None)
    if reached >= 5 and losing_entry and (losing_entry.get("score_pct") or 1) < 0.25:
        habits.append({
            "id": "endgame-entry",
            "title": "Arriving in worse endgames",
            "caption": "By the time the endgame starts you are already worse — the ending is not the leak, the approach is.",
            "n": int(losing_entry.get("n") or 0),
        })

    return habits[:5]


def _phase_habit_caption(phase: str, facts: list[dict[str, Any]]) -> str:
    if phase == "opening":
        return "You are being outplayed before the game has really started."
    if phase == "endgame":
        n = sum(1 for f in facts if f.get("outcome") == "loss" and (f.get("phase_moves") or {}).get("endgame"))
        return (
            f"Endgames are where {n} of these losses were decided."
            if n
            else "Simplified positions are costing you points you should keep."
        )
    return "The middlegame is where plans go missing and tactics decide the game."


def _phase(move_quality: dict[str, Any], endgame: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for p in move_quality.get("by_phase") or []:
        rows.append({
            "id": p.get("phase"),
            "label": str(p.get("phase") or "").capitalize(),
            "accuracy": p.get("accuracy"),
            "moves": p.get("moves"),
        })
    worst = None
    if len(rows) >= 2:
        scored = [r for r in rows if r.get("accuracy") is not None]
        if scored:
            worst = min(scored, key=lambda r: r["accuracy"])
    caption = None
    if worst:
        caption = {
            "opening": "The opening is your weakest phase — you are leaving book into positions you do not handle.",
            "middlegame": "The middlegame is your weakest phase — this is where tactics and plans are leaking.",
            "endgame": "The endgame is your weakest phase — conversions and technical wins are slipping.",
        }.get(str(worst["id"]), f"{worst['label']} is your weakest phase.")
    return {
        "rows": rows,
        "worst": worst,
        "caption": caption,
        "endgame_reached": endgame.get("reached"),
        "endgame_reach_rate": endgame.get("reach_rate"),
    }


def _moment_from_fact(fact: dict[str, Any], *, caption: str | None = None) -> dict[str, Any] | None:
    miss = fact.get("biggest_miss") or {}
    fen = miss.get("fen")
    if not fen:
        return None
    san = miss.get("san") or ""
    best = miss.get("best_san") or ""
    opening = fact.get("opening_name") or ""
    opp = fact.get("opponent") or "Opponent"
    auto = caption or (
        f"You played {san} against {opp}"
        + (f" in the {opening}" if opening else "")
        + (f" — {best} was the move." if best else ".")
    )
    return {
        "fen": fen,
        "san": san,
        "best_san": best,
        "best_uci": miss.get("best_uci"),
        "played_uci": miss.get("move_uci"),
        # Carried so "Open game" can land on the move, not the first ply.
        "ply": miss.get("ply"),
        "game_id": fact.get("game_id"),
        "review_id": fact.get("review_id"),
        "user_color": fact.get("user_color"),
        "opponent": opp,
        "opening": opening,
        "outcome": fact.get("outcome"),
        "sparkline": fact.get("sparkline") or [],
        "caption": auto,
    }


def _moment_from_practice(item: dict[str, Any], fact: dict[str, Any] | None) -> dict[str, Any] | None:
    fen = item.get("fen")
    if not fen:
        return None
    san = item.get("san") or ""
    fact = fact or {}
    return {
        "fen": fen,
        "san": san,
        "best_san": (fact.get("biggest_miss") or {}).get("best_san") or "",
        "best_uci": item.get("best_uci"),
        "played_uci": item.get("move_uci"),
        "ply": item.get("ply"),
        "game_id": item.get("game_id") or fact.get("game_id"),
        "review_id": item.get("review_id") or fact.get("review_id"),
        "user_color": item.get("user_color") or fact.get("user_color"),
        "opponent": item.get("opponent") or fact.get("opponent") or "Opponent",
        "opening": fact.get("opening_name") or "",
        "outcome": fact.get("outcome"),
        "sparkline": fact.get("sparkline") or [],
        "caption": f"You played {san}." if san else "A miss from your own games.",
    }


def _moments(
    facts: list[dict[str, Any]],
    practice: dict[str, Any],
    losses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_review = {f.get("review_id"): f for f in facts}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in list(practice.get("items") or []):
        fact = by_review.get(item.get("review_id"))
        moment = _moment_from_practice(item, fact)
        if not moment:
            continue
        key = f"{moment['game_id']}:{moment.get('played_uci')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(moment)
        if len(out) >= 6:
            break

    if len(out) < 4:
        ranked = sorted(
            losses,
            key=lambda f: float((f.get("biggest_miss") or {}).get("delta_w") or 0),
            reverse=True,
        )
        for fact in ranked:
            moment = _moment_from_fact(fact)
            if not moment:
                continue
            key = f"{moment['game_id']}:{moment.get('played_uci')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(moment)
            if len(out) >= 8:
                break
    return out


def _moment_matching(
    facts: list[dict[str, Any]],
    practice: dict[str, Any],
    *,
    tag: str,
) -> dict[str, Any] | None:
    # Practice flags do not carry tactic tags; use the costliest loss as stand-in.
    del tag
    losses = [f for f in facts if f.get("outcome") == "loss"]
    for fact in sorted(
        losses,
        key=lambda f: float((f.get("biggest_miss") or {}).get("delta_w") or 0),
        reverse=True,
    ):
        moment = _moment_from_fact(fact)
        if moment:
            return moment
    items = list(practice.get("items") or [])
    if items:
        by_review = {f.get("review_id"): f for f in facts}
        return _moment_from_practice(items[0], by_review.get(items[0].get("review_id")))
    return None


def _twin_games(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    wins = [f for f in facts if f.get("outcome") == "win"]
    losses = [f for f in facts if f.get("outcome") == "loss"]
    best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
    for loss in losses:
        for win in wins:
            if win.get("opening_name") != loss.get("opening_name"):
                continue
            if win.get("user_color") != loss.get("user_color"):
                continue
            gap = abs(int(win.get("ply_count") or 0) - int(loss.get("ply_count") or 0))
            if best is None or gap < best[0]:
                best = (gap, win, loss)
    if best is None:
        return None
    _, win, loss = best
    win_m = _moment_from_fact(
        win,
        caption=f"Same opening as White." if win.get("user_color") == "white"
        else "Same opening as Black.",
    )
    loss_m = _moment_from_fact(loss)
    if not win_m or not loss_m:
        return None
    color = "White" if win.get("user_color") == "white" else "Black"
    return {
        "opening": win.get("opening_name"),
        "color": win.get("user_color"),
        "caption": (
            f"Same line as {color} — one win, one loss. "
            "The difference is the moment, not the opening."
        ),
        "win": win_m,
        "loss": loss_m,
    }


def _user_curve(fact: dict[str, Any]) -> list[float]:
    raw = [float(v) for v in (fact.get("sparkline") or []) if v is not None]
    if fact.get("user_color") == "black":
        return [1.0 - v for v in raw]
    return raw


def _mean_curve(series: list[list[float]]) -> list[float]:
    usable = [s for s in series if len(s) >= 2]
    if not usable:
        return []
    out: list[float] = []
    last = SPINE_POINTS - 1
    for i in range(SPINE_POINTS):
        vals: list[float] = []
        for s in usable:
            t = (i / last) * (len(s) - 1)
            lo = int(t)
            hi = min(lo + 1, len(s) - 1)
            frac = t - lo
            vals.append(s[lo] * (1 - frac) + s[hi] * frac)
        out.append(round(sum(vals) / len(vals), 4))
    return out


def _spine(facts: list[dict[str, Any]]) -> dict[str, Any]:
    all_c = [_user_curve(f) for f in facts]
    loss_c = [_user_curve(f) for f in facts if f.get("outcome") == "loss"]
    win_c = [_user_curve(f) for f in facts if f.get("outcome") == "win"]
    return {
        "all": _mean_curve(all_c),
        "losses": _mean_curve(loss_c),
        "wins": _mean_curve(win_c),
    }


def _not_the_reason(
    facts: list[dict[str, Any]],
    openings: dict[str, Any],
    move_quality: dict[str, Any],
    endgame: dict[str, Any],
    resilience: dict[str, Any],
    leaks: list[dict[str, Any]],
    shapes: list[dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    leak_ids = {l.get("id") for l in leaks}

    white = [f for f in facts if f.get("user_color") == "white" and f.get("points") is not None]
    black = [f for f in facts if f.get("user_color") == "black" and f.get("points") is not None]
    if white and black:
        w_score = sum(f["points"] for f in white) / len(white)
        b_score = sum(f["points"] for f in black) / len(black)
        if w_score >= 0.5 and b_score < w_score - 0.08:
            out.append("Your White repertoire is not the problem.")
        elif b_score >= 0.5 and w_score < b_score - 0.08:
            out.append("Your Black repertoire is not the problem.")

    phases = [p for p in (move_quality.get("by_phase") or []) if p.get("accuracy") is not None]
    if len(phases) >= 2:
        best = max(phases, key=lambda p: p["accuracy"])
        worst = min(phases, key=lambda p: p["accuracy"])
        if best["phase"] == "endgame" and best["phase"] != worst["phase"]:
            out.append("Your endgames are fine.")
        if best["phase"] == "opening" and "opening" not in leak_ids:
            out.append("You are not getting outplayed in the opening.")

    conv = resilience.get("conversion") or {}
    if conv.get("n", 0) >= 3 and (conv.get("score_pct") or 0) >= 0.85:
        out.append("You convert the winning positions you reach.")

    if "clock" not in leak_ids and "time_allocation" not in leak_ids:
        out.append("Time trouble is not the main leak.")

    if shapes and shapes[0]["id"] != "never_in_it":
        never = next((s for s in shapes if s["id"] == "never_in_it"), None)
        if never and never["share"] < 0.2:
            out.append("You are not getting blown off the board in the opening.")

    # Deduplicate, cap.
    seen: set[str] = set()
    uniq = []
    for line in out:
        if line not in seen:
            seen.add(line)
            uniq.append(line)
    return uniq[:4]


def _verdict(
    *,
    games: int,
    wins: int,
    draws: int,
    losses_n: int,
    losses: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    leaks: list[dict[str, Any]],
    shapes: list[dict[str, Any]],
    openings: dict[str, Any],
    headline: dict[str, Any],
    not_the_reason: list[str],
    sufficiency: dict[str, Any],
) -> dict[str, Any]:
    headline_text = (
        f"Of your last {games} games you lost {losses_n}."
        if games
        else "No games in this window yet."
    )
    diagnosis = _diagnosis(leaks, shapes, openings, losses, facts)
    chips = _chips(facts, losses, openings, losses_n)
    elo = (headline.get("elo_left_on_board") or {}).get("points")
    not_lines = [
        line for line in not_the_reason
        if not _contradicts_diagnosis(line, diagnosis)
    ]
    return {
        "headline": headline_text,
        "diagnosis": diagnosis,
        "record": {
            "games": games,
            "wins": wins,
            "draws": draws,
            "losses": losses_n,
        },
        "chips": chips,
        "not_the_reason": not_lines,
        "elo_left": elo,
        "sufficiency": sufficiency,
    }


def _chips(
    facts: list[dict[str, Any]],
    losses: list[dict[str, Any]],
    openings: dict[str, Any],
    losses_n: int,
) -> list[dict[str, str]]:
    chips: list[dict[str, str]] = []
    black_losses = sum(1 for f in losses if f.get("user_color") == "black")
    white_losses = sum(1 for f in losses if f.get("user_color") == "white")
    if losses_n:
        if black_losses >= white_losses:
            chips.append({
                "label": "as Black",
                "value": f"{black_losses} of {losses_n} losses",
            })
        else:
            chips.append({
                "label": "as White",
                "value": f"{white_losses} of {losses_n} losses",
            })
    worst = (openings.get("worst") or {})
    split = _opening_split(openings, facts).get("worst")
    hot = split or (
        {
            "opening": worst.get("opening"),
            "color": worst.get("color"),
            "loss_pct": 1 - (worst.get("score_pct") or 0.5),
            "n": worst.get("n"),
        }
        if worst.get("opening")
        else None
    )
    if hot and hot.get("opening") and int(hot.get("n") or 0) >= 5:
        color = "Black" if hot.get("color") == "black" else "White"
        rate = hot.get("loss_pct")
        chips.append({
            "label": f"Worst as {color}",
            "value": (
                f"{_short_opening(hot['opening'], limit=28)}"
                + (f" · {_pct(rate)} lost" if rate is not None else "")
            ),
        })
    return chips[:3]


def _diagnosis(
    leaks: list[dict[str, Any]],
    shapes: list[dict[str, Any]],
    openings: dict[str, Any],
    losses: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> str:
    leak_ids = [l.get("id") for l in leaks]
    top_shape = shapes[0]["id"] if shapes else None
    worst = openings.get("worst") or {}
    opening_name = _short_opening(worst.get("opening")) if worst.get("opening") else None
    opening_color = "Black" if worst.get("color") == "black" else "White"

    if "conversion" in leak_ids and ("clock" in leak_ids or "time_allocation" in leak_ids):
        return (
            "You are not getting outplayed from the start. "
            "You are winning positions, then hanging them in time trouble."
        )
    if "conversion" in leak_ids:
        return (
            "You reach winning positions and let them slip. "
            "The leak is conversion, not the opening."
        )
    if top_shape == "never_in_it" and opening_name:
        return (
            f"You are getting punished out of the opening, especially as "
            f"{opening_color} in the {opening_name}."
        )
    if "tactic" in leak_ids:
        return _plain_leak_caption(next(l for l in leaks if l.get("id") == "tactic"))
    if "phase" in leak_ids:
        return _plain_leak_caption(next(l for l in leaks if l.get("id") == "phase"))
    if "critical" in leak_ids:
        return "Sharp positions are where the games are decided — and where you leak."
    if top_shape == "cliff":
        return "Games stay even, then one move ends them."
    if top_shape == "bleed":
        return "You are being ground down rather than blown off the board."
    if top_shape == "scramble":
        return "The clock is finishing games that were still alive on the board."
    if leaks:
        return _plain_leak_caption(leaks[0])
    if losses:
        black = sum(1 for f in losses if f.get("user_color") == "black")
        if black > len(losses) / 2:
            return "Most of the losses are with Black. The rest of this report names the pattern."
        return "The losses share a shape. Open Why you lose for the pattern."
    if facts:
        return "Not enough losses in this window to name a pattern."
    return "Generate a run to see why the games are going the other way."


def _contradicts_diagnosis(line: str, diagnosis: str) -> bool:
    d = (diagnosis or "").lower()
    t = (line or "").lower()
    if "white repertoire" in t and "as white" in d:
        return True
    if "black repertoire" in t and "as black" in d:
        return True
    if "not getting outplayed in the opening" in t and "out of the opening" in d:
        return True
    return False


def _plain_leak_title(leak: dict[str, Any]) -> str:
    leak_id = leak.get("id")
    title = str(leak.get("title") or "A repeating leak")
    mapping = {
        "phase": title,  # already "Endgame is your weakest phase"
        "critical": "Accuracy drops in sharp positions",
        "clock": "Time scrambles cost you games",
        "time_allocation": "Thinking time is spent on the wrong moves",
        "conversion": "Winning positions slip away",
        "opening": title,
        "tilt": "The game after a loss is worse",
        "tactic": title.replace("You keep missing", "Missed"),
        "game_window": title,
    }
    return _strip_jargon(mapping.get(leak_id, title))


def _plain_leak_caption(leak: dict[str, Any]) -> str:
    leak_id = leak.get("id")
    evidence = leak.get("evidence") or {}
    if leak_id == "phase":
        phase = str(evidence.get("phase") or "middlegame")
        return {
            "opening": "The opening is your weakest phase — you leave book into positions you do not handle.",
            "middlegame": "The middlegame is your weakest phase. Tactics and plans are leaking there.",
            "endgame": "The endgame is your weakest phase. Technical wins and conversions are slipping.",
        }.get(phase, f"{phase.capitalize()} is your weakest phase.")
    if leak_id == "critical":
        return "On the moves that actually decide games, your accuracy falls off."
    if leak_id == "clock":
        return "Under ten seconds you start dropping games that were still holdable."
    if leak_id == "time_allocation":
        return "You think longest on quiet moves and rush the ones that decide the game."
    if leak_id == "conversion":
        n = evidence.get("games")
        return (
            f"You reached a winning position in {n} games and did not convert enough of them."
            if n
            else "Winning positions are slipping away."
        )
    if leak_id == "opening":
        name = _short_opening(evidence.get("opening") or "one opening")
        color = "Black" if evidence.get("color") == "black" else "White"
        return f"{name} as {color} is losing you games."
    if leak_id == "tilt":
        return "The game immediately after a defeat is where the next loss starts."
    if leak_id == "tactic":
        tag = str(evidence.get("tag") or "")
        label = TACTIC_LABELS.get(tag, tag.replace("_", " ") + "s")
        n = evidence.get("n")
        return (
            f"You keep missing {label}"
            + (f" — {n} times in this window." if n else ".")
        )
    if leak_id == "game_window":
        return str(leak.get("title") or "One stretch of the game is where it breaks.")
    return _strip_jargon(str(leak.get("title") or "A repeating leak is costing you games."))


def _strip_jargon(text: str) -> str:
    out = text
    replacements = (
        ("Δw", "winning chances"),
        ("delta_w", "winning chances"),
        ("win% per move", "winning chances each move"),
        ("win%/move", "winning chances each move"),
        ("win% / game", "points per game"),
        ("findability", "how obvious the move was"),
        ("volatility", "sharpness"),
        ("expectation-adjusted", ""),
        ("V ≥ 60", "sharp positions"),
        ("V >= 60", "sharp positions"),
    )
    for src, dst in replacements:
        out = out.replace(src, dst)
    return " ".join(out.split())


def _how_you_win(
    leaks: list[dict[str, Any]],
    strengths: list[dict[str, Any]],
    moments: list[dict[str, Any]],
    practice: dict[str, Any],
    phase: dict[str, Any],
    tactics: list[dict[str, Any]],
    habits: list[dict[str, Any]],
) -> dict[str, Any]:
    n_flags = int(practice.get("count") or len(practice.get("items") or []) or 0)
    fixes: list[dict[str, Any]] = []
    used: set[str] = set()
    pool = [m for m in moments if m.get("fen")]
    cursor = 0

    for leak in leaks[:3]:
        leak_id = str(leak.get("id") or "")
        if leak_id in used:
            continue
        used.add(leak_id)
        practice_kind = leak.get("practice") or "mistakes"
        if leak_id == "phase":
            worst = (phase.get("worst") or {}).get("id")
            practice_kind = PHASE_FIX.get(str(worst or ""), "mistakes")
        related = pool[cursor:cursor + 2]
        cursor += len(related)
        if leak_id == "tactic" and tactics and tactics[0].get("moment"):
            related = [tactics[0]["moment"]] + [m for m in related if m is not tactics[0]["moment"]]
            related = related[:2]
        n = n_flags if practice_kind == "mistakes" else (related and len(related)) or n_flags
        fixes.append({
            "id": leak_id,
            "title": _fix_title(leak),
            "why": _plain_leak_caption(leak),
            "promise": _fix_promise(leak, n, tactics, habits),
            "practice": practice_kind,
            "n": n,
            "moments": related,
        })

    if not fixes and moments:
        fixes.append({
            "id": "mistakes",
            "title": "Drill the misses from your own games",
            "why": "The cheapest rating is the tactic you have already seen and missed.",
            "promise": (
                f"These {len(moments)} positions are from your games. Twenty minutes here beats another opening video."
            ),
            "practice": "mistakes",
            "n": len(moments),
            "moments": moments[:3],
        })

    keep = []
    for s in strengths[:4]:
        keep.append({
            "title": _strip_jargon(str(s.get("title") or "")),
            "detail": _strip_jargon(str(s.get("detail") or "")),
        })
    return {"fixes": fixes[:3], "strengths": keep}


def _fix_title(leak: dict[str, Any]) -> str:
    leak_id = leak.get("id")
    mapping = {
        "clock": "Play with a clock floor",
        "time_allocation": "Think on the moves that decide the game",
        "conversion": "Convert the wins you already have",
        "critical": "Train the sharp positions you actually reach",
        "phase": "Fix the weak phase first",
        "opening": "Stop repeating the opening that is losing",
        "tilt": "Break the session after a loss",
        "tactic": "Drill the motif you keep missing",
        "game_window": "Slow down in the stretch where games break",
    }
    return mapping.get(leak_id, "Drill the misses from your own games")


def _fix_promise(
    leak: dict[str, Any],
    n: int,
    tactics: list[dict[str, Any]],
    habits: list[dict[str, Any]],
) -> str:
    leak_id = leak.get("id")
    if leak_id == "tactic" and tactics:
        label = tactics[0]["label"]
        count = tactics[0]["n"]
        return f"These {count} {label} from your own games are the cheapest rating you can buy this week."
    # Only the clock leak routes to Forced Lines; promising forced-line drills
    # for time_allocation contradicted the button, which goes to Your Mistakes.
    if leak_id == "clock":
        return "Forced-line drills teach you to move in the positions you currently flag."
    if leak_id == "time_allocation":
        return "Sit on these positions with the clock off until spending the time feels automatic."
    if leak_id == "conversion":
        return "Defense gym on the winning positions you already reached — keep the point you had."
    # Every remaining leak used to share one sentence, so two of the three
    # fixes on the page read identically. Each says what its own set is.
    if leak_id == "phase":
        return f"These {n} positions are from the part of the game you actually lose in."
    if leak_id == "critical":
        return f"These {n} positions are the ones that decided the game, and nothing else."
    if leak_id == "opening":
        return "Replay your own games in that line and fix the move where the position turns."
    if leak_id == "tilt":
        return "One rule, no drilling: after a loss, the session is over."
    if leak_id == "game_window":
        return f"These {n} positions sit in the stretch of the game where your results break."
    if n:
        return f"These {n} positions are from your games. Twenty minutes here beats another opening video."
    del habits
    return "Practice the positions you actually miss, not engine trivia."


def user_facing_strings(narrative: dict[str, Any]) -> list[str]:
    """Strings a player can read — used by the jargon guard."""

    out: list[str] = []

    def take(value: Any, keys: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in keys and isinstance(v, str):
                    out.append(v)
                else:
                    take(v, keys)
        elif isinstance(value, list):
            for item in value:
                take(item, keys)

    take(
        narrative,
        (
            "headline",
            "diagnosis",
            "caption",
            "title",
            "why",
            "promise",
            "label",
            "reason",
            "detail",
            "value",
        ),
    )
    out.extend(narrative.get("verdict", {}).get("not_the_reason") or [])
    return [s for s in out if s]


def contains_jargon(narrative: dict[str, Any]) -> list[str]:
    hits = []
    for text in user_facing_strings(narrative):
        lower = text.lower()
        for token in BANNED_JARGON:
            if token.lower() in lower or token in text:
                hits.append(f"{token!r} in {text!r}")
    return hits
