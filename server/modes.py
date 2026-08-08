"""Training-mode logic: eval-hold, defense gym, guessing, forced lines.

All four modes are built on two shared utilities:

- :func:`server.replies.engine_reply` — the one engine-reply service
  (best / top-N sample / Maia).
- :func:`server.evalcheck.check_eval_drop` — the one eval-delta checker.

Eval-hold and the defense gym share a single "hold session" state machine
(:func:`start_hold_session` / :func:`play_hold_move`); they differ only in
how a candidate position is picked and in the per-move fail criterion:

- ``evalhold``: fail when a single move drops more than ``threshold_cp``
  versus the engine's best move (the Puzzles-2.0 rule, looped over N moves).
- ``defense``: start from a worse position (~ -1 to -2) and fail when the
  eval collapses more than ``threshold_cp`` below the starting baseline.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import chess

from server.engine import MATE_SCORE_CP, analyze
from server.evalcheck import EvalCheck, check_eval_drop, position_eval_cp
from server.grading import uci_line_to_san
from server.replies import engine_reply

HoldMode = Literal["evalhold", "defense"]
HOLD_MODES: tuple[HoldMode, ...] = ("evalhold", "defense")

DEFAULT_HOLD_MOVES = 5
DEFAULT_HOLD_THRESHOLD_CP = 100
DEFAULT_DEFENSE_MOVES = 6
DEFAULT_DEFENSE_THRESHOLD_CP = 100

# Defense gym eval band (mover POV): "roughly -1 to -2".
DEFENSE_BAND_CP = (-260, -75)
DEFENSE_SCAN_LIMIT = 10
DEFENSE_EVAL_DEPTH = 12

FORCED_TOLERANCE_CP = 50
FORCED_OPPONENT_TOLERANCE_CP = 30
FORCED_DEPTH = 12

GUESS_EVAL_CLAMP_CP = 800

Analysis = dict[str, list[dict[str, Any]]]
AnalysisFn = Callable[..., Analysis]
ReplyFn = Callable[..., tuple[str | None, str]]


# --------------------------------------------------------------------------- #
# Position picking                                                            #
# --------------------------------------------------------------------------- #


def pick_random_position(
    connection: sqlite3.Connection,
    *,
    classification: str | None = None,
) -> sqlite3.Row | None:
    where = "WHERE classification = ?" if classification else ""
    params = (classification,) if classification else ()
    return connection.execute(
        f"SELECT * FROM positions {where} ORDER BY RANDOM() LIMIT 1",
        params,
    ).fetchone()


def pick_quietish_position(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """Quiet positions first; fall back to anything in the bank."""

    return (
        pick_random_position(connection, classification="quiet")
        or pick_random_position(connection)
    )


def pick_defense_position(
    connection: sqlite3.Connection,
    *,
    analyzer: AnalysisFn = analyze,
    scan_limit: int = DEFENSE_SCAN_LIMIT,
) -> tuple[sqlite3.Row | None, int | None]:
    """Find a bank position whose mover-POV eval sits in the defense band.

    Evals are computed on demand (depth :data:`DEFENSE_EVAL_DEPTH`) and cached
    in ``position_evals`` so repeat lookups are free. Returns
    ``(position, eval_cp)``; falls back to the most-negative known position,
    then to any position, so the mode always starts.
    """

    low, high = DEFENSE_BAND_CP

    cached = connection.execute(
        """
        SELECT p.*, pe.eval_cp AS cached_eval
        FROM positions p JOIN position_evals pe ON pe.position_id = p.id
        WHERE pe.eval_cp BETWEEN ? AND ?
        ORDER BY RANDOM() LIMIT 1
        """,
        (low, high),
    ).fetchone()
    if cached is not None:
        return cached, int(cached["cached_eval"])

    uncached = connection.execute(
        """
        SELECT p.* FROM positions p
        LEFT JOIN position_evals pe ON pe.position_id = p.id
        WHERE pe.position_id IS NULL
        ORDER BY RANDOM() LIMIT ?
        """,
        (scan_limit,),
    ).fetchall()
    for row in uncached:
        try:
            eval_cp = position_eval_cp(
                str(row["fen"]), depth=DEFENSE_EVAL_DEPTH, analyzer=analyzer
            )
        except RuntimeError:
            continue
        connection.execute(
            "INSERT OR REPLACE INTO position_evals (position_id, eval_cp, depth) VALUES (?, ?, ?)",
            (int(row["id"]), eval_cp, DEFENSE_EVAL_DEPTH),
        )
        connection.commit()
        if low <= eval_cp <= high:
            return row, eval_cp

    # No banked position in the band: take the worst (most negative) known one.
    fallback = connection.execute(
        """
        SELECT p.*, pe.eval_cp AS cached_eval
        FROM positions p JOIN position_evals pe ON pe.position_id = p.id
        ORDER BY pe.eval_cp ASC LIMIT 1
        """,
    ).fetchone()
    if fallback is not None:
        return fallback, int(fallback["cached_eval"])
    any_row = pick_random_position(connection)
    return any_row, None


def pick_forced_position(
    connection: sqlite3.Connection,
    *,
    min_plies: int = 3,
) -> sqlite3.Row | None:
    """Tactical puzzle with a solution of at least ``min_plies`` plies."""

    row = connection.execute(
        """
        SELECT * FROM positions
        WHERE classification = 'tactical' AND solution_moves IS NOT NULL
          AND (LENGTH(solution_moves) - LENGTH(REPLACE(solution_moves, ' ', ''))) >= ?
        ORDER BY RANDOM() LIMIT 1
        """,
        (min_plies - 1,),
    ).fetchone()
    if row is not None:
        return row
    return connection.execute(
        """
        SELECT * FROM positions
        WHERE classification = 'tactical' AND solution_moves IS NOT NULL
        ORDER BY RANDOM() LIMIT 1
        """,
    ).fetchone()


# --------------------------------------------------------------------------- #
# Hold sessions (eval-hold + defense gym)                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldStartResult:
    session_id: int
    mode: HoldMode
    position_id: int
    fen: str
    user_color: str
    target_moves: int
    threshold_cp: int
    baseline_eval_cp: int
    maia_rating: int
    streak: int


@dataclass(frozen=True)
class HoldMoveResult:
    status: str                      # active | passed | failed
    fen: str
    move_list: list[str]
    moves_survived: int
    target_moves: int
    threshold_cp: int
    baseline_eval_cp: int
    min_eval_cp: int
    drop_cp: float
    played_eval_cp: float
    best_eval_cp: float
    best_move_uci: str | None
    best_move_san: str | None
    reply_uci: str | None
    reply_san: str | None
    engine: str
    detail: str | None
    streak: int


def current_streak(connection: sqlite3.Connection, user_id: int, mode: str) -> int:
    rows = connection.execute(
        "SELECT passed FROM hold_results WHERE user_id = ? AND mode = ? ORDER BY id DESC LIMIT 200",
        (user_id, mode),
    ).fetchall()
    streak = 0
    for row in rows:
        if not int(row["passed"]):
            break
        streak += 1
    return streak


def best_streak(connection: sqlite3.Connection, user_id: int, mode: str) -> int:
    rows = connection.execute(
        "SELECT passed FROM hold_results WHERE user_id = ? AND mode = ? ORDER BY id ASC",
        (user_id, mode),
    ).fetchall()
    best = run = 0
    for row in rows:
        run = run + 1 if int(row["passed"]) else 0
        best = max(best, run)
    return best


def start_hold_session(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    mode: HoldMode,
    target_moves: int,
    threshold_cp: int,
    maia_rating: int,
    analyzer: AnalysisFn = analyze,
) -> HoldStartResult:
    if mode not in HOLD_MODES:
        raise ValueError(f"Unknown hold mode: {mode}")

    if mode == "defense":
        position, cached_eval = pick_defense_position(connection, analyzer=analyzer)
    else:
        position, cached_eval = pick_quietish_position(connection), None
    if position is None:
        raise ValueError("No positions available")

    fen = str(position["fen"])
    baseline = (
        cached_eval
        if cached_eval is not None
        else position_eval_cp(fen, analyzer=analyzer)
    )
    user_color = str(position["side_to_move"])

    _close_stale_hold_sessions(connection, user_id, mode)
    cursor = connection.execute(
        """
        INSERT INTO hold_sessions (
            user_id, mode, position_id, user_color, maia_rating, engine,
            fen, initial_fen, move_list, target_moves, threshold_cp,
            baseline_eval_cp, moves_survived, min_eval_cp, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            user_id,
            mode,
            int(position["id"]),
            user_color,
            maia_rating,
            "stockfish",
            fen,
            fen,
            "[]",
            target_moves,
            threshold_cp,
            int(baseline),
            0,
            int(baseline),
        ),
    )
    connection.commit()
    return HoldStartResult(
        session_id=int(cursor.lastrowid),
        mode=mode,
        position_id=int(position["id"]),
        fen=fen,
        user_color=user_color,
        target_moves=target_moves,
        threshold_cp=threshold_cp,
        baseline_eval_cp=int(baseline),
        maia_rating=maia_rating,
        streak=current_streak(connection, user_id, mode),
    )


