"""Paste a PGN, get a repertoire.

The builder is a move-at-a-time tool, and a repertoire you already have — a
Lichess study chapter, a Chessbook export, a chapter of notes, or just a line
you typed — should not have to be re-clicked to get in. This walks a PGN into
the authored tree.

**Variations are the point, not an extra.** A repertoire PGN is a tree, and its
branches live in the RAV parentheses: `1.e4 c5 2.Nf3 (2.Nc3 Nc6) d6` is three
decisions, not one line with decoration. Importing only the mainline would throw
away most of what a repertoire PGN carries, so the walk recurses through every
variation and each node becomes an edge.

Three limits, all of them about a paste being hostile by accident rather than by
malice:

* ``max_plies`` stops a full game from filing forty plies of middlegame as
  opening theory.
* ``max_moves`` caps the whole import, because a deeply nested study can hold
  thousands of nodes and every one of them is two SQLite writes.
* Illegal or unparseable input is **skipped and counted**, never fatal. A paste
  of five games where one is malformed should import four and say so.

The result is a report, not a bare count, and it counts ``decisions``
separately from ``added``. Roles are *derived* from whose turn it is, so a
repertoire pasted into the wrong side still imports cleanly — every move simply
lands as an opponent reply and none of it is ever drilled. That is the real
wrong-colour symptom and it is otherwise completely silent, so the report says
so rather than reporting a cheerful success.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterator

import chess
import chess.pgn

from core.repertoire import BLACK, WHITE
from server import openings_build

#: Depth cap, in plies. Same reasoning and same number as the mined repertoire's
#: ``max_plies``: past 8 moves a side it stops being an opening decision.
MAX_PLIES = 16

#: Hard ceiling on one import. A nested study can carry thousands of nodes and
#: each is an INSERT; this keeps a paste bounded without a timeout.
MAX_MOVES = 600

#: How many games to read out of one paste.
MAX_GAMES = 20


@dataclass
class ImportReport:
    games: int = 0
    added: int = 0
    decisions: int = 0
    already: int = 0
    skipped_depth: int = 0
    skipped_illegal: int = 0
    truncated: bool = False
    openings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "games": self.games,
            "added": self.added,
            "decisions": self.decisions,
            "already": self.already,
            "skipped_depth": self.skipped_depth,
            "skipped_illegal": self.skipped_illegal,
            "truncated": self.truncated,
            "openings": self.openings,
            "errors": self.errors,
            "message": self.message(),
        }

    def message(self) -> str:
        if self.errors and not self.added and not self.already:
            return self.errors[0]
        if not self.games:
            return "No games found in that PGN."
        if not self.added and self.already:
            return f"Every move in that PGN was already in your book ({self.already})."
        if not self.added:
            return "Nothing in that PGN could be added to your book."
        parts = [f"Added {self.added} move{'' if self.added == 1 else 's'}"]
        if not self.decisions:
            # Roles are DERIVED from whose turn it is, so a repertoire pasted
            # into the wrong side still imports — every move just lands as an
            # opponent reply and none of it is ever drilled. That is the actual
            # wrong-colour symptom, and it is otherwise completely silent.
            parts.append(
                "none of them yours — if that is your own repertoire, "
                "import it as the other colour"
            )
        if self.already:
            parts.append(f"{self.already} already there")
        if self.skipped_depth:
            parts.append(f"{self.skipped_depth} past the depth limit")
        if self.skipped_illegal:
            parts.append(f"{self.skipped_illegal} illegal")
        if self.truncated:
            parts.append("stopped at the import cap")
        return " · ".join(parts) + "."


def read_games(pgn_text: str, *, max_games: int = MAX_GAMES) -> Iterator[chess.pgn.Game]:
    """Yield parsed games from a paste, stopping at ``max_games``.

    ``read_game`` returns ``None`` at end of input, and raises on some malformed
    input rather than returning; both end the iteration cleanly.
    """

    stream = io.StringIO(pgn_text)
    for _ in range(max_games):
        try:
            game = chess.pgn.read_game(stream)
        except Exception:  # noqa: BLE001 — a bad paste ends the read, not the request
            return
        if game is None:
            return
        yield game


def _walk(
    node: chess.pgn.GameNode,
    board: chess.Board,
    *,
    path: list[str],
    depth: int,
    max_plies: int,
    on_move: Any,
    budget: list[int],
) -> None:
    """Depth-first through the mainline *and* every variation.

    ``board`` is mutated and restored around each branch rather than copied:
    a study with a few thousand nodes copies a lot of boards otherwise, and the
    push/pop pair is exact.
    """

    for child in node.variations:
        if budget[0] <= 0:
            return
        move = child.move
        if move is None:
            continue
        if depth >= max_plies:
            on_move(None, None, None, "depth")
            continue
        if move not in board.legal_moves:
            on_move(None, None, None, "illegal")
            continue

        on_move(board.fen(), move.uci(), list(path), None)
        budget[0] -= 1

        board.push(move)
        path.append(move.uci())
        _walk(
            child,
            board,
            path=path,
            depth=depth + 1,
            max_plies=max_plies,
            on_move=on_move,
            budget=budget,
        )
        path.pop()
        board.pop()


def import_pgn(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    color: str,
    pgn_text: str,
    max_plies: int = MAX_PLIES,
    max_moves: int = MAX_MOVES,
) -> ImportReport:
    """Walk a PGN into the authored tree for ``color``.

    Every node becomes an edge, mainline and variation alike, and
    ``openings_build.add_edge`` derives whose move each one is — so a paste of a
    full game correctly files White's moves as decisions and Black's as replies
    being covered (or the reverse), without the caller having to say.
    """

    report = ImportReport()
    if color not in (WHITE, BLACK):
        report.errors.append(f"unknown colour: {color!r}")
        return report
    if not pgn_text.strip():
        report.errors.append("Paste a PGN first.")
        return report

    budget = [max(1, int(max_moves))]
    seen: set[tuple[str, str]] = set()

    for game in read_games(pgn_text):
        report.games += 1
        try:
            board = game.board()
        except Exception:  # noqa: BLE001 — a broken [FEN] header
            report.skipped_illegal += 1
            continue

        # A study chapter's own name is the most useful opening label we get;
        # the ECO/Opening headers are often absent on hand-written repertoires.
        name = (
            game.headers.get("Opening")
            or game.headers.get("Event")
            or game.headers.get("White")
            or None
        )
        eco = game.headers.get("ECO") or None
        if name and name not in report.openings and name != "?":
            report.openings.append(str(name))

        def record(
            fen: str | None,
            uci: str | None,
            path: list[str] | None,
            skip: str | None,
        ) -> None:
            if skip == "depth":
                report.skipped_depth += 1
                return
            if skip == "illegal":
                report.skipped_illegal += 1
                return
            if fen is None or uci is None:
                return
            key = (fen, uci)
            if key in seen:
                return
            seen.add(key)
            try:
                existing = openings_build.find_node(
                    connection, user_id, color=color, fen=fen
                )
                had = (
                    {e["uci"] for e in openings_build.load_edges(
                        connection, user_id, int(existing["id"])
                    )}
                    if existing is not None
                    else set()
                )
                openings_build.add_edge(
                    connection,
                    user_id,
                    color=color,
                    fen=fen,
                    uci=uci,
                    path_uci=path or [],
                    opening_name=str(name) if name and name != "?" else None,
                    opening_eco=str(eco) if eco and eco != "?" else None,
                )
                if uci in had:
                    report.already += 1
                else:
                    report.added += 1
                    board_at = chess.Board(fen)
                    if (board_at.turn == chess.WHITE) == (color == WHITE):
                        report.decisions += 1
            except ValueError:
                report.skipped_illegal += 1
            except sqlite3.Error as exc:
                report.errors.append(str(exc))

        _walk(
            game,
            board,
            path=[],
            depth=0,
            max_plies=max_plies,
            on_move=record,
            budget=budget,
        )
        if budget[0] <= 0:
            report.truncated = True
            break

    return report


__all__ = [
    "MAX_GAMES",
    "MAX_MOVES",
    "MAX_PLIES",
    "ImportReport",
    "import_pgn",
    "read_games",
]
