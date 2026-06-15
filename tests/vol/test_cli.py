"""Tests for :mod:`chess_vol.cli`.

Uses Typer's ``CliRunner`` plus :class:`FakeEngine` from ``conftest`` so no
real Stockfish process is required. An integration test at the bottom runs
the CLI against a real engine and is auto-skipped when Stockfish is missing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import chess
import pytest
from typer.testing import CliRunner

from chess_vol import cli
from chess_vol.analyze import PlyResult
from chess_vol.cli import app
from chess_vol.cli_report import (
    build_analyze_report,
    build_params,
    ply_to_json,
    volatility_to_json,
)
from chess_vol.engine import Engine
from chess_vol.volatility import TopLine, VolatilityResult

from .conftest import FakeEngine, evals_to_infos, load_pgn, requires_stockfish

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _flat_producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    """A gently decreasing MultiPV fan for any position. Safe for all fixtures."""
    evals = [50, 30, 10, -10, -30, -50][:multipv]
    moves = list(board.legal_moves)[:multipv]
    return evals_to_infos(evals, turn=board.turn, moves=moves)


def _spiky_producer(board: chess.Board, depth: int, multipv: int) -> list[dict[str, Any]]:
    """An only-move-style fan: first move keeps balance, alternatives collapse."""
    evals = [0, -300, -500, -700, -900, -1100][:multipv]
    moves = list(board.legal_moves)[:multipv]
    return evals_to_infos(evals, turn=board.turn, moves=moves)


@contextmanager
def _fake_factory(engine: FakeEngine) -> Iterator[FakeEngine]:
    yield engine


@pytest.fixture
def patch_engine(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Monkeypatch :data:`cli.ENGINE_FACTORY` to yield a chosen ``FakeEngine``.

    Returns a function that installs the given engine and returns it, so tests
    can still assert on the underlying ``FakeEngine`` afterwards.
    """

    def _install(engine: FakeEngine) -> FakeEngine:
        def factory() -> Any:
            return _fake_factory(engine)

        monkeypatch.setattr(cli, "ENGINE_FACTORY", factory)
        return engine

    return _install


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --------------------------------------------------------------------------- #
# `analyze` command                                                           #
# --------------------------------------------------------------------------- #