def play_hold_move(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    user_id: int,
    move_uci: str,
    analyzer: AnalysisFn = analyze,
    reply_fn: ReplyFn = engine_reply,
) -> HoldMoveResult:
    session = connection.execute(
        "SELECT * FROM hold_sessions WHERE id = ? AND user_id = ? AND status = 'active'",
        (session_id, user_id),
    ).fetchone()
    if session is None:
        raise ValueError("Active session not found")

    mode = str(session["mode"])
    fen = str(session["fen"])
    board = chess.Board(fen)
    user_turn = chess.WHITE if str(session["user_color"]) == "w" else chess.BLACK
    if board.turn != user_turn:
        raise ValueError("It is not your turn in this session")

    threshold = int(session["threshold_cp"])
    baseline = int(session["baseline_eval_cp"])
    target = int(session["target_moves"])
    move_list = _decode_moves(session["move_list"])

    check = check_eval_drop(fen, move_uci, threshold_cp=threshold, analyzer=analyzer)
    best_move_san = _safe_san(board, check.best_move_uci)

    if mode == "defense":
        # Defense gym: fail only when the eval collapses below the baseline
        # floor; matching the (already bad) baseline is success.
        failed = check.played_eval_cp < baseline - threshold and not check.mate_delivered
        detail_fail = (
            f"Eval collapsed to {_fmt_cp(check.played_eval_cp)} "
            f"(floor was {_fmt_cp(baseline - threshold)})."
        )
    else:
        failed = not check.ok
        detail_fail = (
            f"That move dropped {abs(round(check.drop_cp))} cp "
            f"(ceiling {threshold} cp)."
        )

    board.push(chess.Move.from_uci(move_uci))
    move_list.append(move_uci)
    min_eval = min(int(session["min_eval_cp"]), int(check.played_eval_cp))

    if failed:
        return _finish_hold(
            connection, session, board, move_list, check,
            best_move_san=best_move_san, status="failed", detail=detail_fail,
            moves_survived=int(session["moves_survived"]), min_eval=min_eval,
            reply_uci=None, reply_san=None, engine=str(session["engine"]),
        )

    moves_survived = int(session["moves_survived"]) + 1

    if check.game_over:
        passed_detail = (
            "Checkmate — you finished it off." if check.mate_delivered
            else "Drawn — you held the position."
        )
        return _finish_hold(
            connection, session, board, move_list, check,
            best_move_san=best_move_san, status="passed", detail=passed_detail,
            moves_survived=moves_survived, min_eval=min_eval,
            reply_uci=None, reply_san=None, engine=str(session["engine"]),
        )

    if moves_survived >= target:
        return _finish_hold(
            connection, session, board, move_list, check,
            best_move_san=best_move_san, status="passed",
            detail=f"Survived all {target} moves.",
            moves_survived=moves_survived, min_eval=min_eval,
            reply_uci=None, reply_san=None, engine=str(session["engine"]),
        )

    reply_uci, engine_name = reply_fn(
        board.fen(), style="maia", maia_rating=int(session["maia_rating"])
    )
    reply_san = None
    if reply_uci is not None:
        try:
            reply_move = chess.Move.from_uci(reply_uci)
        except ValueError:
            reply_move = None
        if reply_move is None or reply_move not in board.legal_moves:
            reply_uci = None
        else:
            reply_san = _safe_san(board, reply_uci)
            board.push(reply_move)
            move_list.append(reply_uci)
    if reply_uci is None:
        # The game isn't over (checked above), so a missing/illegal engine
        # reply is an engine fault. Nothing has been committed for this move,
        # so the session stays at its previous position and the user can
        # simply retry — never persist a session stuck on the wrong turn.
        raise RuntimeError("Engine reply unavailable — please retry the move")

    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        if outcome.winner is not None and outcome.winner != user_turn:
            return _finish_hold(
                connection, session, board, move_list, check,
                best_move_san=best_move_san, status="failed",
                detail="Checkmated by the opponent.",
                moves_survived=moves_survived, min_eval=min_eval,
                reply_uci=reply_uci, reply_san=reply_san, engine=engine_name,
            )
        return _finish_hold(
            connection, session, board, move_list, check,
            best_move_san=best_move_san, status="passed",
            detail="Game over — position held.",
            moves_survived=moves_survived, min_eval=min_eval,
            reply_uci=reply_uci, reply_san=reply_san, engine=engine_name,
        )

    connection.execute(
        """
        UPDATE hold_sessions
        SET fen = ?, move_list = ?, moves_survived = ?, min_eval_cp = ?,
            engine = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (board.fen(), json.dumps(move_list), moves_survived, min_eval, engine_name, session_id),
    )
    connection.commit()
    return HoldMoveResult(
        status="active",
        fen=board.fen(),
        move_list=move_list,
        moves_survived=moves_survived,
        target_moves=target,
        threshold_cp=threshold,
        baseline_eval_cp=baseline,
        min_eval_cp=min_eval,
        drop_cp=check.drop_cp,
        played_eval_cp=check.played_eval_cp,
        best_eval_cp=check.best_eval_cp,
        best_move_uci=check.best_move_uci,
        best_move_san=best_move_san,
        reply_uci=reply_uci,
        reply_san=reply_san,
        engine=engine_name,
        detail=None,
        streak=current_streak(connection, user_id, mode),
    )


def end_hold_session(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    user_id: int,
) -> dict[str, Any]:
    """Abandon an active session. Counts as a fail (streak resets)."""

    session = connection.execute(
        "SELECT * FROM hold_sessions WHERE id = ? AND user_id = ? AND status = 'active'",
        (session_id, user_id),
    ).fetchone()
    if session is None:
        raise ValueError("Active session not found")
    _record_hold_result(connection, session, passed=False, detail="Abandoned.",
                        moves_survived=int(session["moves_survived"]))
    connection.execute("DELETE FROM hold_sessions WHERE id = ?", (session_id,))
    connection.commit()
    mode = str(session["mode"])
    return {
        "status": "failed",
        "detail": "Abandoned.",
        "streak": current_streak(connection, user_id, mode),
    }


def hold_summary(connection: sqlite3.Connection, user_id: int, mode: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(passed), 0) AS passed
        FROM hold_results WHERE user_id = ? AND mode = ?
        """,
        (user_id, mode),
    ).fetchone()
    return {
        "total": int(row["total"]),
        "passed": int(row["passed"]),
        "streak": current_streak(connection, user_id, mode),
        "best_streak": best_streak(connection, user_id, mode),
    }


