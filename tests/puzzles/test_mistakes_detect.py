"""Engine-free tests for the mistakes detector (server/mistakes.py)."""

from __future__ import annotations

from typing import Any

import chess
import chess.pgn
import pytest

from server import mistakes
from server.mistakes import (
    MistakePuzzle,
    detect_mistakes,
    outcome_category,
    parse_clk_seconds,
)


# --------------------------------------------------------------------------- #
# Builders                                                                     #
# --------------------------------------------------------------------------- #


def linear_game(moves: list[str], comments: dict[int, str] | None = None) -> chess.pgn.Game:
    """Build a single-line game from UCI moves, optionally annotating a node."""

    comments = comments or {}
    game = chess.pgn.Game()
    node: chess.pgn.GameNode = game
    for idx, uci in enumerate(moves):
        node = node.add_variation(chess.Move.from_uci(uci))
        if idx in comments:
            node.comment = comments[idx]
    return game


def white_fens(moves: list[str]) -> dict[int, str]:
    """Map each White-to-move ply index → the FEN before that move."""

    board = chess.Board()
    out: dict[int, str] = {}
    for idx, uci in enumerate(moves):
        if board.turn == chess.WHITE:
            out[idx] = board.fen()
        board.push(chess.Move.from_uci(uci))
    return out


def scripted_analysis(script: dict[str, list[dict[str, Any]]]) -> mistakes.AnalysisFn:
    """A fake analyze() returning per-FEN top_moves, truncated to multipv."""

    def fake(fen: str, *, depth: int, multipv: int) -> dict[str, list[dict[str, Any]]]:
        rows = script.get(fen)
        if rows is None:
            raise AssertionError(f"unscripted FEN queried: {fen}")
        return {"top_moves": rows[:multipv]}

    return fake


# A 5-ply line; White (the user) moves at plies 0, 2, 4. The mistake is the
# final White move. Black moves are never analysed (detector skips them).
MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
FENS = white_fens(MOVES)
MISTAKE_PLY = 4


def neutral_white_nodes() -> dict[str, list[dict[str, Any]]]:
    """Script the non-mistake White nodes so top[0] == the move actually played
    (→ pass-1 skip)."""

    return {
        FENS[0]: [{"move": "e2e4", "eval": 20, "pv": ["e2e4"]},
                  {"move": "d2d4", "eval": 10}],
        FENS[2]: [{"move": "g1f3", "eval": 15, "pv": ["g1f3"]},
                  {"move": "b1c3", "eval": 5}],
    }


# --------------------------------------------------------------------------- #
# parse_clk_seconds / outcome_category                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("[%clk 0:01:23.4]", 83.4),
        ("[%clk 0:00:05]", 5.0),
        ("[%clk 1:00:00]", 3600.0),
        ("nothing here", None),
        (None, None),
    ],
)
def test_parse_clk_seconds(comment: str | None, expected: float | None) -> None:
    assert parse_clk_seconds(comment) == expected


@pytest.mark.parametrize(
    ("cp", "cat"),
    [(900, 2), (201, 2), (200, 1), (0, 1), (-200, 1), (-201, 0)],
)
def test_outcome_category(cp: int, cat: int) -> None:
    assert outcome_category(cp) == cat


# --------------------------------------------------------------------------- #
# Bucket A — missed win                                                        #
# --------------------------------------------------------------------------- #


def test_bucket_a_missed_win() -> None:
    script = neutral_white_nodes()
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 300, "pv": ["f3g5", "d7d6"]},  # best, decisive
        {"move": "f1c4", "eval": 120},                          # user's move
        {"move": "d2d4", "eval": 100},
    ]
    game = linear_game(MOVES)

    puzzles = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["f3g5", "f1c4", "d2d4"],
            meta={"opponent": "Magnus", "date": "2026-06-01", "url": "https://x"},
        )
    )

    assert len(puzzles) == 1
    p = puzzles[0]
    assert p.bucket == "missed_win"
    assert p.best_move == "f3g5"
    assert p.user_actual_move == "f1c4"
    assert p.eval_before_cp == 300
    assert p.eval_played_cp == 120
    assert p.second_best_gap_cp == 180
    assert p.maia_best_in_top3 == 1
    assert p.maia_solution_rank == 1
    assert p.solution_moves == "f3g5 d7d6"
    assert p.user_color == "w" and p.side_to_move == "w"
    # Caption uses SAN, not UCI.
    assert "you played Bc4" in p.caption
    assert "win was Ng5" in p.caption
    assert "vs Magnus" in p.caption


