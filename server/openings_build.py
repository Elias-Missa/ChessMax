"""The authored opening repertoire: a tree the player builds, move by move.

ChessMax already mines a repertoire out of the games you played
(:mod:`core.repertoire`) — that one is *descriptive*, and its whole value is
telling you what you actually do. This one is *prescriptive*: you walk a board,
look at the evidence, and decide what you will play. They answer different
questions and both feed the same drill.

Three things are load-bearing here:

* **Identity is the position, so transpositions merge for free.** A node is
  keyed by ``position_key`` (board, side, castling, en-passant — no clocks), so
  1.d4 Nf6 2.c4 e6 3.Nf3 and 1.Nf3 Nf6 2.c4 e6 3.d4 land on the same row and a
  move chosen down one order is already chosen down the other. ``path_uci``
  records *one* way of reaching the node so the UI can show how; it is display
  context and never identity. The spec calls for exactly this (§5, §6.4).

  It is ``position_key`` and not the plain ``fen_key`` because the browser and
  the server disagree about the en-passant square: chess.js writes it after
  every double push, python-chess only when a capture is legal. So the FEN the
  client sends for the position after 1.e4 and the one the server computes by
  pushing 1.e4 differ, and keying on either raw string forks the node, forks the
  explorer cache, and quietly disables transposition merging.
* **Coverage is weighted by how likely a reply is, not by how many there are.**
  Covering the three replies you meet 80% of the time is a finished repertoire;
  covering twelve rare ones and missing the main line is not. Weight comes from
  the peer explorer corpus, so "likely" means likely *at your rating*.
* **Engine and explorer are both optional.** Every evidence source degrades to
  unknown independently, and the builder is fully usable with none of them —
  which is also the only way it is testable on a box with no Stockfish and no
  network.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping, Sequence

import chess

from core.opening_book import (
    Candidate,
    OpeningBookConstants,
    build_candidates,
    rank_candidates,
)
from core.opening_signals import MoveSignals, row_games, signals_for_position
from core.repertoire import BLACK, WHITE, canonical_fen, position_key
from pipeline.lichess_explorer import rating_band
from server import opening_cache

COLORS: tuple[str, ...] = (WHITE, BLACK)
MINE = "mine"
THEIRS = "theirs"
ROLES: tuple[str, ...] = (MINE, THEIRS)

START_FEN = chess.Board().fen()

AnalyzeFn = Callable[..., Mapping[str, Any]]
MaiaFn = Callable[..., Sequence[str] | None]


# --------------------------------------------------------------------------- #
# Evidence                                                                     #
# --------------------------------------------------------------------------- #


def user_rating(connection: sqlite3.Connection, user_id: int) -> int | None:
    try:
        row = connection.execute(
            "SELECT rating FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row["rating"] is None:
        return None
    return int(row["rating"])


def gather_evidence(
    connection: sqlite3.Connection,
    fen: str,
    *,
    user_id: int,
    analyze_fn: AnalyzeFn | None = None,
    maia_fn: MaiaFn | None = None,
    constants: OpeningBookConstants | None = None,
    allow_network: bool = True,
) -> dict[str, Any]:
    """Everything known about one position, with each source failing alone.

    Returns the three raw inputs plus a ``sources`` map saying, per source,
    whether we got an answer and from where. The UI needs that map: "no games
    here" and "we could not reach the explorer" produce the same empty list and
    mean opposite things.
    """

    constants = constants or OpeningBookConstants.load()
    board = chess.Board(fen)

    explorer = opening_cache.lookup_both(
        connection,
        fen,
        ratings=list(rating_band(user_rating(connection, user_id))),
        allow_network=allow_network,
    )

    engine_top: list[dict[str, Any]] | None = None
    engine_source = "off"
    if analyze_fn is not None and not board.is_game_over():
        try:
            result = analyze_fn(
                fen, depth=constants.default_depth, multipv=constants.default_multipv
            )
            raw = list(result.get("top_moves") or [])
            engine_top = raw or None
            engine_source = "engine" if raw else "empty"
        except Exception:  # noqa: BLE001 — a missing binary must not 500 the panel
            engine_top, engine_source = None, "unavailable"

    maia_top: list[str] | None = None
    maia_source = "off"
    if maia_fn is not None and not board.is_game_over():
        try:
            result = maia_fn(fen, net=constants.maia_rating, k=3)
            maia_top = list(result) if result else None
            maia_source = "maia" if maia_top else "unavailable"
        except Exception:  # noqa: BLE001 — same contract as the engine above
            maia_top, maia_source = None, "unavailable"

    return {
        "masters": explorer["masters"],
        "peers": explorer["peers"],
        "engine_top": engine_top,
        "maia_top": maia_top,
        "sources": {
            **explorer["sources"],
            "engine": engine_source,
            "maia": maia_source,
            "maia_rating": constants.maia_rating,
        },
    }


def candidates_for(
    connection: sqlite3.Connection,
    fen: str,
    *,
    user_id: int,
    color: str,
    analyze_fn: AnalyzeFn | None = None,
    maia_fn: MaiaFn | None = None,
    constants: OpeningBookConstants | None = None,
    allow_network: bool = True,
) -> dict[str, Any]:
    """Ranked candidates for one position, each with its three dots."""

    constants = constants or OpeningBookConstants.load()
    board = chess.Board(fen)
    evidence = gather_evidence(
        connection,
        fen,
        user_id=user_id,
        analyze_fn=analyze_fn,
        maia_fn=maia_fn,
        constants=constants,
        allow_network=allow_network,
    )

    peers = evidence["peers"] or {}
    masters = evidence["masters"] or {}
    # The dot is about the moves we are actually showing, so the universe is the
    # union of everything any source mentioned — computed once here and reused
    # for the signals so a move can never be scored against a list it is not in.
    mentioned = {
        str(r["uci"])
        for source in (peers, masters)
        for r in (source.get("moves") or [])
        if r.get("uci")
    }
    mentioned |= {
        str(r["move"]) for r in (evidence["engine_top"] or []) if r.get("move")
    }
    legal = {m.uci() for m in board.legal_moves}
    ucis = sorted(mentioned & legal)

    signals = signals_for_position(
        ucis,
        engine_top_moves=evidence["engine_top"],
        maia_top_moves=evidence["maia_top"],
        explorer_rows=(peers.get("moves") if evidence["peers"] is not None else None),
        for_white=board.turn == chess.WHITE,
    )

    node = find_node(connection, user_id, color=color, fen=fen)
    chosen = [e["uci"] for e in load_edges(connection, user_id, node["id"])] if node else []

    ranked = rank_candidates(
        build_candidates(
            fen,
            masters=masters or None,
            peers=peers or None,
            engine_top_moves=evidence["engine_top"],
            signals=signals,
            repertoire_ucis=chosen,
            constants=constants,
        ),
        constants=constants,
    )

    return {
        "fen": fen,
        "fen_key": position_key(fen),
        "turn": WHITE if board.turn == chess.WHITE else BLACK,
        "user_to_move": (board.turn == chess.WHITE) == (color == WHITE),
        "ply": board.ply(),
        "legal_moves": sorted(legal),
        "candidates": [c.as_dict() for c in ranked],
        "chosen": chosen,
        "opening_name": (peers.get("opening_name") or masters.get("opening_name")),
        "opening_eco": (peers.get("opening_eco") or masters.get("opening_eco")),
        "sources": evidence["sources"],
        "totals": {
            "masters": sum(row_games(r) for r in (masters.get("moves") or [])),
            "peers": sum(row_games(r) for r in (peers.get("moves") or [])),
        },
    }


# --------------------------------------------------------------------------- #
# The tree                                                                     #
# --------------------------------------------------------------------------- #


def find_node(
    connection: sqlite3.Connection, user_id: int, *, color: str, fen: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM opening_nodes WHERE user_id = ? AND color = ? AND fen_key = ?",
        (user_id, color, position_key(fen)),
    ).fetchone()


def ensure_node(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str,
    fen: str,
    path_uci: Sequence[str] = (),
    opening_name: str | None = None,
    opening_eco: str | None = None,
) -> sqlite3.Row:
    """Find or create the node for this position. Idempotent by ``fen_key``.

    A second arrival by a different move order updates nothing but the opening
    name (when we learn one) — in particular it does **not** overwrite
    ``path_uci``, so the first route found stays the one the UI explains.
    """

    existing = find_node(connection, user_id, color=color, fen=fen)
    if existing is not None:
        if opening_name and not existing["opening_name"]:
            connection.execute(
                "UPDATE opening_nodes SET opening_name = ?, opening_eco = ? WHERE id = ?",
                (opening_name, opening_eco, existing["id"]),
            )
            connection.commit()
            return find_node(connection, user_id, color=color, fen=fen)  # type: ignore[return-value]
        return existing

    board = chess.Board(fen)
    connection.execute(
        """
        INSERT INTO opening_nodes
            (user_id, color, fen_key, fen, ply, path_uci, opening_name, opening_eco)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            color,
            position_key(fen),
            canonical_fen(fen),
            board.ply(),
            " ".join(path_uci),
            opening_name,
            opening_eco,
        ),
    )
    connection.commit()
    return find_node(connection, user_id, color=color, fen=fen)  # type: ignore[return-value]