class TestAnalyze:
    def _write_pgn(self, tmp_path: Path, name: str = "sample_game") -> Path:
        path = tmp_path / "game.pgn"
        path.write_text(load_pgn(name), encoding="utf-8")
        return path

    def test_analyze_basic(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        engine = patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)

        result = runner.invoke(app, ["analyze", str(pgn_path), "--max-plies", "4", "--no-color"])

        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if "ply" in line]
        assert len(lines) >= 4
        assert engine.call_count == 4
        assert "V " in result.output
        assert "Analysed 4 plies" in result.output
        assert "shallow" in result.output

    def test_analyze_deep_flag_sets_recurse_depth_two(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        engine = patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)

        result = runner.invoke(
            app,
            [
                "analyze",
                str(pgn_path),
                "--max-plies",
                "1",
                "--deep",
                "--recurse-k",
                "2",
                "--no-color",
            ],
        )

        assert result.exit_code == 0, result.output
        # Phase 1.5 budget for one ply with k=2, recurse_depth=2:
        # 1 + 2 + 4 = 7 analyses (some branches may terminate if checkmate; not here).
        assert engine.call_count == 7
        assert "local" in result.output and "reply" in result.output
        assert "deep" in result.output

    def test_analyze_max_plies(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)

        result = runner.invoke(app, ["analyze", str(pgn_path), "--max-plies", "3", "--no-color"])
        assert result.exit_code == 0
        assert "Analysed 3 plies" in result.output

    def test_analyze_json_output_schema(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)
        out_path = tmp_path / "report.json"

        result = runner.invoke(
            app,
            [
                "analyze",
                str(pgn_path),
                "--max-plies",
                "2",
                "--output",
                str(out_path),
                "--no-color",
                "--quiet",
            ],
        )
        assert result.exit_code == 0, result.output
        assert out_path.is_file()

        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["mode"] == "shallow"
        assert report["params"]["depth"] > 0
        assert report["params"]["max_plies"] == 2
        assert isinstance(report["plies"], list)
        assert len(report["plies"]) == 2

        first = report["plies"][0]
        required_ply_keys = {
            "ply",
            "san",
            "fen_before",
            "fen_after",
            "eval_cp",
            "volatility",
        }
        assert required_ply_keys <= set(first.keys())
        required_vol_keys = {
            "score",
            "raw_cp",
            "local_raw_cp",
            "best_eval_cp",
            "alt_evals_cp",
            "scale",
            "decided",
            "reason",
            "recurse_depth_used",
            "analyses",
            "color",
        }
        assert required_vol_keys <= set(first["volatility"].keys())

    def test_analyze_deep_json_has_deep_mode(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)
        out_path = tmp_path / "deep.json"

        result = runner.invoke(
            app,
            [
                "analyze",
                str(pgn_path),
                "--max-plies",
                "1",
                "--deep",
                "--recurse-k",
                "2",
                "--output",
                str(out_path),
                "--quiet",
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["mode"] == "deep"
        assert report["params"]["recurse_depth"] == 2
        assert report["plies"][0]["volatility"]["recurse_depth_used"] == 2

    def test_analyze_quiet_suppresses_progress(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)
        out_path = tmp_path / "q.json"

        result = runner.invoke(
            app,
            [
                "analyze",
                str(pgn_path),
                "--max-plies",
                "3",
                "--quiet",
                "--output",
                str(out_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "ply   1" not in result.output
        assert "Analysed" not in result.output
        # Output file still written.
        assert out_path.is_file()

    def test_analyze_no_color_strips_ansi(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)

        result = runner.invoke(app, ["analyze", str(pgn_path), "--max-plies", "2", "--no-color"])
        assert result.exit_code == 0
        assert ANSI_RE.search(result.output) is None

    def test_analyze_missing_pgn_file(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        missing = tmp_path / "nope.pgn"

        result = runner.invoke(app, ["analyze", str(missing), "--no-color"])
        assert result.exit_code != 0

    def test_analyze_deep_and_recurse_zero_conflict(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        pgn_path = self._write_pgn(tmp_path)

        result = runner.invoke(
            app,
            [
                "analyze",
                str(pgn_path),
                "--deep",
                "--recurse-depth",
                "0",
                "--no-color",
            ],
        )
        assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# `fen` command                                                               #
# --------------------------------------------------------------------------- #


class TestFen:
    def test_fen_command_basic(
        self,
        runner: CliRunner,
        patch_engine: Any,
    ) -> None:
        engine = patch_engine(FakeEngine(producer=_flat_producer))
        result = runner.invoke(app, ["fen", chess.STARTING_FEN, "--depth", "8", "--no-color"])
        assert result.exit_code == 0, result.output
        assert engine.call_count == 1
        assert "V " in result.output
        # scale and analyses are surfaced on the fen summary line.
        assert "analyses=1" in result.output

    def test_fen_only_move_renders_dashes(
        self,
        runner: CliRunner,
        patch_engine: Any,
    ) -> None:
        only_move_fen = "7k/5K2/6R1/8/8/8/8/8 b - - 0 1"
        patch_engine(FakeEngine(producer=_flat_producer))
        result = runner.invoke(app, ["fen", only_move_fen, "--no-color"])
        assert result.exit_code == 0, result.output
        assert "only_move" in result.output
        # The bar should render as dashes (no numeric score available).
        assert "\u2014" in result.output  # em-dash used by ascii_bar(None)

    def test_fen_deep_shows_split(
        self,
        runner: CliRunner,
        patch_engine: Any,
    ) -> None:
        patch_engine(FakeEngine(producer=_spiky_producer))
        result = runner.invoke(
            app,
            [
                "fen",
                chess.STARTING_FEN,
                "--deep",
                "--recurse-k",
                "2",
                "--no-color",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "local" in result.output and "reply" in result.output

    def test_fen_invalid_exits_nonzero(
        self,
        runner: CliRunner,
        patch_engine: Any,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        result = runner.invoke(app, ["fen", "not-a-fen-at-all", "--no-color"])
        assert result.exit_code != 0
        assert "invalid FEN" in result.output or "invalid FEN" in (result.stderr or "")

    def test_fen_json_output(
        self,
        runner: CliRunner,
        patch_engine: Any,
        tmp_path: Path,
    ) -> None:
        patch_engine(FakeEngine(producer=_flat_producer))
        out_path = tmp_path / "fen.json"
        result = runner.invoke(
            app,
            [
                "fen",
                chess.STARTING_FEN,
                "--output",
                str(out_path),
                "--no-color",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["mode"] == "shallow"
        assert payload["fen"].startswith("rnbqkbnr/")
        assert "score" in payload["volatility"]


# --------------------------------------------------------------------------- #
# `cli_report` pure-logic tests                                               #
# --------------------------------------------------------------------------- #


class TestCliReport:
    def _make_vol_result(self) -> VolatilityResult:
        return VolatilityResult(
            score=42.0,
            raw_cp=80.0,
            local_raw_cp=60.0,
            best_eval_cp=120,
            alt_evals_cp=[80, 40, 20, -10, -50],
            scale=0.87,
            decided=False,
            reason=None,
            recurse_depth_used=2,
            analyses=13,
            top_lines=[
                TopLine(uci="e2e4", san="e4", pv_san=["e4", "e5", "Nf3"], eval_cp=120),
                TopLine(uci="d2d4", san="d4", pv_san=["d4", "d5"], eval_cp=80),
            ],
        )

    def test_volatility_to_json_schema_and_color(self) -> None:
        vol = self._make_vol_result()
        data = volatility_to_json(vol)
        assert data["score"] == 42.0
        assert data["color"] == "medium"
        assert data["recurse_depth_used"] == 2
        assert data["analyses"] == 13
        assert data["alt_evals_cp"] == [80, 40, 20, -10, -50]
        assert data["top_lines"] == [
            {"uci": "e2e4", "san": "e4", "pv_san": ["e4", "e5", "Nf3"], "eval_cp": 120},
            {"uci": "d2d4", "san": "d4", "pv_san": ["d4", "d5"], "eval_cp": 80},
        ]

        # None-score case
        none_vol = VolatilityResult(
            score=None,
            raw_cp=None,
            local_raw_cp=None,
            best_eval_cp=0,
            alt_evals_cp=[],
            scale=1.0,
            decided=False,
            reason="only_move",
            recurse_depth_used=0,
            analyses=1,
        )
        none_data = volatility_to_json(none_vol)
        assert none_data["score"] is None
        assert none_data["color"] is None
        assert none_data["reason"] == "only_move"

    def test_ply_to_json_shape(self) -> None:
        vol = self._make_vol_result()
        ply = PlyResult(
            ply=1,
            san="e4",
            fen_before=chess.STARTING_FEN,
            fen_after="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            eval_cp=vol.best_eval_cp,
            volatility=vol,
        )
        data = ply_to_json(ply)
        assert data["ply"] == 1
        assert data["san"] == "e4"
        assert data["fen_before"] == chess.STARTING_FEN
        assert data["volatility"]["score"] == 42.0

    def test_build_analyze_report_roundtrip(self) -> None:
        vol = self._make_vol_result()
        ply = PlyResult(
            ply=1,
            san="e4",
            fen_before=chess.STARTING_FEN,
            fen_after="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            eval_cp=vol.best_eval_cp,
            volatility=vol,
        )
        params = build_params(
            depth=18,
            multipv=6,
            recurse_depth=2,
            recurse_k=3,
            recurse_alpha=0.5,
            child_depth=12,
            max_plies=1,
        )
        report = build_analyze_report([ply], params=params)
        payload = json.loads(json.dumps(dict(report)))
        assert payload["mode"] == "deep"
        assert payload["params"]["recurse_depth"] == 2
        assert payload["params"]["max_plies"] == 1
        assert len(payload["plies"]) == 1

    def test_mode_label_thresholds(self) -> None:
        from chess_vol.cli_report import mode_label

        assert mode_label(0) == "shallow"
        assert mode_label(1) == "deep"
        assert mode_label(5) == "deep"


# --------------------------------------------------------------------------- #
# Integration: real engine                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@requires_stockfish
class TestCliIntegration:
    def test_cli_real_engine_short_pgn(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pgn = '[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *\n'
        path = tmp_path / "mini.pgn"
        path.write_text(pgn, encoding="utf-8")
        out_path = tmp_path / "report.json"

        # Ensure the real default factory is installed (other tests may have
        # monkeypatched it, but fixtures are function-scoped so this is safe).
        monkeypatch.setattr(cli, "ENGINE_FACTORY", cli._default_engine_factory)

        result = runner.invoke(
            app,
            [
                "analyze",
                str(path),
                "--depth",
                "6",
                "--multipv",
                "4",
                "--output",
                str(out_path),
                "--quiet",
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(report["plies"]) == 6
        for entry in report["plies"]:
            score = entry["volatility"]["score"]
            assert score is not None
            assert 0.0 <= score <= 100.0

    def test_cli_real_engine_fen(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli, "ENGINE_FACTORY", cli._default_engine_factory)
        # Confirm Engine is wired the same way the CLI uses it.
        with Engine() as engine:
            assert engine is not None

        result = runner.invoke(
            app,
            [
                "fen",
                chess.STARTING_FEN,
                "--depth",
                "6",
                "--multipv",
                "4",
                "--no-color",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "V " in result.output
