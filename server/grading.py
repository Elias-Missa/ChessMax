"""Attempt grading and rating updates.

Tactical puzzles (`source = 'lichess_puzzle'`) are graded by **step-driven
exact solution match**. Each Lichess puzzle's `solution_moves` is a UCI
sequence alternating user-move / opponent-forced-response. The client tracks
`step` and submits one user move at a time; the server validates against
`solution_moves[step]` and either:

- returns ``status="continue"`` with the opponent's auto-reply if there are
  more user moves to play, or
- returns ``status="solved"`` (last user move matched, puzzle complete), or
- returns ``status="failed"`` (wrong move).

Stockfish is never invoked for tactical attempts.

Quiet positions use one-shot Stockfish grading: ``status="graded"``,
``multipv=3`` so the response includes the top three candidate lines + evals.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import chess

from server.engine import analyze, grade_from_eval_loss


K_USER = 20
K_POSITION = 10
TACTICAL_MISS_EVAL_LOSS = 999.0
QUIET_MULTIPV = 3
REFUTATION_DEPTH = 12
QUIET_NOT_SOLVED_THRESHOLD = 100

Status = Literal["continue", "solved", "failed", "graded"]
TERMINAL_STATUSES: tuple[Status, ...] = ("solved", "failed", "graded")

Analysis = dict[str, list[dict[str, Any]]]
AnalysisFn = Callable[..., Analysis]


@dataclass(frozen=True)
class AttemptOutcome:
    status: Status
    eval_loss: float
    grade: str
    best_move: str | None              # SAN, for display (terminal statuses only)
    best_move_uci: str | None          # UCI, for DB persistence on terminal
    best_eval: float
    solution_line: str | None          # SAN, for display
    actual_score: int                  # 1/0 — only meaningful on terminal
    can_play_out: bool
    solved: bool
    opponent_move_uci: str | None      # set when there's an auto-reply (continue or solved)
    opponent_move_san: str | None
    next_step: int | None              # set on continue
    top_lines: list[dict[str, Any]] | None  # quiet (graded) only
    refutation_uci: str | None         # engine's best response to a bad user move
    refutation_san: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def evaluate_attempt(
    position: dict[str, Any],
    user_move: str,
    analyzer: AnalysisFn = analyze,
    step: int = 0,
) -> AttemptOutcome:
    """Grade an attempt. Tactical → solution match (no engine). Quiet → Stockfish."""

    start_board = chess.Board(str(position["fen"]))

    if position.get("classification") == "tactical":
        solution_uci = (position.get("solution_moves") or "").split()
        if not solution_uci:
            raise RuntimeError("Tactical position has no solution_moves")
        if step < 0 or step >= len(solution_uci):
            raise ValueError(f"step {step} out of range for solution length {len(solution_uci)}")

        # Replay solution prefix to get to the position where the user is moving.
        board = start_board.copy()
        for i in range(step):
            board.push(chess.Move.from_uci(solution_uci[i]))

        move = chess.Move.from_uci(user_move)
        if move not in board.legal_moves:
            raise ValueError(f"Illegal move for position: {user_move}")

        return grade_tactical(board, move, position, step, start_board, solution_uci, analyzer)

    move = chess.Move.from_uci(user_move)
    if move not in start_board.legal_moves:
        raise ValueError(f"Illegal move for position: {user_move}")
    return grade_quiet(start_board, move, position, analyzer)


def grade_tactical(
    board: chess.Board,
    user_move: chess.Move,
    position: dict[str, Any],
    step: int,
    start_board: chess.Board,
    solution_uci: list[str],
    analyzer: AnalysisFn,
) -> AttemptOutcome:
    expected = chess.Move.from_uci(solution_uci[step])
    expected_san = board.san(expected)
    best_eval = float(position.get("best_eval") or 0.0)
    full_solution_san = uci_line_to_san(start_board, solution_uci)

    # Wrong move — fail immediately. Ask the engine what it would play in
    # response so the UI can show "if you played that, the opponent plays X".
    if user_move != expected:
        after_user = board.copy()
        after_user.push(user_move)
        refutation_uci, refutation_san = engine_refutation(after_user, analyzer)
        return AttemptOutcome(
            status="failed",
            eval_loss=TACTICAL_MISS_EVAL_LOSS,
            grade="blunder",
            best_move=expected_san,
            best_move_uci=solution_uci[step],
            best_eval=best_eval,
            solution_line=full_solution_san,
            actual_score=0,
            can_play_out=not after_user.is_checkmate(),
            solved=False,
            opponent_move_uci=None,
            opponent_move_san=None,
            next_step=None,
            top_lines=None,
            refutation_uci=refutation_uci,
            refutation_san=refutation_san,
        )

    # Match — push user move, then auto-reply if there's an opponent response.
    after_user = board.copy()
    after_user.push(user_move)

    has_opp_reply = step + 1 < len(solution_uci)
    if has_opp_reply:
        opp_uci = solution_uci[step + 1]
        opp_move = chess.Move.from_uci(opp_uci)
        opp_san = after_user.san(opp_move)
        after_opp = after_user.copy()
        after_opp.push(opp_move)
        next_step = step + 2
    else:
        opp_uci = None
        opp_san = None
        after_opp = after_user
        next_step = step + 1

    is_complete = next_step >= len(solution_uci)

    if is_complete:
        return AttemptOutcome(
            status="solved",
            eval_loss=0.0,
            grade="best",
            best_move=expected_san,
            best_move_uci=solution_uci[step],
            best_eval=best_eval,
            solution_line=full_solution_san,
            actual_score=1,
            can_play_out=not after_opp.is_checkmate(),
            solved=True,
            opponent_move_uci=opp_uci,
            opponent_move_san=opp_san,
            next_step=None,
            top_lines=None,
            refutation_uci=None,
            refutation_san=None,
        )

    return AttemptOutcome(
        status="continue",
        eval_loss=0.0,
        grade="best",
        best_move=None,
        best_move_uci=None,
        best_eval=best_eval,
        solution_line=None,
        actual_score=0,
        can_play_out=False,
        solved=False,
        opponent_move_uci=opp_uci,
        opponent_move_san=opp_san,
        next_step=next_step,
        top_lines=None,
        refutation_uci=None,
        refutation_san=None,
    )


def grade_quiet(
    board: chess.Board,
    user_move: chess.Move,
    position: dict[str, Any],
    analyzer: AnalysisFn,
) -> AttemptOutcome:
    original_analysis = analyzer(board.fen(), depth=18, multipv=QUIET_MULTIPV)
    top_moves = original_analysis.get("top_moves", [])
    if not top_moves:
        raise RuntimeError("Engine returned no analysis for original position")

    original_for_san = board.copy()
    top_lines: list[dict[str, Any]] = []
    for tm in top_moves:
        move_uci = str(tm["move"])
        try:
            pv_uci_raw = tm.get("pv", [move_uci])
        except AttributeError:
            pv_uci_raw = [move_uci]
        pv_uci = [str(m) for m in pv_uci_raw] if pv_uci_raw else [move_uci]
        top_lines.append({
            "move_san": original_for_san.san(chess.Move.from_uci(move_uci)),
            "eval_cp": int(tm["eval"]),
            "pv_san": uci_line_to_san(original_for_san, pv_uci),
        })

    best = top_moves[0]
    best_eval = float(best["eval"])
    best_move_uci = str(best["move"])
    best_move_san = top_lines[0]["move_san"]

    board.push(user_move)
    resulting_analysis = analyzer(board.fen(), depth=18, multipv=1)
    if not resulting_analysis["top_moves"]:
        raise RuntimeError("Engine returned no analysis for resulting position")

    opponent_eval_after = float(resulting_analysis["top_moves"][0]["eval"])
    user_eval_after = -opponent_eval_after
    eval_loss = best_eval - user_eval_after
    grade = grade_from_eval_loss(eval_loss)
    solved = eval_loss <= QUIET_NOT_SOLVED_THRESHOLD

    solution_san = None
    if position.get("solution_moves"):
        solution_san = uci_line_to_san(
            chess.Board(str(position["fen"])),
            position["solution_moves"].split(),
        )

    refutation_uci: str | None = None
    refutation_san: str | None = None
    if not solved and not board.is_game_over():
        refutation_uci = str(resulting_analysis["top_moves"][0]["move"])
        try:
            refutation_san = board.san(chess.Move.from_uci(refutation_uci))
        except (ValueError, AssertionError):
            refutation_uci = None

    return AttemptOutcome(
        status="graded",
        eval_loss=eval_loss,
        grade=grade,
        best_move=best_move_san,
        best_move_uci=best_move_uci,
        best_eval=best_eval,
        solution_line=solution_san,
        actual_score=1 if solved else 0,
        can_play_out=not board.is_checkmate(),
        solved=solved,
        opponent_move_uci=None,
        opponent_move_san=None,
        next_step=None,
        top_lines=top_lines,
        refutation_uci=refutation_uci,
        refutation_san=refutation_san,
    )


def engine_refutation(
    board: chess.Board,
    analyzer: AnalysisFn,
) -> tuple[str | None, str | None]:
    """Ask the engine what it would play on ``board``. Returns (uci, san) or (None, None)."""

    if board.is_game_over():
        return None, None
    try:
        analysis = analyzer(board.fen(), depth=REFUTATION_DEPTH, multipv=1)
    except Exception:
        return None, None
    top_moves = analysis.get("top_moves", [])
    if not top_moves:
        return None, None
    uci = str(top_moves[0]["move"])
    try:
        san = board.san(chess.Move.from_uci(uci))
    except (ValueError, AssertionError):
        return None, None
    return uci, san


def uci_line_to_san(board: chess.Board, uci_moves: list[str]) -> str:
    """Convert a UCI move sequence to a SAN string, playing on a board copy."""

    working = board.copy()
    sans: list[str] = []
    for uci in uci_moves:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in working.legal_moves:
            break
        sans.append(working.san(move))
        working.push(move)
    return " ".join(sans)


def rating_update(
    user_rating: int,
    position_rating: int,
    actual_score: int,
) -> tuple[int, int]:
    expected = 1 / (1 + math.pow(10, (position_rating - user_rating) / 400))
    user_after = round(user_rating + K_USER * (actual_score - expected))
    position_after = round(position_rating + K_POSITION * (expected - actual_score))
    return user_after, position_after
