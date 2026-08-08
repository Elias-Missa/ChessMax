"""Tests for Lichess ingest (pipeline/lichess.py) with stubbed HTTP."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from pipeline import lichess

PGN_WITH_CLK = (
    '[Event "Rated blitz game"]\n[White "alice"]\n[Black "bob"]\n\n'
    "1. e4 {[%clk 0:03:00]} e5 {[%clk 0:03:00]} 2. Nf3 {[%clk 0:02:55]} *\n"
)
PGN_NO_CLK = '[Event "Rated"]\n[White "alice"]\n[Black "carol"]\n\n1. d4 d5 *\n'

SINCE = datetime(2026, 5, 1, tzinfo=timezone.utc)


def ndjson_game(**over: Any) -> dict[str, Any]:
    game = {
        "id": "abcdefgh",
        "rated": True,
        "perf": "blitz",
        "createdAt": int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000),
        "status": "mate",
        "winner": "white",
        "players": {
            "white": {"user": {"name": "alice"}, "rating": 1500},
            "black": {"user": {"name": "bob"}, "rating": 1490},
        },
        "opening": {"eco": "C20", "name": "King's Pawn"},
        "pgn": PGN_WITH_CLK,
    }
    game.update(over)
    return game


def make_fetch(games: list[dict[str, Any]]) -> lichess.FetchText:
    body = "\n".join(json.dumps(g) for g in games)

    def fetch(url: str, user_agent: str) -> str:
        fetch.last_url = url  # type: ignore[attr-defined]
        fetch.last_ua = user_agent  # type: ignore[attr-defined]
        return body

    return fetch


def test_collect_games_parses_ndjson_and_meta() -> None:
    fetch = make_fetch([ndjson_game()])
    games, capped = lichess.collect_games(
        "alice", SINCE, time_class="blitz", fetch_text=fetch, max_games=10
    )
    assert capped is False
    assert len(games) == 1
    pgn, meta = games[0]
    assert pgn == PGN_WITH_CLK
    assert meta["user_color"] == "white"
    assert meta["opponent"] == "bob"
    assert meta["time_class"] == "blitz"
    assert meta["game_id"] == "abcdefgh"
    assert "lichess.org/abcdefgh" in meta["url"]
    assert "perfType=blitz" in fetch.last_url  # type: ignore[attr-defined]


def test_user_color_black() -> None:
    fetch = make_fetch([ndjson_game()])
    games, _ = lichess.collect_games("bob", SINCE, fetch_text=fetch)
    assert games[0][1]["user_color"] == "black"


def test_skips_games_without_clocks() -> None:
    fetch = make_fetch([ndjson_game(pgn=PGN_NO_CLK)])
    games, _ = lichess.collect_games("alice", SINCE, fetch_text=fetch)
    assert games == []


def test_rapid_includes_classical_perf() -> None:
    fetch = make_fetch([ndjson_game(perf="classical")])
    games, _ = lichess.collect_games(
        "alice", SINCE, time_class="rapid", fetch_text=fetch
    )
    assert len(games) == 1
    assert games[0][1]["time_class"] == "rapid"
    assert "rapid%2Cclassical" in fetch.last_url or "rapid,classical" in fetch.last_url  # type: ignore[attr-defined]


def test_cap_newest_first() -> None:
    g1 = ndjson_game(
        id="oldgame01",
        createdAt=int(datetime(2026, 5, 10, tzinfo=timezone.utc).timestamp() * 1000),
    )
    g2 = ndjson_game(
        id="newgame02",
        createdAt=int(datetime(2026, 6, 2, tzinfo=timezone.utc).timestamp() * 1000),
    )
    # API returns newest-first when sort=dateDesc
    fetch = make_fetch([g2, g1])
    games, capped = lichess.collect_games(
        "alice", SINCE, fetch_text=fetch, max_games=1
    )
    assert capped is True
    assert games[0][1]["game_id"] == "newgame02"


def test_not_found() -> None:
    def fetch(_url: str, _ua: str) -> str:
        raise lichess.LichessNotFound("missing")

    with pytest.raises(lichess.LichessNotFound):
        lichess.collect_games("missing", SINCE, fetch_text=fetch)
