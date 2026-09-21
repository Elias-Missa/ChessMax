"""Cached Lichess explorer lookups.

The explorer is a public endpoint with a soft rate limit, and the builder hits
one position per click — so every answer is cached in SQLite and a position is
only ever fetched once. Two consequences that matter more than the speed:

* **A warmed position works with no network.** The box this was developed on
  cannot reach lichess.org at all; once a cache row exists the builder is fully
  functional against it, which is also what makes the feature testable.
* **A cache miss with no network is not an error.** It degrades to
  ``None`` — the results dot goes unknown (grey), the ranking falls back to its
  neutral 0.5, and the builder still works. Refusing to show candidates because
  a third-party statistics service is unreachable would be the wrong trade.

Cached by position, not by user: the explorer's answer is a property of the
position and the query, exactly like ``server/position_cache.py``'s reasoning
about engine analyses.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from core.repertoire import position_key
from pipeline import lichess_explorer
from pipeline.lichess_explorer import (
    DEFAULT_SPEEDS,
    ExplorerError,
    LICHESS,
    MASTERS,
)

#: Module-level seam, monkey-patched in tests exactly like
#: ``chess_vol.server.ENGINE_FACTORY``. Signature matches
#: ``lichess_explorer.fetch_position``'s ``fetch_json``.
FETCH_JSON: Any = None


def params_key(
    *, ratings: Sequence[int] | None, speeds: Sequence[str] | None
) -> str:
    """Stable cache discriminator for the query knobs that change the answer.

    Sorted and joined rather than a dict repr so the same query always produces
    the same key regardless of argument order.
    """

    rating_part = ",".join(str(int(r)) for r in sorted(ratings or ()))
    speed_part = ",".join(sorted(speeds or ()))
    return f"r={rating_part}|s={speed_part}"


def read_cache(
    connection: sqlite3.Connection,
    *,
    database: str,
    fen: str,
    params: str,
) -> dict[str, Any] | None:
    try:
        row = connection.execute(
            """
            SELECT payload FROM explorer_cache
            WHERE database = ? AND fen_key = ? AND params = ?
            """,
            (database, position_key(fen), params),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


def write_cache(
    connection: sqlite3.Connection,
    *,
    database: str,
    fen: str,
    params: str,
    payload: Mapping[str, Any],
) -> None:
    try:
        connection.execute(
            """
            INSERT INTO explorer_cache (database, fen_key, params, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(database, fen_key, params) DO UPDATE SET
                payload = excluded.payload,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (database, position_key(fen), params, json.dumps(payload)),
        )
        connection.commit()
    except sqlite3.Error:
        # A cache that cannot be written is a slow cache, not a broken feature.
        pass


def lookup(
    connection: sqlite3.Connection,
    fen: str,
    *,
    database: str = LICHESS,
    ratings: Sequence[int] | None = None,
    speeds: Sequence[str] | None = DEFAULT_SPEEDS,
    moves: int = lichess_explorer.DEFAULT_MOVES,
    allow_network: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    """Return ``(normalized_payload_or_None, source)``.

    ``source`` is one of ``cache`` / ``network`` / ``unavailable`` / ``off``, and
    is surfaced to the client so the UI can distinguish "no games here" (a real
    answer) from "we could not ask" (an unknown dot). Those look identical in
    the payload and mean opposite things.
    """

    params = params_key(
        ratings=ratings if database == LICHESS else None,
        speeds=speeds if database == LICHESS else None,
    )
    cached = read_cache(connection, database=database, fen=fen, params=params)
    if cached is not None:
        return cached, "cache"
    if not allow_network:
        return None, "off"

    try:
        payload = lichess_explorer.fetch_position(
            fen,
            database=database,
            ratings=ratings if database == LICHESS else None,
            speeds=tuple(speeds or ()) if database == LICHESS else (),
            moves=moves,
            fetch_json=FETCH_JSON,
        )
    except ExplorerError:
        return None, "unavailable"
    except Exception:  # noqa: BLE001
        # `_default_fetch_json` normalizes its own failures into ExplorerError,
        # but an INJECTED fetcher makes no such promise, and neither does a
        # future urllib. The panel's contract is that a statistics service can
        # never take it down, so every escape route lands on the same
        # "unavailable" degradation rather than a 500.
        return None, "unavailable"

    write_cache(
        connection, database=database, fen=fen, params=params, payload=payload
    )
    return payload, "network"


def lookup_both(
    connection: sqlite3.Connection,
    fen: str,
    *,
    ratings: Sequence[int] | None = None,
    speeds: Sequence[str] | None = DEFAULT_SPEEDS,
    moves: int = lichess_explorer.DEFAULT_MOVES,
    allow_network: bool = True,
) -> dict[str, Any]:
    """Masters + peers in one call, each independently degradable.

    The two corpora fail separately on purpose: the masters lookup succeeding
    and the peer one rate-limiting should still give you master frequency, and
    the reverse should still give you the results dot.
    """

    masters, masters_source = lookup(
        connection,
        fen,
        database=MASTERS,
        moves=moves,
        allow_network=allow_network,
    )
    peers, peers_source = lookup(
        connection,
        fen,
        database=LICHESS,
        ratings=ratings,
        speeds=speeds,
        moves=moves,
        allow_network=allow_network,
    )
    return {
        "masters": masters,
        "peers": peers,
        "sources": {"masters": masters_source, "peers": peers_source},
    }


__all__ = [
    "FETCH_JSON",
    "lookup",
    "lookup_both",
    "params_key",
    "read_cache",
    "write_cache",
]