def load_edges(
    connection: sqlite3.Connection, user_id: int, node_id: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM opening_edges WHERE user_id = ? AND node_id = ? ORDER BY id",
        (user_id, node_id),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            signals = json.loads(row["signals"]) if row["signals"] else None
        except (json.JSONDecodeError, TypeError):
            signals = None
        out.append(
            {
                "id": int(row["id"]),
                "uci": str(row["uci"]),
                "san": str(row["san"]),
                "role": str(row["role"]),
                "note": row["note"],
                "signals": signals,
            }
        )
    return out


def add_edge(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str,
    fen: str,
    uci: str,
    path_uci: Sequence[str] = (),
    note: str | None = None,
    signals: Mapping[str, Any] | None = None,
    opening_name: str | None = None,
    opening_eco: str | None = None,
) -> dict[str, Any]:
    """Choose ``uci`` at ``fen``. Creates the node and the child node.

    ``role`` is derived, never passed: whose move it is at this position and
    which colour the repertoire is for together decide whether this is the
    player's decision or an opponent reply being covered. Letting the caller
    send it invites a repertoire that drills you on your opponent's moves.

    The signals are **snapshotted onto the edge**. They are a statement about
    what the evidence said when the choice was made, and the evidence moves —
    the engine gets deeper, the explorer accrues games. Same reasoning as
    ``dev_labels`` storing the score it was shown.
    """

    board = chess.Board(fen)
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"{uci} is not legal in that position")

    san = board.san(move)
    role = MINE if (board.turn == chess.WHITE) == (color == WHITE) else THEIRS

    node = ensure_node(
        connection,
        user_id,
        color=color,
        fen=fen,
        path_uci=path_uci,
        opening_name=opening_name,
        opening_eco=opening_eco,
    )
    connection.execute(
        """
        INSERT INTO opening_edges (user_id, node_id, uci, san, role, note, signals)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id, uci) DO UPDATE SET
            note = COALESCE(excluded.note, opening_edges.note),
            signals = COALESCE(excluded.signals, opening_edges.signals)
        """,
        (
            user_id,
            int(node["id"]),
            uci,
            san,
            role,
            note,
            json.dumps(dict(signals)) if signals else None,
        ),
    )

    board.push(move)
    child = ensure_node(
        connection,
        user_id,
        color=color,
        fen=board.fen(),
        path_uci=[*path_uci, uci],
    )
    connection.commit()
    return {
        "node_id": int(node["id"]),
        "child_id": int(child["id"]),
        "uci": uci,
        "san": san,
        "role": role,
        "fen_after": board.fen(),
    }


