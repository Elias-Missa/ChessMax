"""Playout state machine and persistence helpers."""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import chess
import chess.pgn

from server.maia import best_move


ACTIVE_STATUS = "active"
TERMINAL_STATUSES = {"checkmate", "draw", "ended"}
MoveSelector = Callable[[str, int], tuple[str | None, str]]


@dataclass(frozen=True)
class PlayoutStartResult:
    playout_id: int
    fen: str
    status: str
    maia_move: str | None
    engine: str
    maia_rating: int
    initial_fen: str
    move_list: list[str]


@dataclass(frozen=True)
class PlayoutMoveResult:
    fen: str
    status: str
    maia_move: str | None
    result: str | None
    engine: str
    initial_fen: str
    move_list: list[str]


@dataclass(frozen=True)
class PlayoutTakebackResult:
    fen: str
    status: str
    engine: str
    undone_plies: int
    initial_fen: str
    move_list: list[str]


@dataclass(frozen=True)
class PlayoutEndResult:
    final_pgn: str
    result: str
    engine: str


def start_playout(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    position_id: int,
    fen: str,
    maia_rating: int,
    user_color: str,
    move_selector: MoveSelector = best_move,
) -> PlayoutStartResult:
    board = chess.Board(fen)
    color = normalize_user_color(user_color)
    _clear_active_sessions(connection, user_id)

    cursor = connection.execute(
        """
        INSERT INTO playout_sessions (
            user_id,
            position_id,
            maia_rating,
            user_color,
            engine,
            fen,
            initial_fen,
            move_list,
            eval_streak,
            streak_losing_side,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            position_id,
            maia_rating,
            color,
            "maia",
            board.fen(),
            board.fen(),
            "[]",
            0,
            None,
            ACTIVE_STATUS,
        ),
    )
    playout_id = int(cursor.lastrowid)

    maia_move: str | None = None
    engine_name = "maia"
    status, result = terminal_status_for_board(board, color)
    move_list: list[str] = []

    # If it's not the user's turn at start, engine plays immediately.
    if status == ACTIVE_STATUS and board.turn != (chess.WHITE if color == "w" else chess.BLACK):
        maia_move, engine_name = move_selector(board.fen(), maia_rating)
        reply = _legal_move_or_none(board, maia_move)
        if reply is not None:
            board.push(reply)
            move_list.append(maia_move)
            status, result = terminal_status_for_board(board, color)
        else:
            maia_move = None

    connection.execute(
        """
        UPDATE playout_sessions
        SET fen = ?, engine = ?, move_list = ?, status = ?, result = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            board.fen(),
            engine_name,
            json.dumps(move_list),
            status,
            result,
            playout_id,
        ),
    )

    if status in TERMINAL_STATUSES:
        _archive_and_close(connection, playout_id)
    connection.commit()
    return PlayoutStartResult(
        playout_id=playout_id,
        fen=board.fen(),
        status=status,
        maia_move=maia_move,
        engine=engine_name,
        maia_rating=maia_rating,
        initial_fen=fen,
        move_list=move_list,
    )


