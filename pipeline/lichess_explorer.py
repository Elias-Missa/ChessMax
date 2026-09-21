"""Lichess opening explorer: master frequency and results at the player's level.

Two corpora, and the difference between them is the product:

* ``masters`` — OTB master games. Answers "what is the move", which is the
  question an opening book answers and the one that matters least at 1400.
* ``lichess`` — rated online games, filtered to **the user's own rating band**.
  Answers "what actually scores for people like me", which is the question the
  repertoire is really asking. This is the corpus the results dot reads.

No auth and no key; the explorer is a public endpoint with a soft rate limit, so
every answer is cached (see :mod:`server.opening_cache`) and a cached position
costs no network at all. ``fetch_json`` is injectable for exactly the reason the
rest of this repo injects its engines: the tests must run with no network, and
so must the app on a box that cannot reach lichess.org.

Responses are normalized here and nowhere else — the explorer reports W/D/L from
**White's** perspective regardless of whose move it is, and every caller that
forgets to flip that for Black gets a plausible, inverted answer. The
normalization keeps the raw counts and leaves the flip to
:func:`core.opening_signals.row_score`, which is the single place that knows
whose score it is computing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Mapping, Sequence

MASTERS = "masters"
LICHESS = "lichess"
DATABASES: tuple[str, ...] = (MASTERS, LICHESS)

BASE_URL = "https://explorer.lichess.ovh"
DEFAULT_USER_AGENT = "ChessMax/1.0 (local personal trainer; +https://github.com/)"

#: How many candidate moves to ask for. The builder shows a handful and hides
#: the rest behind "Show more"; 15 covers both without a second request.
DEFAULT_MOVES = 15

#: The explorer's own rating buckets. A band is a *pair* of adjacent buckets,
#: not one — a single bucket at 1600 excludes the 1700 opponents you mostly
#: play, and the sample gets thin enough that the results dot stops meaning
#: anything.
RATING_BUCKETS: tuple[int, ...] = (0, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500)

#: Time controls worth learning an opening from. Bullet is excluded: opening
#: choice in bullet is a proxy for what can be pre-moved, which is not a
#: repertoire question.
DEFAULT_SPEEDS: tuple[str, ...] = ("blitz", "rapid", "classical")

FetchJson = Callable[[str, str], Any]


class ExplorerError(Exception):
    """The explorer could not answer. Callers degrade to an unknown dot."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class ExplorerUnavailable(ExplorerError):
    """Network refused, timed out, or was blocked — distinct from a bad query."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Lichess explorer unreachable: {detail}", kind="unavailable"
        )


def _default_fetch_json(url: str, user_agent: str) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ExplorerError(
                "Lichess explorer rate limit reached — try again shortly",
                kind="rate_limited",
            ) from exc
        raise ExplorerError(f"explorer returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ExplorerUnavailable(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ExplorerError("explorer returned malformed JSON") from exc


def rating_band(rating: int | None) -> tuple[int, int]:
    """The two explorer buckets that bracket ``rating``.

    An unknown rating gets the middle of the ladder rather than the bottom:
    defaulting an unrated account to 0-1000 would show it the opening choices
    of beginners, which is worse than showing it the average.
    """

    if rating is None:
        return (1400, 1600)
    lower = RATING_BUCKETS[0]
    for bucket in RATING_BUCKETS:
        if bucket <= rating:
            lower = bucket
        else:
            break
    index = RATING_BUCKETS.index(lower)
    upper = RATING_BUCKETS[min(index + 1, len(RATING_BUCKETS) - 1)]
    if upper == lower:  # top bucket — widen downward instead
        lower = RATING_BUCKETS[max(0, index - 1)]
    return (lower, upper)


def build_url(
    database: str,
    fen: str,
    *,
    ratings: Sequence[int] | None = None,
    speeds: Sequence[str] = DEFAULT_SPEEDS,
    moves: int = DEFAULT_MOVES,
) -> str:
    """The explorer URL for one position.

    ``ratings``/``speeds`` are only meaningful for the ``lichess`` corpus; the
    masters database has neither, and sending them is a 400.
    """

    if database not in DATABASES:
        raise ValueError(f"unknown explorer database: {database!r}")
    params: list[tuple[str, str]] = [
        ("fen", fen),
        ("moves", str(max(1, int(moves)))),
        ("topGames", "0"),
    ]
    if database == MASTERS:
        params.append(("since", "1952"))
    else:
        params.append(("recentGames", "0"))
        if ratings:
            params.append(("ratings", ",".join(str(int(r)) for r in ratings)))
        if speeds:
            params.append(("speeds", ",".join(speeds)))
    return f"{BASE_URL}/{database}?{urllib.parse.urlencode(params)}"


def normalize(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Explorer JSON → the shape the rest of the app reads.

    Keeps raw ``white``/``draws``/``black`` counts per move rather than a
    pre-computed score, because whose score it is depends on the side to move
    and that is not this function's business. Unknown/absent fields become
    zeroes, never guesses.
    """

    data = payload or {}
    raw_moves = data.get("moves") or []
    moves: list[dict[str, Any]] = []
    for entry in raw_moves:
        if not isinstance(entry, Mapping):
            continue
        uci = str(entry.get("uci") or "")
        if not uci:
            continue
        moves.append(
            {
                "uci": uci,
                "san": str(entry.get("san") or ""),
                "white": int(entry.get("white") or 0),
                "draws": int(entry.get("draws") or 0),
                "black": int(entry.get("black") or 0),
                "average_rating": _opt_int(entry.get("averageRating")),
            }
        )
    opening = data.get("opening")
    return {
        "moves": moves,
        "white": int(data.get("white") or 0),
        "draws": int(data.get("draws") or 0),
        "black": int(data.get("black") or 0),
        "opening_name": (
            str(opening.get("name")) if isinstance(opening, Mapping) and opening.get("name") else None
        ),
        "opening_eco": (
            str(opening.get("eco")) if isinstance(opening, Mapping) and opening.get("eco") else None
        ),
    }


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def total_games(normalized: Mapping[str, Any]) -> int:
    """Games behind the whole position, across every listed move."""

    return sum(int(normalized.get(key) or 0) for key in ("white", "draws", "black"))


def fetch_position(
    fen: str,
    *,
    database: str = LICHESS,
    ratings: Sequence[int] | None = None,
    speeds: Sequence[str] = DEFAULT_SPEEDS,
    moves: int = DEFAULT_MOVES,
    fetch_json: FetchJson | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """One position from one corpus, normalized. Raises :class:`ExplorerError`."""

    url = build_url(database, fen, ratings=ratings, speeds=speeds, moves=moves)
    fetcher = fetch_json or _default_fetch_json
    return normalize(fetcher(url, user_agent))


__all__ = [
    "DATABASES",
    "DEFAULT_MOVES",
    "DEFAULT_SPEEDS",
    "DEFAULT_USER_AGENT",
    "ExplorerError",
    "ExplorerUnavailable",
    "LICHESS",
    "MASTERS",
    "RATING_BUCKETS",
    "build_url",
    "fetch_position",
    "normalize",
    "rating_band",
    "total_games",
]
