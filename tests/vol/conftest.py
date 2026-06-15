"""Shared pytest fixtures and a fake engine for engine-free unit tests."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import chess
import chess.engine
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fen(name: str) -> str:
    """Load a FEN fixture by stem (e.g. ``"only_move"``)."""
    return (FIXTURES / f"{name}.fen").read_text(encoding="utf-8").strip()


def load_pgn(name: str) -> str:
    return (FIXTURES / f"{name}.pgn").read_text(encoding="utf-8")


def stockfish_available() -> bool:
    """True if a real Stockfish binary is locatable for integration tests."""
    from chess_vol.engine import _resolve_path

    try:
        _resolve_path(None)
        return True
    except Exception:
        return False


requires_stockfish = pytest.mark.skipif(
    not stockfish_available(),
    reason="Stockfish binary not available; integration test skipped.",
)


# --------------------------------------------------------------------------- #
# Fake engine for deterministic unit tests                                     #
# --------------------------------------------------------------------------- #


def _cp_to_pov_score(cp: int, turn: chess.Color) -> chess.engine.PovScore:
    """Wrap a side-to-move cp value as a ``PovScore`` from white's perspective
    (which is what python-chess's ``info["score"]`` is)."""
    white_cp = cp if turn == chess.WHITE else -cp
    return chess.engine.PovScore(chess.engine.Cp(white_cp), chess.WHITE)


def _mate_to_pov_score(mate_in: int, turn: chess.Color) -> chess.engine.PovScore:
    """Wrap a mate-in-N from side-to-move's POV as a white-POV ``PovScore``."""
    white_mate = mate_in if turn == chess.WHITE else -mate_in
    return chess.engine.PovScore(chess.engine.Mate(white_mate), chess.WHITE)


def make_info(
    cp: int | None = None,
    *,
    mate: int | None = None,
    multipv: int = 1,
    pv: list[chess.Move] | None = None,
    turn: chess.Color = chess.WHITE,
) -> dict[str, Any]:
    """Build a minimal ``python-chess``-style info dict."""
    if (cp is None) == (mate is None):
        raise ValueError("Exactly one of `cp` or `mate` must be given")
    if mate is not None:
        score = _mate_to_pov_score(mate, turn)
    else:
        assert cp is not None
        score = _cp_to_pov_score(cp, turn)
    return {
        "score": score,
        "multipv": multipv,
        "pv": pv if pv is not None else [],
    }


class FakeEngine:
    """Scripted engine: returns pre-seeded MultiPV results per call.

    Two usage modes:

    1. **Scripted:** pass ``scripts=[[info, info, ...], [info, ...]]``; each call
       pops the next list.
    2. **Callable:** pass ``producer=lambda board, depth, multipv: [infos]`` for
       fully dynamic behavior (useful for side-to-move flip tests).

    Tracks every call in ``self.calls`` for assertions.
    """

    def __init__(
        self,
        scripts: list[list[dict[str, Any]]] | None = None,
        producer: Callable[[chess.Board, int, int], list[dict[str, Any]]] | None = None,
    ) -> None:
        if (scripts is None) == (producer is None):
            raise ValueError("Provide exactly one of `scripts` or `producer`")
        self._scripts = list(scripts) if scripts is not None else None
        self._producer = producer
        self.calls: list[tuple[str, int, int]] = []
        """List of ``(fen, depth, multipv)`` for each call."""

    def analyse(
        self,
        board: chess.Board,
        depth: int = 18,
        multipv: int = 6,
    ) -> list[dict[str, Any]]:
        self.calls.append((board.fen(), depth, multipv))
        if self._producer is not None:
            return self._producer(board, depth, multipv)
        assert self._scripts is not None
        if not self._scripts:
            raise AssertionError(
                f"FakeEngine ran out of scripted responses (call #{len(self.calls)})"
            )
        return self._scripts.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def evals_to_infos(
    evals: list[int | str],
    *,
    turn: chess.Color = chess.WHITE,
    moves: list[chess.Move] | None = None,
) -> list[dict[str, Any]]:
    """Build a multipv info list from ``evals``.

    Each entry is either an ``int`` (cp, side-to-move POV) or ``"Mk"`` / ``"-Mk"``
    (mate-in-k from side-to-move POV, positive = us mating, negative = opp mating).
    """
    infos: list[dict[str, Any]] = []
    for i, e in enumerate(evals):
        mv = [moves[i]] if moves is not None and i < len(moves) else []
        if isinstance(e, str):
            sign = -1 if e.startswith("-") else 1
            digits = e.lstrip("-M")
            n = int(digits)
            infos.append(make_info(mate=sign * n, multipv=i + 1, pv=mv, turn=turn))
        else:
            infos.append(make_info(cp=e, multipv=i + 1, pv=mv, turn=turn))
    return infos


# --------------------------------------------------------------------------- #
# Pytest fixtures                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def startpos() -> chess.Board:
    return chess.Board()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def ensure_stockfish_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net so unit tests don't accidentally pick up a user's stockfish."""
    monkeypatch.delenv("STOCKFISH_PATH", raising=False)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **kw: None if name.startswith("stockfish") else real_which(name),
    )