def _finish_hold(
    connection: sqlite3.Connection,
    session: sqlite3.Row,
    board: chess.Board,
    move_list: list[str],
    check: EvalCheck,
    *,
    best_move_san: str | None,
    status: str,
    detail: str,
    moves_survived: int,
    min_eval: int,
    reply_uci: str | None,
    reply_san: str | None,
    engine: str,
) -> HoldMoveResult:
    user_id = int(session["user_id"])
    mode = str(session["mode"])
    _record_hold_result(
        connection, session, passed=status == "passed", detail=detail,
        moves_survived=moves_survived,
    )
    connection.execute("DELETE FROM hold_sessions WHERE id = ?", (int(session["id"]),))
    connection.commit()
    return HoldMoveResult(
        status=status,
        fen=board.fen(),
        move_list=move_list,
        moves_survived=moves_survived,
        target_moves=int(session["target_moves"]),
        threshold_cp=int(session["threshold_cp"]),
        baseline_eval_cp=int(session["baseline_eval_cp"]),
        min_eval_cp=min_eval,
        drop_cp=check.drop_cp,
        played_eval_cp=check.played_eval_cp,
        best_eval_cp=check.best_eval_cp,
        best_move_uci=check.best_move_uci,
        best_move_san=best_move_san,
        reply_uci=reply_uci,
        reply_san=reply_san,
        engine=engine,
        detail=detail,
        streak=current_streak(connection, user_id, mode),
    )


