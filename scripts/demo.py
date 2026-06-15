"""End-to-end demo: shallow vs deep volatility on a handful of positions.

Run with a real Stockfish available (STOCKFISH_PATH set or binary on PATH)::

    python scripts/demo.py
"""

from __future__ import annotations

import time
from pathlib import Path

import chess

from chess_vol import Engine, analyze_pgn, compute_volatility
from chess_vol.config import color_for

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

POSITIONS: list[tuple[str, str]] = [
    ("startpos (quiet)", chess.STARTING_FEN),
    ("quiet fixture", (FIXTURES / "quiet_position.fen").read_text().strip()),
    ("sharp King's-Gambit middlegame", (FIXTURES / "sharp_position.fen").read_text().strip()),
    ("only-move fixture", (FIXTURES / "only_move.fen").read_text().strip()),
]


def bar(score: float | None, width: int = 20) -> str:
    if score is None:
        return "—" * width
    filled = round(width * score / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)


def main() -> None:
    with Engine() as engine:
        print(f"Stockfish : {engine.path}\n")

        print(f"{'Position':40}  {'depth':5}  {'V':>6}  {'scale':>6}  color    bar  reason")
        print("-" * 100)
        for label, fen in POSITIONS:
            board = chess.Board(fen)
            for mode, kwargs in (
                ("shallow", {"depth": 14, "multipv": 6}),
                ("deep   ", {"depth": 14, "multipv": 6, "recurse_depth": 2}),
            ):
                t0 = time.perf_counter()
                result = compute_volatility(board, engine, **kwargs)
                elapsed = time.perf_counter() - t0
                score_str = f"{result.score:6.1f}" if result.score is not None else "   —  "
                color = color_for(result.score) if result.score is not None else "—"
                print(
                    f"{label[:38]:40}  {mode}  {score_str}  "
                    f"{result.scale:6.3f}  {color:6}  {bar(result.score)}  "
                    f"{result.reason or ''}  ({elapsed:.2f}s, {result.analyses} analyses)"
                )
            print()

        print("\n--- Opera Game ply-by-ply (shallow) ---")
        pgn = (FIXTURES / "sample_game.pgn").read_text()
        results = analyze_pgn(pgn, engine, depth=10, multipv=5, max_plies=12)
        for r in results:
            v = r.volatility
            score = v.score
            score_str = f"{score:5.1f}" if score is not None else "  -  "
            print(
                f"  ply {r.ply:2}  {r.san:8}  eval={v.best_eval_cp:+5d}  "
                f"V={score_str}  {bar(score)}  "
                f"{'decided' if v.decided else ''}"
            )


if __name__ == "__main__":
    main()
