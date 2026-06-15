"""Tests for the Chess.com ingest (pipeline/chesscom.py) with stubbed HTTP."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pipeline import chesscom


PGN_WITH_CLK = (
    '[Event "Live Chess"]\n[White "alice"]\n[Black "bob"]\n\n'
    "1. e4 {[%clk 0:03:00]} e5 {[%clk 0:03:00]} 2. Nf3 {[%clk 0:02:55]} *\n"
)
PGN_NO_CLK = '[Event "Daily"]\n[White "alice"]\n[Black "carol"]\n\n1. d4 d5 *\n'


def ts(year: int, month: int, day: int = 15) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def make_fetch(archives: list[str], months: dict[str, dict[str, Any]]) -> chesscom.FetchJson:
    """Build a stub fetch_json returning the archives list then per-URL months.

    Records the User-Agent it was called with on ``.agents``.
    """

    calls: dict[str, Any] = {"agents": []}

    def fetch(url: str, user_agent: str) -> dict[str, Any]:
        calls["agents"].append(user_agent)
        if url.endswith("/games/archives"):
            return {"archives": archives}
        return months.get(url, {"games": []})

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def base_game(**over: Any) -> dict[str, Any]:
    game = {
        "rules": "chess",
        "time_class": "blitz",
        "rated": True,
        "end_time": ts(2026, 6, 1),
        "url": "https://chess.com/game/1",
        "uuid": "uuid-1",
        "pgn": PGN_WITH_CLK,
        "white": {"username": "alice", "rating": 1500, "result": "win"},
        "black": {"username": "bob", "rating": 1490, "result": "checkmated"},
    }
    game.update(over)
    return game


SINCE = datetime(2026, 5, 1, tzinfo=timezone.utc)
ARCHIVE_JUN = "https://api.chess.com/pub/player/alice/games/2026/06"
ARCHIVE_OLD = "https://api.chess.com/pub/player/alice/games/2025/01"


def test_iter_games_yields_eligible_and_sets_color() -> None:
    fetch = make_fetch([ARCHIVE_JUN], {ARCHIVE_JUN: {"games": [base_game()]}})

    results = list(chesscom.iter_games("alice", SINCE, fetch_json=fetch, user_agent="UA/1.0"))

    assert len(results) == 1
    pgn, meta = results[0]
    assert pgn == PGN_WITH_CLK
    assert meta["user_color"] == "white"
    assert meta["opponent"] == "bob"
    assert meta["time_class"] == "blitz"
    assert meta["date"] == "2026-06-01"
    # User-Agent threaded through on every request.
    assert fetch.calls["agents"] == ["UA/1.0", "UA/1.0"]  # type: ignore[attr-defined]


def test_user_color_black_when_username_is_black() -> None:
    fetch = make_fetch([ARCHIVE_JUN], {ARCHIVE_JUN: {"games": [base_game()]}})
    (_, meta), = list(chesscom.iter_games("bob", SINCE, fetch_json=fetch))
    assert meta["user_color"] == "black"
    assert meta["opponent"] == "alice"


def test_filters_variants_old_clockless_and_foreign_games() -> None:
    games = [
        base_game(rules="chess960", url="g960"),                 # variant
        base_game(end_time=ts(2026, 1, 1), url="too-old"),       # before since
        base_game(pgn=PGN_NO_CLK, url="no-clk"),                 # no clocks
        base_game(white={"username": "x", "result": "win"},
                  black={"username": "y", "result": "resigned"}, url="foreign"),  # user absent
        base_game(black={"username": "bob", "result": "abandoned"},
                  white={"username": "alice", "result": "win"}, url="aband"),     # abandoned
        base_game(url="https://chess.com/game/keep"),            # the only keeper
    ]
    fetch = make_fetch([ARCHIVE_JUN], {ARCHIVE_JUN: {"games": games}})

    results = list(chesscom.iter_games("alice", SINCE, fetch_json=fetch))

    assert len(results) == 1
    assert results[0][1]["url"] == "https://chess.com/game/keep"


def test_old_archive_month_is_not_fetched() -> None:
    fetched: list[str] = []

    def fetch(url: str, user_agent: str) -> dict[str, Any]:
        fetched.append(url)
        if url.endswith("/games/archives"):
            return {"archives": [ARCHIVE_OLD, ARCHIVE_JUN]}
        return {"games": [base_game()]}

    list(chesscom.iter_games("alice", SINCE, fetch_json=fetch))

    assert ARCHIVE_OLD not in fetched          # month before `since` skipped
    assert ARCHIVE_JUN in fetched


def test_delay_called_between_months() -> None:
    fetch = make_fetch([ARCHIVE_JUN], {ARCHIVE_JUN: {"games": []}})
    slept: list[float] = []
    list(chesscom.iter_games("alice", SINCE, fetch_json=fetch, delay_s=0.5, sleep=slept.append))
    assert slept == [0.5]