def remove_edge(
    connection: sqlite3.Connection, user_id: int, *, color: str, fen: str, uci: str
) -> bool:
    """Unchoose a move. Leaves orphaned child nodes alone deliberately —
    a node with no edges is a position you might come back to, and deleting the
    subtree would silently discard choices made further down a line you are only
    temporarily removing a move from."""

    node = find_node(connection, user_id, color=color, fen=fen)
    if node is None:
        return False
    cursor = connection.execute(
        "DELETE FROM opening_edges WHERE user_id = ? AND node_id = ? AND uci = ?",
        (user_id, int(node["id"]), uci),
    )
    connection.commit()
    return cursor.rowcount > 0


def load_tree(
    connection: sqlite3.Connection, user_id: int, *, color: str
) -> dict[str, Any]:
    """Every node and edge for one colour, plus what it adds up to."""

    nodes = connection.execute(
        "SELECT * FROM opening_nodes WHERE user_id = ? AND color = ? ORDER BY ply, id",
        (user_id, color),
    ).fetchall()
    edges = connection.execute(
        """
        SELECT e.*, n.fen, n.fen_key, n.ply, n.path_uci
        FROM opening_edges e JOIN opening_nodes n ON n.id = e.node_id
        WHERE e.user_id = ? AND n.color = ?
        ORDER BY n.ply, e.id
        """,
        (user_id, color),
    ).fetchall()

    node_payload = [
        {
            "id": int(n["id"]),
            "fen": str(n["fen"]),
            "fen_key": str(n["fen_key"]),
            "ply": int(n["ply"]),
            "path": str(n["path_uci"]).split() if n["path_uci"] else [],
            "opening_name": n["opening_name"],
            "opening_eco": n["opening_eco"],
        }
        for n in nodes
    ]
    edge_payload = [
        {
            "id": int(e["id"]),
            "node_id": int(e["node_id"]),
            "fen": str(e["fen"]),
            "ply": int(e["ply"]),
            "uci": str(e["uci"]),
            "san": str(e["san"]),
            "role": str(e["role"]),
            "note": e["note"],
            "path": str(e["path_uci"]).split() if e["path_uci"] else [],
        }
        for e in edges
    ]
    mine = [e for e in edge_payload if e["role"] == MINE]
    return {
        "color": color,
        "nodes": node_payload,
        "edges": edge_payload,
        "moves": len(edge_payload),
        "decisions": len(mine),
        "max_ply": max((n["ply"] for n in node_payload), default=0),
        "lines": build_lines(node_payload, edge_payload),
    }


