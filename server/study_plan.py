"""The study plan: what to actually do, for how long, and what it is worth.

Every other Insights surface answers "what happened". This one answers "what do
I do on Tuesday". It is pure over an already-computed metrics blob — the same
``pro`` / ``narrative`` / ``game_explorer`` contract the dashboard and the film
read, so the plan can never disagree with the tables behind it — and it takes no
engine, no network and no database.

Three things about it are load-bearing:

**Blocks are built from evidence or not at all.** A block that cannot name the
opening, the motif, the window or the number behind it is dropped rather than
padded with generic advice. A 6-game run gets three blocks; a 200-game run gets
seven. There is no fallback copy telling somebody to "work on tactics".

**Hours are a budget, not a wish list.** The caller says how many hours a week
they have; blocks are allocated in proportion to what they cost the player, with
a floor, and blocks that do not fit are dropped. Reordering the plan cannot
create time.

**The rating projection is a model with stated assumptions, and it is capped by
a measurement.** ``pro.headline.elo_left_on_board`` already converts the win
chances a player demonstrably threw away into rating points via the FIDE
score-to-difference curve. That is the ceiling: recovering *everything* findable.
This module claims a fraction of it per block, converts the total through the
same curve (so there is one curve in the codebase), and caps it at that ceiling —
because leaks overlap by construction and must never be summed into a number
bigger than the loss actually observed.

Voice follows ``insights_narrative``: no Δw, no volatility, no findability in
anything a player reads. The numbers still reach the UI, as labelled fields in
each block's ``evidence``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from server.game_shape import CENTRE_LABELS, ENDGAME_LABELS
from server.insights_pro import rating_difference

PLAN_SCHEMA = "plan-1"

DEFAULT_HOURS_PER_WEEK = 5.0
DEFAULT_WEEKS = 4
MIN_HOURS_PER_WEEK = 1.0
MAX_HOURS_PER_WEEK = 20.0
MIN_WEEKS = 1
MAX_WEEKS = 12

#: A block needs this many games behind it before it is worth naming.
MIN_BUCKET_GAMES = 3
#: Minimum games in the run before a plan is worth building at all.
MIN_GAMES = 6
#: Blocks below this share of the study budget are dropped instead of shown as
#: a token fifteen minutes.
MIN_BLOCK_HOURS = 0.5
#: Nothing gets more than this share of the week — a plan that is 90% one drill
#: is not a plan.
MAX_BLOCK_SHARE = 0.45

BLUNDER_DELTA_W = 25.0

# ── The effort model ─────────────────────────────────────────────────────────
#
# PLACEHOLDERS. Nothing here is fitted — there is no dataset of "player did N
# hours of rook endings, gained M points". What *is* defensible is the shape and
# the ordering, and both are stated so a reader can disagree with a number
# rather than with a black box:
#
# * ``ceiling`` — the most of a measured leak that focused work can close. It is
#   never 1.0: a player who works on endgames still loses some endgames.
# * ``half_hours`` — hours of deliberate work to reach half that ceiling.
#   Recovery is ``ceiling · h / (h + half_hours)``, so it saturates. Doubling
#   the hours never doubles the gain, which is the honest shape.
#
# The ordering is the actual claim. Memorising a line you already reach is fast
# (openings: 5h). Recognising a motif you have already missed is fast-ish
# (tactics: 8h). Calculating better is slow and has a low ceiling (14h) —
# it is the thing a month does not fix. Habits are not on this curve at all.
STUDY_MODEL: dict[str, dict[str, float]] = {
    "openings": {"ceiling": 0.55, "half_hours": 5.0},
    "tactics": {"ceiling": 0.45, "half_hours": 8.0},
    "mistakes": {"ceiling": 0.40, "half_hours": 6.0},
    "endgames": {"ceiling": 0.45, "half_hours": 10.0},
    "middlegame": {"ceiling": 0.35, "half_hours": 12.0},
    "calculation": {"ceiling": 0.35, "half_hours": 14.0},
}

#: Habits cost no study time — they are rules applied while playing. They are
#: modelled on weeks rather than hours, because what they need is repetition
#: until automatic, and they reach their ceiling at about a month.
HABIT_MODEL: dict[str, float] = {
    "blunders": 0.35,
    "clock": 0.50,
    "mental": 0.60,
}
HABIT_WEEKS_TO_STICK = 4.0

# ── The pace bound ───────────────────────────────────────────────────────────
#
# The recoverable pool answers "how much did you throw away", which on a bad
# window is enormous — a run of 24 games with a blunder in most of them says
# nearly 200 rating points are on the table. That is true and it is not a
# four-week forecast. Nobody absorbs 200 points in a month, and a plan that
# promises it is worthless however good its arithmetic.
#
# So the projection carries a second cap: rating points per week a player can
# plausibly convert. It decays with rating (the same work buys less higher up)
# and scales with the hours actually committed. PLACEHOLDER, like the effort
# model — it is a sanity bound, not a measurement, and the payload says when it
# is the binding constraint so the UI can show the larger pool separately.
WEEKLY_GAIN_AT_1200 = 22.0
PACE_REFERENCE_RATING = 1200.0
PACE_FLOOR_RATING = 800.0
#: Hours a week the pace bound treats as full effort.
PACE_FULL_EFFORT_HOURS = 5.0


def _pace_ceiling(rating: float | None, weeks: int, total_hours: float) -> float:
    """Rating points this plan could plausibly deliver in the time given."""

    base = float(rating or PACE_REFERENCE_RATING)
    scale = (PACE_REFERENCE_RATING / max(PACE_FLOOR_RATING, base)) ** 1.5
    effort = min(1.0, (total_hours / max(1.0, weeks * PACE_FULL_EFFORT_HOURS)) ** 0.5)
    return WEEKLY_GAIN_AT_1200 * scale * weeks * max(0.35, effort)

BLOCK_ORDER = (
    "mistakes", "tactics", "openings", "middlegame",
    "endgames", "calculation", "blunders", "clock", "mental",
)


# ── Small helpers ────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _pct(value: float | None) -> str:
    """A score fraction as a percentage, for prose."""

    return "—" if value is None else f"{round(value * 100)}%"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _short_opening(name: Any, limit: int = 34) -> str:
    text = str(name or "").strip() or "Unknown opening"
    if ":" in text:
        text = text.split(":", 1)[0].strip() or text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _round_quarter(hours: float) -> float:
    return round(hours * 4) / 4


def _minutes(hours: float) -> int:
    return int(round(hours * 60))


def _study_recovery(block_id: str, hours: float) -> float:
    model = STUDY_MODEL.get(block_id)
    if not model or hours <= 0:
        return 0.0
    return model["ceiling"] * hours / (hours + model["half_hours"])


def _habit_recovery(block_id: str, weeks: int) -> float:
    ceiling = HABIT_MODEL.get(block_id)
    if ceiling is None:
        return 0.0
    return ceiling * min(1.0, weeks / HABIT_WEEKS_TO_STICK)


def _drill(
    kind: str,
    label: str,
    detail: str,
    *,
    minutes: int | None = None,
    route: str | None = None,
    game_id: str | None = None,
    ply: int | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """One thing to do. ``kind`` tells the UI how to render the hand-off.

    ``review`` carries a game and a ply so the button lands on the exact move;
    ``trainer`` carries an app route; ``rule`` and ``study`` are things done away
    from a button and carry neither.
    """

    return {
        "kind": kind,
        "label": label,
        "detail": detail,
        "minutes": minutes,
        "route": route,
        "game_id": game_id,
        "ply": ply,
        "count": count,
    }


def _block(
    block_id: str,
    *,
    chapter: str,
    title: str,
    why: str,
    impact: float,
    drills: list[dict[str, Any]],
    measure: str,
    evidence: dict[str, Any] | None = None,
    kind: str = "study",
) -> dict[str, Any]:
    return {
        "id": block_id,
        "kind": kind,
        "chapter": chapter,
        "title": title,
        "why": why,
        "impact": round(max(0.0, impact), 2),
        "drills": drills,
        "measure": measure,
        "evidence": evidence or {},
        # Filled in by the allocator once the budget is known.
        "hours_per_week": 0.0,
        "hours_total": 0.0,
        "recovery": 0.0,
        "rating_gain": None,
    }


# ── Bucketing over the per-game fact table ───────────────────────────────────


def _bucket(facts: list[dict[str, Any]], key: Any) -> dict[Any, dict[str, Any]]:
    """Group games by ``key(fact)`` and summarise each group.

    ``None`` keys are dropped: a bucket a player cannot name is a bucket they
    cannot study.
    """

    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for f in facts:
        k = key(f)
        if k is None:
            continue
        groups[k].append(f)

    out: dict[Any, dict[str, Any]] = {}
    for k, rows in groups.items():
        decided = [r for r in rows if r.get("points") is not None]
        score = sum(float(r["points"]) for r in decided)
        out[k] = {
            "key": k,
            "games": rows,
            "n": len(rows),
            "decided": len(decided),
            "score_pct": (score / len(decided)) if decided else None,
            "losses": sum(1 for r in decided if float(r["points"]) == 0.0),
        }
    return out


def _phase_rate(rows: list[dict[str, Any]], phase: str) -> float | None:
    """Win chances given away per move in one phase, across a set of games."""

    loss = sum(float((r.get("phase_delta_w") or {}).get(phase, 0.0)) for r in rows)
    moves = sum(int((r.get("phase_moves") or {}).get(phase, 0)) for r in rows)
    return (loss / moves) if moves else None


def _worst_games(rows: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    """The games to actually open: losses first, biggest single miss first."""

    def cost(r: dict[str, Any]) -> tuple[int, float]:
        lost = 1 if r.get("points") == 0.0 else 0
        miss = ((r.get("biggest_miss") or {}).get("delta_w")) or 0.0
        return (lost, float(miss))

    return sorted(rows, key=cost, reverse=True)[:limit]


def _review_drills(rows: list[dict[str, Any]], *, note: str, minutes: int = 15) -> list[dict[str, Any]]:
    """Hand-offs into Game Review, landed on the move that decided the game.

    Deduped by game: two flagged moves from one game are one thing to sit down
    and do, and listing it twice makes the plan look padded.
    """

    drills = []
    seen: set[Any] = set()
    for r in rows:
        if r.get("game_id") in seen:
            continue
        seen.add(r.get("game_id"))
        miss = r.get("biggest_miss") or {}
        opponent = str(r.get("opponent") or "opponent")
        move_no = miss.get("move_no")
        where = f"move {move_no}" if move_no else "the turning point"
        drills.append(_drill(
            "review",
            f"Replay your game vs {opponent}",
            f"{note} Opens on {where}, where the game turned.",
            minutes=minutes,
            game_id=r.get("game_id"),
            ply=miss.get("ply"),
        ))
    return drills


# ── Blocks ───────────────────────────────────────────────────────────────────


def _openings_block(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Named lines that are underwater, and the ones played often enough to matter.

    Two different problems share this block because they share the fix: a line
    you score badly in, and a line you score badly in *and play constantly*. The
    second is worth more per hour and is sorted to the top by the volume term.
    """

    total = len(facts)
    if total < MIN_BUCKET_GAMES:
        return None

    buckets = _bucket(
        facts,
        lambda f: (
            (f.get("user_color") or "white", str(f.get("opening_name") or ""))
            if str(f.get("opening_name") or "").strip()
            and "unknown" not in str(f.get("opening_name")).lower()
            else None
        ),
    )

    heavy = max(MIN_BUCKET_GAMES, round(total * 0.15))
    targets = []
    for b in buckets.values():
        score = b["score_pct"]
        if b["n"] < MIN_BUCKET_GAMES or score is None:
            continue
        # Either it is losing outright, or it is a staple that is not winning.
        if score >= 0.45 and not (b["n"] >= heavy and score < 0.5):
            continue
        share = b["n"] / total
        targets.append({
            **b,
            "share": share,
            "impact": (0.5 - score) * 100 * share,
            "opening_rate": _phase_rate(b["games"], "opening"),
        })
    if not targets:
        return None
    targets.sort(key=lambda t: -t["impact"])
    targets = targets[:3]

    lead = targets[0]
    color, name = lead["key"]
    short = _short_opening(name)
    deviations = [
        int(g["deviation_ply"]) for g in lead["games"] if g.get("deviation_ply")
    ]
    leave_move = round(sum(deviations) / len(deviations) / 2) if deviations else None

    share_note = (
        f" — {_pct(lead['share'])} of everything you play"
        if lead["share"] >= 0.15 else ""
    )
    why = (
        f"You score {_pct(lead['score_pct'])} in the {short} as {color} across "
        f"{_plural(lead['n'], 'game')}{share_note}. "
    )
    if leave_move:
        why += (
            f"Your own preparation runs out around move {leave_move}, and the "
            "position is already worse by the time you are thinking for yourself."
        )
    else:
        why += "The position is decided before the middlegame starts."

    drills: list[dict[str, Any]] = []
    for t in targets:
        t_color, t_name = t["key"]
        t_short = _short_opening(t_name)
        drills.extend(_review_drills(
            _worst_games(t["games"], 2),
            note=f"{t_short} as {t_color}.",
        ))
    if leave_move:
        drills.append(_drill(
            "study",
            f"Extend the {short} to move {leave_move + 4}",
            f"Take your own {_plural(lead['n'], 'game')} in this line, find the first move "
            f"that is yours rather than theory, and learn the four moves after it. "
            "Write the plan down in one sentence, not a tree.",
            minutes=30,
        ))
    drills.append(_drill(
        "trainer",
        "Rehearse the line against the engine",
        f"Play the {short} out from the starting position until you are past move "
        f"{(leave_move or 10) + 4} without thinking.",
        minutes=20,
        route="/training/playout",
    ))

    return _block(
        "openings",
        chapter="Openings",
        title=f"Fix the {short} before you play it again",
        why=why,
        impact=sum(t["impact"] for t in targets),
        drills=drills,
        measure=(
            f"Next run: the {short} as {color} scoring above "
            f"{_pct(min(0.5, (lead['score_pct'] or 0) + 0.2))}."
        ),
        evidence={
            "lines": [
                {
                    "opening": t["key"][1],
                    "color": t["key"][0],
                    "games": t["n"],
                    "score_pct": t["score_pct"],
                    "losses": t["losses"],
                    "share": t["share"],
                    "opening_loss_per_move": t["opening_rate"],
                    "game_ids": [g.get("game_id") for g in t["games"][:12]],
                }
                for t in targets
            ],
            "leave_book_move": leave_move,
        },
    )


