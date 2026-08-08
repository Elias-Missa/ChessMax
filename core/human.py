"""Rating-conditioned human-move policy (Game Review 2.0 spec §3.2).

``policy(fen, rating, moves) -> {move: P(move | position, rating)}`` for the
requested moves only. Findability builds its curves by calling this at each
rating in the grid.

Backends
--------
* :func:`uniform_policy` — a deterministic, engine-free fallback. Pure and
  always available; used by the test-suite and as a safe default so findability
  never crashes when no human model is installed.
* :class:`MaiaPolicy` — the production wrapper. Drives lc0 with rating-selected
  Maia weights.

**Spec preference (§3.2):** the intended model is a single *rating-conditioned*
network — **Maia-3** (CSSLab), or Maia-2 as a fallback — because the nine
original per-rating nets are documented as incoherent across ratings. This repo
ships only the per-rating nets, so :class:`MaiaPolicy` selects the nearest net
per requested rating and derives a distribution from its value head as a
**documented approximation**; swap in a Maia-2/3 policy head when available
without touching :mod:`core.findability`.
"""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

import chess

# The seven rating anchors findability samples (spec §3.1).
RATING_GRID: tuple[int, ...] = (800, 1100, 1400, 1700, 2000, 2300, 2600)

# Per-rating Maia nets shipped with this repo (lc0 weight files).
_MAIA_NETS: tuple[int, ...] = (1100, 1300, 1500, 1700, 1900)


