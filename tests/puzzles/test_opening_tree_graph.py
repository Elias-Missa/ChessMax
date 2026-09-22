"""The book as a drawable graph, coloured by how the player actually does.

Engine-free: every number comes from reviews already stored, so nothing here
needs Stockfish, Maia or the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import chess
import pytest
from fastapi.testclient import TestClient

from server import db, opening_cache, opening_tree_stats, openings_build
from server.main import create_app

START_KEY = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def client_for(app: Any, email: str = "graph@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opening_cache, "FETCH_JSON", lambda url, ua: {"moves": []})


def import_line(client: TestClient, moves: str, color: str = "white") -> None:
    client.post(
        "/api/opening-book/import",
        json={"color": color, "pgn": f'[Result "*"]\n\n{moves} *\n'},
    )


def seed_review(
    db_path: Path,
    *,
    review_id: str,
    moves: str,
    result: str,
    user_color: str = "white",
    user_id: int = 1,
    own_rating: int = 1500,
    opp_rating: int = 1500,
    delta_w: dict[int, float] | None = None,
) -> None:
    """One complete review over a game, with per-ply win% loss for user moves."""

    connection = db.connect(db_path)
    pgn = f'[Result "{result}"]\n\n{moves} {result}\n'
    connection.execute(
        """
        INSERT OR REPLACE INTO games
            (game_id, source, pgn, result, white_rating, black_rating)
        VALUES (?, 'pgn', ?, ?, ?, ?)
        """,
        (
            review_id,
            pgn,
            result,
            own_rating if user_color == "white" else opp_rating,
            opp_rating if user_color == "white" else own_rating,
        ),
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO reviews
            (review_id, user_id, game_id, user_color, user_rating, depth_tier,
             status, constants_version)
        VALUES (?, ?, ?, ?, ?, 'full', 'complete', 'v1')
        """,
        (review_id, user_id, review_id, user_color, own_rating),
    )
    board = chess.Board()
    game_moves = [
        m for m in pgn.split("\n\n")[1].split() if not m[0].isdigit() and m != result
    ]
    user_is_white = user_color == "white"
    for index, san in enumerate(game_moves, start=1):
        try:
            move = board.parse_san(san)
        except ValueError:
            break
        is_user = (board.turn == chess.WHITE) == user_is_white
        connection.execute(
            """
            INSERT OR REPLACE INTO review_moves
                (review_id, ply, san, is_user_move, phase, delta_w)
            VALUES (?, ?, ?, ?, 'opening', ?)
            """,
            (
                review_id,
                index,
                san,
                1 if is_user else 0,
                (delta_w or {}).get(index, 0.0) if is_user else None,
            ),
        )
        board.push(move)
    connection.commit()


# ── graph shape ───────────────────────────────────────────────────────────── #


def test_graph_is_empty_for_an_empty_book(tmp_path: Path) -> None:
    body = client_for(create_app(tmp_path / "t.db")).get(
        "/api/opening-book/graph"
    ).json()
    assert body["nodes"] == []
    assert body["links"] == []


def test_a_shared_prefix_is_one_node_not_one_per_line(tmp_path: Path) -> None:
    """The line list duplicates shared prefixes, which is right for text and
    wrong for a picture: 1.e4 must be one circle with two children."""

    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. e4 c5 2. Nf3")
    import_line(client, "1. e4 e5 2. Nf3")

    body = client.get("/api/opening-book/graph").json()
    keys = [n["key"] for n in body["nodes"]]
    assert len(keys) == len(set(keys)), "no duplicate circles"

    after_e4 = next(n for n in body["nodes"] if n["san"] == "e4")
    assert after_e4["children"] == 2, "c5 and e5 hang off the same node"