def _record_hold_result(
    connection: sqlite3.Connection,
    session: sqlite3.Row,
    *,
    passed: bool,
    detail: str,
    moves_survived: int,
) -> None:
    connection.execute(
        """
        INSERT INTO hold_results (
            user_id, mode, position_id, target_moves, threshold_cp,
            moves_survived, passed, detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(session["user_id"]),
            str(session["mode"]),
            int(session["position_id"]),
            int(session["target_moves"]),
            int(session["threshold_cp"]),
            moves_survived,
            1 if passed else 0,
            detail,
        ),
    )


def _close_stale_hold_sessions(
    connection: sqlite3.Connection, user_id: int, mode: str
) -> None:
    stale = connection.execute(
        "SELECT * FROM hold_sessions WHERE user_id = ? AND mode = ? AND status = 'active'",
        (user_id, mode),
    ).fetchall()
    for session in stale:
        _record_hold_result(
            connection, session, passed=False, detail="Abandoned.",
            moves_survived=int(session["moves_survived"]),
        )
        connection.execute("DELETE FROM hold_sessions WHERE id = ?", (int(session["id"]),))


# --------------------------------------------------------------------------- #
# Forced-depth puzzles                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ForcedVerdict:
    ply: int
    user_uci: str | None
    user_san: str | None
    expected_uci: str | None
    expected_san: str | None
    verdict: str  # match | acceptable | wrong | missing | not_reached
    note: str | None = None


@dataclass(frozen=True)
class ForcedOutcome:
    passed: bool
    matched_plies: int
    total_plies: int
    verdicts: list[ForcedVerdict] = field(default_factory=list)
    solution_san: str = ""
    solution_uci: list[str] = field(default_factory=list)


def validate_forced_line(
    fen: str,
    user_line: list[str],
    solution_line: list[str],
    *,
    analyzer: AnalysisFn = analyze,
    tolerance_cp: int = FORCED_TOLERANCE_CP,
    opponent_tolerance_cp: int = FORCED_OPPONENT_TOLERANCE_CP,
    depth: int = FORCED_DEPTH,
) -> ForcedOutcome:
    """Validate a full pre-entered line against the engine's principal play.

    Walks the user's line ply by ply. At each ply the user's move passes if
    it matches the stored solution / engine best move, or if its eval stays
    within ``tolerance_cp`` of best (``opponent_tolerance_cp`` for opponent
    replies, which must be near-best to count as "the opponent's best").
    Validation stops at the first wrong ply.
    """

    start_board = chess.Board(fen)
    solution_san = uci_line_to_san(start_board, solution_line)
    total = len(solution_line)
    board = start_board.copy()
    verdicts: list[ForcedVerdict] = []
    matched = 0
    failed = False

    for ply in range(total):
        expected_uci: str | None = solution_line[ply] if ply < len(solution_line) else None
        expected_san = _safe_san(board, expected_uci)
        user_uci = user_line[ply] if ply < len(user_line) else None

        if failed:
            verdicts.append(ForcedVerdict(
                ply=ply, user_uci=user_uci, user_san=None,
                expected_uci=expected_uci, expected_san=expected_san,
                verdict="not_reached",
            ))
            continue

        if user_uci is None:
            failed = True
            verdicts.append(ForcedVerdict(
                ply=ply, user_uci=None, user_san=None,
                expected_uci=expected_uci, expected_san=expected_san,
                verdict="missing", note="Line ended early.",
            ))
            continue

        try:
            user_move = chess.Move.from_uci(user_uci)
        except ValueError:
            user_move = None
        if user_move is None or user_move not in board.legal_moves:
            failed = True
            verdicts.append(ForcedVerdict(
                ply=ply, user_uci=user_uci, user_san=None,
                expected_uci=expected_uci, expected_san=expected_san,
                verdict="wrong", note="Illegal move at this point in the line.",
            ))
            continue

        user_san = board.san(user_move)

        if expected_uci is not None and user_uci == expected_uci:
            verdicts.append(ForcedVerdict(
                ply=ply, user_uci=user_uci, user_san=user_san,
                expected_uci=expected_uci, expected_san=expected_san,
                verdict="match",
            ))
            matched += 1
            board.push(user_move)
            continue

        # Deviation: ask the engine whether the move is close enough to best.
        is_user_ply = ply % 2 == 0
        allowed = tolerance_cp if is_user_ply else opponent_tolerance_cp
        try:
            check = check_eval_drop(
                board.fen(), user_uci, threshold_cp=allowed,
                depth=depth, analyzer=analyzer,
            )
            within = check.drop_cp <= allowed
        except (RuntimeError, ValueError):
            within = False

        if within:
            verdicts.append(ForcedVerdict(
                ply=ply, user_uci=user_uci, user_san=user_san,
                expected_uci=expected_uci, expected_san=expected_san,
                verdict="acceptable",
                note="Not the main line, but the eval holds.",
            ))
            matched += 1
            board.push(user_move)
            continue

        failed = True
        verdicts.append(ForcedVerdict(
            ply=ply, user_uci=user_uci, user_san=user_san,
            expected_uci=expected_uci, expected_san=expected_san,
            verdict="wrong",
            note=(
                f"Expected {expected_san}." if expected_san
                else "This move loses the thread."
            ),
        ))

    return ForcedOutcome(
        passed=not failed and matched == total,
        matched_plies=matched,
        total_plies=total,
        verdicts=verdicts,
        solution_san=solution_san,
        solution_uci=list(solution_line),
    )


def record_forced_attempt(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    position_id: int,
    user_line: list[str],
    outcome: ForcedOutcome,
) -> None:
    connection.execute(
        """
        INSERT INTO forced_attempts (
            user_id, position_id, user_line, expected_line,
            matched_plies, total_plies, passed
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            position_id,
            " ".join(user_line),
            " ".join(outcome.solution_uci),
            outcome.matched_plies,
            outcome.total_plies,
            1 if outcome.passed else 0,
        ),
    )
    connection.commit()