def uniform_policy(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
    """Uniform distribution over ``moves`` (engine-free fallback).

    Rating-independent by construction, so it produces flat curves — useful for
    tests and as a crash-proof default, not for real scoring.
    """
    if not moves:
        return {}
    p = 1.0 / len(moves)
    return {move: p for move in moves}


def softmax_policy(
    scores: dict[chess.Move, float],
    moves: list[chess.Move],
    *,
    temperature: float = 1.0,
) -> dict[chess.Move, float]:
    """Softmax a per-move score map into a distribution over ``moves``.

    Missing moves get the minimum observed score (so an unlisted move is treated
    as weak, not impossible). ``temperature`` > 1 flattens; < 1 sharpens.
    """
    if not moves:
        return {}
    if not scores:
        return uniform_policy("", 0, moves)
    floor = min(scores.values())
    temp = max(1e-3, temperature)
    logits = {m: (scores.get(m, floor)) / temp for m in moves}
    top = max(logits.values())
    exps = {m: math.exp(v - top) for m, v in logits.items()}
    total = sum(exps.values())
    if total <= 0:
        return uniform_policy("", 0, moves)
    return {m: e / total for m, e in exps.items()}


# --------------------------------------------------------------------------- #
# Maia lc0 backend (production; integration-only — needs lc0 + weights)         #
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _lc0_command() -> str | None:
    env = os.environ.get("CHESS_TRAINER_LC0")
    if env:
        return env
    local = _repo_root() / "data" / "lc0.exe"
    if local.exists():
        return str(local)
    return shutil.which("lc0")


def _weights_dir() -> Path:
    return Path(
        os.environ.get(
            "CHESS_TRAINER_MAIA_WEIGHTS_DIR", str(_repo_root() / "data" / "maia_weights")
        )
    )


def _weight_path(net: int) -> Path | None:
    base = _weights_dir()
    for candidate in (
        base / f"maia-{net}.pb.gz",
        base / f"maia-{net}.pb" / "ckpt-40-400000.pb",
        base / f"maia-{net}.pb",
    ):
        if candidate.exists():
            return candidate
    return None


def _available_nets() -> list[int]:
    command = _lc0_command()
    if not command:
        return []
    if not (Path(command).exists() or shutil.which(command)):
        return []
    return [net for net in _MAIA_NETS if _weight_path(net) is not None]


def _nearest_net(rating: int, nets: list[int]) -> int:
    return min(nets, key=lambda net: abs(net - rating))


class MaiaPolicy:
    """lc0-backed rating-conditioned policy (a documented approximation).

    Keeps one lc0 process per Maia net, reused across positions. For a requested
    rating it selects the nearest available net, runs a single forward pass
    (``nodes=1`` — the policy-shaped move ordering, not a deep search) at
    MultiPV over the requested moves, and softmaxes the returned value scores
    into a distribution. Returns ``{}`` on any failure so findability degrades
    to "engine-only" rather than raising.

    Use as a context manager to guarantee the lc0 processes are closed::

        with MaiaPolicy() as policy:
            dist = policy(fen, 1500, moves)
    """

    def __init__(self, temperature: float = 1.0) -> None:
        self._temperature = temperature
        self._nets = _available_nets()
        self._engines: dict[int, object] = {}

    @property
    def available(self) -> bool:
        return bool(self._nets)

    def __enter__(self) -> "MaiaPolicy":
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        import chess.engine

        for engine in self._engines.values():
            try:
                engine.quit()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — best-effort teardown
                try:
                    engine.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
        self._engines.clear()

    def _engine_for(self, net: int):  # noqa: ANN202 — chess.engine.SimpleEngine
        import chess.engine

        engine = self._engines.get(net)
        if engine is not None:
            return engine
        weight = _weight_path(net)
        if weight is None:
            return None
        try:
            engine = chess.engine.SimpleEngine.popen_uci([_lc0_command(), f"--weights={weight}"])
        except (chess.engine.EngineError, OSError, ValueError):
            return None
        self._engines[net] = engine
        return engine

    def __call__(
        self, fen: str, rating: int, moves: list[chess.Move]
    ) -> dict[chess.Move, float]:
        import chess.engine

        if not moves or not self._nets:
            return {}
        board = chess.Board(fen)
        if board.is_game_over():
            return {}
        net = _nearest_net(rating, self._nets)
        engine = self._engine_for(net)
        if engine is None:
            return {}
        want = min(len(moves), board.legal_moves.count())
        try:
            infos = engine.analyse(board, chess.engine.Limit(nodes=1), multipv=want)
        except (chess.engine.EngineError, OSError, ValueError):
            return {}
        rows = infos if isinstance(infos, list) else [infos]
        scores: dict[chess.Move, float] = {}
        for row in rows:
            pv = row.get("pv")
            score = row.get("score")
            if not pv or score is None:
                continue
            cp = score.pov(board.turn).score(mate_score=100_000)
            scores[pv[0]] = float(cp if cp is not None else 0.0)
        return softmax_policy(scores, moves, temperature=self._temperature)


# Maia-2 weights are ~large and slow to load; share one loaded model across every
# Maia2Policy instance (and thus across review requests) keyed by (type, device).
_MAIA2_CACHE: dict[tuple[str, str], tuple[object, object]] = {}


def _load_maia2(game_type: str, device: str) -> tuple[object, object]:
    key = (game_type, device)
    cached = _MAIA2_CACHE.get(key)
    if cached is not None:
        return cached
    from maia2 import inference, model

    loaded = (model.from_pretrained(type=game_type, device=device), inference.prepare())
    _MAIA2_CACHE[key] = loaded
    return loaded


class Maia2Policy:
    """Rating-conditioned **Maia-2** policy head (CSSLab) — the spec's intended
    backend (§3.2).

    Unlike the nine incoherent per-rating Maia-1 nets, Maia-2 is a *single* model
    conditioned on player Elo, so its curves are coherent across rating by
    construction. Loaded once (weights cached under ``maia2_models/``) and reused.
    Matches the :data:`core.findability.PolicyFn` contract
    ``(fen, rating, moves) -> {move: P}`` and returns the true policy head (move
    probabilities that sum to 1 over legal moves), not a value-head approximation.

    Optional dependency: :attr:`available` is ``False`` when the ``maia2`` package
    is not installed, so callers fall back to Maia-1 / uniform. Elo is clamped to
    the model's trained ``[elo_min, elo_max]`` range.
    """

    def __init__(
        self,
        *,
        game_type: str = "rapid",
        device: str = "cpu",
        elo_oppo: int | None = None,
        elo_min: int = 1100,
        elo_max: int = 2000,
    ) -> None:
        self._game_type = game_type
        self._device = device
        self._elo_oppo = elo_oppo
        self._elo_min = elo_min
        self._elo_max = elo_max
        self._model: object | None = None
        self._prepared: object | None = None

    @property
    def available(self) -> bool:
        try:
            import maia2  # noqa: F401
        except Exception:  # noqa: BLE001 — treat any import failure as "absent"
            return False
        return True

    def _load(self) -> None:
        self._model, self._prepared = _load_maia2(self._game_type, self._device)

    def __enter__(self) -> "Maia2Policy":
        if self._model is None:
            self._load()
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def _clamp(self, rating: int) -> int:
        return max(self._elo_min, min(self._elo_max, int(rating)))

    def __call__(
        self, fen: str, rating: int, moves: list[chess.Move]
    ) -> dict[chess.Move, float]:
        if self._model is None:
            try:
                self._load()
            except Exception:  # noqa: BLE001 — missing backend -> engine-only, never raise
                return {}
        from maia2 import inference

        elo_self = self._clamp(rating)
        elo_oppo = self._clamp(self._elo_oppo if self._elo_oppo is not None else rating)
        try:
            move_probs, _ = inference.inference_each(
                self._model, self._prepared, fen, elo_self, elo_oppo
            )
        except Exception:  # noqa: BLE001 — degrade to engine-only, never raise
            return {}
        return {move: float(move_probs.get(move.uci(), 0.0)) for move in moves}


def default_policy(temperature: float = 1.0) -> MaiaPolicy | None:
    """Return a :class:`MaiaPolicy` if Maia assets are installed, else ``None``.

    Callers treat ``None`` as "no human model" and emit ``findability: null``,
    mirroring how the trainer treats a missing Maia (lenient gate).
    """
    policy = MaiaPolicy(temperature=temperature)
    return policy if policy.available else None


def best_available_policy() -> "Maia2Policy | None":
    """Best installed human model for findability: Maia-2 if present, else ``None``.

    Maia-2 is the coherent rating-conditioned policy head (spec §3.2 preference)
    and, per this repo's Phase 3 calibration, is *strictly* better than the
    per-rating Maia-1 value-head approximation for findability. When Maia-2 is
    absent we return ``None`` so the review gates findability off (``null``)
    rather than surfacing a noisy signal — the honest default. Wire this into
    ``chess_vol.server.POLICY_FACTORY`` to make the live review use Maia-2.
    """
    policy = Maia2Policy()
    return policy if policy.available else None


__all__ = [
    "RATING_GRID",
    "Maia2Policy",
    "MaiaPolicy",
    "best_available_policy",
    "default_policy",
    "softmax_policy",
    "uniform_policy",
]
