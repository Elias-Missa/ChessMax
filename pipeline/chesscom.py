"""Chess.com PubAPI ingest for the "Your Mistakes" feature.

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
ARCHIVES_URL = "https://api.chess.com/pub/player/{username}/games/archives"
DEFAULT_USER_AGENT = "ChessMax/1.0 (local personal trainer; +https://github.com/)"

FetchJson = Callable[[str, str], dict[str, Any]]

_ARCHIVE_MONTH_RE = re.compile(r"/(\d{4})/(\d{2})/?$")


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


def _game_meta(game: dict[str, Any], username: str, since_ts: float) -> dict[str, Any] | None:
    """Build meta for an eligible game, or ``None`` to skip it."""

    if game.get("rules") != "chess":
        return None
    end_time = game.get("end_time")
    if not isinstance(end_time, (int, float)) or end_time < since_ts:
        return None
    pgn = game.get("pgn")
    if not pgn or "%clk" not in pgn:  # require per-move clocks
        return None

    white = game.get("white") or {}
    black = game.get("black") or {}
    wu = str(white.get("username", "")).lower()
    bu = str(black.get("username", "")).lower()
    target = username.lower()
    if target == wu:
        user_color, opponent, opp_rating = "white", black.get("username"), black.get("rating")
    elif target == bu:
        user_color, opponent, opp_rating = "black", white.get("username"), white.get("rating")
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
        "url": game.get("url"),
        "game_id": str(game.get("uuid") or game.get("url") or int(end_time)),
        "date": date,
        "end_time": int(end_time),
        "time_class": game.get("time_class"),
        "rated": bool(game.get("rated")),
    }


def iter_games(
    username: str,
    since: datetime | None = None,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    fetch_json: FetchJson | None = None,
    delay_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(pgn, meta)`` for the user's eligible games since ``since``.

    ``since`` defaults to 90 days ago. Months entirely before ``since`` are not
    fetched. ``meta`` carries ``user_color`` ('white'|'black'), ``opponent``,
    ``url``, ``game_id``, ``date``, ``time_class``.
    """

    if since is None:
        since = default_since()
    fetch = fetch_json or _default_fetch_json
    since_ts = since.timestamp()

    archives = fetch(ARCHIVES_URL.format(username=username.lower()), user_agent)
    for archive_url in archives.get("archives", []):
        if not _archive_in_window(archive_url, since):
            continue
        if delay_s:
            sleep(delay_s)  # be gentle on the API between months
        payload = fetch(archive_url, user_agent)
        for game in payload.get("games", []):
            meta = _game_meta(game, username, since_ts)
            if meta is not None:
                yield str(game["pgn"]), meta