def _middlegame_block(
    facts: list[dict[str, Any]],
    move_quality: dict[str, Any],
) -> dict[str, Any] | None:
    """Positions you reach fine and then have no idea what to do with.

    Two independent signatures, both measured over the player's own games:

    * **The drift.** A line where the opening is played *well* and the
      middlegame is played badly. That gap is what "no plan" looks like in the
      data — the theory carried them, and then it stopped.
    * **The centre.** Games bucketed by the pawn structure the middlegame was
      played in (``game_shape.centre_type``). A player who scores 60% in open
      positions and 25% in closed ones does not have a middlegame problem; they
      have a closed-position problem, which is a different reading list.

    Either alone is enough for a block; the copy names whichever fired.
    """

    total = len(facts)
    if total < MIN_BUCKET_GAMES:
        return None

    def named_line(f: dict[str, Any]) -> Any:
        name = str(f.get("opening_name") or "").strip()
        if not name or "unknown" in name.lower():
            return None
        return (f.get("user_color") or "white", name)

    drift = None
    for b in _bucket(facts, named_line).values():
        if b["n"] < MIN_BUCKET_GAMES or b["score_pct"] is None or b["score_pct"] >= 0.5:
            continue
        opening_rate = _phase_rate(b["games"], "opening")
        middle_rate = _phase_rate(b["games"], "middlegame")
        if opening_rate is None or middle_rate is None or middle_rate < 2.0:
            continue
        if middle_rate < max(1.5 * opening_rate, opening_rate + 1.0):
            continue
        cand = {**b, "opening_rate": opening_rate, "middle_rate": middle_rate}
        gap = cand["middle_rate"] - cand["opening_rate"]
        if drift is None or gap > drift["middle_rate"] - drift["opening_rate"]:
            drift = cand

    centres = {
        k: v for k, v in _bucket(facts, lambda f: f.get("centre")).items()
        if v["n"] >= MIN_BUCKET_GAMES and v["score_pct"] is not None
    }
    structure = None
    if len(centres) >= 2:
        ranked = sorted(centres.values(), key=lambda b: b["score_pct"])
        worst, best = ranked[0], ranked[-1]
        if best["score_pct"] - worst["score_pct"] >= 0.15:
            structure = {"worst": worst, "best": best}

    if drift is None and structure is None:
        return None

    phases = move_quality.get("by_phase") or []
    middle = next((p for p in phases if p.get("phase") == "middlegame"), None)
    others = [p for p in phases if p.get("phase") != "middlegame"]
    baseline = min((p["delta_w_per_move"] for p in others), default=None)

    parts: list[str] = []
    drills: list[dict[str, Any]] = []
    impact = 0.0

    if drift is not None:
        d_color, d_name = drift["key"]
        d_short = _short_opening(d_name)
        parts.append(
            f"In the {d_short} as {d_color} you play the opening better than the "
            f"middlegame that follows it, and score {_pct(drift['score_pct'])} over "
            f"{_plural(drift['n'], 'game')}. You get the position you wanted and then "
            "have nothing to do with it."
        )
        mid_moves = sum(
            int((g.get("phase_moves") or {}).get("middlegame", 0)) for g in drift["games"]
        ) / max(1, drift["n"])
        impact += (drift["middle_rate"] - drift["opening_rate"]) * mid_moves * (drift["n"] / total)
        drills.extend(_review_drills(
            _worst_games(drift["games"], 2),
            note="Stop at your last book move and write a plan down before reading on.",
            minutes=20,
        ))

    if structure is not None:
        w_key = structure["worst"]["key"]
        w_label = CENTRE_LABELS.get(w_key, str(w_key))
        b_label = CENTRE_LABELS.get(structure["best"]["key"], str(structure["best"]["key"]))
        parts.append(
            f"Split by pawn structure, you score {_pct(structure['worst']['score_pct'])} "
            f"in games with a {w_label} against {_pct(structure['best']['score_pct'])} "
            f"with a {b_label} — {_plural(structure['worst']['n'], 'game')} in the "
            "structure that does not suit you."
        )
        if middle and baseline is not None:
            impact += max(0.0, middle["delta_w_per_move"] - baseline) * (middle["moves"] / total) * 0.5
        drills.append(_drill(
            "study",
            f"Learn three plans for a {w_label}",
            "One plan per side of the board: the pawn break, the piece regroup, and "
            "the trade you want. Name all three before your next game in this structure.",
            minutes=30,
        ))
        drills.extend(_review_drills(
            _worst_games(structure["worst"]["games"], 2),
            note=f"A {w_label} you lost.",
            minutes=20,
        ))

    drills.append(_drill(
        "trainer",
        "Hold the balance from a quiet position",
        "Eval Hold starts you in a level middlegame and asks you to keep it level. "
        "It is the drill for positions with no forcing move in them.",
        minutes=20,
        route="/training/eval-hold",
    ))

    title = (
        f"Give your {CENTRE_LABELS.get(structure['worst']['key'], 'middlegame')} a plan"
        if structure is not None
        else "Have a plan for when the theory runs out"
    )
    return _block(
        "middlegame",
        chapter="Middlegame",
        title=title,
        why=" ".join(parts),
        impact=impact,
        drills=drills,
        measure="Next run: under 10 points between your best and worst structure.",
        evidence={
            "drift": (
                {
                    "opening": drift["key"][1],
                    "color": drift["key"][0],
                    "games": drift["n"],
                    "score_pct": drift["score_pct"],
                    "opening_loss_per_move": drift["opening_rate"],
                    "middlegame_loss_per_move": drift["middle_rate"],
                }
                if drift else None
            ),
            "structures": [
                {
                    "centre": b["key"],
                    "label": CENTRE_LABELS.get(b["key"], str(b["key"])),
                    "games": b["n"],
                    "score_pct": b["score_pct"],
                }
                for b in sorted(centres.values(), key=lambda x: -x["n"])
            ],
        },
    )


