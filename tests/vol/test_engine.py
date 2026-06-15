"""Engine wrapper tests.

Unit tests for path resolution (no Stockfish needed) + integration tests that
spawn a real Stockfish and are skipped automatically if none is available.
"""

from __future__ import annotations

import os
from pathlib import Path

import chess
import pytest

from chess_vol.engine import Engine, EngineNotFoundError, _resolve_path

from .conftest import requires_stockfish

# --------------------------------------------------------------------------- #
# Unit: path resolution logic                                                  #
# --------------------------------------------------------------------------- #


class TestResolvePath:
    def test_explicit_missing_path_raises(self) -> None:
        with pytest.raises(EngineNotFoundError):
            _resolve_path("/definitely/not/a/real/stockfish/binary")

    def test_explicit_existing_path_returns_it(self, tmp_path: Path) -> None:
        fake_binary = tmp_path / "stockfish-fake"
        fake_binary.write_text("not really a binary")
        resolved = _resolve_path(str(fake_binary))
        assert Path(resolved).resolve() == fake_binary.resolve()

    def test_env_var_is_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / "stockfish-env"
        fake.write_text("")
        monkeypatch.setenv("STOCKFISH_PATH", str(fake))

        def no_which(_name: str) -> str | None:
            return None

        monkeypatch.setattr("shutil.which", no_which)
        resolved = _resolve_path(None)
        assert Path(resolved).resolve() == fake.resolve()

    def test_raises_with_helpful_message_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STOCKFISH_PATH", raising=False)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        # Blank out install candidate lookups.
        monkeypatch.setattr(
            "chess_vol.engine._UNIX_CANDIDATES",
            (),
        )
        monkeypatch.setattr(
            "chess_vol.engine._windows_candidates",
            lambda: (),
        )
        with pytest.raises(EngineNotFoundError) as excinfo:
            _resolve_path(None)
        # Message should point the user to install / env var.
        assert "STOCKFISH_PATH" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Integration: require real Stockfish                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@requires_stockfish
class TestRealEngine:
    def test_auto_detect_and_analyse_startpos(self) -> None:
        with Engine() as engine:
            infos = engine.analyse(chess.Board(), depth=8, multipv=3)
        assert len(infos) == 3
        # The engine should have assigned multipv indices 1, 2, 3.
        assert sorted(int(info["multipv"]) for info in infos) == [1, 2, 3]

    def test_engine_closes_on_exception(self) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with Engine():
                raise RuntimeError("boom")

    def test_engine_usable_via_compute_volatility(self) -> None:
        from chess_vol.volatility import compute_volatility

        with Engine() as engine:
            result = compute_volatility(chess.Board(), engine, depth=6, multipv=4)
        assert result.score is not None
        assert 0.0 <= result.score <= 100.0
        assert len(result.alt_evals_cp) == 3


@pytest.mark.integration
@requires_stockfish
def test_context_manager_prevents_leak() -> None:
    """Spawn many engines serially — process table should not blow up."""
    for _ in range(3):
        with Engine() as engine:
            engine.analyse(chess.Board(), depth=4, multipv=2)
    # If we got here without hanging or raising, we're good.


def test_pre_enter_usage_raises(
    monkeypatch: pytest.MonkeyPatch, ensure_stockfish_on_path: None
) -> None:
    """Calling .analyse() or .path before __enter__ is a usage bug."""
    engine = Engine()
    with pytest.raises(RuntimeError, match="context manager"):
        _ = engine.path

    board = chess.Board()
    with pytest.raises(RuntimeError, match="context manager"):
        engine.analyse(board, depth=4, multipv=2)

    # Double-close is safe.
    engine.close()
    engine.close()


def test_path_property_requires_enter() -> None:
    """Without __enter__, .path raises rather than returning a stale value."""
    engine = Engine(path=os.devnull)
    with pytest.raises(RuntimeError):
        _ = engine.path
