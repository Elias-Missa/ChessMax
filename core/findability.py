"""Findability: how humanly possible was the engine's best move to find?

Game Review 2.0 spec §3. Findability is **not a number, it is a curve** over
rating. For a position we evaluate a rating-conditioned human model at a grid of
ratings, producing two monotone curves:

    C_star(r) — P(a player rated r plays the engine move m*)
    C_A(r)    — P(a player rated r plays *any* acceptable move) — the headline

Every scalar the UI shows is a functional of those curves:

    findability (0-100, rating-free)  = 100 * clamp(1 - (R_find - 600)/2000, 0, 1)
                                        where R_find = C_A^-1(0.5)
    personal    (needs user_rating)   = C_A(R_user) * time_modifier
    alternate move                    = argmax pi_tilde(m) over A, when m* is
                                        effectively unfindable for the user

The human prior is calculation-reweighted (:mod:`core.features`) before the
curves are built, and each curve is passed through PAVA isotonic regression so
we never inherit local zigzag from an incoherent model (spec §3.6).

This module is **pure** given a ``policy_fn`` — inject a fake in tests, wire
:class:`core.human.MaiaPolicy` in production.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import chess

from core.acceptable import acceptable_set, tau_for
from core.evaluation import clamp_mate_cp, delta_w, win_prob, win_prob_cp
from core.features import MoveEval, compute_features, reweight

# ``policy_fn(fen, rating, moves) -> {move: P(move | position, rating)}`` for the
# requested moves only. See :func:`core.human.uniform_policy` for the contract.
PolicyFn = Callable[[str, int, list[chess.Move]], dict[chess.Move, float]]

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "findability.json"


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FindabilityConstants:
    """The placeholder constants of spec §3.3, loaded from JSON so Phase 3
    refitting never requires a code change."""

    tau: float = 2.5
    score_mode: str = "mean_ca"
    """How the 0-100 score is derived from the C_A curve. ``"mean_ca"`` (default)
    = 100·mean(C_A over grid) — censoring-free; Phase 3 found this recovers ~0.09
    of rating-correlation that ``"rfind"`` (100·clamp(1-(R_find-600)/span), the
    original spec form) loses to the 0.5-crossing censoring. ``r_find`` is still
    reported either way."""
    tau_volatility_enabled: bool = False
    tau_volatility_min: float = 1.0
    tau_volatility_max: float = 6.0
    beta: float = 1.2
    w_dep: float = 0.45
    w_forc: float = 0.35
    w_narr: float = 0.20
    q_weight: float = 0.6
    dep_offset: int = 1
    dep_span: int = 14
    forc_plies: int = 4
    narr_opponent_nodes: int = 3
    q_min_pv_ply: int = 3
    q_delta_mult: float = 3.0
    multipv: int = 8
    nodes: int = 2_500_000
    rating_grid: tuple[int, ...] = (800, 1100, 1400, 1700, 2000, 2300, 2600)
    gate_win_prob_high: float = 0.97
    gate_win_prob_low: float = 0.03
    score_floor_rating: int = 600
    score_span: int = 2000
    alt_cstar_max: float = 0.15
    alt_pi_floor: float = 0.20
    alt_pi_mult: float = 3.0
    t_mod_min: float = 0.5
    t_mod_max: float = 1.3
    bands: tuple[tuple[int, str], ...] = (
        (90, "Obvious"),
        (70, "Natural"),
        (45, "Needs thought"),
        (20, "Hard"),
        (0, "Engine-only"),
    )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FindabilityConstants":
        weights = data.get("calc_weights", {}) or {}
        engine = data.get("engine", {}) or {}
        bands_raw = data.get("bands") or []
        bands = tuple(
            (int(b["min"]), str(b["label"]))
            for b in sorted(bands_raw, key=lambda b: int(b["min"]), reverse=True)
        ) or cls.bands
        base = cls()
        tau_vol = data.get("tau_volatility", {}) or {}
        return cls(
            tau=float(data.get("tau", base.tau)),
            score_mode=str(data.get("score_mode", base.score_mode)),
            tau_volatility_enabled=bool(tau_vol.get("enabled", base.tau_volatility_enabled)),
            tau_volatility_min=float(tau_vol.get("tau_min", base.tau_volatility_min)),
            tau_volatility_max=float(tau_vol.get("tau_max", base.tau_volatility_max)),
            beta=float(data.get("beta", base.beta)),
            w_dep=float(weights.get("dep", base.w_dep)),
            w_forc=float(weights.get("forc", base.w_forc)),
            w_narr=float(weights.get("narr", base.w_narr)),
            q_weight=float(data.get("q_weight", base.q_weight)),
            dep_offset=int(data.get("dep_offset", base.dep_offset)),
            dep_span=int(data.get("dep_span", base.dep_span)),
            forc_plies=int(data.get("forc_plies", base.forc_plies)),
            narr_opponent_nodes=int(data.get("narr_opponent_nodes", base.narr_opponent_nodes)),
            q_min_pv_ply=int(data.get("q_min_pv_ply", base.q_min_pv_ply)),
            q_delta_mult=float(data.get("q_delta_mult", base.q_delta_mult)),
            multipv=int(engine.get("multipv", base.multipv)),
            nodes=int(engine.get("nodes", base.nodes)),
            rating_grid=tuple(int(r) for r in data.get("rating_grid", base.rating_grid)),
            gate_win_prob_high=float(data.get("gate_win_prob_high", base.gate_win_prob_high)),
            gate_win_prob_low=float(data.get("gate_win_prob_low", base.gate_win_prob_low)),
            score_floor_rating=int(data.get("score_floor_rating", base.score_floor_rating)),
            score_span=int(data.get("score_span", base.score_span)),
            alt_cstar_max=float(data.get("alt_cstar_max", base.alt_cstar_max)),
            alt_pi_floor=float(data.get("alt_pi_floor", base.alt_pi_floor)),
            alt_pi_mult=float(data.get("alt_pi_mult", base.alt_pi_mult)),
            t_mod_min=float(data.get("t_mod_min", base.t_mod_min)),
            t_mod_max=float(data.get("t_mod_max", base.t_mod_max)),
            bands=bands,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "FindabilityConstants":
        target = Path(path) if path is not None else _CONSTANTS_PATH
        with open(target, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Alternate:
    """The strongest move a human could realistically have played instead."""

    uci: str
    san: str
    delta_w: float
    pi: float


@dataclass(frozen=True)
class PositionFindability:
    """Findability verdict for one position (spec §7 ``findability`` payload)."""

    score: int
    """0-100, rating-free, time-free. 100 = obvious, 0 = engine-only."""
    r_find: int | None
    """Rating at which C_A crosses 0.5; ``None`` when the curve never does."""
    band: str
    personal: float | None
    """C_A(R_user) * time-modifier; ``None`` when no user rating supplied."""
    personal_star: float | None
    """C_star(R_user) — drives the alternate-move test; ``None`` without a rating."""
    curve: list[tuple[int, float]]
    """(rating, C_A) pairs after isotonic regression — the graph."""
    star_curve: list[tuple[int, float]] = field(default_factory=list)
    alternate: Alternate | None = None
    forced: bool = False


# --------------------------------------------------------------------------- #
# Monotone-curve utilities                                                     #
# --------------------------------------------------------------------------- #


def pava(values: list[float], weights: list[float] | None = None) -> list[float]:
    """Pool-Adjacent-Violators isotonic regression (non-decreasing).

    Returns the monotone-increasing least-squares fit to ``values``. Real
    players improve monotonically with rating, so this guarantees the curve
    does too (spec §3.6).
    """
    n = len(values)
    if n == 0:
        return []
    w = list(weights) if weights is not None else [1.0] * n
    v_stack: list[float] = []
    w_stack: list[float] = []
    count_stack: list[int] = []
    for i in range(n):
        v = values[i]
        wt = w[i]
        count = 1
        while v_stack and v_stack[-1] > v:
            pv = v_stack.pop()
            pw = w_stack.pop()
            pc = count_stack.pop()
            v = (pv * pw + v * wt) / (pw + wt)
            wt = pw + wt
            count += pc
        v_stack.append(v)
        w_stack.append(wt)
        count_stack.append(count)
    out: list[float] = []
    for value, count in zip(v_stack, count_stack, strict=True):
        out.extend([value] * count)
    return out


def invert_monotone(grid: list[int], values: list[float], target: float) -> float | None:
    """Smallest ``x`` where a non-decreasing ``values`` crosses ``target``.

    Linear interpolation between the two bracketing grid points. Returns
    ``None`` when the curve never reaches ``target`` within the grid — the
    spec's "do not extrapolate beyond the model's trained range" rule. When the
    curve is already ``>= target`` at the first grid point, returns that point.
    """
    if not grid or values[-1] < target:
        return None
    if values[0] >= target:
        return float(grid[0])
    for i in range(len(grid) - 1):
        lo, hi = values[i], values[i + 1]
        if lo < target <= hi:
            if hi == lo:
                return float(grid[i + 1])
            frac = (target - lo) / (hi - lo)
            return grid[i] + frac * (grid[i + 1] - grid[i])
    return float(grid[-1])


def interp_at(grid: list[int], values: list[float], x: float) -> float:
    """Linear interpolation of ``values`` over ``grid`` at ``x`` (clamped to range)."""
    if not grid:
        return 0.0
    if x <= grid[0]:
        return values[0]
    if x >= grid[-1]:
        return values[-1]
    for i in range(len(grid) - 1):
        if grid[i] <= x <= grid[i + 1]:
            span = grid[i + 1] - grid[i]
            if span == 0:
                return values[i]
            frac = (x - grid[i]) / span
            return values[i] + frac * (values[i + 1] - values[i])
    return values[-1]


# --------------------------------------------------------------------------- #
# The score                                                                    #
# --------------------------------------------------------------------------- #


def _move_win_prob(me: MoveEval) -> float:
    if me.wdl is not None:
        return win_prob(me.wdl)
    return win_prob_cp(clamp_mate_cp(me.cp, me.mate))


def band_for(score: float, constants: FindabilityConstants) -> str:
    for minimum, label in constants.bands:  # sorted high -> low
        if score >= minimum:
            return label
    return constants.bands[-1][1]


def gate_reason(move_evals: list[MoveEval], constants: FindabilityConstants) -> str | None:
    """Return ``"forced"`` / ``"decided"`` / ``None`` (spec §3.3 Step 1).

    ``forced``: a single candidate move (findability is 100). ``decided``: both
    the best and second-best sit above ``gate_win_prob_high`` or below
    ``gate_win_prob_low`` — no real decision exists, so findability is *not
    scored* (the API emits ``null``, never ``0``). Opening-book gating is the
    caller's responsibility (it needs move history, not just the position).
    """
    if len(move_evals) <= 1:
        return "forced"
    probs = sorted((_move_win_prob(me) for me in move_evals), reverse=True)
    best_wp, second_wp = probs[0], probs[1]
    hi, lo = constants.gate_win_prob_high, constants.gate_win_prob_low
    both_won = best_wp > hi and second_wp > hi
    both_lost = best_wp < lo and second_wp < lo
    if both_won or both_lost:
        return "decided"
    return None


def _time_modifier(
    t_spent: float | None,
    t_expected: float | None,
    constants: FindabilityConstants,
) -> float:
    """``clamp(log(t_spent/t_expected + 1), t_mod_min, t_mod_max)`` (spec §3.3 Step 7)."""
    if not t_spent or not t_expected or t_expected <= 0:
        return 1.0
    raw = math.log(t_spent / t_expected + 1.0)
    return max(constants.t_mod_min, min(constants.t_mod_max, raw))


def score_position(
    fen: str,
    move_evals: list[MoveEval],
    policy_fn: PolicyFn,
    constants: FindabilityConstants,
    *,
    m_star: chess.Move | None = None,
    user_rating: int | None = None,
    t_spent: float | None = None,
    t_expected: float | None = None,
    volatility: float | None = None,
) -> PositionFindability | None:
    """Compute findability for the position ``fen`` given its MultiPV moves.

    Returns ``None`` when the position is gated out as *decided* (spec §7: the
    payload is ``null``, not ``0``). A *forced* (single-move) position returns a
    result with ``score = 100`` and ``forced = True``.

    ``volatility`` (0-100) feeds the Phase 4 ``tau = f(volatility)`` mapping; it
    is a no-op unless ``constants.tau_volatility_enabled`` is set.
    """
    reason = gate_reason(move_evals, constants)
    if reason == "decided":
        return None
    if reason == "forced":
        return PositionFindability(
            score=100,
            r_find=None,
            band="Forced",
            personal=1.0 if user_rating is not None else None,
            personal_star=1.0 if user_rating is not None else None,
            curve=[(r, 1.0) for r in constants.rating_grid],
            star_curve=[(r, 1.0) for r in constants.rating_grid],
            alternate=None,
            forced=True,
        )

    board = chess.Board(fen)
    moves = [me.move for me in move_evals]
    probs = {me.move: _move_win_prob(me) for me in move_evals}
    if m_star is None:
        m_star = max(move_evals, key=_move_win_prob).move
    best_prob = probs[m_star]

    tau = tau_for(
        volatility,
        base=constants.tau,
        enabled=constants.tau_volatility_enabled,
        tau_min=constants.tau_volatility_min,
        tau_max=constants.tau_volatility_max,
    )
    acceptable = acceptable_set(probs, tau)

    # Calculation-reweight ingredients are rating-independent — compute once.
    calc_by_move = {
        me.move: compute_features(
            me,
            board,
            dep_offset=constants.dep_offset,
            dep_span=constants.dep_span,
            forc_plies=constants.forc_plies,
            w_dep=constants.w_dep,
            w_forc=constants.w_forc,
            w_narr=constants.w_narr,
            q_weight=constants.q_weight,
        ).calc
        for me in move_evals
    }

    def curves_at(rating: int) -> tuple[dict[chess.Move, float], float, float]:
        pi = policy_fn(fen, rating, moves)
        pi_tilde = reweight(pi, calc_by_move, beta=constants.beta)
        c_star = pi_tilde.get(m_star, 0.0)
        c_a = sum(pi_tilde.get(m, 0.0) for m in acceptable)
        return pi_tilde, c_star, c_a

    grid = list(constants.rating_grid)
    c_star_raw: list[float] = []
    c_a_raw: list[float] = []
    for rating in grid:
        _pi_tilde, c_star, c_a = curves_at(rating)
        c_star_raw.append(c_star)
        c_a_raw.append(c_a)

    c_star_iso = [max(0.0, min(1.0, v)) for v in pava(c_star_raw)]
    c_a_iso = [max(0.0, min(1.0, v)) for v in pava(c_a_raw)]

    # ``r_find`` (the 0.5 crossing) is always reported for display, but the 0-100
    # score can be derived censoring-free from the whole curve (spec §4 Phase 3).
    r_find = invert_monotone(grid, c_a_iso, 0.5)
    if constants.score_mode == "mean_ca":
        score = round(100.0 * (sum(c_a_iso) / len(c_a_iso)))
        band = band_for(score, constants)
    elif r_find is None:
        score = 0
        band = "Engine-only"
    else:
        frac = 1.0 - (r_find - constants.score_floor_rating) / constants.score_span
        score = round(100.0 * max(0.0, min(1.0, frac)))
        band = band_for(score, constants)

    curve = list(zip(grid, c_a_iso, strict=True))
    star_curve = list(zip(grid, c_star_iso, strict=True))

    personal: float | None = None
    personal_star: float | None = None
    alternate: Alternate | None = None
    if user_rating is not None:
        pi_tilde_user, c_star_user, c_a_user = curves_at(user_rating)
        t_mod = _time_modifier(t_spent, t_expected, constants)
        personal = max(0.0, min(1.0, c_a_user)) * t_mod
        personal_star = c_star_user
        alternate = _best_alternate(
            board, pi_tilde_user, acceptable, probs, best_prob, m_star, c_star_user, constants
        )

    return PositionFindability(
        score=int(score),
        r_find=None if r_find is None else int(round(r_find)),
        band=band,
        personal=personal,
        personal_star=personal_star,
        curve=[(int(r), float(v)) for r, v in curve],
        star_curve=[(int(r), float(v)) for r, v in star_curve],
        alternate=alternate,
        forced=False,
    )


def _best_alternate(
    board: chess.Board,
    pi_tilde_user: dict[chess.Move, float],
    acceptable: set[chess.Move],
    probs: dict[chess.Move, float],
    best_prob: float,
    m_star: chess.Move,
    c_star_user: float,
    constants: FindabilityConstants,
) -> Alternate | None:
    """Recommend a different move iff the engine move is effectively unfindable
    for the user yet an acceptable move is realistically playable (spec §3.3 Step 8)."""
    if c_star_user >= constants.alt_cstar_max:
        return None
    threshold = max(constants.alt_pi_floor, constants.alt_pi_mult * c_star_user)
    candidates = [
        (m, pi_tilde_user.get(m, 0.0))
        for m in acceptable
        if m != m_star and pi_tilde_user.get(m, 0.0) >= threshold
    ]
    if not candidates:
        return None
    move, pi = max(candidates, key=lambda pair: pair[1])
    try:
        san = board.san(move)
    except (ValueError, AssertionError):
        san = move.uci()
    return Alternate(
        uci=move.uci(),
        san=san,
        delta_w=delta_w(best_prob, probs.get(move, best_prob)),
        pi=pi,
    )


__all__ = [
    "Alternate",
    "FindabilityConstants",
    "PolicyFn",
    "PositionFindability",
    "band_for",
    "gate_reason",
    "interp_at",
    "invert_monotone",
    "pava",
    "score_position",
]