def play_user_move(
    connection: sqlite3.Connection,
    *,
    playout_id: int,
    user_id: int,
    move_uci: str,
    move_selector: MoveSelector = best_move,
) -> PlayoutMoveResult:
    session = _load_active_session(connection, playout_id, user_id)
    board = chess.Board(str(session["fen"]))
    color = str(session["user_color"])
    user_turn = chess.WHITE if color == "w" else chess.BLACK
    move_list = _decode_move_list(session["move_list"])
    maia_rating = int(session["maia_rating"])

    if board.turn != user_turn:
        raise ValueError("It is not your turn in this playout")

    move = chess.Move.from_uci(move_uci)
    if move not in board.legal_moves:
        raise ValueError(f"Illegal move for playout: {move_uci}")

    board.push(move)
    move_list.append(move_uci)

    status, result = terminal_status_for_board(board, color)
    maia_move: str | None = None
    engine_name = str(session["engine"] or "maia")

    if status == ACTIVE_STATUS:
        maia_move, engine_name = move_selector(board.fen(), maia_rating)
        reply = _legal_move_or_none(board, maia_move)
        if reply is None:
            # The game isn't over, so no reply means the engine failed.
            # Nothing is committed yet — surface it and let the user retry
            # instead of silently recording a fake draw.
            raise RuntimeError("Engine reply unavailable — please retry the move")
        board.push(reply)
        move_list.append(maia_move)
        status, result = terminal_status_for_board(board, color)

    connection.execute(
        """
        UPDATE playout_sessions
        SET fen = ?, engine = ?, move_list = ?, status = ?, result = ?,
            eval_streak = 0, streak_losing_side = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            board.fen(),
            engine_name,
            json.dumps(move_list),
            status,
            result,
            playout_id,
        ),
    )
    if status in TERMINAL_STATUSES:
        _archive_and_close(connection, playout_id)
    connection.commit()

    return PlayoutMoveResult(
        fen=board.fen(),
        status=status,
        maia_move=maia_move,
        result=result,
        engine=engine_name,
        initial_fen=str(session["initial_fen"]),
        move_list=move_list,
    )


def takeback_playout(
    connection: sqlite3.Connection,
    *,
    playout_id: int,
    user_id: int,
) -> PlayoutTakebackResult:
    session = _load_active_session(connection, playout_id, user_id)
    initial_fen = str(session["initial_fen"])
    color = str(session["user_color"])
    user_turn = chess.WHITE if color == "w" else chess.BLACK
    move_list = _decode_move_list(session["move_list"])
    if not move_list:
        raise ValueError("No moves to take back")

    trimmed = move_list[:]
    undone = 0
    while trimmed:
        trimmed.pop()
        undone += 1
        board = _rebuild_board(initial_fen, trimmed)
        if board.turn == user_turn:
            break

    board = _rebuild_board(initial_fen, trimmed)
    connection.execute(
        """
        UPDATE playout_sessions
        SET fen = ?, move_list = ?, status = 'active', result = NULL,
            eval_streak = 0, streak_losing_side = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (board.fen(), json.dumps(trimmed), playout_id),
    )
    connection.commit()
    return PlayoutTakebackResult(
        fen=board.fen(),
        status=ACTIVE_STATUS,
        engine=str(session["engine"] or "maia"),
        undone_plies=undone,
        initial_fen=initial_fen,
        move_list=trimmed,
    )


def end_playout(
    connection: sqlite3.Connection,
    *,
    playout_id: int,
    user_id: int,
) -> PlayoutEndResult:
    session = connection.execute(
        """
        SELECT * FROM playout_sessions
        WHERE id = ? AND user_id = ?
        """,
        (playout_id, user_id),
    ).fetchone()
    if session is None:
        raise ValueError("Playout not found")

    board = chess.Board(str(session["fen"]))
    color = str(session["user_color"])
    status, inferred_result = terminal_status_for_board(board, color)
    result = str(session["result"] or inferred_result or "draw")
    if status == ACTIVE_STATUS:
        status = "ended"

    connection.execute(
        """
        UPDATE playout_sessions
        SET status = ?, result = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, result, playout_id),
    )
    archived = _archive_and_close(connection, playout_id)
    connection.commit()
    return PlayoutEndResult(
        final_pgn=archived["pgn"],
        result=archived["result"],
        engine=archived["engine"],
    )


def list_recent_playouts(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, position_id, maia_rating, result, pgn, engine, timestamp
        FROM playouts
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        initial_fen, move_list = parse_pgn_state(str(row["pgn"]))
        payload.append(
            {
                "id": int(row["id"]),
                "position_id": int(row["position_id"]),
                "maia_rating": int(row["maia_rating"]),
                "result": str(row["result"]),
                "engine": str(row["engine"]),
                "timestamp": row["timestamp"],
                "pgn": str(row["pgn"]),
                "initial_fen": initial_fen,
                "move_list": move_list,
            }
        )
    return payload


def parse_pgn_state(pgn_text: str) -> tuple[str, list[str]]:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return chess.STARTING_FEN, []
    board = game.board()
    initial_fen = board.fen()
    moves: list[str] = []
    for move in game.mainline_moves():
        moves.append(move.uci())
        board.push(move)
    return initial_fen, moves


def normalize_user_color(value: str) -> str:
    if value not in {"w", "b"}:
        raise ValueError("user_color must be 'w' or 'b'")
    return value


def terminal_status_for_board(board: chess.Board, user_color: str) -> tuple[str, str | None]:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return ACTIVE_STATUS, None
    if outcome.winner is None:
        return "draw", "draw"
    user_is_white = user_color == "w"
    user_won = bool(outcome.winner) == user_is_white
    return "checkmate", "win" if user_won else "loss"


def _legal_move_or_none(board: chess.Board, uci: str | None) -> chess.Move | None:
    """Parse ``uci`` and return it only if legal in ``board``."""

    if not uci:
        return None
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    return move if move in board.legal_moves else None


def _rebuild_board(initial_fen: str, moves: list[str]) -> chess.Board:
    board = chess.Board(initial_fen)
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            break
        board.push(move)
    return board


def _decode_move_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(move) for move in data if isinstance(move, str)]


def _load_active_session(
    connection: sqlite3.Connection,
    playout_id: int,
    user_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM playout_sessions
        WHERE id = ? AND user_id = ? AND status = 'active'
        """,
        (playout_id, user_id),
    ).fetchone()
    if row is None:
        raise ValueError("Active playout not found")
    return row


