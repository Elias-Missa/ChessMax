"""The volatility algorithm — Phase 1 (one-ply) and Phase 1.5 (recursive).

Implements the procedure described in README §3. See that section for the
formulas; this module is faithful to the named constants in :mod:`chess_vol.config`.

The public entry point is :func:`compute_volatility`. All recursion happens
through an internal helper :func:`_compute_raw` so that normalization to 0-100
occurs exactly once, at the root call (README §3.2).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import chess
import chess.polyglot

from chess_vol.config import (
    DECIDED_ALT_CP,
    DECIDED_BEST_CP,
    DEFAULT_CHILD_DEPTH,
    DEFAULT_DEPTH,
    DEFAULT_MULTIPV,
    DEFAULT_RECURSE_ALPHA,
    DEFAULT_RECURSE_DEPTH,
    DEFAULT_RECURSE_K,
    DROP_CAP,
    EVAL_SCALE_GRACE,
    EVAL_SCALE_MAX,
    EVAL_SCALE_WIDTH,
    K_DEEP,
    K_SHALLOW,
    MATE_BASE,
    MATE_MAX_N,
    MATE_STEP,
    RECAPTURE_DAMPEN,
)

# --------------------------------------------------------------------------- #
# Engine protocol                                                              #
# --------------------------------------------------------------------------- #


class EngineLike(Protocol):
    """Minimal interface volatility needs from an engine.

    :class:`chess_vol.engine.Engine` satisfies this; tests inject fakes.
    """

    def analyse(
        self,
        board: chess.Board,
        depth: int = DEFAULT_DEPTH,
        multipv: int = DEFAULT_MULTIPV,
    ) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #


def mate_to_cp(n: int) -> int:
    """Convert a mate-in-``n`` to a centipawn value per README §3.3.

    ``n > 0`` means *we* mate in ``n`` plies; ``n < 0`` means the opponent
    mates us in ``|n|`` plies. ``n = 0`` is invalid — mate-in-0 would mean
    the game is already over, which should be handled by the caller.
    """
    if n == 0:
        raise ValueError("mate_to_cp(0) is undefined; position is already mated")
    sign = 1 if n > 0 else -1
    distance = min(abs(n), MATE_MAX_N)
    return sign * (MATE_BASE - MATE_STEP * distance)


def info_to_cp(info: dict[str, Any], turn: chess.Color) -> int:
    """Convert a python-chess info dict's ``score`` to side-to-move cp.

    Mate scores are mapped through :func:`mate_to_cp`; plain cp scores are
    returned as ints from ``turn``'s perspective.
    """
    pov_score = info["score"].pov(turn)
    mate = pov_score.mate()
    if mate is not None:
        return mate_to_cp(mate)
    cp = pov_score.score()
    if cp is None:
        raise ValueError(f"Engine info has neither mate nor cp: {info!r}")
    return int(cp)


def default_weights(n: int) -> list[float]:
    """Return the default drop weights ``[1/(i-1) for i in 2..n]`` (README §3.1)."""
    if n < 2:
        return []
    return [1.0 / (i - 1) for i in range(2, n + 1)]


def default_scale_fn(e1_cp: int, e2_cp: int) -> float:
    """Eval-aware dampening factor (README §3.1).

    Returns a value in ``(0, 1]``. Uses ``min(|e_1|, |e_2|)`` so that a
    winning best with a losing backup is *not* dampened (that's a real decision).
    """
    scale_eval = min(abs(e1_cp), abs(e2_cp), EVAL_SCALE_MAX)
    excess = max(0.0, scale_eval - EVAL_SCALE_GRACE)
    ratio = excess / EVAL_SCALE_WIDTH
    return 1.0 / (1.0 + ratio * ratio)


def _is_decided(e1_cp: int, e2_cp: int) -> bool:
    """Decided-position flag per README §3.4."""
    return (
        abs(e1_cp) > DECIDED_BEST_CP and (e1_cp > 0) == (e2_cp > 0) and abs(e2_cp) > DECIDED_ALT_CP
    )


def _is_obvious_recapture(
    board: chess.Board,
    line_evals: list[tuple[int, dict[str, Any]]],
) -> bool:
    """Detect a forced-recapture position.

    Fires when the opponent's previous move was a capture on square S and the
    engine's best line begins with a recapture on the same square. In that
    setup every non-recapture alternative drops material by definition, so the
    drop pattern looks like a knife edge even though the player has no real
    choice — a false positive that this flag lets the caller dampen.

    Requires ``board.move_stack`` to be non-empty: positions loaded directly
    from a FEN have no recapture context and never trigger.
    """
    if not board.move_stack or not line_evals:
        return False
    pv = line_evals[0][1].get("pv") or []
    if not pv:
        return False
    best_move = pv[0]
    if not board.is_capture(best_move):
        return False
    last_move = board.peek()
    if last_move.to_square != best_move.to_square:
        return False
    board.pop()
    try:
        return board.is_capture(last_move)
    finally:
        board.push(last_move)


# --------------------------------------------------------------------------- #
# Result dataclass                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class TopLine:
    """A single engine line (best or alternative) returned from the root MultiPV.

    ``uci``/``san`` describe the first move of the line (the one the engine
    would actually play for that candidate); ``pv_san`` is the full principal
    variation in SAN for UI display (truncated to a small number of plies).
    """

    uci: str
    san: str
    pv_san: list[str]
    eval_cp: int


@dataclass
class VolatilityResult:
    """Output of :func:`compute_volatility`. See README §5.2."""

    score: float | None
    """Normalized 0-100 volatility, or ``None`` if undefined (see ``reason``)."""

    raw_cp: float | None
    """Un-normalized weighted total (centipawns). ``None`` when ``score`` is ``None``."""

    local_raw_cp: float | None
    """Root's own contribution to ``raw_cp`` — for UI local-vs-reply split."""

    best_eval_cp: int
    """Best-line eval, side-to-move POV, mate-translated."""

    alt_evals_cp: list[int] = field(default_factory=list)
    """Alternative-line evals (same POV, mate-translated)."""

    scale: float = 1.0
    """Eval-aware scale factor that was applied."""

    decided: bool = False
    """Whether the position is 'decided' per §3.4 (UI should dim the bar)."""

    recapture: bool = False
    """Whether the forced-recapture rule fired (V_local was multiplied by
    ``RECAPTURE_DAMPEN``). True when the opponent's last move was a capture
    and the engine's best move recaptures on the same square."""

    reason: str | None = None
    """``None`` normally; ``"only_move"``, ``"checkmate"``, or ``"stalemate"``
    when ``score`` is ``None``."""

    recurse_depth_used: int = 0
    """Actual ``recurse_depth`` used for this call (mirrors the argument)."""

    analyses: int = 0
    """Number of engine ``analyse`` calls performed (for budget verification)."""

    top_lines: list[TopLine] = field(default_factory=list)
    """Top engine lines from the root MultiPV call (best-first). Empty for
    non-root / terminal results. Children do not populate this to save work."""


# --------------------------------------------------------------------------- #
# Internal raw result                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class _RawResult:
    total_raw: float
    local_raw: float
    best_cp: int
    alts_cp: list[int]
    scale: float
    decided: bool
    reason: str | None
    analyses: int
    recapture: bool = False
    top_lines: list[TopLine] = field(default_factory=list)


_TERMINAL_RAW = _RawResult(
    total_raw=0.0,
    local_raw=0.0,
    best_cp=0,
    alts_cp=[],
    scale=1.0,
    decided=False,
    reason=None,
    analyses=0,
)

_PV_SAN_MAX_PLIES = 8
"""Upper bound on how many plies of the PV we serialize in SAN."""


# --------------------------------------------------------------------------- #
# Core engine-facing computation                                               #
# --------------------------------------------------------------------------- #


WeightsFn = Callable[[int], list[float]]
ScaleFn = Callable[[int, int], float]


def _build_top_lines(
    board: chess.Board,
    line_evals: list[tuple[int, dict[str, Any]]],
) -> list[TopLine]:
    """Build ``TopLine`` entries for each MultiPV line on ``board``.

    ``line_evals`` must already be sorted best-first and each info must carry
    a ``pv`` list of :class:`chess.Move` (empty pv → the line is skipped).
    SAN generation walks a throwaway board copy so ``board`` is untouched.
    """
    lines: list[TopLine] = []
    for cp, info in line_evals:
        pv = info.get("pv") or []
        if not pv:
            continue
        first = pv[0]
        uci = first.uci()
        scratch = board.copy(stack=False)
        pv_san: list[str] = []
        try:
            first_san = scratch.san(first)
        except (ValueError, AssertionError):
            continue
        pv_san.append(first_san)
        scratch.push(first)
        for mv in pv[1:_PV_SAN_MAX_PLIES]:
            try:
                san = scratch.san(mv)
            except (ValueError, AssertionError):
                break
            pv_san.append(san)
            scratch.push(mv)
        lines.append(TopLine(uci=uci, san=first_san, pv_san=pv_san, eval_cp=int(cp)))
    return lines


def _compute_local(
    evals: list[int],
    weights_fn: WeightsFn,
    scale_fn: ScaleFn,
) -> tuple[float, float, bool]:
    """Given mate-translated evals (sorted best-first), return
    ``(V_local_raw, scale, decided)``.

    The eval list must have at least 2 entries; callers handle 0-1 cases.
    """
    e1 = evals[0]
    e2 = evals[1]
    n = len(evals)

    weights = weights_fn(n)
    if len(weights) != n - 1:
        raise ValueError(f"weights_fn({n}) returned {len(weights)} weights; expected {n - 1}")

    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError(f"weights_fn({n}) produced non-positive sum {weight_sum}")

    drops = [min(e1 - e_i, DROP_CAP) for e_i in evals[1:]]
    weighted = sum(w * d for w, d in zip(weights, drops, strict=True))
    weighted_sum = weighted / weight_sum

    scale = scale_fn(e1, e2)
    v_local_raw = scale * weighted_sum
    decided = _is_decided(e1, e2)
    return v_local_raw, scale, decided


def _compute_raw(
    board: chess.Board,
    engine: EngineLike,
    depth: int,
    multipv: int,
    recurse_depth: int,
    recurse_k: int,
    recurse_alpha: float,
    child_depth: int,
    weights_fn: WeightsFn,
    scale_fn: ScaleFn,
    *,
    build_top_lines: bool = False,
) -> _RawResult:
    """Recursive raw computation. Does NOT normalize; that happens only at root."""

    # Terminal checks (README §3.5).
    if board.is_checkmate():
        return _RawResult(
            total_raw=0.0,
            local_raw=0.0,
            best_cp=0,
            alts_cp=[],
            scale=1.0,
            decided=False,
            reason="checkmate",
            analyses=0,
        )
    if board.is_stalemate() or board.is_insufficient_material():
        # Only stalemate collapses the bar to "—"; bare insufficient material is
        # handled the same structurally (0 volatility, no recursion). Optional
        # draw *claims* (threefold / 50-move) are NOT terminal — play continues
        # unless someone claims, so the position must be analyzed normally.
        terminal_reason: str | None = "stalemate" if board.is_stalemate() else None
        return _RawResult(
            total_raw=0.0,
            local_raw=0.0,
            best_cp=0,
            alts_cp=[],
            scale=1.0,
            decided=False,
            reason=terminal_reason,
            analyses=0,
        )

    legal_moves = list(board.legal_moves)
    legal_count = len(legal_moves)
    effective_multipv = max(1, min(multipv, legal_count))

    infos = engine.analyse(board, depth=depth, multipv=effective_multipv)
    analyses = 1
    turn = board.turn

    # Convert every line to cp from side-to-move POV, then sort best-first.
    line_evals: list[tuple[int, dict[str, Any]]] = [
        (info_to_cp(info, turn), info) for info in infos
    ]
    line_evals.sort(key=lambda pair: pair[0], reverse=True)
    evals = [cp for cp, _ in line_evals]
    best_cp = evals[0] if evals else 0
    alts_cp = evals[1:]
    top_lines = _build_top_lines(board, line_evals) if build_top_lines else []

    # Only-legal-move case: local V is undefined / treated as 0. The engine can
    # also return fewer MultiPV lines than requested (e.g. shallow depths or
    # near-mate positions); without at least two evals local V is undefined too.
    reason: str | None
    if legal_count == 1 or len(evals) < 2:
        local_raw = 0.0
        scale = 1.0
        decided = False
        reason = "only_move"
    else:
        local_raw, scale, decided = _compute_local(evals, weights_fn, scale_fn)
        reason = None

    recapture = _is_obvious_recapture(board, line_evals)
    if recapture:
        local_raw *= RECAPTURE_DAMPEN

    total_raw = local_raw

    if recurse_depth > 0:
        # Top-k candidate moves from the same MultiPV result (no extra engine call).
        k = max(1, min(recurse_k, len(line_evals)))
        top_moves: list[chess.Move] = []
        for _, info in line_evals[:k]:
            pv = info.get("pv")
            if pv:
                top_moves.append(pv[0])
        if not top_moves and legal_count >= 1:
            # Fallback: synthetic engine / missing pv; use legal moves.
            top_moves = legal_moves[:k]

        child_raws: list[float] = []
        for move in top_moves:
            board.push(move)
            try:
                child = _compute_raw(
                    board=board,
                    engine=engine,
                    depth=child_depth,
                    multipv=multipv,
                    recurse_depth=recurse_depth - 1,
                    recurse_k=recurse_k,
                    recurse_alpha=recurse_alpha,
                    child_depth=child_depth,
                    weights_fn=weights_fn,
                    scale_fn=scale_fn,
                )
            finally:
                board.pop()
            child_raws.append(child.total_raw)
            analyses += child.analyses

        if child_raws:
            mean_child = sum(child_raws) / len(child_raws)
            total_raw += recurse_alpha * mean_child

    return _RawResult(
        total_raw=total_raw,
        local_raw=local_raw,
        best_cp=best_cp,
        alts_cp=alts_cp,
        scale=scale,
        decided=decided,
        reason=reason,
        analyses=analyses,
        recapture=recapture,
        top_lines=top_lines,
    )


class _MemoEngine:
    """Per-call transposition memo in front of an engine.

    Scoped to a single :func:`compute_volatility` call, so nothing leaks
    between positions. It sits *below* the ``analyses`` counter, which keeps
    reporting the recursion's logical budget rather than how many searches the
    memo happened to save.
    """

    __slots__ = ("_inner", "_seen")

    def __init__(self, inner: EngineLike) -> None:
        self._inner = inner
        self._seen: dict[tuple[int, int, int], list[dict[str, Any]]] = {}

    def analyse(
        self,
        board: chess.Board,
        depth: int = DEFAULT_DEPTH,
        multipv: int = DEFAULT_MULTIPV,
    ) -> list[dict[str, Any]]:
        key = (chess.polyglot.zobrist_hash(board), depth, multipv)
        hit = self._seen.get(key)
        if hit is not None:
            return hit
        infos = self._inner.analyse(board, depth=depth, multipv=multipv)
        self._seen[key] = infos
        return infos


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


def compute_volatility(
    board: chess.Board,
    engine: EngineLike,
    depth: int = DEFAULT_DEPTH,
    multipv: int = DEFAULT_MULTIPV,
    *,
    recurse_depth: int = DEFAULT_RECURSE_DEPTH,
    recurse_k: int = DEFAULT_RECURSE_K,
    recurse_alpha: float = DEFAULT_RECURSE_ALPHA,
    child_depth: int = DEFAULT_CHILD_DEPTH,
    weights: WeightsFn | None = None,
    scale_fn: ScaleFn | None = None,
    k: float | None = None,
) -> VolatilityResult:
    """Compute the volatility score for ``board``.

    Parameters
    ----------
    board:
        Position to evaluate. Not mutated on return (legal-move recursion
        pushes/pops internally and always restores).
    engine:
        Any object implementing :class:`EngineLike`. Reused across recursion
        (README §6 — never open a fresh Stockfish per level).
    depth, multipv:
        Stockfish analysis parameters for the root. Default depth=18, multipv=6.
    recurse_depth:
        ``0`` → pure one-ply Phase 1 behavior (the default). ``>= 1`` enables
        Phase 1.5 reply-volatility recursion (README §3.2).
    recurse_k:
        Number of top candidate moves to recurse into at each level.
    recurse_alpha:
        Weight applied to the mean child volatility at each level.
    child_depth:
        Depth passed to recursive calls. Typically < ``depth`` — children are
        sensors, not oracles.
    weights:
        Optional drop-weight function. Defaults to ``default_weights``.
    scale_fn:
        Optional eval-aware scale function. Defaults to ``default_scale_fn``.
    k:
        Optional normalization constant override. Defaults to ``K_SHALLOW``
        when ``recurse_depth == 0`` else ``K_DEEP`` (README §3.2).
    """
    if depth < 1:
        raise ValueError(f"depth must be >= 1, got {depth}")
    if multipv < 1:
        raise ValueError(f"multipv must be >= 1, got {multipv}")
    if recurse_depth < 0:
        raise ValueError(f"recurse_depth must be >= 0, got {recurse_depth}")
    if recurse_k < 1:
        raise ValueError(f"recurse_k must be >= 1, got {recurse_k}")
    if child_depth < 1:
        raise ValueError(f"child_depth must be >= 1, got {child_depth}")

    weights_fn = weights if weights is not None else default_weights
    s_fn = scale_fn if scale_fn is not None else default_scale_fn
    k_value = k if k is not None else (K_SHALLOW if recurse_depth == 0 else K_DEEP)

    # Deep mode walks the top-k tree, where move orders transpose constantly
    # (…Nf3 d4 and …d4 Nf3 reach the same position). One analysis per distinct
    # position per budget is enough — the engine is deterministic for a given
    # position, so this returns exactly what a re-search would.
    search_engine = _MemoEngine(engine) if recurse_depth > 0 else engine

    raw = _compute_raw(
        board=board,
        engine=search_engine,
        depth=depth,
        multipv=multipv,
        recurse_depth=recurse_depth,
        recurse_k=recurse_k,
        recurse_alpha=recurse_alpha,
        child_depth=child_depth,
        weights_fn=weights_fn,
        scale_fn=s_fn,
        build_top_lines=True,
    )

    # Root-level only_move / terminal short-circuit (README §3.5).
    if raw.reason is not None:
        return VolatilityResult(
            score=None,
            raw_cp=None,
            local_raw_cp=None,
            best_eval_cp=raw.best_cp,
            alt_evals_cp=raw.alts_cp,
            scale=raw.scale,
            decided=raw.decided,
            recapture=raw.recapture,
            reason=raw.reason,
            recurse_depth_used=recurse_depth,
            analyses=raw.analyses,
            top_lines=raw.top_lines,
        )

    score = 100.0 * (1.0 - math.exp(-raw.total_raw / k_value))
    return VolatilityResult(
        score=score,
        raw_cp=raw.total_raw,
        local_raw_cp=raw.local_raw,
        best_eval_cp=raw.best_cp,
        alt_evals_cp=raw.alts_cp,
        scale=raw.scale,
        decided=raw.decided,
        recapture=raw.recapture,
        reason=None,
        recurse_depth_used=recurse_depth,
        analyses=raw.analyses,
        top_lines=raw.top_lines,
    )