def test_bucket_a_dropped_when_solution_not_maia_findable() -> None:
    script = neutral_white_nodes()
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 300, "pv": ["f3g5"]},
        {"move": "f1c4", "eval": 120},
        {"move": "d2d4", "eval": 100},
    ]
    game = linear_game(MOVES)

    puzzles = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["f1c4", "d2d4", "b1c3"],  # best NOT in top-3
        )
    )

    assert puzzles == []


def test_maia_none_is_lenient_but_strict_drops() -> None:
    script = neutral_white_nodes()
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 300, "pv": ["f3g5"]},
        {"move": "f1c4", "eval": 120},
    ]
    game = linear_game(MOVES)

    lenient = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: None,
        )
    )
    assert len(lenient) == 1
    assert lenient[0].maia_best_in_top3 is None

    strict = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: None,
            strict_maia=True,
        )
    )
    assert strict == []


# --------------------------------------------------------------------------- #
# Bucket B — blunder (category crossing)                                       #
# --------------------------------------------------------------------------- #


def test_bucket_b_blunder_equal_to_losing() -> None:
    script = neutral_white_nodes()
    # gap (50-30=20) < 150 so NOT bucket A; user move drops equal -> losing.
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 50, "pv": ["f3g5"]},   # best, only ~equal
        {"move": "d2d4", "eval": 30},
        {"move": "f1c4", "eval": -400},                 # user's move, losing
    ]
    game = linear_game(MOVES)

    puzzles = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["f3g5", "d2d4"],
        )
    )

    assert len(puzzles) == 1
    p = puzzles[0]
    assert p.bucket == "blunder"
    assert p.best_move == "f3g5"
    assert p.eval_before_cp == 50
    assert p.eval_played_cp == -400
    assert "save was Ng5" in p.caption


def test_no_puzzle_when_user_played_best() -> None:
    script = neutral_white_nodes()
    # top[0] == user's move at the mistake node -> skipped at pass 1.
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f1c4", "eval": 40, "pv": ["f1c4"]},
        {"move": "d2d4", "eval": 30},
    ]
    game = linear_game(MOVES)

    puzzles = list(
        detect_mistakes(game, chess.WHITE, analysis_fn=scripted_analysis(script))
    )
    assert puzzles == []


def test_clock_floor_skips_time_scramble() -> None:
    script = neutral_white_nodes()
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 300, "pv": ["f3g5"]},
        {"move": "f1c4", "eval": 120},
    ]
    # 10 seconds left at the mistake move -> dropped before any analysis.
    game = linear_game(MOVES, comments={MISTAKE_PLY: "[%clk 0:00:10]"})

    puzzles = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["f3g5"],
        )
    )
    assert puzzles == []


def test_black_user_only_analyses_black_moves() -> None:
    # User is Black; only Black-to-move nodes are analysed. Black's mistake is
    # the 4th ply (b8c6). Script just that node.
    board = chess.Board()
    black_fens: dict[int, str] = {}
    for idx, uci in enumerate(MOVES):
        if board.turn == chess.BLACK:
            black_fens[idx] = board.fen()
        board.push(chess.Move.from_uci(uci))

    script = {
        black_fens[1]: [{"move": "e7e5", "eval": -10, "pv": ["e7e5"]},
                        {"move": "c7c5", "eval": -20}],
        black_fens[3]: [{"move": "g8f6", "eval": 300, "pv": ["g8f6"]},  # best (Black POV)
                        {"move": "b8c6", "eval": 100}],                 # user's move
    }
    game = linear_game(MOVES)

    puzzles = list(
        detect_mistakes(
            game, chess.BLACK,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["g8f6", "b8c6"],
        )
    )
    assert len(puzzles) == 1
    assert puzzles[0].bucket == "missed_win"
    assert puzzles[0].user_color == "b"
    assert puzzles[0].best_move == "g8f6"
    assert puzzles[0].user_actual_move == "b8c6"


def test_volatility_fn_tags_puzzle() -> None:
    script = neutral_white_nodes()
    script[FENS[MISTAKE_PLY]] = [
        {"move": "f3g5", "eval": 300, "pv": ["f3g5"]},
        {"move": "f1c4", "eval": 120},
    ]
    game = linear_game(MOVES)

    puzzles = list(
        detect_mistakes(
            game, chess.WHITE,
            analysis_fn=scripted_analysis(script),
            maia_topk_fn=lambda fen: ["f3g5"],
            volatility_fn=lambda fen: 72.5,
        )
    )
    assert puzzles[0].volatility == 72.5
