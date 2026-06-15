"""Stockfish-backed analysis helpers for Phase 1.

The public functions in this module intentionally match the M1 API from
``chess_trainer_spec.md`` so later pipeline and server code can build on them.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import chess
import chess.engine


DEFAULT_ENGINE_COMMAND = "stockfish"
MATE_SCORE_CP = 100_000

GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (10, "best"),
    (30, "good"),
    (100, "acceptable"),
    (150, "inaccuracy"),
    (300, "mistake"),
    (float("inf"), "blunder"),
)


def analyze(
    fen: str,
    depth: int = 20,
    multipv: int = 5,
    engine_command: str = DEFAULT_ENGINE_COMMAND,
) -> dict[str, list[dict[str, int | str]]]:
    """Analyze ``fen`` with Stockfish and return top moves from side-to-move POV.

    Evaluations are centipawns normalized to the player whose turn it is in
    ``fen``. Mate scores are converted to large centipawn values so callers can
    still compare candidate moves numerically.
    """

    if multipv < 1:
        raise ValueError("multipv must be at least 1")

    with StockfishAnalyzer(engine_command) as analyzer:
        return analyzer.analyze(fen, depth=depth, multipv=multipv)


class StockfishAnalyzer:
    """Reusable Stockfish process for batch analysis jobs."""

    def __init__(
        self,
        engine_command: str = DEFAULT_ENGINE_COMMAND,
        threads: int = 1,
        hash_mb: int = 128,
    ) -> None:
        self.engine_command = engine_command
        self.threads = threads
        self.hash_mb = hash_mb
        self.engine: chess.engine.SimpleEngine | None = None

    def __enter__(self) -> "StockfishAnalyzer":
        self.engine = chess.engine.SimpleEngine.popen_uci(self.engine_command)
        options: dict[str, int] = {}
        if self.threads > 1:
            options["Threads"] = self.threads
        if self.hash_mb:
            options["Hash"] = self.hash_mb
        if options:
            self.engine.configure(options)
        return self

    def __exit__(self, *args: object) -> None:
        if self.engine is not None:
            self.engine.quit()
            self.engine = None

    def analyze(
        self,
        fen: str,
        depth: int = 20,
        multipv: int = 5,
    ) -> dict[str, list[dict[str, int | str]]]:
        if multipv < 1:
            raise ValueError("multipv must be at least 1")
        if self.engine is None:
            raise RuntimeError("StockfishAnalyzer must be used as a context manager")

        board = chess.Board(fen)
        raw_infos = self.engine.analyse(
            board,
            chess.engine.Limit(depth=depth),
            multipv=multipv,
        )
        return analysis_to_top_moves(board, raw_infos)


def analysis_to_top_moves(
    board: chess.Board,
    raw_infos: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    infos = raw_infos if isinstance(raw_infos, list) else [raw_infos]
    top_moves: list[dict[str, Any]] = []

    for info in infos:
        pv = info.get("pv")
        score = info.get("score")
        if not pv or score is None:
            continue

        eval_cp = score.pov(board.turn).score(mate_score=MATE_SCORE_CP)
        if eval_cp is None:
            continue

        top_moves.append({
            "move": pv[0].uci(),
            "eval": int(eval_cp),
            "pv": [m.uci() for m in pv[:5]],
        })

    return {"top_moves": top_moves}


def is_quiet_position(analysis: dict[str, list[dict[str, Any]]]) -> bool:
    """Return whether an analysis result passes the spec's quiet criteria."""

    top_moves = analysis.get("top_moves", [])
    evals = [float(move["eval"]) for move in top_moves if "eval" in move]

    if len(evals) < 5:
        return False

    top_three_are_balanced = all(-100 <= eval_cp <= 100 for eval_cp in evals[:3])
    at_least_five_reasonable = sum(-150 <= eval_cp <= 150 for eval_cp in evals) >= 5

    return top_three_are_balanced and at_least_five_reasonable


def grade_from_eval_loss(eval_loss: float) -> str:
    """Map centipawn loss to the spec's grade labels."""

    for threshold, grade in GRADE_THRESHOLDS:
        if eval_loss <= threshold:
            return grade

    raise RuntimeError("unreachable grade threshold")


def grade_move(
    fen: str,
    user_move: str,
    best_eval: float,
    depth: int = 18,
    engine_command: str = DEFAULT_ENGINE_COMMAND,
) -> dict[str, float | str]:
    """Grade a user move by comparing its resulting eval to the best eval.

    ``best_eval`` must be the original position's best-move evaluation from the
    user's POV. After the user move, the board turn belongs to the opponent, so
    the resulting Stockfish eval is flipped back to the user's POV.
    """

    board = chess.Board(fen)
    move = chess.Move.from_uci(user_move)
    if move not in board.legal_moves:
        raise ValueError(f"Illegal move for position: {user_move}")

    board.push(move)
    response = analyze(
        board.fen(),
        depth=depth,
        multipv=1,
        engine_command=engine_command,
    )

    if not response["top_moves"]:
        raise RuntimeError("Engine returned no analysis for resulting position")

    opponent_eval_after = float(response["top_moves"][0]["eval"])
    user_eval_after = -opponent_eval_after
    eval_loss = float(best_eval) - user_eval_after

    return {
        "eval_loss": eval_loss,
        "grade": grade_from_eval_loss(eval_loss),
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Analyze a FEN with Stockfish.")
    parser.add_argument("fen", help="FEN to analyze")
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--multipv", type=int, default=5)
    parser.add_argument("--engine", default=DEFAULT_ENGINE_COMMAND)
    args = parser.parse_args()

    result = analyze(
        args.fen,
        depth=args.depth,
        multipv=args.multipv,
        engine_command=args.engine,
    )
    result["is_quiet"] = is_quiet_position(result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _main()