def _clear_active_sessions(connection: sqlite3.Connection, user_id: int) -> None:
    stale = connection.execute(
        "SELECT id FROM playout_sessions WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()
    for row in stale:
        _archive_and_close(connection, int(row["id"]))


def _archive_and_close(connection: sqlite3.Connection, playout_id: int) -> dict[str, str]:
    session = connection.execute(
        "SELECT * FROM playout_sessions WHERE id = ?",
        (playout_id,),
    ).fetchone()
    if session is None:
        raise ValueError("Playout session not found")

    move_list = _decode_move_list(session["move_list"])
    # A session with no recorded result was abandoned mid-game (e.g. the user
    # started a new playout) — archive it as 'ended', not as a fake draw.
    result = str(session["result"] or "ended")
    user_color = str(session["user_color"] or "w")
    pgn = _build_pgn(str(session["initial_fen"]), move_list, result, user_color)
    engine = str(session["engine"] or "maia")

    connection.execute(
        """
        INSERT INTO playouts (user_id, position_id, maia_rating, result, pgn, engine)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(session["user_id"]),
            int(session["position_id"]),
            int(session["maia_rating"]),
            result,
            pgn,
            engine,
        ),
    )
    connection.execute(
        "DELETE FROM playout_sessions WHERE id = ?",
        (playout_id,),
    )
    return {"pgn": pgn, "result": result, "engine": engine}


def _build_pgn(initial_fen: str, moves: list[str], result: str, user_color: str = "w") -> str:
    board = chess.Board(initial_fen)
    game = chess.pgn.Game()
    game.setup(chess.Board(initial_fen))
    game.headers["Event"] = "Chess Trainer Playout"
    game.headers["Site"] = "Local"
    game.headers["Result"] = pgn_result_token(result, user_color)
    node = game
    for move_uci in moves:
        move = chess.Move.from_uci(move_uci)
        if move not in board.legal_moves:
            break
        board.push(move)
        node = node.add_variation(move)
    game.headers["FEN"] = initial_fen
    return str(game)


def pgn_result_token(result: str, user_color: str = "w") -> str:
    """PGN Result header: 'win'/'loss' are from the USER's perspective."""

    user_is_white = user_color != "b"
    if result == "win":
        return "1-0" if user_is_white else "0-1"
    if result == "loss":
        return "0-1" if user_is_white else "1-0"
    if result == "draw":
        return "1/2-1/2"
    return "*"  # abandoned / unfinished
