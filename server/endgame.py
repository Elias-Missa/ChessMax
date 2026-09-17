"""Endgame Arena — pick a mined endgame, play it out against Maia, grade it.

Positions come from two corpora in roughly equal measure, because each is weak
where the other is strong:

* **Your own reviewed games** (``review_moves``) — endgames you actually
  reached, already carrying a stored evaluation, so bucketing is a DB query
  rather than an engine pass. Worthless on a cold account with no reviews.
* **Lichess puzzle endgames** (``positions``, themes like ``rookEndgame``) —
  always available and rating-tagged, but they are tactical positions with a
  forced solution rather than the quiet holds and conversions the mode is for.

Mixing them means a new account still has something to play and a returning one
mostly drills its own endgames. The split is a target, not a quota: whichever
corpus can fill the gap does.

The rules — what qualifies, who you face, whether a draw sticks — live in
:mod:`core.endgame` and are pure. This module is the database and session half.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

import chess
import chess.pgn

from core.endgame import (
    BUCKETS,
    DRAWN,
    EndgameConstants,
    bucket_for_eval,
    draw_offer_verdict,
    grade_outcome,
    is_endgame,
    maia_rating_for,
)

#: ``(fen, maia_rating) -> uci``. Injected so tests never need lc0.
MoveSelector = Callable[..., str | None]
#: ``fen -> cp`` from White's point of view. Injected so tests never need Stockfish.
EvalFn = Callable[[str], int | None]

OWN_GAMES = "own_games"
PUZZLES = "puzzles"

# Lichess theme tags that mark a position as an endgame. `themes` is a
# space-joined string, so these are matched with LIKE rather than parsed.
_ENDGAME_THEMES: tuple[str, ...] = (
    "rookEndgame",
    "pawnEndgame",
    "queenEndgame",
    "bishopEndgame",
    "knightEndgame",
    "queenRookEndgame",
    "endgame",
)

_RECENT_EXCLUDE = 40


@dataclass(frozen=True)
class ArenaPosition:
    fen: str
    bucket: str
    source: str
    source_ref: str | None
    user_color: str
    eval_cp: int
    """Evaluation from the **user's** point of view, in centipawns."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fen": self.fen,
            "bucket": self.bucket,
            "source": self.source,
            "source_ref": self.source_ref,
            "user_color": self.user_color,
            "eval_cp": self.eval_cp,
        }


# --------------------------------------------------------------------------- #
# Sourcing                                                                     #
# --------------------------------------------------------------------------- #


def _user_pov(eval_cp_white: int, user_color: str) -> int:
    return eval_cp_white if user_color == "w" else -eval_cp_white


def _recent_fens(connection: sqlite3.Connection, user_id: int) -> set[str]:
    """Starting positions the user has seen lately, so the arena does not repeat."""

    rows = connection.execute(
        "SELECT initial_fen FROM endgame_sessions WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, _RECENT_EXCLUDE),
    ).fetchall()
    return {str(row["initial_fen"]) for row in rows}


