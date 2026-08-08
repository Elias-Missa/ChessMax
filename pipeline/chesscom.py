"""Chess.com PubAPI ingest for Mistakes and Insights.

Read-only, no auth. Fetches the player's monthly archives, keeps standard
(``rules == "chess"``) games finished within the lookback window that carry
per-move clocks, and yields ``(pgn_text, meta)`` for the detector.

Chess.com blocks default library user-agents, so every request carries a
descriptive ``User-Agent``. The HTTP layer is injectable (``fetch_json``) so
tests run without network access.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

LOOKBACK_DAYS = 90
MAX_GAMES = 300
VALID_TIME_CLASSES = frozenset({"bullet", "blitz", "rapid", "daily"})
ARCHIVES_URL = "https://api.chess.com/pub/player/{username}/games/archives"
DEFAULT_USER_AGENT = "ChessMax/1.0 (local personal trainer; +https://github.com/)"

FetchJson = Callable[[str, str], dict[str, Any]]

_ARCHIVE_MONTH_RE = re.compile(r"/(\d{4})/(\d{2})/?$")


class ChesscomError(Exception):
    """Base for Chess.com ingest failures the API can map to HTTP statuses."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class ChesscomNotFound(ChesscomError):
    def __init__(self, username: str) -> None:
        super().__init__(f"Chess.com user {username!r} not found", kind="not_found")


class ChesscomPrivate(ChesscomError):
    def __init__(self, username: str) -> None:
        super().__init__(
            f"Chess.com games for {username!r} are private or unavailable",
            kind="private",
        )


def default_since(days: int = LOOKBACK_DAYS) -> datetime:
    """UTC datetime ``days`` before now (the lookback floor)."""

    return datetime.now(timezone.utc) - timedelta(days=days)


def _default_fetch_json(url: str, user_agent: str, *, retries: int = 3) -> dict[str, Any]:
    """GET ``url`` as JSON with a real User-Agent and simple 429 backoff."""

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                raise ChesscomNotFound(url) from exc
            if exc.code in (401, 403):
                raise ChesscomPrivate(url) from exc
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))  # back off and retry
                continue
            raise
    assert last_error is not None
    raise last_error


def _archive_in_window(archive_url: str, since: datetime) -> bool:
    """True if the monthly archive could hold games on/after ``since``."""

    match = _ARCHIVE_MONTH_RE.search(archive_url)
    if match is None:
        return True  # unknown shape — fetch it to be safe
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) >= (since.year, since.month)


def _game_meta(
    game: dict[str, Any],
    username: str,
    since_ts: float,
    *,
    require_clocks: bool = True,
    time_class: str | None = None,
) -> dict[str, Any] | None:
    """Build meta for an eligible game, or ``None`` to skip it."""

    if game.get("rules") != "chess":
        return None
    if time_class is not None and game.get("time_class") != time_class:
        return None
    end_time = game.get("end_time")
    if not isinstance(end_time, (int, float)) or end_time < since_ts:
        return None
    pgn = game.get("pgn")
    if not pgn:
        return None
    if require_clocks and "%clk" not in pgn:
        return None

    white = game.get("white") or {}
    black = game.get("black") or {}
    wu = str(white.get("username", "")).lower()
    bu = str(black.get("username", "")).lower()
    target = username.lower()
    if target == wu:
        user_color, opponent, opp_rating = "white", black.get("username"), black.get("rating")
        user_rating = white.get("rating")
    elif target == bu:
        user_color, opponent, opp_rating = "black", white.get("username"), white.get("rating")
        user_rating = black.get("rating")
    else:
        return None  # username not a player in this game

    # Skip abandoned games (not instructive).
    if "abandoned" in {white.get("result"), black.get("result")}:
        return None

    date = datetime.fromtimestamp(float(end_time), tz=timezone.utc).date().isoformat()
    return {
        "user_color": user_color,
        "opponent": opponent,
        "opponent_rating": opp_rating,
        "user_rating": user_rating,
        "url": game.get("url"),
        "game_id": str(game.get("uuid") or game.get("url") or int(end_time)),
        "date": date,
        "end_time": int(end_time),
        "time_class": game.get("time_class"),
        "rated": bool(game.get("rated")),
        "white_username": white.get("username"),
        "black_username": black.get("username"),
        "white_rating": white.get("rating"),
        "black_rating": black.get("rating"),
        "white_result": white.get("result"),
        "black_result": black.get("result"),
        "time_control": game.get("time_control"),
        "eco": game.get("eco"),
    }


def iter_games(
    username: str,
    since: datetime | None = None,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    fetch_json: FetchJson | None = None,
    delay_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    time_class: str | None = None,
    max_games: int | None = MAX_GAMES,
    require_clocks: bool = True,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(pgn, meta)`` for the user's eligible games since ``since``.

    ``since`` defaults to 90 days ago. Months entirely before ``since`` are not
    fetched. ``meta`` carries ``user_color`` ('white'|'black'), ``opponent``,
    ``url``, ``game_id``, ``date``, ``time_class``.

    When ``max_games`` is set, yields at most that many games, newest first
    (archives are walked newest→oldest; chess.com archives list is chronological
    so we reverse it). Sets ``meta["_capped"]`` is not used here — callers check
    whether the iterator stopped early via a ``games_capped`` flag on a wrapper.
    """

    if since is None:
        since = default_since()
    if time_class is not None and time_class not in VALID_TIME_CLASSES:
        raise ValueError(f"invalid time_class: {time_class!r}")
    fetch = fetch_json or _default_fetch_json
    since_ts = since.timestamp()

    try:
        archives_payload = fetch(ARCHIVES_URL.format(username=username.lower()), user_agent)
    except ChesscomNotFound as exc:
        raise ChesscomNotFound(username) from exc
    except ChesscomPrivate as exc:
        raise ChesscomPrivate(username) from exc

    archives = list(archives_payload.get("archives", []))
    # Newest months first so the 300-game cap keeps the most recent games.
    archives.reverse()

    yielded = 0
    for archive_url in archives:
        if not _archive_in_window(archive_url, since):
            continue
        if max_games is not None and yielded >= max_games:
            return
        if delay_s:
            sleep(delay_s)  # be gentle on the API between months
        try:
            payload = fetch(archive_url, user_agent)
        except ChesscomNotFound:
            continue
        except ChesscomPrivate as exc:
            raise ChesscomPrivate(username) from exc

        # Games within a month are usually chronological; reverse for newest-first.
        month_games = list(payload.get("games", []))
        month_games.reverse()
        for game in month_games:
            if max_games is not None and yielded >= max_games:
                return
            meta = _game_meta(
                game,
                username,
                since_ts,
                require_clocks=require_clocks,
                time_class=time_class,
            )
            if meta is not None:
                yielded += 1
                yield str(game["pgn"]), meta


def collect_games(
    username: str,
    since: datetime | None = None,
    *,
    time_class: str | None = None,
    max_games: int = MAX_GAMES,
    fetch_json: FetchJson | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    require_clocks: bool = True,
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Materialize up to ``max_games`` games; return ``(games, capped)``.

    ``capped`` is True when at least ``max_games`` eligible games were found
    (more may exist beyond the cap).
    """

    # Fetch one extra to know whether we hit the cap.
    limit = max_games + 1
    games = list(
        iter_games(
            username,
            since,
            time_class=time_class,
            max_games=limit,
            fetch_json=fetch_json,
            user_agent=user_agent,
            require_clocks=require_clocks,
        )
    )
    capped = len(games) > max_games
    if capped:
        games = games[:max_games]
    return games, capped
