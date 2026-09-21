"""Opening builder API (server/openings_build*.py, server/opening_cache.py).

Engine-free and network-free. Stockfish arrives through ``app.state.analyze_fn``
and Maia through ``app.state.maia_topk_fn``, both replaced with fakes here; the
Lichess explorer arrives through ``server.opening_cache.FETCH_JSON``, monkey-
patched the same way ``chess_vol.server.ENGINE_FACTORY`` is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chess
import pytest
from fastapi.testclient import TestClient

from server import opening_cache
from server.main import create_app

START = chess.Board().fen()
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


def client_for(app: Any, email: str = "builder@x.com") -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/api/auth/register", json={"email": email, "password": "secret1"})
    return c


class FakeExplorer:
    """Scripted explorer. Keys on the corpus and the FEN in the query string."""

    def __init__(self, by_fen: dict[str, dict[str, Any]] | None = None) -> None:
        self.by_fen = by_fen or {}
        self.calls: list[str] = []

    def __call__(self, url: str, user_agent: str) -> Any:
        self.calls.append(url)
        for fen, payload in self.by_fen.items():
            if chess.Board(fen).board_fen() in url.replace("%2F", "/"):
                return payload
        return {"moves": [], "white": 0, "draws": 0, "black": 0}


def explorer_payload(*specs: tuple[str, str, int, int, int]) -> dict[str, Any]:
    return {
        "white": sum(s[2] for s in specs),
        "draws": sum(s[3] for s in specs),
        "black": sum(s[4] for s in specs),
        "opening": {"name": "King's Pawn Game", "eco": "B00"},
        "moves": [
            {
                "uci": uci,
                "san": san,
                "white": w,
                "draws": d,
                "black": b,
                "averageRating": 1550,
            }
            for uci, san, w, d, b in specs
        ],
    }


class FakeAnalyzer:
    def __init__(self, moves: list[tuple[str, int]]) -> None:
        self.moves = moves
        self.calls = 0

    def __call__(self, fen: str, depth: int = 18, multipv: int = 6) -> dict[str, Any]:
        self.calls += 1
        return {"top_moves": [{"move": u, "eval": cp, "pv": []} for u, cp in self.moves]}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to an explorer that answers with nothing, so a test
    that forgets to script one cannot silently reach the real network."""

    monkeypatch.setattr(opening_cache, "FETCH_JSON", FakeExplorer())


# ── auth ──────────────────────────────────────────────────────────────────── #


def test_requires_auth(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "t.db"), raise_server_exceptions=False)
    assert client.get("/api/opening-book/candidates").status_code == 401
    assert client.get("/api/opening-book/tree").status_code == 401


# ── candidates and the three dots ─────────────────────────────────────────── #


def test_candidates_light_all_three_dots_when_the_sources_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        opening_cache,
        "FETCH_JSON",
        FakeExplorer({START: explorer_payload(
            ("e2e4", "e4", 600, 100, 300), ("d2d4", "d4", 200, 50, 250)
        )}),
    )
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = FakeAnalyzer([("e2e4", 30), ("d2d4", 25)])
    app.state.maia_topk_fn = lambda fen, net=1900, k=3: ["e2e4", "d2d4"]

    body = client_for(app).get("/api/opening-book/candidates").json()
    by_uci = {c["uci"]: c for c in body["candidates"]}

    assert by_uci["e2e4"]["signals"] == {"engine": True, "human": True, "results": True}
    assert by_uci["d2d4"]["signals"] == {
        "engine": False, "human": False, "results": False
    }
    assert body["sources"]["maia_rating"] == 1900