def forced_summary(connection: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(passed), 0) AS passed
        FROM forced_attempts WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    rows = connection.execute(
        "SELECT passed FROM forced_attempts WHERE user_id = ? ORDER BY id DESC LIMIT 200",
        (user_id,),
    ).fetchall()
    streak = 0
    for r in rows:
        if not int(r["passed"]):
            break
        streak += 1
    return {"total": int(row["total"]), "passed": int(row["passed"]), "streak": streak}


# --------------------------------------------------------------------------- #
# Eval + sharpness guessing                                                   #
# --------------------------------------------------------------------------- #


def default_guess_actuals(fen: str) -> dict[str, Any]:
    """Compute the actual eval (white POV) + sharpness for ``fen``.

    Reuses the volatility engine wholesale: one ``compute_volatility`` call
    yields both the best-move eval and the 0-100 sharpness score.
    """

    from chess_vol.server import ENGINE_FACTORY
    from chess_vol.volatility import compute_volatility

    board = chess.Board(fen)
    with ENGINE_FACTORY() as engine:
        result = compute_volatility(board, engine, depth=14, multipv=6)

    mover_eval = int(result.best_eval_cp)
    white_eval = mover_eval if board.turn == chess.WHITE else -mover_eval
    sharpness = float(result.score) if result.score is not None else 0.0
    return {
        "actual_eval_cp": white_eval,
        "actual_sharpness": round(sharpness, 1),
        "decided": bool(result.decided),
        "reason": result.reason,
    }


