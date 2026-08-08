"""Integration tests for the real Stockfish findability search path.

Skipped automatically when no Stockfish binary is locatable. Exercises
:func:`core.engine.multipv_move_evals` — the fixed-node, iterative-deepening
MultiPV capture that yields ``d_star`` for free (spec §3.3 Step 2).
"""

from __future__ import annotations

import chess
import chess.engine
import pytest

from core.engine import multipv_move_evals


def _stockfish_path() -> str | None:
    from chess_vol.engine import _resolve_path

    try:
        return _resolve_path(None)
    except Exception:
        return None


SF = _stockfish_path()
requires_stockfish = pytest.mark.skipif(SF is None, reason="Stockfish binary not available")

# A quiet middlegame-ish start position keeps the search cheap and stable.
_FEN = chess.STARTING_FEN


@pytest.mark.integration
@requires_stockfish
def test_multipv_capture_shapes() -> None:
    board = chess.Board(_FEN)
    with chess.engine.SimpleEngine.popen_uci(SF) as engine:
        engine.configure({"Threads": 1, "Hash": 16})
        evals = multipv_move_evals(engine, board, multipv=4, nodes=200_000)
    assert len(evals) >= 2
    assert all(e.move in board.legal_moves for e in evals)
    assert all(e.cp is not None or e.mate is not None for e in evals)
    assert all(e.d_star >= 1 for e in evals)
    assert all(e.pv and e.pv[0] == e.move for e in evals)


@pytest.mark.integration
@requires_stockfish
def test_fixed_nodes_are_reproducible() -> None:
    board = chess.Board(_FEN)

    def run() -> list[tuple[str, int | None, int | None]]:
        with chess.engine.SimpleEngine.popen_uci(SF) as engine:
            engine.configure({"Threads": 1, "Hash": 16})
            evals = multipv_move_evals(engine, board, multipv=4, nodes=200_000)
        return [(e.move.uci(), e.cp, e.mate) for e in evals]

    # Fixed *nodes* (not depth), single thread → identical result across runs.
    assert run() == run()