def test_each_dot_goes_unknown_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real state of a box with Stockfish but no Maia and no network: the
    engine dot answers and the other two are grey, not dark."""

    monkeypatch.setattr(opening_cache, "FETCH_JSON", None)
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = FakeAnalyzer([("e2e4", 30), ("d2d4", 25)])
    app.state.maia_topk_fn = lambda fen, net=1900, k=3: None

    body = client_for(app).get(
        "/api/opening-book/candidates?network=false"
    ).json()
    by_uci = {c["uci"]: c for c in body["candidates"]}

    assert by_uci["e2e4"]["signals"]["engine"] is True
    assert by_uci["e2e4"]["signals"]["human"] is None
    assert by_uci["e2e4"]["signals"]["results"] is None
    assert body["sources"]["peers"] == "off"
    assert body["sources"]["maia"] == "unavailable"


def test_a_missing_engine_does_not_500_the_panel(tmp_path: Path) -> None:
    """Stockfish absent is the common case on a fresh install. The candidate
    list must still render from the explorer alone."""

    def boom(*args: object, **kwargs: object) -> dict[str, Any]:
        raise FileNotFoundError("stockfish")

    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = boom
    app.state.maia_topk_fn = lambda *a, **k: None

    resp = client_for(app).get("/api/opening-book/candidates")
    assert resp.status_code == 200
    assert resp.json()["sources"]["engine"] == "unavailable"


def test_black_candidates_score_from_blacks_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        opening_cache,
        "FETCH_JSON",
        FakeExplorer({AFTER_E4: explorer_payload(
            ("e7e5", "e5", 700, 50, 250),   # White scores well — bad for Black
            ("c7c5", "c5", 300, 60, 640),   # Black scores well
        )}),
    )
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = None
    app.state.maia_topk_fn = None

    body = client_for(app).get(
        f"/api/opening-book/candidates?color=black&fen={AFTER_E4}"
    ).json()
    by_uci = {c["uci"]: c for c in body["candidates"]}

    assert by_uci["c7c5"]["signals"]["results"] is True
    assert by_uci["e7e5"]["signals"]["results"] is False
    assert by_uci["c7c5"]["peer_wins"] == 640
    assert body["user_to_move"] is True


def test_an_invalid_fen_is_a_422_not_a_500(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    resp = client.get("/api/opening-book/candidates?fen=not-a-fen")
    assert resp.status_code == 422


# ── the explorer cache ────────────────────────────────────────────────────── #


def test_the_explorer_is_asked_once_per_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeExplorer({START: explorer_payload(("e2e4", "e4", 10, 10, 10))})
    monkeypatch.setattr(opening_cache, "FETCH_JSON", fake)
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = None
    app.state.maia_topk_fn = None
    client = client_for(app)

    client.get("/api/opening-book/candidates")
    first = len(fake.calls)
    client.get("/api/opening-book/candidates")

    assert first == 2, "one masters call and one peers call"
    assert len(fake.calls) == first, "second request is served from cache"


def test_a_cached_position_works_with_the_network_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeExplorer({START: explorer_payload(("e2e4", "e4", 600, 100, 300))})
    monkeypatch.setattr(opening_cache, "FETCH_JSON", fake)
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = None
    app.state.maia_topk_fn = None
    client = client_for(app)
    client.get("/api/opening-book/candidates")  # warm

    body = client.get("/api/opening-book/candidates?network=false").json()
    assert body["sources"]["peers"] == "cache"
    assert body["candidates"], "a warmed position is fully usable offline"


def test_an_unreachable_explorer_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(url: str, ua: str) -> Any:
        raise OSError("network is unreachable")

    monkeypatch.setattr(opening_cache, "FETCH_JSON", refuse)
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = FakeAnalyzer([("e2e4", 30)])
    app.state.maia_topk_fn = None

    resp = client_for(app).get("/api/opening-book/candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]["peers"] == "unavailable"
    assert body["candidates"][0]["signals"]["results"] is None


def test_the_rating_band_is_part_of_the_cache_key(tmp_path: Path) -> None:
    from server.opening_cache import params_key

    assert params_key(ratings=[1400, 1600], speeds=["blitz"]) != params_key(
        ratings=[1800, 2000], speeds=["blitz"]
    )
    assert params_key(ratings=[1600, 1400], speeds=["rapid", "blitz"]) == params_key(
        ratings=[1400, 1600], speeds=["blitz", "rapid"]
    ), "argument order must not fork the cache"


# ── the tree ──────────────────────────────────────────────────────────────── #


def test_adding_a_move_creates_the_node_and_its_child(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    resp = client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": START, "uci": "e2e4", "path": []},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "mine"

    tree = client.get("/api/opening-book/tree?color=white").json()
    assert tree["decisions"] == 1
    assert len(tree["nodes"]) == 2, "the position before and the position after"


def test_role_is_derived_from_whose_move_it_is(tmp_path: Path) -> None:
    """An opponent reply is covered, not drilled. Deriving the role rather than
    accepting it is what stops a repertoire quizzing you on Black's moves."""

    client = client_for(create_app(tmp_path / "t.db"))
    client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": START, "uci": "e2e4", "path": []},
    )
    resp = client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": AFTER_E4, "uci": "e7e5", "path": ["e2e4"]},
    )
    assert resp.json()["role"] == "theirs"

    tree = client.get("/api/opening-book/tree?color=white").json()
    assert tree["moves"] == 2
    assert tree["decisions"] == 1, "only the White move is a decision"


def test_transpositions_land_on_one_node(tmp_path: Path) -> None:
    """1.d4 Nf6 2.c4 and 1.c4 Nf6 2.d4 reach the same position. Keying on
    `fen_key` means a move chosen down one order is already chosen down the
    other — the spec's §6.4 requirement."""

    client = client_for(create_app(tmp_path / "t.db"))
    board = chess.Board()
    for san in ("d4", "Nf6", "c4"):
        client.post(
            "/api/opening-book/edges",
            json={"color": "white", "fen": board.fen(),
                  "uci": board.parse_san(san).uci(), "path": []},
        )
        board.push_san(san)
    target = board.fen()

    other = chess.Board()
    for san in ("c4", "Nf6", "d4"):
        client.post(
            "/api/opening-book/edges",
            json={"color": "white", "fen": other.fen(),
                  "uci": other.parse_san(san).uci(), "path": []},
        )
        other.push_san(san)

    tree = client.get("/api/opening-book/tree?color=white").json()
    keys = [n["fen_key"] for n in tree["nodes"]]
    assert len(keys) == len(set(keys)), "no duplicate positions"
    assert " ".join(target.split(" ")[:4]) in keys


