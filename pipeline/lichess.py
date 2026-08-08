"""Lichess public games export for Insights (ndjson, no auth)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from pipeline import chesscom

MAX_GAMES = chesscom.MAX_GAMES
VALID_TIME_CLASSES = chesscom.VALID_TIME_CLASSES
DEFAULT_USER_AGENT = "ChessMax/1.0 (local personal trainer; +https://github.com/)"
GAMES_URL = "https://lichess.org/api/games/user/{username}"

# Lichess perfType ↔ our time_class
PERF_MAP = {
    "bullet": "bullet",
    "blitz": "blitz",
    "rapid": "rapid",
    "classical": "rapid",
    "correspondence": "daily",
}

FetchText = Callable[[str, str], str]


class LichessError(Exception):
    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class LichessNotFound(LichessError):
    def __init__(self, username: str) -> None:
        super().__init__(f"Lichess user {username!r} not found", kind="not_found")


def _default_fetch_text(url: str, user_agent: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/x-ndjson",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LichessNotFound(url) from exc
        raise


def _result(winner: str | None, status: str | None) -> str | None:
    if status in ("draw", "stalemate"):
        return "1/2-1/2"
    if winner == "white":
        return "1-0"
    if winner == "black":
        return "0-1"
    return None


def _meta_from_game(game: dict[str, Any], username: str) -> dict[str, Any] | None:
    players = game.get("players") or {}
    white = players.get("white") or {}
    black = players.get("black") or {}
    wu = str((white.get("user") or {}).get("name") or white.get("userId") or "").lower()
    bu = str((black.get("user") or {}).get("name") or black.get("userId") or "").lower()
    target = username.lower()
    if target == wu:
        user_color, opponent = "white", (black.get("user") or {}).get("name")
        user_rating = white.get("rating")
        opp_rating = black.get("rating")
    elif target == bu:
        user_color, opponent = "black", (white.get("user") or {}).get("name")
        user_rating = black.get("rating")
        opp_rating = white.get("rating")
    else:
        return None

    perf = str(game.get("perf") or game.get("speed") or "").lower()
    time_class = PERF_MAP.get(perf)
    if time_class is None:
        return None

    created = game.get("createdAt") or game.get("lastMoveAt")
    if not isinstance(created, (int, float)):
        return None
    # Lichess timestamps are ms
    end_time = int(created / 1000)
    date = datetime.fromtimestamp(end_time, tz=timezone.utc).date().isoformat()
    opening = game.get("opening") or {}
    return {
        "user_color": user_color,
        "opponent": opponent,
        "opponent_rating": opp_rating,
        "user_rating": user_rating,
        "url": f"https://lichess.org/{game.get('id')}",
        "game_id": str(game.get("id")),
        "date": date,
        "end_time": end_time,
        "time_class": time_class,
        "rated": bool(game.get("rated")),
        "white_username": (white.get("user") or {}).get("name"),
        "black_username": (black.get("user") or {}).get("name"),
        "white_rating": white.get("rating"),
        "black_rating": black.get("rating"),
        "white_result": "win" if game.get("winner") == "white" else game.get("status"),
        "black_result": "win" if game.get("winner") == "black" else game.get("status"),
        "time_control": None,
        "eco": opening.get("eco"),
        "result": _result(game.get("winner"), game.get("status")),
    }


def collect_games(
    username: str,
    since: datetime | None = None,
    *,
    time_class: str | None = None,
    max_games: int = MAX_GAMES,
    fetch_text: FetchText | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    require_clocks: bool = True,
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Return ``(games, capped)`` newest-first for Insights."""

    if since is None:
        since = chesscom.default_since()
    if time_class is not None and time_class not in VALID_TIME_CLASSES:
        raise ValueError(f"invalid time_class: {time_class!r}")

    # Map our time_class to lichess perfType (may be comma-list)
    perf = None
    if time_class == "bullet":
        perf = "bullet"
    elif time_class == "blitz":
        perf = "blitz"
    elif time_class == "rapid":
        perf = "rapid,classical"
    elif time_class == "daily":
        perf = "correspondence"

    params: dict[str, Any] = {
        "since": int(since.timestamp() * 1000),
        "max": max_games + 1,
        "clocks": "true",
        "pgnInJson": "true",
        "opening": "true",
        "moves": "true",
        "sort": "dateDesc",
    }
    if perf:
        params["perfType"] = perf

    query = urllib.parse.urlencode(params)
    url = GAMES_URL.format(username=username.lower()) + "?" + query
    fetch = fetch_text or _default_fetch_text
    try:
        body = fetch(url, user_agent)
    except LichessNotFound as exc:
        raise LichessNotFound(username) from exc

    games: list[tuple[str, dict[str, Any]]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            game = json.loads(line)
        except json.JSONDecodeError:
            continue
        pgn = game.get("pgn")
        if not pgn:
            continue
        if require_clocks and "%clk" not in pgn:
            continue
        meta = _meta_from_game(game, username)
        if meta is None:
            continue
        if time_class is not None and meta.get("time_class") != time_class:
            # classical mapped to rapid — already filtered by perfType mostly
            if not (time_class == "rapid" and meta.get("time_class") == "rapid"):
                continue
        games.append((str(pgn), meta))
        if len(games) > max_games:
            break

    capped = len(games) > max_games
    if capped:
        games = games[:max_games]
    return games, capped
