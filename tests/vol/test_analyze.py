"""PGN analysis tests.

Unit tests use :class:`FakeEngine` for determinism; integration tests use a
real Stockfish and auto-skip when unavailable.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import chess
import pytest

from chess_vol.analyze import PlyResult, analyze_pgn
from chess_vol.engine import Engine

from .conftest import FakeEngine, evals_to_infos, load_pgn, requires_stockfish

# --------------------------------------------------------------------------- #
# Unit: PGN parsing + analyze_pgn plumbing                                     #
# --------------------------------------------------------------------------- #


def _flat_producer(b: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    evals = [50, 30, 10, -10, -30, -50][:multipv]
    moves = list(b.legal_moves)[:multipv]
    return evals_to_infos(evals, turn=b.turn, moves=moves)


class TestAnalyzePgn:
    def test_result_per_ply(self) -> None:
        pgn = load_pgn("sample_game")
        engine = FakeEngine(producer=_flat_producer)

        results = analyze_pgn(pgn, engine)

        # Morphy's Opera Game is 17 full moves. We just check the count is in
        # range and that every ply produced a PlyResult + engine call.
        assert len(results) >= 20
        assert all(isinstance(r, PlyResult) for r in results)
        assert engine.call_count == len(results) - sum(
            1 for r in results if r.volatility.reason == "checkmate"
        )

    def test_max_plies_respected(self) -> None:
        pgn = load_pgn("sample_game")
        engine = FakeEngine(producer=_flat_producer)
        results = analyze_pgn(pgn, engine, max_plies=5)
        assert len(results) == 5

    def test_progress_callback_invoked(self) -> None:
        pgn = load_pgn("sample_game")
        engine = FakeEngine(producer=_flat_producer)
        calls: list[tuple[int, int]] = []

        def progress(done: int, total: int, result: PlyResult) -> None:
            calls.append((done, total))
            assert isinstance(result, PlyResult)

        analyze_pgn(pgn, engine, max_plies=4, progress=progress)
        assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]

    def test_empty_pgn_raises(self) -> None:
        engine = FakeEngine(producer=_flat_producer)
        with pytest.raises(ValueError):
            analyze_pgn("", engine)

    def test_fen_before_and_after_are_consistent(self) -> None:
        pgn = load_pgn("sample_game")
        engine = FakeEngine(producer=_flat_producer)
        results = analyze_pgn(pgn, engine, max_plies=3)

        # fen_after of ply N must equal fen_before of ply N+1.
        for prev, curr in pairwise(results):
            assert prev.fen_after == curr.fen_before

    def test_volatility_kwargs_pass_through(self) -> None:
        """Passing ``recurse_depth=1`` should cause multiple engine calls per ply."""
        pgn = load_pgn("sample_game")
        engine = FakeEngine(producer=_flat_producer)
        results = analyze_pgn(pgn, engine, max_plies=2, recurse_depth=1, recurse_k=2)
        # Per ply: 1 root + 2 children = 3 engine calls (skipping any terminal nodes).
        assert engine.call_count >= len(results) * 3

    def test_checkmate_ply_returns_none_score(self) -> None:
        """The last ply of Opera Game — 17...Rd8# — is the move *delivering* mate,
        so the board before it is *not* yet checkmate. The 'mating' position
        shows up post-mate. Instead verify the analyzer handles a custom PGN
        ending in check-that-is-mate."""
        pgn = '[Result "1-0"]\n\n' "1. f3 e5 2. g4 Qh4# 1-0\n"
        engine = FakeEngine(producer=_flat_producer)
        results = analyze_pgn(pgn, engine)
        # 4 plies: f3, e5, g4, Qh4#. Last ply was played from a non-terminal
        # position (it's black's move delivering mate).
        assert len(results) == 4
        # All four positions had legal moves; no terminal reason expected.
        assert all(r.volatility.reason is None for r in results)


# --------------------------------------------------------------------------- #
# Integration: real engine                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@requires_stockfish
class TestAnalyzePgnIntegration:
    def test_short_game_one_ply(self) -> None:
        pgn = '[Result "*"]\n\n' "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *\n"
        with Engine() as engine:
            results = analyze_pgn(pgn, engine, depth=8, multipv=4)
        assert len(results) == 6
        for result in results:
            assert result.volatility.score is not None
            assert 0.0 <= result.volatility.score <= 100.0