def test_every_node_but_the_root_knows_its_parent_and_move(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. e4 c5 2. Nf3")
    body = client.get("/api/opening-book/graph").json()

    roots = [n for n in body["nodes"] if n["parent"] is None]
    assert len(roots) == 1
    assert roots[0]["key"] == START_KEY
    for node in body["nodes"]:
        if node["parent"] is not None:
            assert node["san"], "an edge into a node names the move that made it"
            assert node["role"] in ("mine", "theirs")


def test_links_match_the_nodes_they_join(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. d4 Nf6 2. c4")
    body = client.get("/api/opening-book/graph").json()
    keys = {n["key"] for n in body["nodes"]}
    for link in body["links"]:
        assert link["from"] in keys and link["to"] in keys


def test_a_transposition_is_reported_not_drawn_as_a_second_parent(
    tmp_path: Path,
) -> None:
    """A node with two parents has no place on a layered layout, so the second
    link becomes a hint rather than a branch."""

    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. d4 Nf6 2. c4 e6 3. Nf3")
    import_line(client, "1. Nf3 Nf6 2. c4 e6 3. d4")

    body = client.get("/api/opening-book/graph").json()
    parents = [n["parent"] for n in body["nodes"] if n["parent"] is not None]
    assert len(parents) == len(body["links"])
    assert body["transpositions"], "the second route in is kept, not silently dropped"


def test_graph_carries_the_book_totals(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. e4 c5 2. Nf3")
    body = client.get("/api/opening-book/graph").json()
    assert body["moves"] == 3
    assert body["decisions"] == 2
    assert body["color"] == "white"


# ── the colour ────────────────────────────────────────────────────────────── #


def test_a_node_you_have_never_reached_is_grey_not_neutral(tmp_path: Path) -> None:
    """`None` and 0.5 are different facts: 'never been here' is not 'exactly
    average here', and collapsing them invents data."""

    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. e4 c5")
    body = client.get("/api/opening-book/graph").json()
    for entry in body["stats"]["nodes"].values():
        assert entry["health"] is None
        assert entry["games"] == 0


def test_losing_from_a_node_pulls_it_toward_red(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3")
    for i in range(8):
        seed_review(db_path, review_id=f"loss{i}", moves="1. e4 c5 2. Nf3", result="0-1")

    body = client.get("/api/opening-book/graph").json()
    after_e4 = next(n for n in body["nodes"] if n["san"] == "e4")
    entry = body["stats"]["nodes"][after_e4["key"]]
    assert entry["games"] == 8
    assert entry["health"] < 0.5
    assert entry["score_pct"] == 0.0
    assert entry["notable"] is True


def test_winning_from_a_node_pulls_it_toward_green(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3")
    for i in range(8):
        seed_review(db_path, review_id=f"win{i}", moves="1. e4 c5 2. Nf3", result="1-0")

    body = client.get("/api/opening-book/graph").json()
    after_e4 = next(n for n in body["nodes"] if n["san"] == "e4")
    entry = body["stats"]["nodes"][after_e4["key"]]
    assert entry["health"] > 0.5
    assert entry["score_pct"] == 1.0


def test_two_games_are_shrunk_toward_neutral(tmp_path: Path) -> None:
    """Two games is not evidence of a disaster — but it is not nothing either,
    so the node glows faintly rather than screaming or vanishing."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3")
    for i in range(2):
        seed_review(db_path, review_id=f"few{i}", moves="1. e4 c5 2. Nf3", result="0-1")
    thin = client.get("/api/opening-book/graph").json()
    thin_entry = next(
        v for k, v in thin["stats"]["nodes"].items() if v["games"] == 2
    )

    for i in range(20):
        seed_review(db_path, review_id=f"many{i}", moves="1. e4 c5 2. Nf3", result="0-1")
    thick = client.get("/api/opening-book/graph").json()
    thick_entry = next(
        v for v in thick["stats"]["nodes"].values() if v["games"] == 22
    )

    assert thin_entry["health"] > thick_entry["health"], "less evidence, less colour"
    assert thin_entry["confidence"] < thick_entry["confidence"]


def test_colour_is_measured_against_opposition_not_against_fifty_percent(
    tmp_path: Path,
) -> None:
    """Raw win% paints a whole repertoire red for anyone who plays up. Scoring
    50% against much stronger players is a good result, not a leak."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3")
    for i in range(10):
        seed_review(
            db_path,
            review_id=f"up{i}",
            moves="1. e4 c5 2. Nf3",
            result="1-0" if i % 2 else "0-1",
            own_rating=1500,
            opp_rating=1900,
        )

    body = client.get("/api/opening-book/graph").json()
    entry = next(v for v in body["stats"]["nodes"].values() if v["games"] == 10)
    assert entry["score_pct"] == 0.5
    assert entry["par_pct"] < 0.25, "expectancy against 1900s is low"
    assert entry["health"] > 0.5, "50% against 1900s must not read as red"


def test_accuracy_is_measured_against_the_players_own_norm(tmp_path: Path) -> None:
    """A careful player and a wild one otherwise get incomparable colours; the
    question is always 'is this line worse *for me*'."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3")
    import_line(client, "1. d4 d5 2. c4")
    # Clean in the d4 games, sloppy in the e4 ones, results identical.
    for i in range(6):
        seed_review(
            db_path, review_id=f"clean{i}", moves="1. d4 d5 2. c4", result="1/2-1/2",
            delta_w={1: 0.5, 3: 0.5},
        )
        seed_review(
            db_path, review_id=f"messy{i}", moves="1. e4 c5 2. Nf3", result="1/2-1/2",
            delta_w={1: 18.0, 3: 18.0},
        )

    body = client.get("/api/opening-book/graph").json()
    by_san = {n["san"]: n["key"] for n in body["nodes"] if n["san"]}
    # Accuracy belongs to the node the move was played FROM, so the nodes that
    # carry it are the ones where it is the player's turn — here, after the
    # opponent's first reply. An opponent-to-move node has no accuracy at all,
    # and its colour rests on results alone.
    clean = body["stats"]["nodes"][by_san["d5"]]
    messy = body["stats"]["nodes"][by_san["c5"]]

    assert clean["accuracy"] is not None and messy["accuracy"] is not None
    assert clean["accuracy"] > messy["accuracy"]
    assert clean["accuracy_delta"] > 0 > messy["accuracy_delta"]
    assert clean["health"] > messy["health"]


def test_an_opponent_to_move_node_has_no_accuracy_of_its_own(
    tmp_path: Path,
) -> None:
    """You played no move there, so there is nothing to be accurate about — the
    node is coloured on results alone rather than on an invented number."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3")
    for i in range(6):
        seed_review(db_path, review_id=f"g{i}", moves="1. e4 c5 2. Nf3", result="1-0")

    body = client.get("/api/opening-book/graph").json()
    after_e4 = next(n for n in body["nodes"] if n["san"] == "e4")
    entry = body["stats"]["nodes"][after_e4["key"]]
    assert entry["games"] == 6
    assert entry["accuracy"] is None
    assert entry["health"] is not None, "results still colour it"


def test_accuracy_is_compared_at_the_same_ply(tmp_path: Path) -> None:
    """A first move is near-perfect for everyone. Measured against an
    all-depths mean it looks unusually good, and the top of every tree glows
    green for an artefact of depth rather than a fact about the repertoire."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. e4 c5 2. Nf3 d6 3. d4")
    for i in range(8):
        seed_review(
            db_path,
            review_id=f"g{i}",
            moves="1. e4 c5 2. Nf3 d6 3. d4",
            result="1/2-1/2",
            # Clean at move one, sloppy deeper — the usual shape of a real game.
            delta_w={1: 0.5, 3: 14.0, 5: 16.0},
        )

    body = client.get("/api/opening-book/graph").json()
    root = next(n for n in body["nodes"] if n["parent"] is None)
    entry = body["stats"]["nodes"][root["key"]]

    assert entry["accuracy"] > 95, "ply 1 really is played accurately"
    assert entry["accuracy_delta"] is None or abs(entry["accuracy_delta"]) < 1.0, (
        "but it is unremarkable against other ply-1 moves, so it must not "
        "read as an improvement"
    )


def test_a_transposed_node_collects_games_from_both_move_orders(
    tmp_path: Path,
) -> None:
    """The whole reason identity is the position rather than the path."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. d4 Nf6 2. c4 e6 3. Nf3")
    for i in range(3):
        seed_review(
            db_path, review_id=f"a{i}", moves="1. d4 Nf6 2. c4 e6 3. Nf3", result="1-0"
        )
    for i in range(3):
        seed_review(
            db_path, review_id=f"b{i}", moves="1. Nf3 Nf6 2. c4 e6 3. d4", result="1-0"
        )

    body = client.get("/api/opening-book/graph").json()
    # The two orders meet after FIVE plies, with Black to move. After four they
    # are different positions — the side to move is part of the identity.
    target = chess.Board()
    for san in ("d4", "Nf6", "c4", "e6", "Nf3"):
        target.push_san(san)
    from core.repertoire import position_key

    entry = body["stats"]["nodes"].get(position_key(target.fen()))
    assert entry is not None
    assert entry["games"] == 6, "both move orders reach the same circle"


def test_a_repetition_does_not_count_a_game_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    import_line(client, "1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3")
    seed_review(db_path, review_id="rep", moves="1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3", result="1-0")

    body = client.get("/api/opening-book/graph").json()
    for entry in body["stats"]["nodes"].values():
        assert entry["games"] <= 1


def test_stats_can_be_turned_off(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    import_line(client, "1. e4 c5")
    body = client.get("/api/opening-book/graph?stats=false").json()
    assert "stats" not in body


def test_graph_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "t.db"), raise_server_exceptions=False)
    assert client.get("/api/opening-book/graph").status_code == 401


# ── the health function on its own ────────────────────────────────────────── #


def test_health_is_none_without_games() -> None:
    out = opening_tree_stats.health_for(
        opening_tree_stats.NodeStats(), {"accuracy": 70.0, "score_shift": 0.0}
    )
    assert out["health"] is None


def test_health_saturates_rather_than_running_away() -> None:
    """A node 40 points below expectancy over three games is not four times
    worse than one 10 points below; it is 'bad', and the scale should stop."""

    baseline = {"accuracy": None, "score_shift": 0.0, "score_pct": 0.5}
    bad = opening_tree_stats.NodeStats(games=40, points=0.0, expected=20.0, rated_games=40)
    worse = opening_tree_stats.NodeStats(games=40, points=0.0, expected=36.0, rated_games=40)
    a = opening_tree_stats.health_for(bad, baseline)["health"]
    b = opening_tree_stats.health_for(worse, baseline)["health"]
    assert a == b, "both are clipped to the floor"
    assert 0.0 <= a <= 1.0