def candidates_from_own_games(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    buckets: tuple[str, ...] = BUCKETS,
    limit: int = 400,
    constants: EndgameConstants | None = None,
) -> list[ArenaPosition]:
    """Endgames out of the user's own reviewed games.

    ``review_moves.detail`` already carries ``fen_before`` and ``eval_cp`` for
    every ply, so this needs no engine — only a piece-count filter and a bucket.
    """

    constants = constants or EndgameConstants.load()
    try:
        rows = connection.execute(
            """
            SELECT rm.detail, r.user_color, r.game_id
            FROM review_moves rm
            JOIN reviews r ON r.review_id = rm.review_id
            WHERE r.user_id = ? AND r.status = 'complete' AND rm.detail IS NOT NULL
            ORDER BY RANDOM() LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    except sqlite3.Error:
        return []

    out: list[ArenaPosition] = []
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (json.JSONDecodeError, TypeError):
            continue
        fen = detail.get("fen_before")
        eval_cp = detail.get("eval_cp")
        if not fen or eval_cp is None:
            continue
        try:
            board = chess.Board(fen)
        except ValueError:
            continue
        if not board.is_valid() or board.is_game_over():
            continue
        if not is_endgame(board, constants):
            continue

        # Stored `eval_cp` is side-to-move POV; normalise to White, then to the
        # user. Getting this wrong silently inverts every bucket.
        eval_white = eval_cp if board.turn == chess.WHITE else -eval_cp
        user_color = "w" if str(row["user_color"]) == "white" else "b"
        user_eval = _user_pov(int(eval_white), user_color)
        bucket = bucket_for_eval(user_eval, constants)
        if bucket is None or bucket not in buckets:
            continue
        # The user must be the one to move, or the exercise starts on someone
        # else's decision.
        if (board.turn == chess.WHITE) != (user_color == "w"):
            continue
        out.append(
            ArenaPosition(
                fen=fen,
                bucket=bucket,
                source=OWN_GAMES,
                source_ref=str(row["game_id"]),
                user_color=user_color,
                eval_cp=user_eval,
            )
        )
    return out


def candidates_from_puzzles(
    connection: sqlite3.Connection,
    *,
    buckets: tuple[str, ...] = BUCKETS,
    limit: int = 400,
    constants: EndgameConstants | None = None,
) -> list[ArenaPosition]:
    """Endgames out of the imported Lichess puzzle set."""

    constants = constants or EndgameConstants.load()
    theme_clause = " OR ".join("themes LIKE ?" for _ in _ENDGAME_THEMES)
    params: list[Any] = [f"%{theme}%" for theme in _ENDGAME_THEMES]
    try:
        rows = connection.execute(
            f"""
            SELECT id, fen, side_to_move, best_eval, themes
            FROM positions
            WHERE themes IS NOT NULL AND ({theme_clause})
            ORDER BY RANDOM() LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    except sqlite3.Error:
        return []

    out: list[ArenaPosition] = []
    for row in rows:
        fen = str(row["fen"])
        try:
            board = chess.Board(fen)
        except ValueError:
            continue
        if not board.is_valid() or board.is_game_over():
            continue
        if not is_endgame(board, constants):
            continue

        # `positions.best_eval` is the solver's own evaluation in pawns, from the
        # side to move — and the solver is who the user plays as.
        user_color = "w" if board.turn == chess.WHITE else "b"
        user_eval = int(round(float(row["best_eval"]) * 100))
        bucket = bucket_for_eval(user_eval, constants)
        if bucket is None or bucket not in buckets:
            continue
        out.append(
            ArenaPosition(
                fen=fen,
                bucket=bucket,
                source=PUZZLES,
                source_ref=str(row["id"]),
                user_color=user_color,
                eval_cp=user_eval,
            )
        )
    return out


def pick_position(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    bucket: str | None = None,
    rng: random.Random | None = None,
    constants: EndgameConstants | None = None,
) -> ArenaPosition | None:
    """One endgame to play, drawn ~50/50 from the two corpora.

    The split is a target rather than a quota: a cold account has no reviewed
    games and gets puzzles, and a DB with no puzzle import gets its own games.
    Whichever side can fill the gap does.
    """

    constants = constants or EndgameConstants.load()
    rng = rng or random.Random()
    wanted = (bucket,) if bucket in BUCKETS else BUCKETS

    own = candidates_from_own_games(
        connection, user_id, buckets=wanted, constants=constants
    )
    puzzles = candidates_from_puzzles(connection, buckets=wanted, constants=constants)

    # Prefer positions the user has not just seen — but only prefer. On a small
    # corpus the exclusion window swallows the whole pool, and repeating an
    # endgame is a far better answer than refusing to deal one.
    seen = _recent_fens(connection, user_id)
    fresh_own = [p for p in own if p.fen not in seen]
    fresh_puzzles = [p for p in puzzles if p.fen not in seen]
    if fresh_own or fresh_puzzles:
        own, puzzles = fresh_own, fresh_puzzles

    first, second = (own, puzzles) if rng.random() < 0.5 else (puzzles, own)
    pool = first or second
    if not pool:
        return None
    return rng.choice(pool)


# --------------------------------------------------------------------------- #
# Session state                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArenaState:
    session_id: int
    bucket: str
    source: str
    user_color: str
    maia_rating: int
    initial_fen: str
    fen: str
    moves: list[str]
    status: str
    result: str | None = None
    outcome: str | None = None
    passed: bool | None = None
    message: str | None = None
    start_eval_cp: int | None = None
    takebacks: int = 0
    last_move_uci: str | None = None
    maia_move_uci: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "bucket": self.bucket,
            "source": self.source,
            "user_color": self.user_color,
            "maia_rating": self.maia_rating,
            "initial_fen": self.initial_fen,
            "fen": self.fen,
            "moves": list(self.moves),
            "status": self.status,
            "result": self.result,
            "outcome": self.outcome,
            "passed": self.passed,
            "message": self.message,
            "start_eval_cp": self.start_eval_cp,
            "takebacks": self.takebacks,
            "last_move_uci": self.last_move_uci,
            "maia_move_uci": self.maia_move_uci,
        }


