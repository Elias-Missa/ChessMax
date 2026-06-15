"""Shared engine-reply service for all training modes.

Every mode that needs an opponent move (playout, eval-hold, defense gym, …)
calls :func:`engine_reply` instead of talking to engines directly.

Styles:

- ``"best"``: Stockfish's best move — deterministic, maximally punishing.
- ``"topn"``: sample one of Stockfish's top-N moves that stay within an eval
  window of the best move, weighted toward better moves. Feels human-ish
  without Maia.
- ``"maia"``: Maia (lc0) when assets are available; otherwise falls back to
  ``"topn"`` so replies still feel natural rather than engine-perfect.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Literal

import chess

from server.engine import analyze
from server.maia import has_maia_assets, maia_move

ReplyStyle = Literal["best", "topn", "maia"]

DEFAULT_REPLY_DEPTH = 12
DEFAULT_TOP_N = 3
DEFAULT_EVAL_WINDOW_CP = 90
SOFTMAX_TEMPERATURE_CP = 60.0

Analysis = dict[str, list[dict[str, Any]]]
AnalysisFn = Callable[..., Analysis]


def engine_reply(
    fen: str,
    *,
    style: ReplyStyle = "maia",
    maia_rating: int = 1500,
    top_n: int = DEFAULT_TOP_N,
    depth: int = DEFAULT_REPLY_DEPTH,
    eval_window_cp: int = DEFAULT_EVAL_WINDOW_CP,
    analyzer: AnalysisFn = analyze,
    rng: random.Random | None = None,
) -> tuple[str | None, str]:
    """Return ``(uci_move, engine_label)`` for the side to move in ``fen``.

    Returns ``(None, label)`` when the game is already over or the engine
    produces nothing.
    """

    board = chess.Board(fen)
    if board.is_game_over():
        return None, "none"

    if style == "maia":
        if has_maia_assets(maia_rating):
            move = maia_move(fen, maia_rating)
            if move is not None:
                return move, "maia"
        style = "topn"

    if style == "best":
        top_moves = analyzer(fen, depth=depth, multipv=1).get("top_moves", [])
        if not top_moves:
            return None, "stockfish"
        return str(top_moves[0]["move"]), "stockfish"

    top_moves = analyzer(fen, depth=depth, multipv=max(1, top_n)).get("top_moves", [])
    if not top_moves:
        return None, "stockfish"
    return _sample_top_moves(top_moves, eval_window_cp, rng), "stockfish"


def _sample_top_moves(
    top_moves: list[dict[str, Any]],
    eval_window_cp: int,
    rng: random.Random | None,
) -> str:
    """Sample a move among those within ``eval_window_cp`` of the best line."""

    best_eval = float(top_moves[0]["eval"])
    candidates = [
        (str(tm["move"]), float(tm["eval"]))
        for tm in top_moves
        if best_eval - float(tm["eval"]) <= eval_window_cp
    ]
    if len(candidates) == 1:
        return candidates[0][0]

    weights = [
        math.exp(-(best_eval - eval_cp) / SOFTMAX_TEMPERATURE_CP)
        for _, eval_cp in candidates
    ]
    chooser = rng or random
    return chooser.choices([move for move, _ in candidates], weights=weights, k=1)[0]