def build_lines(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Walk the tree into displayable lines, one per leaf.

    Depth-first from every root, following every chosen edge. A cycle is
    impossible in a legal game tree keyed by position *plus* side to move, but
    repetition can revisit a position, so the walk carries a visited set —
    without it a repetition in a stored line hangs the request.

    A root is a node nothing points *at*, not "the node at ply 0". A PGN
    imported from a ``[FEN]``/``[SetUp]`` header — which is how most study
    chapters are written — has no ply-0 node at all, and anchoring on one made
    every such import vanish from this view while still sitting in the database.
    """

    by_fen: dict[str, list[Mapping[str, Any]]] = {}
    for edge in edges:
        by_fen.setdefault(position_key(str(edge["fen"])), []).append(edge)
    if not by_fen:
        return []

    reached: set[str] = set()
    for edge in edges:
        try:
            board = chess.Board(str(edge["fen"]))
            board.push(chess.Move.from_uci(str(edge["uci"])))
        except ValueError:
            continue
        reached.add(position_key(board.fen()))

    roots = [
        n
        for n in nodes
        if position_key(str(n["fen"])) in by_fen
        and position_key(str(n["fen"])) not in reached
    ]
    if not roots:
        return []

    lines: list[dict[str, Any]] = []

    def walk(fen: str, moves: list[Mapping[str, Any]], seen: frozenset[str]) -> None:
        key = position_key(fen)
        children = by_fen.get(key, [])
        if not children or key in seen or len(moves) > 60:
            if moves:
                lines.append(
                    {
                        "key": " ".join(str(m["uci"]) for m in moves),
                        "moves": [
                            {
                                "uci": str(m["uci"]),
                                "san": str(m["san"]),
                                "role": str(m["role"]),
                                "fen": str(m["fen"]),
                                "note": m.get("note"),
                            }
                            for m in moves
                        ],
                        "plies": len(moves),
                    }
                )
            return
        for edge in children:
            board = chess.Board(str(edge["fen"]))
            board.push(chess.Move.from_uci(str(edge["uci"])))
            walk(board.fen(), [*moves, edge], seen | {key})

    for root in roots:
        walk(str(root["fen"]), [], frozenset())
    lines.sort(key=lambda line: (-line["plies"], line["key"]))
    return lines


# --------------------------------------------------------------------------- #
# The graph                                                                    #
# --------------------------------------------------------------------------- #


def build_graph(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The tree as a drawable graph: nodes with parents, edges between them.

    Distinct from :func:`build_lines`, which flattens the tree into readable
    lines and duplicates shared prefixes across them — fine for a text list,
    wrong for a picture, where 1.e4 must be *one* circle with several children
    rather than one per line through it.

    Transpositions are the reason this returns a DAG's spanning tree rather than
    the DAG: a position reachable two ways has two parents, and a node with two
    parents has no place on a tidy layered layout. The first parent found (by
    ply, then insertion order) keeps the child; the other link is reported in
    ``transpositions`` so the UI can draw it as a hint rather than a branch.
    """

    by_key: dict[str, dict[str, Any]] = {}
    for node in nodes:
        key = position_key(str(node["fen"]))
        by_key.setdefault(
            key,
            {
                "key": key,
                "fen": str(node["fen"]),
                "ply": int(node["ply"]),
                "opening_name": node.get("opening_name"),
                "parent": None,
                "san": None,
                "uci": None,
                "role": None,
                "children": 0,
            },
        )

    links: list[dict[str, Any]] = []
    transpositions: list[dict[str, Any]] = []
    for edge in sorted(edges, key=lambda e: (int(e["ply"]), int(e["id"]))):
        parent_key = position_key(str(edge["fen"]))
        try:
            board = chess.Board(str(edge["fen"]))
            board.push(chess.Move.from_uci(str(edge["uci"])))
        except ValueError:
            continue
        child_key = position_key(board.fen())
        child = by_key.get(child_key)
        if child is None:
            # An edge whose child node row is missing (a half-written import).
            # Draw the edge but do not invent a node for it.
            continue
        link = {
            "from": parent_key,
            "to": child_key,
            "san": str(edge["san"]),
            "uci": str(edge["uci"]),
            "role": str(edge["role"]),
        }
        if child["parent"] is None and child_key != parent_key:
            child["parent"] = parent_key
            child["san"] = str(edge["san"])
            child["uci"] = str(edge["uci"])
            child["role"] = str(edge["role"])
            links.append(link)
            if parent_key in by_key:
                by_key[parent_key]["children"] += 1
        else:
            transpositions.append(link)

    # Only nodes that are on the tree — a position with no parent and no
    # children is a stray row (a child node created for an edge later removed)
    # and drawing it as a floating circle is noise.
    roots = [n for n in by_key.values() if n["parent"] is None and n["children"]]
    keep = {n["key"] for n in by_key.values() if n["parent"] is not None}
    keep |= {n["key"] for n in roots}

    return {
        "nodes": [n for n in by_key.values() if n["key"] in keep],
        "links": [l for l in links if l["from"] in keep and l["to"] in keep],
        "transpositions": [
            t for t in transpositions if t["from"] in keep and t["to"] in keep
        ],
        "roots": [n["key"] for n in roots],
    }


# --------------------------------------------------------------------------- #
# Coverage                                                                     #
# --------------------------------------------------------------------------- #


def coverage_for(
    connection: sqlite3.Connection,
    fen: str,
    *,
    user_id: int,
    color: str,
    allow_network: bool = False,
) -> dict[str, Any]:
    """How much of the likely reply space at ``fen`` this repertoire answers.

    Weighted by frequency in the peer corpus, so three replies covering 80% of
    what you actually face beats twelve covering 30%. Returns ``None`` coverage
    — not ``0`` — when there is no explorer data, because "you have covered
    nothing" and "we do not know what you face" are different and only one of
    them is your fault.
    """

    peers, source = opening_cache.lookup(
        connection,
        fen,
        ratings=list(rating_band(user_rating(connection, user_id))),
        allow_network=allow_network,
    )
    node = find_node(connection, user_id, color=color, fen=fen)
    chosen = {e["uci"] for e in load_edges(connection, user_id, node["id"])} if node else set()

    if peers is None:
        return {"coverage": None, "source": source, "uncovered": [], "chosen": sorted(chosen)}

    rows = peers.get("moves") or []
    total = sum(row_games(r) for r in rows)
    if not total:
        return {"coverage": None, "source": source, "uncovered": [], "chosen": sorted(chosen)}

    covered = sum(row_games(r) for r in rows if str(r.get("uci")) in chosen)
    uncovered = sorted(
        (
            {
                "uci": str(r["uci"]),
                "san": str(r.get("san") or ""),
                "share": round(row_games(r) / total, 4),
                "games": row_games(r),
            }
            for r in rows
            if str(r.get("uci")) not in chosen and row_games(r) > 0
        ),
        key=lambda r: -r["share"],
    )
    return {
        "coverage": round(min(1.0, covered / total), 4),
        "source": source,
        "uncovered": uncovered[:8],
        "chosen": sorted(chosen),
    }


__all__ = [
    "COLORS",
    "MINE",
    "ROLES",
    "START_FEN",
    "THEIRS",
    "add_edge",
    "build_graph",
    "build_lines",
    "candidates_for",
    "coverage_for",
    "ensure_node",
    "find_node",
    "gather_evidence",
    "load_edges",
    "load_tree",
    "remove_edge",
    "user_rating",
]