def _tactics_block(
    missed_tactics: dict[str, Any],
    practice: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The motifs you have already missed, by name and by count."""

    tags = [t for t in (missed_tactics.get("tags") or []) if int(t.get("n") or 0) >= 2]
    if not tags:
        return None
    tags = sorted(tags, key=lambda t: -(float(t.get("mean_delta_w") or 0) * int(t["n"])))[:3]
    lead = tags[0]
    label = str(lead["tag"]).replace("_", " ")
    total = max(1, len(facts))

    why = f"You missed {_plural(int(lead['n']), label)} in this window"
    if len(tags) > 1:
        rest = ", ".join(f"{str(t['tag']).replace('_', ' ')}s" for t in tags[1:])
        why += f", along with {rest}"
    why += (
        ". These are not positions you failed to calculate — they are patterns you "
        "did not see, and pattern recognition is the fastest thing on this page to fix."
    )

    items = [i for i in (practice.get("items") or []) if i.get("fen")]
    drills: list[dict[str, Any]] = [
        _drill(
            "trainer",
            "Your Mistakes, top of the set",
            "Every position in there is one you have already had on the board and got "
            "wrong. Do them until you get each one right twice.",
            minutes=25,
            route="/training/mistakes",
            count=len(items) or None,
        ),
        _drill(
            "trainer",
            f"Twenty minutes of {label}s a day",
            "Speed matters less than getting the pattern into the part of your brain "
            "that fires before you start calculating.",
            minutes=20,
            route="/puzzles",
        ),
    ]
    seen: set[Any] = set()
    for item in items:
        if len(seen) >= 2 or item.get("game_id") in seen:
            continue
        seen.add(item.get("game_id"))
        drills.append(_drill(
            "review",
            f"The miss against {item.get('opponent') or 'your opponent'}",
            "Find the move yourself before you press the arrow.",
            minutes=10,
            game_id=item.get("game_id"),
            ply=item.get("ply"),
        ))

    impact = sum(float(t.get("mean_delta_w") or 0) * int(t["n"]) for t in tags) / total
    return _block(
        "tactics",
        chapter="Tactics",
        title=f"Stop missing {label}s",
        why=why,
        impact=impact,
        drills=drills,
        measure=f"Next run: fewer than {max(1, int(lead['n']) // 2)} missed {label}s.",
        evidence={
            "motifs": [
                {
                    "tag": t["tag"],
                    "label": str(t["tag"]).replace("_", " "),
                    "n": int(t["n"]),
                    "mean_cost": float(t.get("mean_delta_w") or 0),
                }
                for t in tags
            ],
            "practice_positions": len(items),
        },
    )


def _mistakes_block(
    practice: dict[str, Any],
    headline: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The practice set itself — the highest-yield hour on the page.

    Distinct from the tactics block: that one is about a *motif*, this one is
    about the specific positions, whether or not they share a pattern.
    """

    items = [i for i in (practice.get("items") or []) if i.get("fen")]
    if len(items) < 4:
        return None
    total = max(1, len(facts))
    mean_cost = sum(float(i.get("delta_w") or 0) for i in items) / len(items)
    per_100 = _num((headline.get("error_rates") or {}).get("blunders_per_100"))

    why = (
        f"{len(items)} positions from your own games where a move you could "
        "reasonably have found was sitting there and you played something else"
    )
    why += f". That is one roughly every {round(100 / per_100)} moves." if per_100 else "."

    drills = [
        _drill(
            "trainer",
            f"Work the set — {_plural(len(items), 'position')}",
            "They are ranked by what they cost you, so the top ten are worth more "
            "than everything below them put together.",
            minutes=30,
            route="/training/mistakes",
            count=len(items),
        )
    ]
    seen: set[Any] = set()
    for item in items:
        if len(seen) >= 3 or item.get("game_id") in seen:
            continue
        seen.add(item.get("game_id"))
        move_no = (int(item.get("ply") or 0) + 1) // 2
        drills.append(_drill(
            "review",
            f"vs {item.get('opponent') or 'opponent'}, move {move_no or '?'}",
            f"You played {item.get('san') or 'a move'} here.",
            minutes=8,
            game_id=item.get("game_id"),
            ply=item.get("ply"),
        ))

    return _block(
        "mistakes",
        chapter="Your own games",
        title="Drill the misses from your own games first",
        why=why,
        impact=(mean_cost * len(items)) / total,
        drills=drills,
        measure="Next run: this set smaller than it is today.",
        evidence={
            "positions": len(items),
            "mean_cost": mean_cost,
            "blunders_per_100": per_100,
        },
    )


def _endgames_block(
    facts: list[dict[str, Any]],
    endgame: dict[str, Any],
    resilience: dict[str, Any],
) -> dict[str, Any] | None:
    """Which endings you actually reach, and what you do with them.

    Named by material (``game_shape.endgame_type``) because that is how endgame
    study material is organised — "rook endings" is a chapter, "endgames" is not.
    The conversion half comes from where the endgame was *entered*: dropping
    points from level endings and dropping them from winning ones are different
    problems with different drills.
    """

    total = len(facts)
    reached = int(endgame.get("reached") or 0)
    if total < MIN_BUCKET_GAMES or reached < MIN_BUCKET_GAMES:
        return None

    types = {
        k: v for k, v in _bucket(facts, lambda f: f.get("endgame_type")).items()
        if v["n"] >= MIN_BUCKET_GAMES and v["score_pct"] is not None
    }
    worst_type = min(types.values(), key=lambda b: b["score_pct"]) if types else None

    entries = {str(r.get("key")): r for r in (endgame.get("entry") or [])}
    winning = entries.get("winning") or {}
    level = entries.get("equal") or {}
    conversion = resilience.get("conversion") or {}

    parts: list[str] = []
    impact = 0.0
    drills: list[dict[str, Any]] = []

    per_move = _num(endgame.get("delta_w_per_move"))
    reach_rate = _num(endgame.get("reach_rate")) or 0.0

    if worst_type is not None and worst_type["score_pct"] < 0.45:
        label = ENDGAME_LABELS.get(worst_type["key"], f"{worst_type['key']} endings")
        parts.append(
            f"You score {_pct(worst_type['score_pct'])} in {label} across "
            f"{_plural(worst_type['n'], 'game')}."
        )
        impact += (0.5 - worst_type["score_pct"]) * 100 * (worst_type["n"] / total)
        drills.append(_drill(
            "study",
            f"One week of {label}",
            "The theoretical positions first — the ones with a known result — then "
            "your own games. You are looking for the technique, not the trick.",
            minutes=40,
        ))
        drills.extend(_review_drills(
            _worst_games(worst_type["games"], 2),
            note=f"{label[0].upper() + label[1:]}.",
            minutes=15,
        ))

    if int(winning.get("n") or 0) >= MIN_BUCKET_GAMES and winning.get("score_pct") is not None \
            and winning["score_pct"] < 0.85:
        parts.append(
            f"You entered {_plural(int(winning['n']), 'endgame')} already winning and "
            f"scored {_pct(winning['score_pct'])} from them."
        )
        impact += (0.9 - winning["score_pct"]) * 100 * (int(winning["n"]) / total)
        drills.append(_drill(
            "trainer",
            "Defense Gym on the positions you had won",
            "It hands you a position and asks you not to spoil it. That is the exact "
            "skill an endgame you are already winning needs.",
            minutes=20,
            route="/training/defense",
        ))
    elif int(level.get("n") or 0) >= MIN_BUCKET_GAMES and level.get("score_pct") is not None \
            and level["score_pct"] < 0.45:
        parts.append(
            f"From a level endgame you score {_pct(level['score_pct'])} over "
            f"{_plural(int(level['n']), 'game')} — the half point is going somewhere."
        )
        impact += (0.5 - level["score_pct"]) * 100 * (int(level["n"]) / total)

    if not parts:
        return None

    if int(conversion.get("n") or 0) >= MIN_BUCKET_GAMES:
        dropped = _num(conversion.get("points_dropped")) or 0.0
        if dropped >= 1.0:
            parts.append(
                f"Across the whole window you dropped {dropped:.1f} full points from "
                "positions you had already won."
            )

    reach_note = (
        f"You reach one in {_pct(reach_rate)} of your games, so this is not a rare event."
        if reach_rate >= 0.3 else ""
    )
    if reach_note:
        parts.append(reach_note)

    title = (
        f"Learn your {ENDGAME_LABELS.get(worst_type['key'], 'endgames')}"
        if worst_type is not None and worst_type["score_pct"] < 0.45
        else "Convert the endgames you already reach"
    )
    return _block(
        "endgames",
        chapter="Endgames",
        title=title,
        why=" ".join(parts),
        impact=impact,
        drills=drills,
        measure="Next run: scoring above 85% from endgames you enter winning.",
        evidence={
            "reach_rate": reach_rate,
            "loss_per_move": per_move,
            "by_type": [
                {
                    "type": b["key"],
                    "label": ENDGAME_LABELS.get(b["key"], str(b["key"])),
                    "games": b["n"],
                    "score_pct": b["score_pct"],
                }
                for b in sorted(types.values(), key=lambda x: -x["n"])
            ],
            "entry": endgame.get("entry") or [],
        },
    )


def _calculation_block(
    critical: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Accuracy on the moves that decide the game versus the ones that do not.

    The gap is the whole finding: a player who is equally accurate everywhere
    has a knowledge problem, and a player who falls apart only when the position
    is sharp has a calculation problem. Only the second gets this block.
    """

    gap = _num(critical.get("criticality_gap"))
    buckets = {str(b.get("key")): b for b in (critical.get("buckets") or [])}
    crit = buckets.get("critical")
    if gap is None or crit is None or int(crit.get("moves") or 0) < 8 or gap <= 4:
        return None

    total = max(1, len(facts))
    handled = _num(crit.get("handled_rate"))
    impact = float(crit["delta_w_per_move"]) * (int(crit["moves"]) / total)

    why = (
        f"On the moves where the game is genuinely in the balance you play "
        f"{gap:.0f} accuracy points below your own standard, and you come through "
        f"{_pct(handled)} of them cleanly. "
        "Everywhere else you are fine, which is what makes this a calculation "
        "problem rather than a knowledge one."
    )
    drills = [
        _drill(
            "trainer",
            "Forced Lines",
            "Play only the moves that are forced, to the end of the line. It builds "
            "the habit of finishing a calculation instead of abandoning it.",
            minutes=25,
            route="/training/forced",
        ),
        _drill(
            "trainer",
            "Eval Hold on sharp positions",
            "Sit in a position where something is about to happen and hold the "
            "assessment. No clock pressure, no guessing.",
            minutes=20,
            route="/training/eval-hold",
        ),
        _drill(
            "study",
            "Calculate one position a day, written down",
            "Pick a position from your own games, write the lines out on paper to "
            "three moves deep, then check. Writing is what stops you looping.",
            minutes=15,
        ),
    ]
    return _block(
        "calculation",
        chapter="Calculation",
        title="Train the sharp positions, not the quiet ones",
        why=why,
        impact=impact,
        drills=drills,
        measure=f"Next run: the gap under {max(2, round(gap / 2))} points.",
        evidence={
            "gap": gap,
            "critical_moves": int(crit.get("moves") or 0),
            "handled_rate": handled,
            "accuracy": _num(crit.get("accuracy")),
        },
    )


# ── Habit blocks ─────────────────────────────────────────────────────────────
#
# These cost no study time. They are rules applied while playing, so they do not
# compete for the hours budget — which also means a player with two hours a week
# still gets all of them.


def _blunders_block(
    headline: dict[str, Any],
    blunder_timing: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """A checklist, aimed at the stretch of the game where it actually breaks."""

    errors = headline.get("error_rates") or {}
    per_100 = _num(errors.get("blunders_per_100"))
    total_blunders = int(errors.get("blunders") or 0)
    if per_100 is None or per_100 <= 0 or total_blunders < 3:
        return None

    total = max(1, len(facts))
    window = blunder_timing.get("worst_window")
    rows = {str(r.get("key")): r for r in (blunder_timing.get("buckets") or [])}
    row = rows.get(str(window)) if window else None
    first_error = _num(blunder_timing.get("mean_first_error_move"))
    clean = _num(errors.get("clean_game_rate"))

    why = f"You give away a game-changing move every {round(100 / per_100)} moves"
    if clean is not None:
        why += f", and {_pct(1 - clean)} of your games contain at least one"
    why += "."
    if row and _num(row.get("blunder_rate")):
        why += (
            f" They cluster in moves {window}, where {_pct(row['blunder_rate'])} of "
            "your moves are one."
        )
    if first_error:
        why += f" The first one lands around move {round(first_error)}."

    impact = (total_blunders * BLUNDER_DELTA_W) / total * 0.5
    drills = [
        _drill(
            "rule",
            "Before every move: what did their last move attack?",
            "Not what you want to do — what changed. Say it to yourself. Most of "
            "these are not missed calculations, they are moves you never looked at.",
        ),
        _drill(
            "rule",
            f"Two extra checks in moves {window}" if window else "Two extra checks at the crisis",
            "Every capture, every check and every one-move threat, for both sides."
            + (" Your games break in that window more than anywhere else." if window else ""),
        ),
        _drill(
            "trainer",
            "Guess the Eval",
            "Ten positions. It trains you to look at the whole board before "
            "committing, which is the same habit in a different shape.",
            minutes=10,
            route="/training/guess-eval",
        ),
    ]
    return _block(
        "blunders",
        chapter="Blunder-proofing",
        title="Put a check between you and the move",
        why=why,
        impact=impact,
        drills=drills,
        measure=f"Next run: under {round(per_100 * 0.7, 1)} per 100 moves.",
        kind="habit",
        evidence={
            "blunders": total_blunders,
            "per_100": per_100,
            "worst_window": window,
            "clean_game_rate": clean,
            "mean_first_error_move": first_error,
        },
    )


def _clock_block(
    scramble: dict[str, Any],
    time_vs_criticality: dict[str, Any],
    critical: dict[str, Any],
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Where the time goes, and the two rules that fix it."""

    total = max(1, len(facts))
    rows = {str(r.get("key")): r for r in (scramble.get("buckets") or [])}
    scr, deep = rows.get("scramble"), rows.get("deep")

    high = _num(time_vs_criticality.get("avg_time_high_vol"))
    low = _num(time_vs_criticality.get("avg_time_low_vol"))
    inverted = high is not None and low is not None and high < low

    parts: list[str] = []
    drills: list[dict[str, Any]] = []
    impact = 0.0

    if scr and deep and int(scr.get("moves") or 0) >= 10 and int(deep.get("moves") or 0) >= 10:
        excess = float(scr["delta_w_per_move"]) - float(deep["delta_w_per_move"])
        rate = _num(scr.get("blunder_rate")) or 0.0
        deep_rate = _num(deep.get("blunder_rate")) or 0.0
        # Gate on the rate the sentence is about. Reading it off `excess` let the
        # block fire and then announce "0% of your moves are errors".
        if excess > 0 and rate > 0.05:
            multiple = (
                f" — {rate / deep_rate:.0f} times what you play with time in hand"
                if deep_rate > 0 and rate / deep_rate >= 1.5 else ""
            )
            parts.append(
                f"Under ten seconds on the clock, {_pct(rate)} of your moves are "
                f"game-changing errors{multiple}."
            )
            impact += excess * (int(scr["moves"]) / total)
            drills.append(_drill(
                "rule",
                "Set a clock floor and defend it",
                "Below a third of your starting time you move on general principles "
                "and stop calculating. Losing on the clock and losing to a scramble "
                "blunder cost exactly the same.",
            ))

    if inverted:
        parts.append(
            f"You spend about {high:.0f} seconds on the moves that decide the game "
            f"and {low:.0f} on the ones that do not — the budget is backwards."
        )
        crit = next(
            (b for b in (critical.get("buckets") or []) if str(b.get("key")) == "critical"),
            None,
        )
        if crit and int(crit.get("moves") or 0) >= 8:
            impact += float(crit["delta_w_per_move"]) * (int(crit["moves"]) / total) * 0.5
        drills.append(_drill(
            "rule",
            "Spend the time where the position is sharp",
            "When a capture, a check or a real threat is available to either side, "
            "that is the move worth a minute. A quiet developing move never is.",
        ))

    if not parts:
        return None

    drills.append(_drill(
        "trainer",
        "Forced Lines against the clock",
        "It rewards seeing the end of a line quickly, which is what a scramble needs.",
        minutes=15,
        route="/training/forced",
    ))
    return _block(
        "clock",
        chapter="The clock",
        title="Spend your time on the moves that decide the game",
        why=" ".join(parts),
        impact=impact,
        drills=drills,
        measure="Next run: more time on sharp positions than on quiet ones.",
        kind="habit",
        evidence={
            "scramble": scr,
            "deep": deep,
            "time_high": high,
            "time_low": low,
            "inverted": inverted,
        },
    )


def _mental_block(tier3: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """When to stop playing. The cheapest rating on the page and the least fun.

    Everything here is a scheduling decision rather than a chess skill, which is
    why it costs no study hours and why every drill is a rule.
    """

    total = max(1, len(facts))
    decided = [f for f in facts if f.get("points") is not None]
    overall = (
        sum(1 for f in decided if float(f["points"]) == 1.0) / len(decided)
        if decided else None
    )

    after = tier3.get("after_loss") or {}
    lengths = tier3.get("by_session_length") or {}
    index_rows = tier3.get("by_session_index") or []
    hour_rows = [h for h in (tier3.get("accuracy_by_hour") or []) if int(h.get("n") or 0) >= 3]

    parts: list[str] = []
    drills: list[dict[str, Any]] = []
    impact = 0.0
    tilt_drop = None

    if int(after.get("n") or 0) >= 4 and after.get("win_rate") is not None and overall is not None:
        tilt_drop = overall - float(after["win_rate"])
        if tilt_drop > 0.1:
            parts.append(
                f"In the game straight after a loss you win {_pct(after['win_rate'])} "
                f"against {_pct(overall)} the rest of the time, over "
                f"{_plural(int(after['n']), 'game')}."
            )
            impact += tilt_drop * 100 * (int(after["n"]) / total) * 0.5
            drills.append(_drill(
                "rule",
                "One loss ends the session",
                "Not two, and not 'one more to get it back'. This is worth more "
                "rating than any drill on this page and it costs no study time.",
            ))

    long_b, short_b = lengths.get("long") or {}, lengths.get("short") or {}
    if int(long_b.get("n") or 0) >= 4 and int(short_b.get("n") or 0) >= 4:
        short_rate = _num(short_b.get("win_rate")) or 0.0
        long_rate = _num(long_b.get("win_rate")) or 0.0
        if short_rate - long_rate > 0.1:
            parts.append(
                f"Sessions of six games or more win {_pct(long_rate)}; your short "
                f"sessions win {_pct(short_rate)}."
            )
            impact += (short_rate - long_rate) * 100 * (int(long_b["n"]) / total) * 0.4
            drills.append(_drill(
                "rule",
                "Cap the session at four games",
                "Your results fall away past that point, consistently.",
            ))

    if len(index_rows) >= 3:
        first = index_rows[0]
        later = [r for r in index_rows[2:] if int(r.get("n") or 0) >= 3]
        if later and _num(first.get("win_rate")) is not None:
            tail = sum(float(r["win_rate"]) for r in later) / len(later)
            if float(first["win_rate"]) - tail > 0.12:
                parts.append(
                    f"Your first game of a session wins {_pct(first['win_rate'])}; "
                    f"from the third onwards it is {_pct(tail)}."
                )

    if hour_rows:
        worst_h = min(hour_rows, key=lambda h: _num(h.get("mean_accuracy")) or 100.0)
        best_h = max(hour_rows, key=lambda h: _num(h.get("mean_accuracy")) or 0.0)
        spread = (_num(best_h.get("mean_accuracy")) or 0) - (_num(worst_h.get("mean_accuracy")) or 0)
        if spread >= 6:
            parts.append(
                f"You play your worst chess around {int(worst_h['hour']):02d}:00 and "
                f"your best around {int(best_h['hour']):02d}:00."
            )
            drills.append(_drill(
                "rule",
                f"No rated games at {int(worst_h['hour']):02d}:00",
                "Puzzles are fine. Rated games at your worst hour are a donation.",
            ))

    if not parts:
        return None
    return _block(
        "mental",
        chapter="Habits",
        title="Stop playing before the rating leaves",
        why=" ".join(parts),
        impact=impact,
        drills=drills,
        measure="Next run: the game after a loss winning as often as any other.",
        kind="habit",
        evidence={
            "after_loss": after,
            "by_session_length": lengths,
            "tilt_drop": tilt_drop,
            "hours": hour_rows,
        },
    )


# ── Budget, projection, schedule ─────────────────────────────────────────────


def _allocate(blocks: list[dict[str, Any]], hours_per_week: float, weeks: int) -> list[dict[str, Any]]:
    """Split the weekly budget across the study blocks, then drop what doesn't fit.

    Proportional to what each block costs the player, floored so nothing appears
    as a token fifteen minutes, and capped so one block cannot eat the week. If
    the floors do not fit in the budget the lowest-impact blocks are removed
    rather than everything being shaved — a plan with four things in it that
    cannot be done is worse than a plan with two that can.
    """

    study = [b for b in blocks if b["kind"] == "study"]
    habits = [b for b in blocks if b["kind"] != "study"]
    for b in habits:
        b["recovery"] = _habit_recovery(b["id"], weeks)

    if not study:
        return habits

    study.sort(key=lambda b: -b["impact"])
    kept = study[: max(1, int(hours_per_week // MIN_BLOCK_HOURS))]

    cap = max(MIN_BLOCK_HOURS, hours_per_week * MAX_BLOCK_SHARE)
    # A run where nothing is measurably costly still splits the week evenly
    # rather than dividing by zero.
    weight = {b["id"]: b["impact"] for b in kept}
    if not sum(weight.values()):
        weight = {b["id"]: 1.0 for b in kept}
    span = sum(weight.values())
    hours = {b["id"]: hours_per_week * weight[b["id"]] / span for b in kept}

    # Water-fill: clamp to [floor, cap], hand the slack back to whatever is
    # still between the two bounds. Converges in a couple of passes for the
    # handful of blocks a plan ever has.
    for _ in range(6):
        clamped = {k: min(cap, max(MIN_BLOCK_HOURS, v)) for k, v in hours.items()}
        slack = hours_per_week - sum(clamped.values())
        free = [k for k, v in clamped.items() if MIN_BLOCK_HOURS < v < cap]
        if abs(slack) < 0.01 or not free:
            hours = clamped
            break
        share = slack / len(free)
        hours = {k: (v + share if k in free else v) for k, v in clamped.items()}

    for b in kept:
        b["hours_per_week"] = _round_quarter(hours[b["id"]])
        b["hours_total"] = round(b["hours_per_week"] * weeks, 2)
        b["recovery"] = _study_recovery(b["id"], b["hours_total"])

    return [b for b in kept if b["hours_per_week"] >= MIN_BLOCK_HOURS] + habits


def _projection(
    blocks: list[dict[str, Any]],
    headline: dict[str, Any],
    weeks: int,
) -> dict[str, Any]:
    """Rating the plan is worth, capped by the rating the player actually lost.

    Each block contributes ``impact × recovery`` win chances per game. Those are
    summed — legitimately, because a block is a *plan item* rather than a leak,
    and the sum is then capped at ``elo_left_on_board``'s measured recoverable
    pool, which is what stops overlapping findings inflating the number. The
    total goes through the same FIDE score-to-difference curve the headline uses,
    so the two numbers are on one scale, and the per-block split is proportional.
    """

    elo = headline.get("elo_left_on_board") or {}
    record = headline.get("record") or {}
    rating = headline.get("rating") or {}
    decided = int(record.get("decided") or 0)
    actual = _num(record.get("score_pct"))
    pool = _num(elo.get("recoverable_score"))
    games = int(elo.get("games") or 0)

    unavailable = {
        "rating_gain": None,
        "current_rating": _num(rating.get("end")) or _num(rating.get("mean")),
        "projected_rating": None,
        "confidence": "unknown",
        "basis": elo.get("basis"),
        "ceiling": _num(elo.get("points")),
        "note": (
            "Not enough finished, rated games in this window to put a number on it. "
            "The plan still holds; the projection needs more games."
        ),
    }
    if not decided or actual is None or pool is None or not games:
        return unavailable

    gain_per_game = sum(
        float(b["impact"]) * float(b["recovery"]) for b in blocks
    ) / 100.0
    ceiling_per_game = pool / games
    capped = min(gain_per_game, ceiling_per_game)

    potential = min(0.999, actual + capped)
    points = max(0.0, rating_difference(potential) - rating_difference(actual))

    current = _num(rating.get("end")) or _num(rating.get("mean"))
    total_hours = sum(float(b["hours_total"]) for b in blocks)
    pace = _pace_ceiling(current, weeks, total_hours)
    limited_by = None
    if gain_per_game > ceiling_per_game:
        limited_by = "measured"
    if points > pace:
        points = pace
        limited_by = "pace"

    weight_total = sum(float(b["impact"]) * float(b["recovery"]) for b in blocks) or 1.0
    for b in blocks:
        share = (float(b["impact"]) * float(b["recovery"])) / weight_total
        b["rating_gain"] = round(points * share)

    basis = elo.get("basis")
    confidence = (
        "high" if decided >= 40 and basis == "findability"
        else "medium" if decided >= 20
        else "low"
    )
    return {
        "rating_gain": round(points),
        "current_rating": current,
        "projected_rating": round(current + points) if current is not None else None,
        "confidence": confidence,
        "basis": basis,
        "ceiling": _num(elo.get("points")),
        "pace_ceiling": round(pace),
        "limited_by": limited_by,
        "weeks": weeks,
        "note": (
            f"Over {_plural(weeks, 'week')}, assuming the plan is followed. "
            + (
                "Held down by how fast anyone absorbs this much; the full pool is "
                f"{round(_num(elo.get('points')) or 0)} points and takes longer than "
                f"{_plural(weeks, 'week')} to collect."
                if limited_by == "pace"
                else "Capped by what you measurably threw away in these games — the "
                "plan can recover part of that, never more."
            )
        ),
    }


def _schedule(blocks: list[dict[str, Any]], hours_per_week: float) -> list[dict[str, Any]]:
    """Turn weekly hours into named sessions you could actually sit down to.

    Work is cut into chunks of at most half an hour and dealt largest-first
    across the sessions, so no session is one long grind and none is five
    minutes.
    """

    study = [b for b in blocks if b["kind"] == "study" and b["hours_per_week"] > 0]
    if not study:
        return []
    count = max(2, min(5, int(round(hours_per_week / 1.25)) or 2))

    chunks: list[dict[str, Any]] = []
    for b in study:
        left = _minutes(b["hours_per_week"])
        while left > 0:
            take = min(30, left)
            if left - take < 10:
                take = left
            chunks.append({"block": b["id"], "title": b["title"], "minutes": take})
            left -= take
    chunks.sort(key=lambda c: -c["minutes"])

    sessions: list[dict[str, Any]] = [
        {"label": f"Session {i + 1}", "minutes": 0, "items": []} for i in range(count)
    ]
    for chunk in chunks:
        target = min(sessions, key=lambda s: s["minutes"])
        # Two chunks of one block landing in the same session are one sitting,
        # not two — listing the same title twice reads as a scheduling bug.
        same = next((i for i in target["items"] if i["block"] == chunk["block"]), None)
        if same is not None:
            same["minutes"] += chunk["minutes"]
        else:
            target["items"].append(dict(chunk))
        target["minutes"] += chunk["minutes"]
    return [s for s in sessions if s["items"]]


# ── Public API ───────────────────────────────────────────────────────────────


def _clamp_inputs(hours_per_week: Any, weeks: Any) -> tuple[float, int]:
    hpw = _num(hours_per_week)
    hpw = DEFAULT_HOURS_PER_WEEK if hpw is None else hpw
    wks = _num(weeks)
    wks = DEFAULT_WEEKS if wks is None else wks
    return (
        min(MAX_HOURS_PER_WEEK, max(MIN_HOURS_PER_WEEK, round(hpw * 2) / 2)),
        int(min(MAX_WEEKS, max(MIN_WEEKS, round(wks)))),
    )


def build_study_plan(
    metrics: dict[str, Any] | None,
    *,
    hours_per_week: float = DEFAULT_HOURS_PER_WEEK,
    weeks: int = DEFAULT_WEEKS,
) -> dict[str, Any]:
    """The plan. Pure over a computed metrics blob — no engine, no DB, no network.

    Always returns the same shape. ``available`` is ``False`` with a stated
    reason when the window is too thin to say anything specific, because an
    invented plan is worse than none.
    """

    hours_per_week, weeks = _clamp_inputs(hours_per_week, weeks)
    metrics = metrics or {}
    pro = metrics.get("pro") or {}
    facts = list(metrics.get("game_explorer") or [])
    headline = pro.get("headline") or {}

    base: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "available": False,
        "reason": None,
        "hours_per_week": hours_per_week,
        "weeks": weeks,
        "total_hours": 0.0,
        "blocks": [],
        "habits": [],
        "schedule": [],
        "arc": [],
        "projection": None,
        "games": len(facts),
        "model": (
            "Each block recovers a fraction of what it measurably costs you, "
            "saturating with hours spent. The total is capped by the rating you "
            "actually left on the board in these games."
        ),
    }
    if len(facts) < MIN_GAMES:
        base["reason"] = (
            f"A plan needs at least {MIN_GAMES} analyzed games to name anything "
            f"specific. This run has {len(facts)}."
        )
        return base

    candidates = [
        _mistakes_block(metrics.get("practice_flags") or {}, headline, facts),
        _tactics_block(metrics.get("missed_tactics") or {}, metrics.get("practice_flags") or {}, facts),
        _openings_block(facts),
        _middlegame_block(facts, pro.get("move_quality") or {}),
        _endgames_block(facts, pro.get("endgame") or {}, pro.get("resilience") or {}),
        _calculation_block(pro.get("critical_moments") or {}, facts),
        _blunders_block(headline, pro.get("blunder_timing") or {}, facts),
        _clock_block(
            metrics.get("time_scramble_decay") or {},
            metrics.get("time_vs_criticality") or {},
            pro.get("critical_moments") or {},
            facts,
        ),
        _mental_block(metrics.get("tier3") or {}, facts),
    ]
    blocks = [b for b in candidates if b]
    if not blocks:
        base["reason"] = (
            "Nothing in this window is costing you enough to build a plan around. "
            "Play more games and run it again."
        )
        return base

    blocks = _allocate(blocks, hours_per_week, weeks)
    order = {bid: i for i, bid in enumerate(BLOCK_ORDER)}
    blocks.sort(key=lambda b: (b["kind"] != "study", -b["impact"], order.get(b["id"], 99)))
    projection = _projection(blocks, headline, weeks)
    for i, b in enumerate(blocks):
        b["priority"] = i + 1

    study = [b for b in blocks if b["kind"] == "study"]
    habits = [b for b in blocks if b["kind"] != "study"]

    base.update({
        "available": True,
        "blocks": study,
        "habits": habits,
        "schedule": _schedule(blocks, hours_per_week),
        "arc": _arc(study, habits, weeks),
        "projection": projection,
        "total_hours": round(sum(b["hours_per_week"] for b in study) * weeks, 2),
    })
    return base


def _arc(
    study: list[dict[str, Any]],
    habits: list[dict[str, Any]],
    weeks: int,
) -> list[dict[str, Any]]:
    """One line per week: what leads, and what is being checked at the end of it.

    The lead rotates through the study blocks so a four-week plan is not four
    identical weeks, and week one always leads with the biggest item — the point
    at which people are most likely to still be doing this.
    """

    if not study:
        return []
    out = []
    for i in range(weeks):
        lead = study[i % len(study)]
        habit = habits[i % len(habits)] if habits else None
        # "The clock" would otherwise read "with the the clock rule".
        habit_name = (
            habit["chapter"].lower().removeprefix("the ").strip() if habit else ""
        )
        out.append({
            "week": i + 1,
            "lead": lead["id"],
            "title": lead["title"],
            "focus": (
                f"{lead['chapter']} leads the week"
                + (f", with the {habit_name} rule running underneath" if habit else "")
                + "."
            ),
            "checkpoint": lead["measure"],
        })
    return out


def plan_strings(plan: dict[str, Any] | None) -> list[str]:
    """Every string a player reads — the input to the jargon guard."""

    out: list[str] = []
    if not plan:
        return out
    for key in ("reason", "model"):
        if isinstance(plan.get(key), str):
            out.append(plan[key])
    projection = plan.get("projection") or {}
    if isinstance(projection.get("note"), str):
        out.append(projection["note"])
    for block in list(plan.get("blocks") or []) + list(plan.get("habits") or []):
        for key in ("title", "why", "measure", "chapter"):
            if isinstance(block.get(key), str):
                out.append(block[key])
        for drill in block.get("drills") or []:
            for key in ("label", "detail"):
                if isinstance(drill.get(key), str):
                    out.append(drill[key])
    for week in plan.get("arc") or []:
        for key in ("title", "focus", "checkpoint"):
            if isinstance(week.get(key), str):
                out.append(week[key])
    return [s for s in out if s]


def ensure_study_plan(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """Attach a default-shaped plan to a metrics blob that predates it.

    Same contract as ``insights_narrative.ensure_narrative``: pure over stored
    metrics, so an old run gains the tab on read without a rebuild.
    """

    if not isinstance(metrics, dict):
        return metrics
    plan = metrics.get("study_plan")
    if isinstance(plan, dict) and plan.get("schema") == PLAN_SCHEMA:
        return metrics
    metrics["study_plan"] = build_study_plan(metrics)
    return metrics
