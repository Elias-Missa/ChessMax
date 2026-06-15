"""Context-managed Stockfish wrapper.

Path resolution order (README §2):
    1. Explicit ``path`` argument.
    2. ``STOCKFISH_PATH`` environment variable.
    3. ``shutil.which("stockfish")`` / ``shutil.which("stockfish.exe")``.
    4. Known install locations (Windows, macOS, Linux).

If none of these resolve to an existing executable, ``EngineNotFoundError``
is raised with install instructions.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import chess
import chess.engine

from chess_vol.config import DEFAULT_DEPTH, DEFAULT_MULTIPV


class EngineNotFoundError(RuntimeError):
    """Raised when a Stockfish binary cannot be located on the system."""


_UNIX_CANDIDATES: tuple[str, ...] = (
    "/usr/local/bin/stockfish",
    "/usr/bin/stockfish",
    "/opt/homebrew/bin/stockfish",
    "/opt/local/bin/stockfish",
)


def _windows_candidates() -> tuple[str, ...]:
    candidates: list[str] = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env_var)
        if not base:
            continue
        candidates.extend(
            [
                str(Path(base) / "Stockfish" / "stockfish.exe"),
                str(Path(base) / "Stockfish" / "stockfish-windows-x86-64-avx2.exe"),
                str(Path(base) / "Programs" / "Stockfish" / "stockfish.exe"),
                str(Path(base) / "stockfish" / "stockfish.exe"),
            ]
        )
    return tuple(candidates)


_INSTALL_HINT = (
    "Stockfish binary not found. Install it and/or set the STOCKFISH_PATH environment variable.\n"
    "  Windows : winget install stockfish  OR  choco install stockfish\n"
    "            (or download from https://stockfishchess.org/download/ and point STOCKFISH_PATH at stockfish.exe)\n"
    "  macOS   : brew install stockfish\n"
    "  Linux   : apt install stockfish  (or the equivalent for your distro)\n"
    "Auto-detection order: explicit path -> $STOCKFISH_PATH -> PATH lookup -> common install locations."
)


def _resolve_path(explicit: str | os.PathLike[str] | None) -> str:
    """Return an absolute, existing path to a Stockfish executable.

    Raises :class:`EngineNotFoundError` if none can be found.
    """
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise EngineNotFoundError(
            f"Provided Stockfish path does not exist: {explicit!r}\n\n{_INSTALL_HINT}"
        )

    env_path = os.environ.get("STOCKFISH_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return str(p)

    for name in ("stockfish", "stockfish.exe"):
        found = shutil.which(name)
        if found:
            return found

    candidates: tuple[str, ...] = (
        _windows_candidates() if sys.platform == "win32" else _UNIX_CANDIDATES
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate

    raise EngineNotFoundError(_INSTALL_HINT)


class Engine:
    """Context-managed Stockfish wrapper.

    Usage::

        with Engine() as engine:
            infos = engine.analyse(board, depth=18, multipv=6)

    The underlying ``chess.engine.SimpleEngine`` process is always closed on
    exit — even if an exception is raised mid-analysis.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._explicit_path = path
        self._engine: chess.engine.SimpleEngine | None = None
        self._resolved_path: str | None = None

    @property
    def path(self) -> str:
        """Resolved path to the Stockfish binary (available after ``__enter__``)."""
        if self._resolved_path is None:
            raise RuntimeError("Engine is not started; use it as a context manager.")
        return self._resolved_path

    def __enter__(self) -> Engine:
        self._resolved_path = _resolve_path(self._explicit_path)
        self._engine = chess.engine.SimpleEngine.popen_uci(self._resolved_path)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Shut down the engine process. Safe to call multiple times."""
        engine = self._engine
        self._engine = None
        if engine is not None:
            try:
                engine.quit()
            except chess.engine.EngineTerminatedError:
                pass
            except Exception:
                engine.close()

    def analyse(
        self,
        board: chess.Board,
        depth: int = DEFAULT_DEPTH,
        multipv: int = DEFAULT_MULTIPV,
    ) -> list[dict[str, Any]]:
        """Analyse ``board`` to ``depth`` plies with MultiPV = ``multipv``.

        Returns the per-line info dictionaries from ``python-chess``, sorted
        best-first (``multipv=1`` first).
        """
        if self._engine is None:
            raise RuntimeError("Engine is not started; use it as a context manager.")
        if multipv < 1:
            raise ValueError(f"multipv must be >= 1, got {multipv}")
        if depth < 1:
            raise ValueError(f"depth must be >= 1, got {depth}")
        raw = self._engine.analyse(
            board,
            chess.engine.Limit(depth=depth),
            multipv=multipv,
        )
        info_list: list[dict[str, Any]] = (
            [dict(item) for item in raw] if isinstance(raw, list) else [dict(raw)]
        )
        info_list.sort(key=lambda info: int(info.get("multipv", 1)))
        return info_list
