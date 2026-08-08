"""Maia (lc0) move selection with Stockfish fallback."""

from __future__ import annotations

import atexit
import os
import random
import shutil
import threading
from pathlib import Path
from typing import Any

import chess
import chess.engine

from server.engine import analyze


MAIA_RATINGS: tuple[int, ...] = (1100, 1300, 1500, 1700, 1900)
# Anchored to the repo root so lc0/weights resolve regardless of the CWD the
# server was started from.
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_LC0 = _REPO_ROOT / "data" / "lc0.exe"
DEFAULT_LC0_COMMAND = os.environ.get("CHESS_TRAINER_LC0", "lc0")
DEFAULT_WEIGHTS_DIR = Path(
    os.environ.get(
        "CHESS_TRAINER_MAIA_WEIGHTS_DIR",
        str(_REPO_ROOT / "data" / "maia_weights"),
    )
)
DEFAULT_MAIA_NODES = int(os.environ.get("CHESS_TRAINER_MAIA_NODES", "800"))
MAIA_STRENGTH_PROFILES: dict[int, dict[str, float]] = {
    # Lower buckets intentionally use fewer nodes + sampled top-k to feel
    # closer to human practical play, rather than deterministic engine play.
    1100: {"nodes": 35, "topk": 4, "temperature": 2.2},
    1300: {"nodes": 60, "topk": 4, "temperature": 1.8},
    1500: {"nodes": 120, "topk": 3, "temperature": 1.5},
    1700: {"nodes": 220, "topk": 2, "temperature": 1.25},
    1900: {"nodes": float(DEFAULT_MAIA_NODES), "topk": 1, "temperature": 1.0},
}


def normalize_maia_rating(value: int) -> int:
    """Snap an arbitrary rating to the nearest supported Maia bucket."""

    return min(MAIA_RATINGS, key=lambda candidate: abs(candidate - value))


def _weight_path(rating: int) -> Path:
    return DEFAULT_WEIGHTS_DIR / f"maia-{rating}.pb.gz"


