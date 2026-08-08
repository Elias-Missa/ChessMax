from __future__ import annotations

import chess
import chess.engine as ce

from core.engine import reduce_analysis_stream

E4 = chess.Move.from_uci("e2e4")
D4 = chess.Move.from_uci("d2d4")


def _cp(cp: int) -> ce.PovScore:
    return ce.PovScore(ce.Cp(cp), chess.WHITE)


def test_captures_final_line_per_slot_and_d_star() -> None:
    board = chess.Board()
    # d4 leads at depth 1, e4 overtakes as best at depth 5.
    infos = [
        {"multipv": 1, "depth": 1, "pv": [D4], "score": _cp(20)},
        {"multipv": 2, "depth": 1, "pv": [E4], "score": _cp(18)},
        {"multipv": 1, "depth": 5, "pv": [E4], "score": _cp(30)},
        {"multipv": 2, "depth": 5, "pv": [D4], "score": _cp(15)},
    ]
    evals = reduce_analysis_stream(board, infos)
    assert [e.move for e in evals] == [E4, D4]  # best-first by final slot
    assert evals[0].cp == 30
    assert evals[1].cp == 15
    # e4 first appeared (as a line head) at depth 1 (slot 2), d4 at depth 1 too.
    assert evals[0].d_star == 1
    assert evals[1].d_star == 1


def test_d_star_reflects_first_appearance_depth() -> None:
    board = chess.Board()
    infos = [
        {"multipv": 1, "depth": 1, "pv": [D4], "score": _cp(20)},
        {"multipv": 1, "depth": 8, "pv": [E4], "score": _cp(40)},  # e4 only shows up deep
    ]
    evals = reduce_analysis_stream(board, infos)
    assert evals[0].move == E4
    assert evals[0].d_star == 8


def test_mate_score_extracted() -> None:
    board = chess.Board()
    infos = [{"multipv": 1, "depth": 4, "pv": [E4], "score": ce.PovScore(ce.Mate(3), chess.WHITE)}]
    (ev,) = reduce_analysis_stream(board, infos)
    assert ev.mate == 3
    assert ev.cp is None


def test_wdl_captured_when_present() -> None:
    board = chess.Board()
    info = {
        "multipv": 1,
        "depth": 3,
        "pv": [E4],
        "score": _cp(30),
        "wdl": ce.PovWdl(ce.Wdl(600, 300, 100), chess.WHITE),
    }
    (ev,) = reduce_analysis_stream(board, [info])
    assert ev.wdl == (600, 300, 100)