def _decode(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [m for m in decoded if isinstance(m, str)]


def _rebuild(initial_fen: str, moves: list[str]) -> chess.Board:
    board = chess.Board(initial_fen)
    for uci in moves:
        try:
            board.push(chess.Move.from_uci(uci))
        except (ValueError, AssertionError):
            break
    return board


def _load(connection: sqlite3.Connection, session_id: int, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM endgame_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()
    if row is None:
        raise ValueError("Arena session not found")
    if str(row["status"]) != "active":
        raise ValueError("This arena game is already finished")
    return row


def _result_for(board: chess.Board, user_color: str) -> str | None:
    """``win``/``draw``/``loss`` from the user's side, or ``None`` if unfinished."""

    if not board.is_game_over(claim_draw=True):
        return None
    outcome = board.outcome(claim_draw=True)
    if outcome is None or outcome.winner is None:
        return "draw"
    user_is_white = user_color == "w"
    return "win" if outcome.winner == user_is_white else "loss"


def _persist(
    connection: sqlite3.Connection,
    session_id: int,
    board: chess.Board,
    moves: list[str],
    **extra: Any,
) -> None:
    sets = ["fen = ?", "move_list = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list[Any] = [board.fen(), json.dumps(moves)]
    for key, value in extra.items():
        sets.append(f"{key} = ?")
        params.append(value)
    params.append(session_id)
    connection.execute(
        f"UPDATE endgame_sessions SET {', '.join(sets)} WHERE id = ?", params
    )
    connection.commit()


def _finish(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    board: chess.Board,
    moves: list[str],
    result: str,
    *,
    end_eval_cp: int | None = None,
    message_override: str | None = None,
) -> ArenaState:
    """Close a session, grade it against the bucket it started in, and record it."""

    bucket = str(row["bucket"])
    outcome, passed, message = grade_outcome(bucket, result)
    session_id = int(row["id"])

    _persist(connection, session_id, board, moves, status="finished", result=result)
    connection.execute(
        """
        INSERT INTO endgame_results (
            user_id, session_id, bucket, source, maia_rating, user_color,
            start_eval_cp, end_eval_cp, result, outcome, passed, plies,
            takebacks, initial_fen, pgn
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(row["user_id"]),
            session_id,
            bucket,
            str(row["source"]),
            int(row["maia_rating"]),
            str(row["user_color"]),
            row["start_eval_cp"],
            end_eval_cp,
            result,
            outcome,
            1 if passed else 0,
            len(moves),
            int(row["takebacks"] or 0),
            str(row["initial_fen"]),
            build_pgn(row, moves, result),
        ),
    )
    connection.commit()

    return ArenaState(
        session_id=session_id,
        bucket=bucket,
        source=str(row["source"]),
        user_color=str(row["user_color"]),
        maia_rating=int(row["maia_rating"]),
        initial_fen=str(row["initial_fen"]),
        fen=board.fen(),
        moves=moves,
        status="finished",
        result=result,
        outcome=outcome,
        passed=passed,
        message=message_override or message,
        start_eval_cp=row["start_eval_cp"],
        takebacks=int(row["takebacks"] or 0),
    )


def build_pgn(row: sqlite3.Row, moves: list[str], result: str) -> str:
    """A PGN for the finished game, so it can be handed to the full review."""

    board = chess.Board(str(row["initial_fen"]))
    game = chess.pgn.Game()
    game.setup(board)
    node: Any = game
    for uci in moves:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        node = node.add_variation(move)
        board.push(move)

    user_is_white = str(row["user_color"]) == "w"
    maia = f"Maia {int(row['maia_rating'])}"
    game.headers["Event"] = f"Endgame Arena ({row['bucket']})"
    game.headers["White"] = "You" if user_is_white else maia
    game.headers["Black"] = maia if user_is_white else "You"
    token = {"win": "1-0", "loss": "0-1", "draw": "1/2-1/2"}.get(result, "*")
    game.headers["Result"] = token if user_is_white else {
        "1-0": "0-1", "0-1": "1-0"
    }.get(token, token)
    game.headers["SetUp"] = "1"
    game.headers["FEN"] = str(row["initial_fen"])
    return str(game)


def start_session(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    user_rating: int,
    bucket: str | None = None,
    move_selector: MoveSelector,
    rng: random.Random | None = None,
    constants: EndgameConstants | None = None,
) -> ArenaState | None:
    """Mine a position, pick the opponent off the ladder, and open a session."""

    constants = constants or EndgameConstants.load()
    position = pick_position(
        connection, user_id, bucket=bucket, rng=rng, constants=constants
    )
    if position is None:
        return None

    maia_rating = maia_rating_for(position.bucket, user_rating, constants)
    connection.execute(
        "UPDATE endgame_sessions SET status = 'abandoned' "
        "WHERE user_id = ? AND status = 'active'",
        (user_id,),
    )
    cursor = connection.execute(
        """
        INSERT INTO endgame_sessions (
            user_id, bucket, source, source_ref, user_color, maia_rating,
            user_rating, start_eval_cp, initial_fen, fen, move_list
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
        """,
        (
            user_id,
            position.bucket,
            position.source,
            position.source_ref,
            position.user_color,
            maia_rating,
            user_rating,
            position.eval_cp,
            position.fen,
            position.fen,
        ),
    )
    connection.commit()
    session_id = int(cursor.lastrowid)

    row = connection.execute(
        "SELECT * FROM endgame_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    board = chess.Board(position.fen)
    moves: list[str] = []

    # The picker guarantees the user is to move, but a puzzle row could still
    # disagree; if so, let Maia open rather than stalling the board.
    user_turn = chess.WHITE if position.user_color == "w" else chess.BLACK
    if board.turn != user_turn:
        reply = _maia_reply(board, int(maia_rating), move_selector)
        if reply is not None:
            moves.append(reply.uci())
            board.push(reply)
            _persist(connection, session_id, board, moves)

    return ArenaState(
        session_id=session_id,
        bucket=position.bucket,
        source=position.source,
        user_color=position.user_color,
        maia_rating=maia_rating,
        initial_fen=position.fen,
        fen=board.fen(),
        moves=moves,
        status="active",
        start_eval_cp=position.eval_cp,
    )


def _maia_reply(
    board: chess.Board, maia_rating: int, move_selector: MoveSelector
) -> chess.Move | None:
    if board.is_game_over(claim_draw=True):
        return None
    try:
        uci = move_selector(board.fen(), maia_rating)
    except Exception:  # noqa: BLE001 — a dead engine must not strand the session
        uci = None
    if uci:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            move = None
        if move is not None and move in board.legal_moves:
            return move
    # Falling back to a legal move keeps the game playable rather than hanging
    # on a missing lc0; it is worse chess, never a broken board.
    legal = list(board.legal_moves)
    return legal[0] if legal else None


def play_move(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    user_id: int,
    move_uci: str,
    move_selector: MoveSelector,
    eval_fn: EvalFn | None = None,
) -> ArenaState:
    """Apply the user's move, then Maia's reply."""

    row = _load(connection, session_id, user_id)
    board = _rebuild(str(row["initial_fen"]), _decode(row["move_list"]))
    moves = _decode(row["move_list"])
    user_color = str(row["user_color"])
    user_turn = chess.WHITE if user_color == "w" else chess.BLACK

    if board.turn != user_turn:
        raise ValueError("It is not your turn")
    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError as exc:
        raise ValueError(f"Unparseable move: {move_uci}") from exc
    if move not in board.legal_moves:
        raise ValueError(f"Illegal move: {move_uci}")

    moves.append(move.uci())
    board.push(move)

    result = _result_for(board, user_color)
    if result is not None:
        return _finish(connection, row, board, moves, result,
                       end_eval_cp=_safe_eval(eval_fn, board, user_color))

    reply = _maia_reply(board, int(row["maia_rating"]), move_selector)
    maia_uci = None
    if reply is not None:
        maia_uci = reply.uci()
        moves.append(maia_uci)
        board.push(reply)

    result = _result_for(board, user_color)
    if result is not None:
        return _finish(connection, row, board, moves, result,
                       end_eval_cp=_safe_eval(eval_fn, board, user_color))

    _persist(connection, session_id, board, moves)
    return ArenaState(
        session_id=session_id,
        bucket=str(row["bucket"]),
        source=str(row["source"]),
        user_color=user_color,
        maia_rating=int(row["maia_rating"]),
        initial_fen=str(row["initial_fen"]),
        fen=board.fen(),
        moves=moves,
        status="active",
        start_eval_cp=row["start_eval_cp"],
        takebacks=int(row["takebacks"] or 0),
        last_move_uci=move.uci(),
        maia_move_uci=maia_uci,
    )


def _safe_eval(
    eval_fn: EvalFn | None, board: chess.Board, user_color: str
) -> int | None:
    if eval_fn is None:
        return None
    try:
        white_cp = eval_fn(board.fen())
    except Exception:  # noqa: BLE001
        return None
    if white_cp is None:
        return None
    return _user_pov(int(white_cp), user_color)


def takeback(
    connection: sqlite3.Connection, *, session_id: int, user_id: int
) -> ArenaState:
    """Undo back to the user's previous turn — both their move and Maia's reply.

    This is a learning mode, so takebacks are free and unlimited; they are
    counted and shown on the result so a game full of them reads honestly.
    """

    row = _load(connection, session_id, user_id)
    moves = _decode(row["move_list"])
    if not moves:
        raise ValueError("No moves to take back")

    user_color = str(row["user_color"])
    user_turn = chess.WHITE if user_color == "w" else chess.BLACK
    initial_fen = str(row["initial_fen"])

    trimmed = moves[:]
    while trimmed:
        trimmed.pop()
        board = _rebuild(initial_fen, trimmed)
        if board.turn == user_turn:
            break

    board = _rebuild(initial_fen, trimmed)
    takebacks = int(row["takebacks"] or 0) + 1
    _persist(connection, session_id, board, trimmed, takebacks=takebacks)
    return ArenaState(
        session_id=session_id,
        bucket=str(row["bucket"]),
        source=str(row["source"]),
        user_color=user_color,
        maia_rating=int(row["maia_rating"]),
        initial_fen=initial_fen,
        fen=board.fen(),
        moves=trimmed,
        status="active",
        start_eval_cp=row["start_eval_cp"],
        takebacks=takebacks,
    )


def offer_draw(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    user_id: int,
    eval_fn: EvalFn | None = None,
    constants: EndgameConstants | None = None,
) -> ArenaState:
    """Offer a draw. Maia accepts only when it is not winning.

    This is the mode's teaching mechanism, so the decline path deliberately
    leaves the game running: a position you threw away is one you have to keep
    playing.
    """

    constants = constants or EndgameConstants.load()
    row = _load(connection, session_id, user_id)
    moves = _decode(row["move_list"])
    board = _rebuild(str(row["initial_fen"]), moves)
    user_color = str(row["user_color"])

    user_eval = _safe_eval(eval_fn, board, user_color)
    maia_eval = None if user_eval is None else -user_eval

    verdict = draw_offer_verdict(
        maia_eval_cp=maia_eval, plies_played=len(moves), constants=constants
    )
    connection.execute(
        "UPDATE endgame_sessions SET draw_offers = draw_offers + 1 WHERE id = ?",
        (session_id,),
    )
    connection.commit()

    if verdict.accepted:
        return _finish(
            connection, row, board, moves, "draw",
            end_eval_cp=user_eval, message_override=verdict.message,
        )

    return ArenaState(
        session_id=session_id,
        bucket=str(row["bucket"]),
        source=str(row["source"]),
        user_color=user_color,
        maia_rating=int(row["maia_rating"]),
        initial_fen=str(row["initial_fen"]),
        fen=board.fen(),
        moves=moves,
        status="active",
        message=verdict.message,
        start_eval_cp=row["start_eval_cp"],
        takebacks=int(row["takebacks"] or 0),
    )


def resign(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    user_id: int,
    eval_fn: EvalFn | None = None,
) -> ArenaState:
    row = _load(connection, session_id, user_id)
    moves = _decode(row["move_list"])
    board = _rebuild(str(row["initial_fen"]), moves)
    return _finish(
        connection, row, board, moves, "loss",
        end_eval_cp=_safe_eval(eval_fn, board, str(row["user_color"])),
    )


def active_session(
    connection: sqlite3.Connection, user_id: int
) -> ArenaState | None:
    row = connection.execute(
        "SELECT * FROM endgame_sessions WHERE user_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    moves = _decode(row["move_list"])
    return ArenaState(
        session_id=int(row["id"]),
        bucket=str(row["bucket"]),
        source=str(row["source"]),
        user_color=str(row["user_color"]),
        maia_rating=int(row["maia_rating"]),
        initial_fen=str(row["initial_fen"]),
        fen=str(row["fen"]),
        moves=moves,
        status="active",
        start_eval_cp=row["start_eval_cp"],
        takebacks=int(row["takebacks"] or 0),
    )


def summary(connection: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Per-bucket record, so "I cannot convert rook endings" becomes visible."""

    rows = connection.execute(
        "SELECT bucket, result, outcome, passed FROM endgame_results WHERE user_id = ?",
        (user_id,),
    ).fetchall()

    per_bucket: dict[str, dict[str, Any]] = {
        bucket: {"played": 0, "passed": 0, "win": 0, "draw": 0, "loss": 0}
        for bucket in BUCKETS
    }
    for row in rows:
        bucket = str(row["bucket"])
        if bucket not in per_bucket:
            continue
        entry = per_bucket[bucket]
        entry["played"] += 1
        entry["passed"] += 1 if row["passed"] else 0
        result = str(row["result"])
        if result in entry:
            entry[result] += 1

    for entry in per_bucket.values():
        entry["pass_rate"] = (
            round(entry["passed"] / entry["played"], 3) if entry["played"] else None
        )

    return {
        "played": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "buckets": per_bucket,
    }


def recent_results(
    connection: sqlite3.Connection, user_id: int, limit: int = 10
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT bucket, source, maia_rating, result, outcome, passed, plies, "
        "takebacks, start_eval_cp, end_eval_cp, created_at "
        "FROM endgame_results WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "OWN_GAMES",
    "PUZZLES",
    "ArenaPosition",
    "ArenaState",
    "active_session",
    "build_pgn",
    "candidates_from_own_games",
    "candidates_from_puzzles",
    "offer_draw",
    "pick_position",
    "play_move",
    "recent_results",
    "resign",
    "start_session",
    "summary",
    "takeback",
]
