"""Structural shape of a game: the pawn centre it was played in, and the
endgame it turned into.

The rest of the Insights catalogue measures *how well* moves were played. This
module measures *what kind of position* they were played in, so a study plan can
say "you lose rook endings" instead of "work on endgames". Both classifications
are cheap, deterministic replays of the stored PGN — no engine, no network.

Both are **heuristics, and labelled as such**. They exist to bucket a player's
own games into groups big enough to compare, not to adjudicate a position. The
rule for each is written out beside it so a reader can decide whether a bucket
means what they think it means.
"""

from __future__ import annotations

import io
from typing import Any

import chess
import chess.pgn

#: Files that make up "the centre" for the pawn-structure rule. c and f are in
#: because a locked d/e chain is nearly always braced by one of them, and a
#: French/King's Indian centre reads as open without them.
CENTRE_FILES = (chess.BB_FILE_C | chess.BB_FILE_D | chess.BB_FILE_E | chess.BB_FILE_F)

CENTRE_LABELS = {
    "closed": "closed centre",
    "semi_open": "semi-open centre",
    "open": "open centre",
}

ENDGAME_LABELS = {
    "pawn": "pawn endings",
    "minor": "minor-piece endings",
    "rook": "rook endings",
    "rook_minor": "rook-and-minor endings",
    "queen": "queen endings",
    "heavy": "queens-still-on endings",
}


def centre_type(board: chess.Board) -> str:
    """Classify the pawn centre.

    Counts pawns of both colours on the c–f files and how many of them are
    blocked head-on by an enemy pawn. Two or more blocked pairs is a structure
    neither side can open by choice; a nearly empty centre is an open game.

    * ``closed``    — 2+ blocked pawn pairs in the centre complex.
    * ``open``      — 3 or fewer centre pawns left standing.
    * ``semi_open`` — everything in between, the ordinary asymmetric middlegame.
    """

    pawns = 0
    blocked = 0
    for color in (chess.WHITE, chess.BLACK):
        forward = 8 if color == chess.WHITE else -8
        for square in chess.scan_forward(board.pieces_mask(chess.PAWN, color) & CENTRE_FILES):
            pawns += 1
            ahead = square + forward
            if not 0 <= ahead < 64:
                continue
            blocker = board.piece_at(ahead)
            if blocker and blocker.piece_type == chess.PAWN and blocker.color != color:
                blocked += 1

    # Each blocked pair is counted from both sides above.
    if blocked // 2 >= 2:
        return "closed"
    if pawns <= 3:
        return "open"
    return "semi_open"


def endgame_type(board: chess.Board) -> str:
    """Name the endgame by the pieces still on, ignoring which side owns them.

    Deliberately coarse: the point is to pool a player's games into buckets
    large enough to carry a win rate, and "rook ending" is the level at which
    endgame study material is actually organised.
    """

    def count(piece_type: int) -> int:
        return len(board.pieces(piece_type, chess.WHITE)) + len(
            board.pieces(piece_type, chess.BLACK)
        )

    queens = count(chess.QUEEN)
    rooks = count(chess.ROOK)
    minors = count(chess.BISHOP) + count(chess.KNIGHT)

    if queens and (rooks or minors):
        return "heavy"
    if queens:
        return "queen"
    if rooks and minors:
        return "rook_minor"
    if rooks:
        return "rook"
    if minors:
        return "minor"
    return "pawn"


def board_at_ply(pgn: Any, ply: int) -> chess.Board | None:
    """The position *before* ``ply`` (1-based, matching ``review_moves.ply``).

    Returns ``None`` rather than raising on anything malformed — a game whose
    PGN will not parse simply has no shape, and every caller treats that as
    "unknown" instead of a failure.
    """

    if not pgn or ply is None or int(ply) < 1:
        return None
    try:
        game = chess.pgn.read_game(io.StringIO(str(pgn)))
    except (ValueError, RuntimeError):
        return None
    if game is None:
        return None
    board = game.board()
    target = int(ply) - 1
    played = 0
    try:
        for move in game.mainline_moves():
            if played >= target:
                break
            board.push(move)
            played += 1
    except (ValueError, AssertionError):
        return None
    if played < target:
        return None
    return board


def game_shape(
    pgn: Any,
    *,
    middlegame_ply: int | None,
    endgame_ply: int | None,
) -> dict[str, str | None]:
    """Centre type at the first middlegame move, endgame type at the first
    endgame move. Either half is ``None`` when the game never reached that
    phase or the PGN would not replay — never a guessed default.
    """

    centre = None
    endgame = None
    if middlegame_ply is not None:
        board = board_at_ply(pgn, middlegame_ply)
        if board is not None:
            centre = centre_type(board)
    if endgame_ply is not None:
        board = board_at_ply(pgn, endgame_ply)
        if board is not None:
            endgame = endgame_type(board)
    return {"centre": centre, "endgame_type": endgame}