def score_guess(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    position_id: int | None,
    fen: str,
    guessed_eval_cp: int,
    guessed_sharpness: float,
    actuals_fn: Callable[[str], dict[str, Any]] = default_guess_actuals,
) -> dict[str, Any]:
    actuals = actuals_fn(fen)
    actual_eval = int(actuals["actual_eval_cp"])
    actual_sharp = float(actuals["actual_sharpness"])

    clamped_actual = _clamp(actual_eval, -GUESS_EVAL_CLAMP_CP, GUESS_EVAL_CLAMP_CP)
    clamped_guess = _clamp(guessed_eval_cp, -GUESS_EVAL_CLAMP_CP, GUESS_EVAL_CLAMP_CP)
    eval_error = abs(clamped_guess - clamped_actual)
    sharp_error = abs(guessed_sharpness - actual_sharp)

    connection.execute(
        """
        INSERT INTO guess_attempts (
            user_id, position_id, fen, guessed_eval_cp, actual_eval_cp,
            guessed_sharpness, actual_sharpness
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, position_id, fen, int(guessed_eval_cp), actual_eval,
            float(guessed_sharpness), actual_sharp,
        ),
    )
    connection.commit()

    return {
        "actual_eval_cp": actual_eval,
        "actual_sharpness": actual_sharp,
        "guessed_eval_cp": int(guessed_eval_cp),
        "guessed_sharpness": float(guessed_sharpness),
        "eval_error_cp": eval_error,
        "sharpness_error": round(sharp_error, 1),
        "decided": actuals.get("decided", False),
        "reason": actuals.get("reason"),
    }


def guess_history(
    connection: sqlite3.Connection, user_id: int, limit: int = 200
) -> list[dict[str, Any]]:
    # Newest N attempts, returned oldest-first for charting. A plain ASC LIMIT
    # would forever return the first N attempts ever made.
    rows = connection.execute(
        """
        SELECT * FROM (
            SELECT id, guessed_eval_cp, actual_eval_cp, guessed_sharpness,
                   actual_sharpness, timestamp
            FROM guess_attempts WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
        """,
        (user_id, limit),
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "guessed_eval_cp": int(r["guessed_eval_cp"]),
            "actual_eval_cp": int(r["actual_eval_cp"]),
            "guessed_sharpness": float(r["guessed_sharpness"]),
            "actual_sharpness": float(r["actual_sharpness"]),
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #


def _decode_moves(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(m) for m in data if isinstance(m, str)] if isinstance(data, list) else []


def _safe_san(board: chess.Board, uci: str | None) -> str | None:
    if not uci:
        return None
    try:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            return None
        return board.san(move)
    except ValueError:
        return None


def _fmt_cp(cp: float) -> str:
    if abs(cp) >= MATE_SCORE_CP / 2:
        return "mate"
    pawns = cp / 100.0
    return f"{'+' if pawns >= 0 else ''}{pawns:.2f}"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
