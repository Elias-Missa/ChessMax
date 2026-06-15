import random
import sqlite3
from pathlib import Path

import pytest

from server import db
from server.selection import roll_bucket, select_next_position


def test_roll_bucket_no_openings_is_50_50() -> None:
    rng = SequenceRng([0.0, 0.49, 0.5, 0.99])

    assert roll_bucket(rng, has_openings=False) == "tactical_general"
    assert roll_bucket(rng, has_openings=False) == "tactical_general"
    assert roll_bucket(rng, has_openings=False) == "quiet"
    assert roll_bucket(rng, has_openings=False) == "quiet"


def test_roll_bucket_with_openings_is_30_40_30() -> None:
    rng = SequenceRng([0.0, 0.29, 0.3, 0.69, 0.7, 0.99])

    assert roll_bucket(rng, has_openings=True) == "tactical_general"
    assert roll_bucket(rng, has_openings=True) == "tactical_general"
    assert roll_bucket(rng, has_openings=True) == "tactical_opening"
    assert roll_bucket(rng, has_openings=True) == "tactical_opening"
    assert roll_bucket(rng, has_openings=True) == "quiet"
    assert roll_bucket(rng, has_openings=True) == "quiet"


def test_select_opening_tactical_filters_by_selected_openings(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    with db.connect(db_path) as conn:
        insert_tactical(conn, rating=1500, opening_tag="london")
        insert_tactical(conn, rating=1500, opening_tag="caro-kann")
        insert_tactical(conn, rating=1500, opening_tag=None)

    with db.connect(db_path) as conn:
        # Force the bucket selection to land on tactical_opening
        rng = AlwaysFirstBucketRng(target=0.5)
        position = select_next_position(
            conn,
            user_id=1,
            user_rating=1500,
            selected_openings=["london"],
            rng=rng,
        )

    assert position is not None
    assert position["opening_tag"] == "london"


def test_select_general_tactical_excludes_opening_tagged(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    with db.connect(db_path) as conn:
        insert_tactical(conn, rating=1500, opening_tag="london")
        # No untagged tactical at this rating — selector should fall back to quiet, not pick the London one.
        insert_quiet(conn)

    with db.connect(db_path) as conn:
        rng = AlwaysFirstBucketRng(target=0.0)  # tactical_general
        position = select_next_position(
            conn,
            user_id=1,
            user_rating=1500,
            selected_openings=[],
            rng=rng,
        )

    assert position is not None
    # No matching general-tactical → falls back through quiet (next in FALLBACK_ORDER)
    assert position["classification"] == "quiet"


def test_select_falls_back_when_preferred_bucket_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    with db.connect(db_path) as conn:
        insert_quiet(conn)
        # No tactical at all

    with db.connect(db_path) as conn:
        rng = AlwaysFirstBucketRng(target=0.0)  # prefers tactical_general
        position = select_next_position(
            conn, user_id=1, user_rating=1500, rng=rng
        )

    assert position is not None
    assert position["classification"] == "quiet"


def test_select_excludes_recent_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "trainer.db"
    with db.connect(db_path) as conn:
        user = db.get_singleton_user(conn)
        recent_id = insert_tactical(conn, rating=1500, opening_tag=None)
        record_attempt(conn, user["id"], recent_id)
        # Only one tactical in range; if it gets excluded, must fall through to quiet
        fresh_quiet = insert_quiet(conn)

    with db.connect(db_path) as conn:
        rng = AlwaysFirstBucketRng(target=0.0)  # prefers tactical_general
        position = select_next_position(
            conn,
            user_id=db.get_singleton_user(conn)["id"],
            user_rating=1500,
            rng=rng,
        )

    assert position is not None
    assert position["id"] == fresh_quiet


class SequenceRng:
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)

    def random(self) -> float:
        return self.values.pop(0)

    def choice(self, items: list[object]) -> object:
        return items[0]


class AlwaysFirstBucketRng:
    """random.Random stand-in that always rolls a fixed value."""

    def __init__(self, target: float) -> None:
        self.target = target

    def random(self) -> float:
        return self.target

    def choice(self, items: list[object]) -> object:
        return items[0]


def insert_tactical(
    conn: sqlite3.Connection,
    rating: int,
    opening_tag: str | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO positions (
            fen, side_to_move, source, classification, opening_tag,
            best_move, best_eval, solution_moves, themes, rating, rating_deviation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "w",
            "lichess_puzzle",
            "tactical",
            opening_tag,
            "e2e4",
            60.0,
            "e2e4",
            None,
            rating,
            None,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_quiet(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO positions (
            fen, side_to_move, source, classification, opening_tag,
            best_move, best_eval, solution_moves, themes, rating, rating_deviation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "w",
            "pipeline_quiet",
            "quiet",
            None,
            "e2e4",
            10.0,
            None,
            None,
            1500,
            None,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def record_attempt(conn: sqlite3.Connection, user_id: int, position_id: int) -> None:
    conn.execute(
        """
        INSERT INTO attempts (
            user_id, position_id, user_move, eval_loss, grade,
            user_rating_before, user_rating_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, position_id, "e2e4", 0.0, "best", 1500, 1500),
    )
    conn.commit()
