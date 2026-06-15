"""Puzzle selection.

Mix:
- No openings selected: 50% tactical / 50% quiet.
- Openings selected:    30% general-tactical / 40% opening-tactical / 30% quiet.

Quiet positions are opening-agnostic; opening flavor comes from Lichess's tagged
puzzle pool only.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Sequence
from typing import Literal


Bucket = Literal["tactical_general", "tactical_opening", "quiet"]
FALLBACK_ORDER: tuple[Bucket, ...] = ("tactical_general", "quiet", "tactical_opening")


def select_next_position(
    connection: sqlite3.Connection,
    user_id: int,
    user_rating: int,
    selected_openings: Sequence[str] | None = None,
    rng: random.Random | None = None,
) -> sqlite3.Row | None:
    """Pick one position, falling back to other buckets if the preferred one is empty."""

    rng = rng or random.Random()
    openings = [o for o in (selected_openings or []) if o]

    preferred = roll_bucket(rng, has_openings=bool(openings))
    order = [preferred, *[b for b in FALLBACK_ORDER if b != preferred]]

    for bucket in order:
        row = select_from_bucket(connection, user_id, user_rating, bucket, openings)
        if row is not None:
            return row
    return None


def roll_bucket(rng: random.Random, has_openings: bool) -> Bucket:
    r = rng.random()
    if not has_openings:
        return "tactical_general" if r < 0.5 else "quiet"
    if r < 0.3:
        return "tactical_general"
    if r < 0.7:
        return "tactical_opening"
    return "quiet"


def select_from_bucket(
    connection: sqlite3.Connection,
    user_id: int,
    user_rating: int,
    bucket: Bucket,
    selected_openings: Sequence[str],
) -> sqlite3.Row | None:
    if bucket == "tactical_opening" and not selected_openings:
        return None

    where: list[str] = []
    params: list[object] = []

    if bucket == "quiet":
        where.append("classification = 'quiet'")
    else:
        where.append("classification = 'tactical'")
        where.append("rating BETWEEN ? AND ?")
        params.extend([user_rating - 200, user_rating + 200])
        if bucket == "tactical_opening":
            placeholders = ",".join("?" for _ in selected_openings)
            where.append(f"opening_tag IN ({placeholders})")
            params.extend(selected_openings)
        else:
            where.append("opening_tag IS NULL")

    recent_ids = [
        row["position_id"]
        for row in connection.execute(
            """
            SELECT position_id
            FROM attempts
            WHERE user_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 50
            """,
            (user_id,),
        ).fetchall()
    ]
    if recent_ids:
        placeholders = ",".join("?" for _ in recent_ids)
        where.append(f"id NOT IN ({placeholders})")
        params.extend(recent_ids)

    sql = f"""
        SELECT *
        FROM positions
        WHERE {' AND '.join(where)}
        ORDER BY RANDOM()
        LIMIT 1
    """
    return connection.execute(sql, params).fetchone()
