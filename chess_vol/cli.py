"""Command-line interface for the Chess Volatility Bar.

Phase 2 of the roadmap (README §7). Exposes two commands:

    chess-vol analyze game.pgn [--deep] [--depth 18] [--multipv 6] ...
    chess-vol fen    "FEN..."  [--deep] [--depth 18] [--multipv 6] ...

This module is a thin orchestration layer on top of :mod:`chess_vol.volatility`
and :mod:`chess_vol.analyze`. All rendering is kept dependency-light: colors go
through :func:`typer.style` (which respects ``--no-color`` / non-TTY), and no
optional extras are required for basic output.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Annotated, Any

import chess
import typer

from chess_vol.analyze import PlyResult, analyze_pgn
from chess_vol.cli_report import (
    build_analyze_report,
    build_fen_report,
    build_params,
    volatility_to_json,
)
from chess_vol.config import (
    DEFAULT_CHILD_DEPTH,
    DEFAULT_DEPTH,
    DEFAULT_MULTIPV,
    DEFAULT_RECURSE_ALPHA,
    DEFAULT_RECURSE_K,
    color_for,
)
from chess_vol.engine import Engine, EngineNotFoundError
from chess_vol.volatility import EngineLike, VolatilityResult, compute_volatility


def _fix_console_encoding() -> None:
    """Force stdout/stderr to UTF-8 when the console can't encode bar glyphs.

    On Windows the default console encoding is ``cp1252``, which cannot emit
    ``\u2588`` / ``\u2591`` and would otherwise raise ``UnicodeEncodeError``
    mid-render. We leave already-UTF-8 streams alone, and fall back silently
    when a stream doesn't support ``reconfigure`` (e.g. pytest's capture).
    """
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if not enc or enc.lower().replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_fix_console_encoding()


app = typer.Typer(
    name="chess-vol",
    help="Chess Volatility Bar — measure how sharp/volatile a chess position is.",
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# Engine factory — overridable in tests                                       #
# --------------------------------------------------------------------------- #


EngineFactory = Callable[[], AbstractContextManager[EngineLike]]


@contextmanager
def _default_engine_factory() -> Iterator[EngineLike]:
    """Open a real Stockfish process as a context manager."""
    with Engine() as engine:
        yield engine


#: Tests monkey-patch this to inject a :class:`FakeEngine` instead of Stockfish.
ENGINE_FACTORY: EngineFactory = _default_engine_factory


# --------------------------------------------------------------------------- #
# Rendering helpers                                                           #
# --------------------------------------------------------------------------- #


_COLOR_TO_TYPER: dict[str, str] = {
    "low": typer.colors.GREEN,
    "medium": typer.colors.YELLOW,
    "high": typer.colors.RED,
}


def ascii_bar(score: float | None, width: int = 10) -> str:
    """Render a 0-100 score as a unicode bar. ``None`` → all-dashes."""
    if score is None:
        return "—" * width
    clamped = max(0.0, min(100.0, score))
    filled = round(width * clamped / 100.0)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _format_score(score: float | None) -> str:
    return f"{score:5.1f}" if score is not None else "  —  "


def _styled_bar(score: float | None, *, use_color: bool, width: int = 10) -> str:
    """ASCII bar plus numeric score; colored per §3.6 thresholds."""
    bar = ascii_bar(score, width=width)
    text = f"{bar} {_format_score(score)}"
    if score is None or not use_color:
        return text
    fg = _COLOR_TO_TYPER.get(color_for(score), typer.colors.WHITE)
    return typer.style(text, fg=fg)


def _format_deep_split(vol: VolatilityResult) -> str:
    """Render the "local X  reply +Y  → total Z" split line (deep mode).

    Both components are shown in raw-cp space (before the exp normalization)
    because the *deep* score on the bar is already the normalized total.
    """
    if vol.raw_cp is None or vol.local_raw_cp is None:
        return ""
    local = vol.local_raw_cp
    reply = vol.raw_cp - vol.local_raw_cp
    return f"local {local:6.1f}  reply {reply:+7.1f}  → raw {vol.raw_cp:6.1f}"


def _decided_flag(vol: VolatilityResult) -> str:
    return "decided" if vol.decided else ""


def _format_ply_line(ply: PlyResult, *, deep: bool, use_color: bool) -> str:
    """One line per ply. In deep mode, appends the local/reply split."""
    vol = ply.volatility
    bar = _styled_bar(vol.score, use_color=use_color)
    reason = f" [{vol.reason}]" if vol.reason else ""
    decided = f" [{_decided_flag(vol)}]" if vol.decided else ""
    base = (
        f"ply {ply.ply:3d}  {ply.san:8s}  "
        f"eval {vol.best_eval_cp:+6d}cp  "
        f"V {bar}{reason}{decided}"
    )
    if deep:
        split = _format_deep_split(vol)
        if split:
            base += f"\n         {split}"
    return base


def _format_fen_line(fen: str, vol: VolatilityResult, *, deep: bool, use_color: bool) -> str:
    bar = _styled_bar(vol.score, use_color=use_color)
    reason = f" [{vol.reason}]" if vol.reason else ""
    decided = f" [{_decided_flag(vol)}]" if vol.decided else ""
    base = (
        f"fen {fen}\n"
        f"  eval {vol.best_eval_cp:+6d}cp  "
        f"V {bar}{reason}{decided}  "
        f"(analyses={vol.analyses}, scale={vol.scale:.3f})"
    )
    if deep:
        split = _format_deep_split(vol)
        if split:
            base += f"\n  {split}"
    return base


# --------------------------------------------------------------------------- #
# Shared option handling                                                      #
# --------------------------------------------------------------------------- #


def _resolve_recurse_depth(deep: bool, recurse_depth: int | None) -> int:
    """``--deep`` is shorthand for ``--recurse-depth 2``; they must agree if
    both are supplied explicitly."""
    if deep and recurse_depth is not None and recurse_depth == 0:
        raise typer.BadParameter(
            "--deep conflicts with --recurse-depth 0; drop one of them.",
            param_hint="--deep / --recurse-depth",
        )
    if recurse_depth is not None:
        return recurse_depth
    if deep:
        return 2
    return 0


def _use_color(no_color: bool) -> bool:
    if no_color:
        return False
    return sys.stdout.isatty()


def _echo(message: str, *, use_color: bool, err: bool = False) -> None:
    typer.echo(message, color=use_color, err=err)


def _write_report(data: dict[str, Any], output: Path) -> None:
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


@app.command()
def analyze(
    pgn_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Path to a PGN file. Only the first game is analysed.",
        ),
    ],
    depth: Annotated[
        int, typer.Option("--depth", min=1, help="Root search depth.")
    ] = DEFAULT_DEPTH,
    multipv: Annotated[
        int,
        typer.Option("--multipv", min=1, help="Number of lines to analyse per position."),
    ] = DEFAULT_MULTIPV,
    deep: Annotated[bool, typer.Option("--deep", help="Shorthand for --recurse-depth 2.")] = False,
    recurse_depth: Annotated[
        int | None,
        typer.Option(
            "--recurse-depth",
            min=0,
            help="Recursion depth for reply-volatility (Phase 1.5).",
        ),
    ] = None,
    recurse_k: Annotated[
        int, typer.Option("--recurse-k", min=1, help="Top-k moves to recurse into.")
    ] = DEFAULT_RECURSE_K,
    child_depth: Annotated[
        int,
        typer.Option("--child-depth", min=1, help="Depth used for recursive calls."),
    ] = DEFAULT_CHILD_DEPTH,
    max_plies: Annotated[
        int | None,
        typer.Option("--max-plies", min=1, help="Only analyse the first N plies."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write a JSON report to this path."),
    ] = None,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable ANSI colors.")] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress per-ply progress output.")
    ] = False,
) -> None:
    """Analyse a PGN file move-by-move and print per-ply volatility."""
    effective_recurse = _resolve_recurse_depth(deep, recurse_depth)
    use_color = _use_color(no_color)
    is_deep = effective_recurse > 0

    try:
        pgn_text = pgn_file.read_text(encoding="utf-8")
    except OSError as exc:
        _echo(f"error: could not read PGN file: {exc}", use_color=False, err=True)
        raise typer.Exit(1) from exc

    def on_progress(done: int, total: int, ply: PlyResult) -> None:
        if quiet:
            return
        line = _format_ply_line(ply, deep=is_deep, use_color=use_color)
        prefix = f"[{done:3d}/{total:3d}] "
        _echo(prefix + line, use_color=use_color)

    try:
        with ENGINE_FACTORY() as engine:
            results = analyze_pgn(
                pgn_text,
                engine,
                max_plies=max_plies,
                progress=on_progress,
                depth=depth,
                multipv=multipv,
                recurse_depth=effective_recurse,
                recurse_k=recurse_k,
                child_depth=child_depth,
            )
    except EngineNotFoundError as exc:
        _echo(f"error: {exc}", use_color=False, err=True)
        raise typer.Exit(2) from exc
    except ValueError as exc:
        _echo(f"error: {exc}", use_color=False, err=True)
        raise typer.Exit(1) from exc

    if not quiet:
        total_analyses = sum(r.volatility.analyses for r in results)
        _echo(
            f"\nAnalysed {len(results)} plies "
            f"(mode={'deep' if is_deep else 'shallow'}, "
            f"engine calls={total_analyses}).",
            use_color=use_color,
        )

    if output is not None:
        params = build_params(
            depth=depth,
            multipv=multipv,
            recurse_depth=effective_recurse,
            recurse_k=recurse_k,
            recurse_alpha=DEFAULT_RECURSE_ALPHA,
            child_depth=child_depth,
            max_plies=max_plies,
        )
        report = build_analyze_report(results, params=params)
        _write_report(dict(report), output)
        if not quiet:
            _echo(f"Wrote JSON report to {output}", use_color=use_color)


@app.command()
def fen(
    fen_str: Annotated[str, typer.Argument(metavar="FEN", help="FEN string to analyse.")],
    depth: Annotated[
        int, typer.Option("--depth", min=1, help="Root search depth.")
    ] = DEFAULT_DEPTH,
    multipv: Annotated[
        int, typer.Option("--multipv", min=1, help="Number of lines to analyse.")
    ] = DEFAULT_MULTIPV,
    deep: Annotated[bool, typer.Option("--deep", help="Shorthand for --recurse-depth 2.")] = False,
    recurse_depth: Annotated[
        int | None, typer.Option("--recurse-depth", min=0, help="Recursion depth.")
    ] = None,
    recurse_k: Annotated[
        int, typer.Option("--recurse-k", min=1, help="Top-k moves to recurse into.")
    ] = DEFAULT_RECURSE_K,
    child_depth: Annotated[
        int,
        typer.Option("--child-depth", min=1, help="Depth used for recursive calls."),
    ] = DEFAULT_CHILD_DEPTH,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write a JSON report to this path."),
    ] = None,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable ANSI colors.")] = False,
) -> None:
    """Analyse a single FEN position and print its volatility."""
    effective_recurse = _resolve_recurse_depth(deep, recurse_depth)
    use_color = _use_color(no_color)
    is_deep = effective_recurse > 0

    try:
        board = chess.Board(fen_str.strip())
    except ValueError as exc:
        _echo(f"error: invalid FEN: {exc}", use_color=False, err=True)
        raise typer.Exit(1) from exc

    try:
        with ENGINE_FACTORY() as engine:
            result = compute_volatility(
                board,
                engine,
                depth=depth,
                multipv=multipv,
                recurse_depth=effective_recurse,
                recurse_k=recurse_k,
                child_depth=child_depth,
            )
    except EngineNotFoundError as exc:
        _echo(f"error: {exc}", use_color=False, err=True)
        raise typer.Exit(2) from exc

    _echo(
        _format_fen_line(board.fen(), result, deep=is_deep, use_color=use_color),
        use_color=use_color,
    )

    if output is not None:
        params = build_params(
            depth=depth,
            multipv=multipv,
            recurse_depth=effective_recurse,
            recurse_k=recurse_k,
            recurse_alpha=DEFAULT_RECURSE_ALPHA,
            child_depth=child_depth,
        )
        report = build_fen_report(board.fen(), result, params=params)
        _write_report(dict(report), output)
        _echo(f"Wrote JSON report to {output}", use_color=use_color)


@app.command()
def serve(
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind to.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="TCP port.")] = 8000,
    reload: Annotated[
        bool,
        typer.Option("--reload", help="Restart on source changes (dev only)."),
    ] = False,
    log_level: Annotated[
        str, typer.Option("--log-level", help="Uvicorn log level.")
    ] = "info",
) -> None:
    """Launch the local web app (Phase 3).

    Requires the ``web`` extras: ``pip install -e .[web]``. The frontend is
    served at ``http://HOST:PORT/``; ``POST /analyze/fen`` and
    ``POST /analyze/pgn`` back it.
    """
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        _echo(
            "error: web extras not installed. Run `pip install -e .[web]`.",
            use_color=False,
            err=True,
        )
        raise typer.Exit(3) from exc

    uvicorn.run(
        "chess_vol.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )


# Expose a raw no-subcommand volatility json for programmatic callers.
def result_to_dict(result: VolatilityResult) -> dict[str, Any]:
    """Convenience wrapper around :func:`volatility_to_json` (public API)."""
    return dict(volatility_to_json(result))


__all__ = ["ENGINE_FACTORY", "app", "ascii_bar", "result_to_dict"]
