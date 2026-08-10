"""Phase 12 — greatest hits, the coach memo, and annotated PGN export.

Three outputs that leave the app:

* **Greatest hits (12.3).** Strengths are currently categories. These are
  *moments*: moves the user **found** that had low findability. Positive
  reinforcement, and the shareable artefact — nobody posts their blunder rate,
  everybody posts a brilliancy.
* **Coach memo (12.4).** One page a human coach reads in two minutes. Coaches do
  not want a dashboard, and every export travels, which is distribution.
* **Annotated PGN (12.5).** NAGs plus per-move Δw, findability and volatility.
  Opens in any GUI. Nearly free given what is already stored, and portability
  builds trust rather than lock-in.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Sequence

import chess
import chess.pgn

#: Findability at or below which finding the move is genuinely creditable.
HARD_TO_FIND = 45

#: Δw the move must have been worth to count as a hit.
HIT_MIN_VALUE = 8.0

#: Standard NAGs, so the export means the same thing in any GUI.
NAG_BY_CLASSIFICATION = {
    "brilliant": 3,     # !!
    "great": 1,         # !
    "best": 1,
    "excellent": 0,
    "good": 0,
    "book": 0,
    "inaccuracy": 6,    # ?!
    "mistake": 2,       # ?
    "miss": 2,
    "blunder": 4,       # ??
}


# ── 12.3 Greatest hits ────────────────────────────────────────────────────────


def greatest_hits(
    rows: Sequence[dict[str, Any]], *, limit: int = 8
) -> list[dict[str, Any]]:
    """Moves the user found that a player at their level usually would not.

    Requires the move to have been both *hard* (low findability) and *worth
    something* — a hard-to-find move that changes nothing is trivia, not a hit.
    """

    hits = []
    for row in rows:
        if not row.get("is_user_move") or row.get("is_book"):
            continue
        findability = row.get("findability")
        if findability is None or float(findability) > HARD_TO_FIND:
            continue
        if float(row.get("delta_w") or 0.0) > 2.0:
            continue  # they did not actually play it well
        played = row.get("move_uci")
        best = row.get("best_uci")
        if not played or not best or played != best:
            continue  # credit is for finding it, not for being near it
        value = float(row.get("volatility") or 0.0)
        if value < HIT_MIN_VALUE:
            continue
        hits.append({
            "game_id": row.get("game_id"),
            "ply": row.get("ply"),
            "san": row.get("san"),
            "findability": int(findability),
            "volatility": value,
            "fen": row.get("fen_before"),
            "move_uci": row.get("move_uci"),
            "opponent": row.get("opponent"),
            "played_at": row.get("played_at"),
            "caption": (
                f"{row.get('san')} — findability {int(findability)}, and you found it."
            ),
        })
    hits.sort(key=lambda h: (h["findability"], -h["volatility"]))
    return hits[:limit]


# ── 12.4 Coach memo ───────────────────────────────────────────────────────────


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def coach_memo(metrics: dict[str, Any], meta: dict[str, Any]) -> str:
    """A one-page markdown briefing a coach can read in two minutes."""

    pro = metrics.get("pro") or {}
    head = pro.get("headline") or {}
    record = head.get("record") or {}
    accuracy = head.get("accuracy") or {}
    rates = head.get("error_rates") or {}
    elo = head.get("elo_left_on_board") or {}
    leaks = pro.get("leaks") or []
    strengths = pro.get("strengths") or []

    handle = meta.get("handle") or meta.get("chesscom_handle") or "Player"
    window = meta.get("window_days")
    time_class = meta.get("time_class") or "—"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"# Coaching brief — {handle}",
        "",
        f"*{record.get('games', 0)} {time_class} games over the last {window} days · "
        f"generated {generated}*",
        "",
        "## Where they stand",
        "",
        f"- **Record** {record.get('wins', 0)}W–{record.get('draws', 0)}D–"
        f"{record.get('losses', 0)}L ({_fmt((record.get('score_pct') or 0) * 100, 1, '%')})",
        f"- **Performance rating** {head.get('performance_rating') or '—'} "
        f"against an average of {_fmt((head.get('opponents') or {}).get('mean_rating'), 0)}",
        f"- **Accuracy** {_fmt(accuracy.get('mean'), 1, '%')} "
        f"(±{_fmt(accuracy.get('stdev'), 1)}, {accuracy.get('consistency') or '—'})",
        f"- **Blunders** {_fmt(rates.get('blunders_per_100'), 2)} per 100 moves; "
        f"{_fmt((rates.get('clean_game_rate') or 0) * 100, 0, '%')} of games are blunder-free",
        f"- **Recoverable** roughly {elo.get('points') or '—'} rating points "
        f"(basis: {elo.get('basis') or 'n/a'})",
        "",
        "## What to work on, in order",
        "",
    ]

    if leaks:
        for idx, leak in enumerate(leaks[:4], start=1):
            lines.append(
                f"{idx}. **{leak['title']}** — {leak['detail']} "
                f"*(≈{_fmt(leak['impact_win_pct_per_game'], 1)} win% per game)*"
            )
    else:
        lines.append("_No leak clears the measurement threshold in this window._")

    attribution = pro.get("attribution") or {}
    if attribution.get("available"):
        lines += ["", "## Where the lost win% actually goes", ""]
        lines.append(
            f"Total {_fmt(attribution.get('total_loss'), 0)} win% lost. "
            "Attribution is additive — these do not overlap:"
        )
        lines.append("")
        for row in (attribution.get("features") or [])[:5]:
            if row["added"] <= 0:
                continue
            lines.append(
                f"- {row['label']}: **{_fmt(row['added'], 0)}** win% "
                f"({_fmt((row.get('share_of_excess') or 0) * 100, 0, '%')} of the excess)"
            )

    skill = pro.get("skill_model") or {}
    if skill.get("available"):
        lines += ["", "## Ability by skill", ""]
        for row in skill.get("categories") or []:
            if row.get("theta") is None or row.get("below_floor"):
                continue
            lines.append(f"- {row['label']}: **{row['theta']}** ± {row['stderr']} ({row['items']} items)")

    structure = (pro.get("structure") or {}).get("endgame_types") or {}
    if structure.get("note"):
        lines += ["", "## Endgames", "", structure["note"]]

    if strengths:
        lines += ["", "## What is already working", ""]
        for item in strengths[:4]:
            lines.append(f"- **{item['title']}** — {item['detail']}")

    practice = metrics.get("practice_flags") or {}
    if practice.get("count"):
        lines += [
            "",
            "## Ready-made practice set",
            "",
            f"{practice['count']} positions from their own games where the miss was both "
            f"costly (Δw ≥ {practice.get('delta_w_threshold', 15)}) and findable.",
        ]

    lines += [
        "",
        "---",
        "",
        "*Δw is expected-points loss in win% (0–100). Findability is the modelled "
        "chance a player at their rating finds the move. Accuracy is CAPS2-style.*",
    ]
    return "\n".join(lines) + "\n"


# ── 12.5 Annotated PGN ────────────────────────────────────────────────────────


def annotated_pgn(
    pgn_text: str,
    moves: Sequence[Any],
    *,
    user_color: str | None = None,
) -> str:
    """Re-emit a game with NAGs and per-move Δw / findability / volatility.

    Opens in any GUI, which is the point: portability builds trust where lock-in
    does not.
    """

    game = chess.pgn.read_game(io.StringIO(pgn_text))
    # ``read_game`` answers with an empty Game rather than None for unparseable
    # input, so exporting it would hand back a fabricated stub. Pass the
    # original through instead: we cannot annotate what we cannot read.
    if game is None or not game.variations:
        return pgn_text

    by_ply = {int(m["ply"]): m for m in moves}
    node = game
    ply = 0
    while node.variations:
        node = node.variation(0)
        ply += 1
        row = by_ply.get(ply)
        if row is None:
            continue

        classification = row["classification"] if "classification" in row.keys() else None
        nag = NAG_BY_CLASSIFICATION.get(str(classification or ""), 0)
        if nag:
            node.nags.add(nag)

        parts = []
        delta_w = row["delta_w"]
        if delta_w is not None and float(delta_w) >= 0.05:
            parts.append(f"Δw {float(delta_w):.1f}")
        if row["volatility"] is not None:
            parts.append(f"V {float(row['volatility']):.0f}")
        if row["findability"] is not None:
            parts.append(f"find {int(row['findability'])}")
        if classification:
            parts.append(str(classification))
        if parts:
            node.comment = " · ".join(parts)

    game.headers["Annotator"] = "ChessMax Insights"
    if user_color:
        game.headers["ChessMaxColor"] = user_color

    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
    return game.accept(exporter)
