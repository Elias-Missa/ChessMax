"""Prep suggestions from ROI, and PGN import into the book. Engine-free.

Both features are pure database work — the prep list re-uses the ranking
Insights already computed and the importer only replays moves — so nothing here
needs Stockfish, Maia or the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import chess
import pytest
from fastapi.testclient import TestClient

from server import db, opening_cache, opening_prep
from server.main import create_app


def client_for(app: Any, email: str = "prep@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opening_cache, "FETCH_JSON", lambda url, ua: {"moves": []})


def pgn_for(moves: str, opening: str = "Sicilian Defense") -> str:
    return f'[Event "x"]\n[Opening "{opening}"]\n[Result "*"]\n\n{moves} *\n'


def seed_run(
    db_path: Path,
    *,
    user_id: int = 1,
    roi_rows: list[dict[str, Any]] | None = None,
    games: dict[str, str] | None = None,
    status: str = "complete",
) -> None:
    """One completed Insights run carrying an ROI table, plus its games."""

    connection = db.connect(db_path)
    for game_id, pgn in (games or {}).items():
        connection.execute(
            "INSERT OR REPLACE INTO games (game_id, source, pgn) VALUES (?, 'pgn', ?)",
            (game_id, pgn),
        )
    metrics = {"pro": {"openings": {"roi": {"rows": roi_rows or []}}}}
    connection.execute(
        """
        INSERT OR REPLACE INTO insight_runs
            (run_id, user_id, chesscom_handle, source, window_days, time_class,
             status, metrics)
        VALUES ('run-1', ?, 'someone', 'chesscom', 90, 'blitz', ?, ?)
        """,
        (user_id, status, json.dumps(metrics)),
    )
    connection.commit()


def roi_row(**kwargs: Any) -> dict[str, Any]:
    base = {
        "opening": "Sicilian Defense",
        "eco": "B20",
        "color": "white",
        "n": 12,
        "share": 0.18,
        "score_pct": 0.33,
        "par_pct": 0.51,
        "points_per_100_games": 7.4,
        "roi_score": 9.1,
        "accuracy_gap": 4.0,
        "game_ids": [],
    }
    base.update(kwargs)
    return base


# ── the pure prefix logic ─────────────────────────────────────────────────── #


def test_common_prefix_takes_the_super_majority_line() -> None:
    """A strict common prefix lets one odd game cut the line to nothing; the
    position before *that* is not where preparation ran out."""

    lines = [
        ["e2e4", "c7c5", "g1f3"],
        ["e2e4", "c7c5", "g1f3"],
        ["e2e4", "c7c5", "b1c3"],  # the odd one
    ]
    assert opening_prep.common_prefix(lines, share=0.6) == ["e2e4", "c7c5", "g1f3"]


def test_common_prefix_stops_where_the_player_actually_diverges() -> None:
    lines = [
        ["e2e4", "c7c5", "g1f3"],
        ["e2e4", "c7c5", "b1c3"],
        ["e2e4", "c7c5", "c2c3"],
    ]
    assert opening_prep.common_prefix(lines, share=0.6) == ["e2e4", "c7c5"]


def test_common_prefix_of_nothing_is_empty() -> None:
    assert opening_prep.common_prefix([]) == []
    assert opening_prep.common_prefix([[]]) == []


def test_line_to_position_replays_to_a_fen() -> None:
    fen, sans = opening_prep.line_to_position(["e2e4", "c7c5"])
    assert sans == ["e4", "c5"]
    assert chess.Board(fen).fullmove_number == 2


def test_line_to_position_stops_at_an_illegal_move() -> None:
    fen, sans = opening_prep.line_to_position(["e2e4", "e2e4"])
    assert sans == ["e4"]


def test_roi_rows_survives_an_older_metrics_blob() -> None:
    """Runs are immutable snapshots kept for ten generations, so an old one may
    predate `pro.openings.roi` entirely and must read as 'no suggestions'."""

    assert opening_prep.roi_rows({}) == []
    assert opening_prep.roi_rows({"pro": {}}) == []
    assert opening_prep.roi_rows({"pro": {"openings": {}}}) == []
    assert opening_prep.roi_rows({"pro": {"openings": {"roi": {"rows": "nope"}}}}) == []


# ── the prep endpoint ─────────────────────────────────────────────────────── #


def test_prep_says_to_run_insights_when_there_is_no_run(tmp_path: Path) -> None:
    """'Run Insights first' and 'your repertoire is fine' are different answers
    and an empty list cannot tell them apart."""

    body = client_for(create_app(tmp_path / "t.db")).get(
        "/api/opening-book/prep"
    ).json()
    assert body["available"] is False
    assert "Insights" in body["reason"]
    assert body["suggestions"] == []


def test_prep_ranks_by_the_roi_insights_already_computed(tmp_path: Path) -> None:
    """Never re-ranked here: a second ranking that can disagree with the
    dashboard is worse than one that can be wrong."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(
        db_path,
        roi_rows=[
            roi_row(opening="Small Leak", roi_score=2.0, points_per_100_games=2.0),
            roi_row(opening="Big Leak", roi_score=9.0, points_per_100_games=8.0),
        ],
    )
    body = client.get("/api/opening-book/prep").json()
    assert [s["opening"] for s in body["suggestions"]] == ["Big Leak", "Small Leak"]


