"""Detection core for "Your Mistakes" — personalized puzzles from real games.

Given a parsed game and the user's color, walk the user's moves and yield the
positions where they **missed a win/tactic** (Bucket A) or **blundered**
(Bucket B). Pure and dependency-injected: the caller supplies the Stockfish
analysis function, the Maia top-K "findability" function, and (optionally) a
volatility tagger, so this module never talks to an engine directly and is
unit-testable without one.

Eval convention (mirrors :mod:`server.evalcheck`): all centipawns are from the
**mover's POV**, and the mover at a candidate node is the user — so every eval
here is already **user-POV** (positive = good for the user). Mate folds to
``±MATE_SCORE_CP`` (from :mod:`server.engine`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import chess
import chess.pgn

from server.engine import MATE_SCORE_CP

# --------------------------------------------------------------------------- #
# Config defaults (overridable per call)                                       #
# --------------------------------------------------------------------------- #

LOOKBACK_DAYS = 90
PASS1_DEPTH = 12          # shallow scan over every user move
PASS2_DEPTH = 20          # deep verification of candidates
PASS2_MULTIPV = 4
DECISIVE_CP = 200         # |eval| above this = decided outcome category
TACTIC_GAP_CP = 150       # best vs 2nd-best gap that marks an "only move"
CLOCK_FLOOR_S = 30        # drop moves played with < this many seconds left
MAIA_NET = 1900
MAIA_TOPK = 3

BUCKET_MISSED_WIN = "missed_win"
BUCKET_BLUNDER = "blunder"

Analysis = dict[str, list[dict[str, Any]]]
AnalysisFn = Callable[..., Analysis]
MaiaTopKFn = Callable[[str], list[str] | None]
VolatilityFn = Callable[[str], float | None]

_CLK_RE = re.compile(r"\[%clk\s+([0-9:.]+)\]")


# --------------------------------------------------------------------------- #
# Small pure helpers                                                           #
# --------------------------------------------------------------------------- #


def parse_clk_seconds(comment: str | None) -> float | None:
    """Parse the ``[%clk H:MM:SS(.f)]`` annotation into absolute seconds.

    Returns ``None`` when no clock annotation is present.
    """

    if not comment:
        return None
    match = _CLK_RE.search(comment)
    if match is None:
        return None
    parts = match.group(1).split(":")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for value in values:                # H:MM:SS, MM:SS, or SS
        seconds = seconds * 60 + value
    return seconds


def outcome_category(eval_cp: int, *, decisive_cp: int = DECISIVE_CP) -> int:
    """Map a user-POV eval to an ordinal outcome: 0 losing, 1 equal, 2 winning."""

    if eval_cp > decisive_cp:
        return 2
    if eval_cp < -decisive_cp:
        return 0
    return 1


def build_caption(
    bucket: str,
    *,
    opponent: str | None,
    date: str | None,
    actual_san: str,
    solution_san: str,
) -> str:
    verb = "win" if bucket == BUCKET_MISSED_WIN else "save"
    opp = opponent or "your opponent"
    when = f" on {date}" if date else ""
    return (
        f"In your game vs {opp}{when}, you played {actual_san}. "
        f"The {verb} was {solution_san}."
    )


# --------------------------------------------------------------------------- #
# Puzzle record                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class MistakePuzzle:
    bucket: str
    fen: str
    side_to_move: str
    user_color: str
    best_move: str                 # UCI
    solution_moves: str | None     # space-joined UCI PV
    user_actual_move: str          # UCI
    eval_before_cp: int            # user-POV, position value with best play
    eval_best_cp: int              # user-POV, after the solution move
    eval_played_cp: int            # user-POV, after the move actually played
    second_best_gap_cp: int | None
    volatility: float | None
    maia_solution_rank: int | None
    maia_best_in_top3: int | None
    clock_seconds: float | None
    ply_number: int
    game_url: str | None
    game_id: str | None
    game_date: str | None
    time_class: str | None
    opponent: str | None
    caption: str


# --------------------------------------------------------------------------- #
# Detection                                                                    #
# --------------------------------------------------------------------------- #


def _eval_after_move(
    board: chess.Board,
    move: chess.Move,
    top_moves: list[dict[str, Any]],
    *,
    analysis_fn: AnalysisFn,
    depth: int,
    user_color: chess.Color,
) -> int:
    """User-POV eval of the position *after* ``move`` (same math as evalcheck)."""

    uci = move.uci()
    for entry in top_moves:                 # already analysed at this node?
        if str(entry["move"]) == uci:
            return int(entry["eval"])

    after = board.copy(stack=False)
    after.push(move)
    outcome = after.outcome(claim_draw=True)
    if outcome is not None:
        if outcome.winner is None:
            return 0
        return MATE_SCORE_CP if outcome.winner == user_color else -MATE_SCORE_CP

    res = analysis_fn(after.fen(), depth=depth, multipv=1).get("top_moves", [])
    if not res:
        raise RuntimeError("Engine returned no analysis for resulting position")
    return -int(res[0]["eval"])             # flip opponent POV back to the user


def detect_mistakes(
    game: chess.pgn.Game,
    user_color: chess.Color,
    *,
    analysis_fn: AnalysisFn,
    maia_topk_fn: MaiaTopKFn | None = None,
    volatility_fn: VolatilityFn | None = None,
    meta: dict[str, Any] | None = None,
    pass1_depth: int = PASS1_DEPTH,
    pass2_depth: int = PASS2_DEPTH,
    multipv: int = PASS2_MULTIPV,
    decisive_cp: int = DECISIVE_CP,
    gap_cp: int = TACTIC_GAP_CP,
    clock_floor_s: int = CLOCK_FLOOR_S,
    strict_maia: bool = False,
) -> Iterator[MistakePuzzle]:
    """Yield :class:`MistakePuzzle` records for the user's mistakes in ``game``.

    Two passes for compute sanity: a shallow scan (``pass1_depth``) cheaply
    rejects nodes where the user already played the best move; only survivors
    get the deep MultiPV verification (``pass2_depth``). The Maia findability
    gate runs last (and only on positions that already passed), so the costly
    lc0 call is rare.
    """

    meta = meta or {}
    board = game.board()

    for node in game.mainline():
        move = node.move
        if move is None:
            break

        if board.turn != user_color:
            board.push(move)
            continue

        fen = board.fen()
        user_uci = move.uci()
        clock_seconds = parse_clk_seconds(node.comment)
        ply_number = board.ply()

        # Filter 2: clock floor. Scramble errors aren't instructive.
        if clock_seconds is not None and clock_seconds < clock_floor_s:
            board.push(move)
            continue

        # Pass 1 (shallow): if the user already matched the best move, skip.
        shallow = analysis_fn(fen, depth=pass1_depth, multipv=2).get("top_moves", [])
        if not shallow or str(shallow[0]["move"]) == user_uci:
            board.push(move)
            continue

        # Pass 2 (deep verify).
        top = analysis_fn(fen, depth=pass2_depth, multipv=multipv).get("top_moves", [])
        if not top:
            board.push(move)
            continue
        best_uci = str(top[0]["move"])
        eval_before = int(top[0]["eval"])
        if best_uci == user_uci:            # deep search agrees the user was best
            board.push(move)
            continue

        gap = (eval_before - int(top[1]["eval"])) if len(top) >= 2 else None
        eval_played = _eval_after_move(
            board, move, top,
            analysis_fn=analysis_fn, depth=pass2_depth, user_color=user_color,
        )

        # Bucket classification (A takes priority over B).
        bucket: str | None = None
        if gap is not None and gap >= gap_cp and eval_before >= decisive_cp:
            bucket = BUCKET_MISSED_WIN
        elif outcome_category(eval_before, decisive_cp=decisive_cp) > outcome_category(
            eval_played, decisive_cp=decisive_cp
        ):
            bucket = BUCKET_BLUNDER
        if bucket is None:
            board.push(move)
            continue

        # Filter 3: Maia findability gate on the solution (best) move.
        maia_rank: int | None = None
        maia_in_top3: int | None = None
        if maia_topk_fn is not None:
            topk = maia_topk_fn(fen)
            if topk is None:
                if strict_maia:             # can't verify findability -> drop
                    board.push(move)
                    continue
            elif best_uci in topk:
                maia_in_top3 = 1
                maia_rank = topk.index(best_uci) + 1
            else:
                board.push(move)            # solution not humanly findable -> drop
                continue

        # User-POV eval after the solution move (the docstring contract for
        # eval_best_cp). Resolved through the same helper as eval_played so
        # both numbers fold terminal positions identically.
        eval_best = _eval_after_move(
            board, chess.Move.from_uci(best_uci), top,
            analysis_fn=analysis_fn, depth=pass2_depth, user_color=user_color,
        )

        pv = [str(m) for m in top[0].get("pv", [])] or [best_uci]
        actual_san = board.san(move)
        solution_san = board.san(chess.Move.from_uci(best_uci))
        opponent = meta.get("opponent")
        date = meta.get("date")

        yield MistakePuzzle(
            bucket=bucket,
            fen=fen,
            side_to_move="w" if board.turn == chess.WHITE else "b",
            user_color="w" if user_color == chess.WHITE else "b",
            best_move=best_uci,
            solution_moves=" ".join(pv),
            user_actual_move=user_uci,
            eval_before_cp=eval_before,
            eval_best_cp=eval_best,
            eval_played_cp=eval_played,
            second_best_gap_cp=gap,
            volatility=volatility_fn(fen) if volatility_fn is not None else None,
            maia_solution_rank=maia_rank,
            maia_best_in_top3=maia_in_top3,
            clock_seconds=clock_seconds,
            ply_number=ply_number,
            game_url=meta.get("url"),
            game_id=meta.get("game_id"),
            game_date=date,
            time_class=meta.get("time_class"),
            opponent=opponent,
            caption=build_caption(
                bucket, opponent=opponent, date=date,
                actual_san=actual_san, solution_san=solution_san,
            ),
        )

        board.push(move)
