"""Stats aggregation for the single-user dashboard."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any


def load_stats(connection: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    attempts = connection.execute(
        """
        SELECT
            a.id,
            a.eval_loss,
            a.timestamp,
            a.user_rating_before,
            a.user_rating_after,
            p.classification,
            p.themes,
            p.opening_tag
        FROM attempts a
        JOIN positions p ON p.id = a.position_id
        WHERE a.user_id = ?
        ORDER BY a.timestamp ASC, a.id ASC
        """,
        (user_id,),
    ).fetchall()

    playouts = connection.execute(
        """
        SELECT result, maia_rating, engine, timestamp
        FROM playouts
        WHERE user_id = ?
        ORDER BY timestamp ASC, id ASC
        """,
        (user_id,),
    ).fetchall()

    overall = summarize_accuracy(attempts)
    tactical = summarize_accuracy([row for row in attempts if row["classification"] == "tactical"])
    quiet = summarize_accuracy([row for row in attempts if row["classification"] == "quiet"])
    theme = summarize_theme_accuracy(attempts)
    opening = summarize_opening_accuracy(attempts)
    rating_history = build_rating_history(attempts)

    return {
        "overall": overall,
        "tactical": tactical,
        "quiet": quiet,
        "theme_accuracy": theme,
        "opening_accuracy": opening,
        "rating_history": rating_history,
        "playouts": {
            "total": len(playouts),
            "wins": sum(1 for p in playouts if p["result"] == "win"),
            "losses": sum(1 for p in playouts if p["result"] == "loss"),
            "draws": sum(1 for p in playouts if p["result"] == "draw"),
            "recent": [
                {
                    "timestamp": p["timestamp"],
                    "result": p["result"],
                    "maia_rating": p["maia_rating"],
                    "engine": p["engine"],
                }
                for p in playouts[-10:]
            ],
        },
    }


def summarize_accuracy(rows: list[sqlite3.Row]) -> dict[str, float | int]:
    total = len(rows)
    solved = sum(1 for row in rows if float(row["eval_loss"]) <= 100.0)
    accuracy = 0.0 if total == 0 else round((solved / total) * 100.0, 1)
    return {"attempts": total, "solved": solved, "accuracy_pct": accuracy}


def summarize_theme_accuracy(rows: list[sqlite3.Row]) -> list[dict[str, float | int | str]]:
    by_theme: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        if row["classification"] != "tactical" or not row["themes"]:
            continue
        themes = [token.strip() for token in str(row["themes"]).split(",") if token.strip()]
        solved = float(row["eval_loss"]) <= 100.0
        for theme in themes:
            by_theme[theme].append(solved)
    return [
        {
            "theme": theme,
            "attempts": len(outcomes),
            "accuracy_pct": round((sum(outcomes) / len(outcomes)) * 100.0, 1),
        }
        for theme, outcomes in sorted(by_theme.items())
        if outcomes
    ]


def summarize_opening_accuracy(rows: list[sqlite3.Row]) -> list[dict[str, float | int | str]]:
    by_opening: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        opening = row["opening_tag"]
        if not opening:
            continue
        by_opening[str(opening)].append(float(row["eval_loss"]) <= 100.0)
    return [
        {
            "opening": opening,
            "attempts": len(outcomes),
            "accuracy_pct": round((sum(outcomes) / len(outcomes)) * 100.0, 1),
        }
        for opening, outcomes in sorted(by_opening.items())
        if outcomes
    ]


def build_rating_history(rows: list[sqlite3.Row]) -> list[dict[str, int | str]]:
    if not rows:
        return []
    history = [
        {
            "timestamp": rows[0]["timestamp"],
            "rating": int(rows[0]["user_rating_before"]),
            "label": "start",
        }
    ]
    history.extend(
        {
            "timestamp": row["timestamp"],
            "rating": int(row["user_rating_after"]),
            "label": "attempt",
        }
        for row in rows
    )
    return history