def test_prep_drops_lines_below_the_points_floor(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(db_path, roi_rows=[roi_row(points_per_100_games=0.2, roi_score=0.2)])
    body = client.get("/api/opening-book/prep").json()
    assert body["available"] is True
    assert body["suggestions"] == []
    assert "below par" in body["reason"]


def test_prep_points_at_the_position_the_games_actually_reach(tmp_path: Path) -> None:
    """The ROI row names an opening; the prep suggestion has to name a
    position, and it comes from replaying the games behind the row."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(
        db_path,
        roi_rows=[roi_row(game_ids=["g1", "g2", "g3"])],
        games={
            "g1": pgn_for("1. e4 c5 2. Nf3 d6"),
            "g2": pgn_for("1. e4 c5 2. Nf3 Nc6"),
            "g3": pgn_for("1. e4 c5 2. Nf3 e6"),
        },
    )
    (s,) = client.get("/api/opening-book/prep").json()["suggestions"]
    assert s["line_san"] == ["e4", "c5", "Nf3"]
    assert s["plies"] == 3
    assert chess.Board(s["fen"]).turn is chess.BLACK
    assert s["in_book"] is False


def test_prep_reports_what_the_book_already_covers(tmp_path: Path) -> None:
    """A leak the book already answers is not a to-do."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(
        db_path,
        roi_rows=[roi_row(game_ids=["g1", "g2"])],
        games={"g1": pgn_for("1. e4 c5 2. Nf3"), "g2": pgn_for("1. e4 c5 2. Nf3")},
    )
    before = client.get("/api/opening-book/prep").json()["suggestions"][0]
    assert before["first_gap_ply"] == 0, "no move chosen at the start position"

    client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": chess.Board().fen(), "uci": "e2e4", "path": []},
    )
    after = client.get("/api/opening-book/prep").json()["suggestions"][0]
    assert after["first_gap_ply"] == 2, "e4 is answered; move 2 is the next decision"
    assert "runs out" in after["why"]
    assert "Nothing chosen" in before["why"], "an empty book is not 'no answer by move 1'"


def test_prep_only_counts_the_players_own_turns_as_gaps(tmp_path: Path) -> None:
    """An unanswered opponent reply is a branch not yet explored, not a hole in
    the repertoire — counting it would report a gap at every other ply."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(
        db_path,
        roi_rows=[roi_row(color="black", game_ids=["g1"])],
        games={"g1": pgn_for("1. e4 c5 2. Nf3")},
    )
    (s,) = client.get("/api/opening-book/prep").json()["suggestions"]
    assert s["color"] == "black"
    # As Black the first decision is at ply 1 (after 1.e4), not ply 0.
    assert s["first_gap_ply"] == 1


def test_prep_says_prep_will_not_help_when_accuracy_is_already_good(
    tmp_path: Path,
) -> None:
    """ROI's attribution term already demotes these; the copy says why so the
    ranking is legible rather than mysterious."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(db_path, roi_rows=[roi_row(accuracy_gap=-6.0, game_ids=[])])
    (s,) = client.get("/api/opening-book/prep").json()["suggestions"]
    assert "lost later" in s["why"]