def _resolved_weight_path(rating: int) -> Path | None:
    candidates = [
        DEFAULT_WEIGHTS_DIR / f"maia-{rating}.pb.gz",
        DEFAULT_WEIGHTS_DIR / f"maia-{rating}.pb" / "ckpt-40-400000.pb",
        DEFAULT_WEIGHTS_DIR / f"maia-{rating}.pb",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolved_lc0_command() -> str:
    env_value = os.environ.get("CHESS_TRAINER_LC0")
    if env_value:
        return env_value
    if DEFAULT_LOCAL_LC0.exists():
        return str(DEFAULT_LOCAL_LC0)
    return DEFAULT_LC0_COMMAND


def _command_exists(command: str) -> bool:
    command_path = Path(command)
    if command_path.exists():
        return True
    return bool(shutil.which(command))


def _has_maia_assets(rating: int) -> bool:
    return _command_exists(_resolved_lc0_command()) and _resolved_weight_path(rating) is not None


def maia_capabilities() -> dict[str, Any]:
    resolved = _resolved_lc0_command()
    available_ratings = [rating for rating in MAIA_RATINGS if _has_maia_assets(rating)]
    return {
        "lc0_command": resolved,
        "weights_dir": str(DEFAULT_WEIGHTS_DIR),
        "maia_available": bool(available_ratings),
        "available_ratings": available_ratings,
        "fallback_engine": "stockfish",
        "supported_ratings": list(MAIA_RATINGS),
    }


def choose_engine_for_rating(rating: int) -> str:
    """Return the engine that will be used for a playout."""

    return "maia" if _has_maia_assets(normalize_maia_rating(rating)) else "stockfish"


def has_maia_assets(rating: int) -> bool:
    """Public check: lc0 + weights are present for the (normalized) rating."""

    return _has_maia_assets(normalize_maia_rating(rating))


def maia_move(fen: str, maia_rating: int) -> str | None:
    """Public Maia move lookup (no Stockfish fallback). ``None`` on failure."""

    return _best_move_maia(fen, normalize_maia_rating(maia_rating))


def _policy_top_moves(engine: chess.engine.SimpleEngine, fen: str, k: int) -> list[str] | None:
    """Top-``k`` Maia policy moves from an already-open lc0 ``engine``.

    Runs a single forward pass (``nodes=1``) at ``MultiPV=k`` — the raw policy
    head, i.e. the human move distribution Maia reproduces. Returns ``None`` for
    terminal positions or engine errors.
    """

    board = chess.Board(fen)
    if board.is_game_over():
        return None
    want = max(1, min(k, board.legal_moves.count()))
    try:
        infos = engine.analyse(board, chess.engine.Limit(nodes=1), multipv=want)
    except (chess.engine.EngineError, OSError, ValueError):
        return None

    rows = infos if isinstance(infos, list) else [infos]
    moves: list[str] = []
    for row in rows:
        pv = row.get("pv")
        if pv:
            uci = pv[0].uci()
            if uci not in moves:
                moves.append(uci)
    return moves or None


class MaiaPolicyEngine:
    """Persistent lc0 process for Maia policy top-k lookups.

    Opens lc0 with the Maia weights **once** and reuses it across many
    positions, instead of the cold-start-per-call behaviour of
    :func:`maia_top_moves` (which reloads the weight file on every call — far
    too slow for scanning whole games in "Your Mistakes"). Use as a context
    manager::

        with MaiaPolicyEngine() as maia:
            topk = maia.top_moves(fen)

    ``top_moves`` mirrors :func:`maia_top_moves`'s contract: up to ``k`` UCI
    moves in policy order, or ``None`` when Maia assets are unavailable or the
    position is terminal — so callers can use it as a drop-in ``maia_topk_fn``.
    """

    def __init__(self, net: int = 1900) -> None:
        self._rating = normalize_maia_rating(net)
        self._engine: chess.engine.SimpleEngine | None = None

    def __enter__(self) -> "MaiaPolicyEngine":
        if not _has_maia_assets(self._rating):
            return self
        weight_path = _resolved_weight_path(self._rating)
        if weight_path is None:
            return self
        command = [_resolved_lc0_command(), f"--weights={weight_path}"]
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(command)
        except (chess.engine.EngineError, OSError, ValueError):
            self._engine = None
        return self

    def __exit__(self, *_exc: object) -> bool:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._engine = None
        return False

    @property
    def available(self) -> bool:
        return self._engine is not None

    def top_moves(self, fen: str, k: int = 3) -> list[str] | None:
        if self._engine is None:
            return None
        return _policy_top_moves(self._engine, fen, k)


def maia_top_moves(fen: str, net: int = 1900, k: int = 3) -> list[str] | None:
    """Return up to ``k`` moves Maia(``net``) most favours, in policy order.

    Cold-starts a fresh lc0 process per call (loading the weight file each
    time). This remains the lenient global ``maia_topk_fn`` seam wired in
    :mod:`server.main`; the long-running "Your Mistakes" generation instead
    reuses one :class:`MaiaPolicyEngine` for the whole run.

    This backs the "Your Mistakes" findability gate: a tactic is only a fair
    puzzle if its solution is among the moves a ~1900 human would actually
    consider. Returns ``None`` when Maia assets are unavailable or the position
    is terminal — callers treat ``None`` as "gate not applied" (lenient).
    """

    normalized = normalize_maia_rating(net)
    if not _has_maia_assets(normalized):
        return None
    board = chess.Board(fen)
    if board.is_game_over():
        return None
    with MaiaPolicyEngine(normalized) as engine:
        return engine.top_moves(fen, k)


def best_move(
    fen: str,
    maia_rating: int,
    fallback_depth: int = 12,
) -> tuple[str | None, str]:
    """Return (uci_move, engine_name) for the given playout position."""

    board = chess.Board(fen)
    if board.is_game_over():
        return None, choose_engine_for_rating(maia_rating)

    normalized = normalize_maia_rating(maia_rating)
    if _has_maia_assets(normalized):
        move = _best_move_maia(fen, normalized)
        if move is not None:
            return move, "maia"

    top_moves = analyze(fen, depth=fallback_depth, multipv=1).get("top_moves", [])
    if not top_moves:
        return None, "stockfish"
    return str(top_moves[0]["move"]), "stockfish"


# Long-lived lc0 processes keyed by Maia rating. Spawning lc0 (and reloading
# the weight file) for every playout/reply move took multiple seconds per ply;
# reusing one process per rating makes moves near-instant after warmup.
_PERSISTENT_LOCK = threading.Lock()
_PERSISTENT_ENGINES: dict[int, chess.engine.SimpleEngine] = {}


def _persistent_engine_locked(rating: int) -> chess.engine.SimpleEngine | None:
    engine = _PERSISTENT_ENGINES.get(rating)
    if engine is not None:
        return engine
    weight_path = _resolved_weight_path(rating)
    if weight_path is None:
        return None
    command = [_resolved_lc0_command(), f"--weights={weight_path}"]
    try:
        engine = chess.engine.SimpleEngine.popen_uci(command)
    except (chess.engine.EngineError, OSError, ValueError):
        return None
    _PERSISTENT_ENGINES[rating] = engine
    return engine


def _drop_persistent_engine_locked(rating: int) -> None:
    engine = _PERSISTENT_ENGINES.pop(rating, None)
    if engine is not None:
        try:
            engine.quit()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


def _close_persistent_engines() -> None:
    with _PERSISTENT_LOCK:
        for rating in list(_PERSISTENT_ENGINES):
            _drop_persistent_engine_locked(rating)


atexit.register(_close_persistent_engines)


def _best_move_maia(fen: str, maia_rating: int) -> str | None:
    board = chess.Board(fen)
    profile = MAIA_STRENGTH_PROFILES.get(
        maia_rating,
        {"nodes": float(DEFAULT_MAIA_NODES), "topk": 1, "temperature": 1.0},
    )
    nodes = max(1, int(profile["nodes"]))
    topk = max(1, int(profile["topk"]))

    with _PERSISTENT_LOCK:
        for _attempt in range(2):
            engine = _persistent_engine_locked(maia_rating)
            if engine is None:
                return None
            try:
                if topk == 1:
                    result = engine.play(board, chess.engine.Limit(nodes=nodes))
                    if result.move is None:
                        return None
                    return result.move.uci()
                infos = engine.analyse(
                    board,
                    chess.engine.Limit(nodes=nodes),
                    multipv=topk,
                )
            except Exception:  # engine died mid-request — restart once
                _drop_persistent_engine_locked(maia_rating)
                continue
            return _sample_uci_from_infos(infos, float(profile["temperature"]))
    return None


def _sample_uci_from_infos(
    infos: dict[str, Any] | list[dict[str, Any]],
    temperature: float,
) -> str | None:
    rows = infos if isinstance(infos, list) else [infos]
    moves: list[str] = []
    for row in rows:
        pv = row.get("pv")
        if not pv:
            continue
        moves.append(pv[0].uci())
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]

    temp = max(0.5, temperature)
    exponent = 1.0 / temp
    weights = [(1.0 / (index + 1)) ** exponent for index, _ in enumerate(moves)]
    return random.choices(moves, weights=weights, k=1)[0]
