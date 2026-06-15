import sqlite3

from pipeline.seed_demo import DEMO_POSITIONS, seed_demo_positions


def test_seed_demo_positions_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    first = seed_demo_positions(connection)
    second = seed_demo_positions(connection)

    assert first == len(DEMO_POSITIONS)
    assert second == len(DEMO_POSITIONS)

    rows = connection.execute(
        """
        SELECT classification, COUNT(*) AS count
        FROM positions
        WHERE source = 'demo_seed'
        GROUP BY classification
        ORDER BY classification
        """
    ).fetchall()
    assert [(row["classification"], row["count"]) for row in rows] == [
        ("quiet", 5),
        ("tactical", 3),
    ]


def test_seed_demo_positions_are_selectable() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    seed_demo_positions(connection)

    quiet = connection.execute(
        "SELECT * FROM positions WHERE classification = 'quiet' AND opening_tag IS NULL"
    ).fetchall()
    tactical = connection.execute(
        """
        SELECT *
        FROM positions
        WHERE classification = 'tactical'
          AND rating BETWEEN 1300 AND 1700
        """
    ).fetchall()

    assert len(quiet) == 5
    assert len(tactical) == 3