def test_prep_copy_carries_no_jargon(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(db_path, roi_rows=[roi_row(game_ids=["g1"])], games={"g1": pgn_for("1. e4")})
    for s in client.get("/api/opening-book/prep").json()["suggestions"]:
        lowered = s["why"].lower()
        for word in ("volatility", "findability", "expectation-adjusted", "δw", "delta_w"):
            assert word not in lowered


def test_prep_ignores_an_unfinished_run(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(db_path, roi_rows=[roi_row()], status="running")
    assert client.get("/api/opening-book/prep").json()["available"] is False


def test_prep_survives_a_game_whose_pgn_will_not_parse(tmp_path: Path) -> None:
    """One malformed archive row must not cost the user their whole prep list."""

    db_path = tmp_path / "t.db"
    client = client_for(create_app(db_path))
    seed_run(
        db_path,
        roi_rows=[roi_row(game_ids=["good", "bad"])],
        games={"good": pgn_for("1. e4 c5"), "bad": "!!! not a pgn !!!"},
    )
    (s,) = client.get("/api/opening-book/prep").json()["suggestions"]
    assert s["line_san"] == ["e4", "c5"]


# ── PGN import ────────────────────────────────────────────────────────────── #


def test_import_walks_a_mainline_into_the_book(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    body = client.post(
        "/api/opening-book/import",
        json={"color": "white", "pgn": pgn_for("1. e4 c5 2. Nf3 d6 3. d4")},
    ).json()
    assert body["added"] == 5
    assert body["games"] == 1

    tree = client.get("/api/opening-book/tree?color=white").json()
    assert tree["moves"] == 5
    assert tree["decisions"] == 3, "e4, Nf3 and d4 are White's"
    assert [m["san"] for m in tree["lines"][0]["moves"]] == ["e4", "c5", "Nf3", "d6", "d4"]


def test_import_takes_variations_as_branches(tmp_path: Path) -> None:
    """A repertoire PGN is a tree and its branches live in the parentheses.
    Importing only the mainline throws away most of what it carries."""

    client = client_for(create_app(tmp_path / "t.db"))
    body = client.post(
        "/api/opening-book/import",
        json={"color": "white", "pgn": pgn_for("1. e4 c5 (1... e5 2. Nf3) 2. Nf3")},
    ).json()
    assert body["added"] == 5, "e4, c5, Nf3, plus e5 and Nf3 from the variation"

    tree = client.get("/api/opening-book/tree?color=white").json()
    sans = {tuple(m["san"] for m in line["moves"]) for line in tree["lines"]}
    assert ("e4", "c5", "Nf3") in sans
    assert ("e4", "e5", "Nf3") in sans


def test_import_derives_role_rather_than_trusting_the_paste(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    client.post(
        "/api/opening-book/import",
        json={"color": "black", "pgn": pgn_for("1. e4 c5 2. Nf3 d6")},
    )
    tree = client.get("/api/opening-book/tree?color=black").json()
    roles = {m["san"]: m["role"] for line in tree["lines"] for m in line["moves"]}
    assert roles["c5"] == "mine" and roles["d6"] == "mine"
    assert roles["e4"] == "theirs" and roles["Nf3"] == "theirs"


def test_importing_twice_is_idempotent(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    payload = {"color": "white", "pgn": pgn_for("1. e4 c5 2. Nf3")}
    first = client.post("/api/opening-book/import", json=payload).json()
    second = client.post("/api/opening-book/import", json=payload).json()
    assert first["added"] == 3
    assert second["added"] == 0
    assert second["already"] == 3
    assert client.get("/api/opening-book/tree?color=white").json()["moves"] == 3


def test_import_reads_several_games_from_one_paste(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    body = client.post(
        "/api/opening-book/import",
        json={
            "color": "white",
            "pgn": pgn_for("1. e4 c5") + "\n" + pgn_for("1. d4 Nf6", "Indian"),
        },
    ).json()
    assert body["games"] == 2
    assert body["added"] == 4
    assert set(body["openings"]) == {"Sicilian Defense", "Indian"}


def test_import_stops_at_the_depth_limit(tmp_path: Path) -> None:
    """A full game must not file forty plies of middlegame as opening theory."""

    long_game = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5"
    client = client_for(create_app(tmp_path / "t.db"))
    body = client.post(
        "/api/opening-book/import",
        json={"color": "white", "pgn": pgn_for(long_game), "max_plies": 4},
    ).json()
    assert body["added"] == 4
    assert body["skipped_depth"] >= 1


def test_import_rejects_junk_without_raising(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    resp = client.post(
        "/api/opening-book/import",
        json={"color": "white", "pgn": "this is not a pgn at all"},
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 0


def test_import_skips_an_illegal_move_and_keeps_the_legal_ones(
    tmp_path: Path,
) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    body = client.post(
        "/api/opening-book/import",
        json={"color": "white", "pgn": '[Result "*"]\n\n1. e4 c5 2. Qz9 *\n'},
    ).json()
    assert body["added"] == 2, "e4 and c5 survive the bad third move"


def test_import_flags_a_paste_with_none_of_your_own_moves(tmp_path: Path) -> None:
    """Roles are derived, so a repertoire pasted into the wrong side imports
    cleanly and is never drilled — a silent failure the report has to name."""

    client = client_for(create_app(tmp_path / "t.db"))
    # A Black-only line (starts after 1.e4) imported as White: every move in it
    # belongs to the opponent.
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    pgn = f'[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n1... c5 *\n'
    body = client.post(
        "/api/opening-book/import", json={"color": "white", "pgn": pgn}
    ).json()
    assert body["added"] == 1
    assert body["decisions"] == 0
    assert "other colour" in body["message"]

    ok = client.post(
        "/api/opening-book/import", json={"color": "black", "pgn": pgn}
    ).json()
    assert ok["decisions"] == 1
    assert "other colour" not in ok["message"]


def test_import_accepts_a_setup_position(tmp_path: Path) -> None:
    """A study chapter often starts from a FEN rather than move one."""

    fen = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    pgn = f'[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n2. Nf3 d6 *\n'
    client = client_for(create_app(tmp_path / "t.db"))
    body = client.post(
        "/api/opening-book/import", json={"color": "white", "pgn": pgn}
    ).json()
    assert body["added"] == 2

    tree = client.get("/api/opening-book/tree?color=white").json()
    assert any(m["san"] == "Nf3" for line in tree["lines"] for m in line["moves"])


def test_import_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "t.db"), raise_server_exceptions=False)
    resp = client.post(
        "/api/opening-book/import", json={"color": "white", "pgn": "1. e4 *"}
    )
    assert resp.status_code == 401


def test_import_rejects_an_unknown_colour(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    resp = client.post(
        "/api/opening-book/import", json={"color": "purple", "pgn": "1. e4 *"}
    )
    assert resp.status_code == 422


def test_imported_lines_are_drillable_through_the_builder(tmp_path: Path) -> None:
    """The import is only worth anything if the result behaves like a book you
    built by hand — same nodes, same candidate flagging."""

    client = client_for(create_app(tmp_path / "t.db"))
    client.post(
        "/api/opening-book/import",
        json={"color": "white", "pgn": pgn_for("1. e4 c5 2. Nf3")},
    )
    body = client.get("/api/opening-book/candidates").json()
    assert body["chosen"] == ["e2e4"]