def test_a_chess_js_fen_and_a_python_chess_fen_are_one_node(tmp_path: Path) -> None:
    """chess.js writes the ep square after every double push; python-chess only
    when a capture is legal. The browser therefore sends `... b KQkq e3` for the
    position the server computes as `... b KQkq -`. Keyed on the raw FEN those
    are two nodes, the explorer cache forks, and transposition merging silently
    stops working — so both must normalize to the same node."""

    client = client_for(create_app(tmp_path / "t.db"))
    js_fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"

    # The server reaches the position by pushing the move...
    client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": START, "uci": "e2e4", "path": []},
    )
    # ...the client arrives at the same position with its own spelling.
    client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": js_fen, "uci": "e7e5", "path": ["e2e4"]},
    )

    tree = client.get("/api/opening-book/tree?color=white").json()
    keys = [n["fen_key"] for n in tree["nodes"]]
    assert len(keys) == len(set(keys)), f"duplicate positions: {keys}"
    assert sum("e3" in k for k in keys) == 0, "the unusable ep square must be dropped"


def test_a_capturable_en_passant_square_is_still_its_own_position(
    tmp_path: Path,
) -> None:
    """Normalizing must not go too far: when the capture IS legal the two
    positions genuinely differ, and the spec forbids merging them (§6.4)."""

    from core.repertoire import position_key

    with_ep = "rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3"
    without = "rnbqkbnr/ppp1pppp/8/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3"
    assert position_key(with_ep) != position_key(without)


def test_an_illegal_move_is_rejected(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    resp = client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": START, "uci": "e2e5", "path": []},
    )
    assert resp.status_code == 422


def test_removing_a_move_leaves_the_rest_of_the_tree(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    for uci in ("e2e4", "d2d4"):
        client.post(
            "/api/opening-book/edges",
            json={"color": "white", "fen": START, "uci": uci, "path": []},
        )
    resp = client.post(
        "/api/opening-book/edges/delete",
        json={"color": "white", "fen": START, "uci": "d2d4"},
    )
    assert resp.json()["removed"] is True

    tree = client.get("/api/opening-book/tree?color=white").json()
    assert [e["uci"] for e in tree["edges"]] == ["e2e4"]


def test_chosen_moves_are_flagged_on_the_candidate_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        opening_cache,
        "FETCH_JSON",
        FakeExplorer({START: explorer_payload(("e2e4", "e4", 10, 10, 10))}),
    )
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = None
    app.state.maia_topk_fn = None
    client = client_for(app)
    client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": START, "uci": "e2e4", "path": []},
    )

    body = client.get("/api/opening-book/candidates").json()
    assert body["chosen"] == ["e2e4"]
    assert next(c for c in body["candidates"] if c["uci"] == "e2e4")["in_repertoire"]


def test_lines_are_walked_out_of_the_tree(tmp_path: Path) -> None:
    client = client_for(create_app(tmp_path / "t.db"))
    board = chess.Board()
    for san in ("e4", "e5", "Nf3"):
        client.post(
            "/api/opening-book/edges",
            json={"color": "white", "fen": board.fen(),
                  "uci": board.parse_san(san).uci(), "path": []},
        )
        board.push_san(san)

    tree = client.get("/api/opening-book/tree?color=white").json()
    assert tree["lines"]
    assert [m["san"] for m in tree["lines"][0]["moves"]] == ["e4", "e5", "Nf3"]


# ── coverage ──────────────────────────────────────────────────────────────── #


def test_coverage_is_weighted_by_how_likely_a_reply_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covering the one reply you meet 80% of the time beats covering three you
    meet 20% of the time between them."""

    monkeypatch.setattr(
        opening_cache,
        "FETCH_JSON",
        FakeExplorer({AFTER_E4: explorer_payload(
            ("e7e5", "e5", 400, 0, 400),   # 800 games
            ("c7c5", "c5", 100, 0, 100),   # 200 games
        )}),
    )
    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = None
    app.state.maia_topk_fn = None
    client = client_for(app)
    client.get(f"/api/opening-book/candidates?color=white&fen={AFTER_E4}")  # warm

    before = client.get(
        f"/api/opening-book/coverage?color=white&fen={AFTER_E4}"
    ).json()
    assert before["coverage"] == 0.0
    assert before["uncovered"][0]["uci"] == "e7e5"

    client.post(
        "/api/opening-book/edges",
        json={"color": "white", "fen": AFTER_E4, "uci": "e7e5", "path": ["e2e4"]},
    )
    after = client.get(
        f"/api/opening-book/coverage?color=white&fen={AFTER_E4}"
    ).json()
    assert after["coverage"] == pytest.approx(0.8)


def test_coverage_is_null_not_zero_without_explorer_data(tmp_path: Path) -> None:
    """'You have covered nothing' and 'we do not know what you face' are
    different statements and only one of them is the user's fault."""

    app = create_app(tmp_path / "t.db")
    app.state.analyze_fn = None
    app.state.maia_topk_fn = None
    body = client_for(app).get("/api/opening-book/coverage?color=white").json()
    assert body["coverage"] is None
