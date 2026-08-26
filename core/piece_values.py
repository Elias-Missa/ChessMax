"""Contextual piece values by ablation.

**A piece is worth what the position loses without it.** Remove it, re-search,
and the eval drop is that piece's value *in this position*. A bishop biting on
its own pawn chain barely registers; a protected passer on the seventh reads like
a rook; a knight on a permanent outpost next to the enemy king reads like more.

This is a real counterfactual, which is the whole point. The alternative — summing
mobility, king proximity and pawn-structure terms into a hand-weighted score — is
exactly the classical evaluation function that took Stockfish a decade to tune,
and it cannot be validated against anything. An ablation can: see
``chess_vol/calibrate_piece_values.py``, whose ``trades`` mode checks whether
these numbers predict the eval change across real captures better than 1/3/3/5/9
does. If they don't, the algorithm adds nothing.

:mod:`core.piece_features` supplies the *explanation* ("outpost", "trapped") but
never the number.

Pure given an engine, in the same shape as :func:`core.volatility.compute_volatility`
— any :class:`~core.volatility.EngineLike` works, including
:class:`server.position_cache.CachingEngine`, which makes every probe
Zobrist-cached across sessions for free.

The math
--------

All centipawns, White POV. ``E`` is the eval of the position itself.

Raw ablation, owner-POV so positive always means "helps its owner"::

    White piece:  a_i = E − E(without i)
    Black piece:  a_i = E(without i) − E

Anchoring. Leave-one-out marginals sum to nothing useful — remove either of two
doubled rooks and the loss is small, remove both and it is huge — so they
systematically undercount redundant pieces. With ``sign_i = ±1`` by colour::

    S   = Σ sign_i · a_i
    R   = E − S
    w_i = |a_i| / Σ |a_j|
    v_i = a_i + sign_i · w_i · R

which makes ``Σ sign_i · v_i = E`` hold exactly. That is the Shapley *efficiency
axiom*; the redistribution is a cheap approximation of the correction that
restores it, and ``--mode shapley`` measures how far off it lands.

The payoff is that the material gap decomposes. With ``M`` the static material
and ``premium_i = v_i − static_i``::

    Σ sign_i · premium_i = E − M

So "up five in material, evaluation minus two" splits into per-piece premiums
summing to exactly −7 — true by construction, not by assertion.

``raw_cp`` is stored alongside ``anchored_cp`` because the raw number is an
independently verifiable counterfactual that never changes, while the anchored
one carries a share of a global correction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import chess

from core.piece_features import (
    CLASSIC_VALUES_CP,
    PieceFeatures,
    material_balance_cp,
    piece_features,
    square_is_safe_for,
)
if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.volatility import EngineLike

# `core.volatility` imports `chess_vol.config`, whose package __init__ imports
# back into `core.volatility` — a pre-existing cycle that only resolves when
# `chess_vol` is imported first. Deferring the one function we need keeps this
# module importable standalone whatever the import order.

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "piece_values.json"

_PIECE_NAMES: dict[str, chess.PieceType] = {
    "pawn": chess.PAWN,
    "knight": chess.KNIGHT,
    "bishop": chess.BISHOP,
    "rook": chess.ROOK,
    "queen": chess.QUEEN,
}


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalScale:
    """Stockfish evaluation → material-equivalent centipawns.

    **Without this, ablation does not measure piece value.** Removing a queen
    from the opening position moves the eval by 773cp while removing a bishop
    moves it 613 — the queen reads as worth less than a bishop, because both
    land deep in the region where the engine's eval stops being linear in
    material. A single scale factor cannot fix a distortion that is not
    monotone in the static table.

    So evals are mapped to "how much material would produce this evaluation"
    *before* differencing::

        raw_i = to_material(E) − to_material(E without piece i)

    ``anchors`` is a monotone ``(eval_cp, material_cp)`` table for non-negative
    evals, extended by odd symmetry; between anchors it interpolates linearly
    and past the last one it extrapolates along the final segment. The shipped
    table is fitted by ``chess_vol/calibrate_piece_values.py --mode recover``,
    whose whole job is to make the mean ablation per piece type land on the
    classical values — because the classical values *are* the mean of the
    contextual ones over the position distribution.

    The default is the identity, so an unfitted install degrades to raw cp
    rather than to a silently wrong curve.
    """

    anchors: tuple[tuple[float, float], ...] = ((0.0, 0.0), (10000.0, 10000.0))

    def to_material(self, cp: float) -> float:
        sign = 1.0 if cp >= 0 else -1.0
        x = abs(float(cp))
        points = self.anchors
        if len(points) < 2:
            return cp
        for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
            if x <= x1:
                if x1 == x0:
                    return sign * y1
                t = (x - x0) / (x1 - x0)
                return sign * (y0 + t * (y1 - y0))
        # Past the table: continue along the last segment's slope.
        (x0, y0), (x1, y1) = points[-2], points[-1]
        slope = (y1 - y0) / (x1 - x0) if x1 != x0 else 1.0
        return sign * (y1 + (x - x1) * slope)


@dataclass(frozen=True)
class PieceValueConstants:
    """Tunables, loaded from JSON so a refit never touches code.

    Every field carries its shipped value as a Python default, so the JSON is a
    pure override layer rather than a requirement — the same contract
    :class:`core.findability.FindabilityConstants` uses.
    """

    values: dict[chess.PieceType, int] = field(
        default_factory=lambda: dict(CLASSIC_VALUES_CP)
    )
    probe_depth: int = 16
    probe_multipv: int = 1
    shrinkage: float = 0.5
    """How far ``shrunk_cp`` moves from the static value toward the measured one.

    Ablation is near-unbiased but noisy — two depth-limited searches enter every
    comparison — so the pure measurement loses on absolute error to a constant
    even while correlating with the truth that the constant misses entirely.
    Blending back toward the static value is the James-Stein bargain, and
    measurement puts the optimum near 0.5.
    """
    saturation_cp: int = 800
    relocate_candidates: int = 8
    relocate_min_gain_cp: int = 25
    terminal_mate_cp: int = 1950
    eval_scale: EvalScale = field(default_factory=EvalScale)
    type_scale: dict[chess.PieceType, float] = field(
        default_factory=lambda: dict.fromkeys(_PIECE_NAMES.values(), 1.0)
    )
    """Per-piece-type redundancy correction, applied to ``raw_cp``.

    Leave-one-out marginals are biased by how substitutable a piece type
    typically is. Measurement (``--mode recover``) puts minors ~35% above their
    static value and rooks ~12% below: a side usually has two rooks, so removing
    one leaves the other doing much of its work, while a lone queen has no
    understudy. This is the Shapley non-additivity the anchoring only partly
    repairs, and a uniform factor *within* a type fixes the cross-type
    comparison without disturbing any contextual ordering inside it.
    """

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PieceValueConstants":
        base = cls()
        raw_values = data.get("values") or {}
        values = dict(CLASSIC_VALUES_CP)
        for name, piece_type in _PIECE_NAMES.items():
            if name in raw_values:
                values[piece_type] = int(raw_values[name])
        anchors_raw = data.get("eval_scale_anchors")
        eval_scale = (
            EvalScale(
                anchors=tuple(
                    (float(a[0]), float(a[1])) for a in anchors_raw
                )
            )
            if anchors_raw
            else base.eval_scale
        )
        raw_type_scale = data.get("type_scale") or {}
        type_scale = dict.fromkeys(_PIECE_NAMES.values(), 1.0)
        for name, piece_type in _PIECE_NAMES.items():
            if name in raw_type_scale:
                type_scale[piece_type] = float(raw_type_scale[name])
        return cls(
            values=values,
            eval_scale=eval_scale,
            type_scale=type_scale,
            probe_depth=int(data.get("probe_depth", base.probe_depth)),
            shrinkage=float(data.get("shrinkage", base.shrinkage)),
            probe_multipv=int(data.get("probe_multipv", base.probe_multipv)),
            saturation_cp=int(data.get("saturation_cp", base.saturation_cp)),
            relocate_candidates=int(
                data.get("relocate_candidates", base.relocate_candidates)
            ),
            relocate_min_gain_cp=int(
                data.get("relocate_min_gain_cp", base.relocate_min_gain_cp)
            ),
            terminal_mate_cp=int(data.get("terminal_mate_cp", base.terminal_mate_cp)),
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PieceValueConstants":
        target = Path(path) if path is not None else _CONSTANTS_PATH
        with open(target, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


# --------------------------------------------------------------------------- #
# Results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Relocation:
    """Where this piece would rather be."""

    to_square: str
    gain_cp: float
    """Owner-POV eval gain from standing there instead."""


@dataclass(frozen=True)
class PieceValue:
    """One piece's contextual value."""

    square: str
    piece_type: chess.PieceType
    symbol: str
    color: bool
    static_cp: int
    raw_cp: float
    """The ablation itself: owner-POV eval loss when this piece disappears."""
    anchored_cp: float
    """``raw_cp`` plus its share of the residual, so the board sums to the eval."""
    premium_cp: float
    """``anchored_cp − static_cp``. These sum (signed) to exactly ``gap_cp``."""
    shrunk_cp: float
    """``anchored_cp`` pulled back toward ``static_cp`` by ``shrinkage``.

    Use this one to *predict* (what a trade is worth); use ``anchored_cp`` to
    *explain* (it is the one that reconciles to the eval). They differ because
    the two jobs want opposite things from noise: prediction wants it damped,
    reconciliation wants the books to balance."""
    method: str
    """``ablation`` | ``turn_flipped`` | ``static_fallback`` — see the module docstring."""
    features: PieceFeatures | None = None
    tags: tuple[str, ...] = ()
    relocation: Relocation | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "square": self.square,
            "piece_type": self.piece_type,
            "symbol": self.symbol,
            "color": "white" if self.color else "black",
            "static_cp": self.static_cp,
            "raw_cp": round(self.raw_cp, 1),
            "anchored_cp": round(self.anchored_cp, 1),
            "premium_cp": round(self.premium_cp, 1),
            "shrunk_cp": round(self.shrunk_cp, 1),
            "method": self.method,
            "tags": list(self.tags),
            "features": self.features.as_dict() if self.features else None,
            "relocation": (
                {
                    "to_square": self.relocation.to_square,
                    "gain_cp": round(self.relocation.gain_cp, 1),
                }
                if self.relocation
                else None
            ),
        }


@dataclass(frozen=True)
class PositionValues:
    """Every piece's contextual value, plus the reconciliation."""

    fen: str
    eval_cp: int
    """``E`` — the engine's evaluation, White POV, measured at ``depth``."""
    eval_material_cp: float
    """``E`` mapped through :class:`EvalScale`. This — not ``eval_cp`` — is what
    the piece values reconcile to, because it is the one in material units."""
    material_cp: int
    """``M`` — static material balance, White POV."""
    gap_cp: float
    """``eval_material_cp − M``. What the static count fails to explain, and
    what the per-piece premiums decompose exactly."""
    residual_cp: float
    """``eval_material_cp − Σ sign·raw``. How much the leave-one-out marginals
    missed, before anchoring redistributed it."""
    saturated: bool
    """``|E|`` is past the point where ablations still discriminate."""
    pieces: list[PieceValue]
    analyses: int
    depth: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fen": self.fen,
            "eval_cp": self.eval_cp,
            "eval_material_cp": round(self.eval_material_cp, 1),
            "material_cp": self.material_cp,
            "gap_cp": round(self.gap_cp, 1),
            "residual_cp": round(self.residual_cp, 1),
            "saturated": self.saturated,
            "analyses": self.analyses,
            "depth": self.depth,
            "pieces": [p.as_dict() for p in self.pieces],
        }


# --------------------------------------------------------------------------- #
# Board surgery                                                                #
# --------------------------------------------------------------------------- #


def _repair(board: chess.Board, owner: chess.Color) -> str | None:
    """Make a mutated board legal again, or report that it cannot be.

    ``board.remove_piece_at`` does not clean up after itself, and each way it
    leaves a board broken needs a different answer:

    * ``BAD_CASTLING_RIGHTS`` — a corner rook is gone. Drop the right.
    * ``INVALID_EP_SQUARE`` — the pawn that just double-pushed is gone. Drop it.
    * ``OPPOSITE_CHECK`` — the removed piece was shielding its own king and the
      *opponent* is to move, which is not a position at all. Give the owner the
      move instead: the probe still measures something real (tagged
      ``turn_flipped`` so it is never mistaken for a clean ablation).

    Note what is deliberately *not* repaired: the owner being in check while the
    owner is to move. That is a perfectly legal position, and the dreadful eval
    it produces is the correct answer — the piece was holding the king together.

    Returns the method tag, or ``None`` when the board cannot be salvaged.
    """

    method = "ablation"
    status = board.status()
    if status & chess.STATUS_BAD_CASTLING_RIGHTS:
        board.castling_rights = board.clean_castling_rights()
        status = board.status()
    if status & chess.STATUS_INVALID_EP_SQUARE:
        board.ep_square = None
        status = board.status()
    if status & chess.STATUS_OPPOSITE_CHECK:
        board.turn = owner
        method = "turn_flipped"
        status = board.status()
    return method if status == chess.STATUS_VALID else None


def _without(
    board: chess.Board, square: chess.Square
) -> tuple[chess.Board, str] | None:
    """``board`` minus the piece on ``square``, repaired, with its method tag."""

    piece = board.piece_at(square)
    if piece is None:
        return None
    probe = board.copy(stack=False)
    probe.remove_piece_at(square)
    method = _repair(probe, piece.color)
    return (probe, method) if method is not None else None


def _moved(
    board: chess.Board, square: chess.Square, target: chess.Square
) -> chess.Board | None:
    """``board`` with the piece teleported from ``square`` to an empty ``target``."""

    piece = board.piece_at(square)
    if piece is None or board.piece_at(target) is not None:
        return None
    probe = board.copy(stack=False)
    probe.remove_piece_at(square)
    probe.set_piece_at(target, piece)
    return probe if _repair(probe, piece.color) is not None else None


# --------------------------------------------------------------------------- #
# Evaluation                                                                   #
# --------------------------------------------------------------------------- #


def _white_cp(
    board: chess.Board,
    engine: "EngineLike",
    depth: int,
    multipv: int,
    constants: PieceValueConstants,
) -> tuple[int, int]:
    """Evaluate ``board`` White-POV. Returns ``(cp, analyses_spent)``.

    Terminal positions are answered without touching the engine — an ablation
    that leaves the owner mated is the most informative result there is, and no
    engine will search a finished game.
    """

    # `chess_vol` first: `core.volatility` imports `chess_vol.config`, whose
    # package __init__ imports back into `core.volatility`. Importing the
    # package eagerly lets that cycle resolve before we ask for the name,
    # which keeps `core.piece_values` usable whatever was imported first.
    import chess_vol  # noqa: F401
    from core.volatility import info_to_cp

    if board.is_checkmate():
        # The side to move has lost.
        stm = -constants.terminal_mate_cp
        return (stm if board.turn == chess.WHITE else -stm), 0
    if board.is_stalemate() or board.is_insufficient_material():
        return 0, 0

    infos = engine.analyse(board, depth=depth, multipv=multipv)
    if not infos:
        return 0, 1
    cp = info_to_cp(infos[0], board.turn)
    return (cp if board.turn == chess.WHITE else -cp), 1


# --------------------------------------------------------------------------- #
# Relocation                                                                   #
# --------------------------------------------------------------------------- #


def _relocation_candidates(
    board: chess.Board,
    square: chess.Square,
    constants: PieceValueConstants,
) -> list[chess.Square]:
    """Empty squares worth probing for ``square``'s piece, best guess first.

    Ranked by would-be reach and king-zone pressure, so the handful we actually
    search are the ones a player would consider. Squares a cheaper enemy piece
    covers are dropped outright — "your knight belongs on d5" is not advice if
    a pawn takes it there.
    """

    piece = board.piece_at(square)
    if piece is None or piece.piece_type == chess.PAWN:
        return []

    scored: list[tuple[int, chess.Square]] = []
    for target in chess.SQUARES:
        if target == square or board.piece_at(target) is not None:
            continue
        probe = _moved(board, square, target)
        if probe is None:
            continue
        if not square_is_safe_for(probe, target, piece, constants.values):
            continue
        features = piece_features(probe, target, constants.values)
        if features is None:
            continue
        scored.append(
            (features.safe_mobility + 2 * features.king_zone_pressure, target)
        )

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [target for _, target in scored[: constants.relocate_candidates]]


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #


def compute_piece_values(
    board: chess.Board,
    engine: "EngineLike",
    *,
    depth: int | None = None,
    multipv: int | None = None,
    relocate_top_k: int = 0,
    constants: PieceValueConstants | None = None,
) -> PositionValues:
    """Contextual value of every non-king piece on ``board``.

    The baseline eval is always measured here, at the probe depth, rather than
    accepted from the caller. Taking a review's depth-18 eval as the baseline
    while probing at depth 12 would fold the depth gap into *every* ablation —
    one extra search out of ~27 is a cheap price for deltas that mean what they
    say.

    ``relocate_top_k`` additionally asks "where does this piece belong?" for the
    ``k`` pieces with the largest ``|raw_cp|``, at ``relocate_candidates`` extra
    searches each.
    """

    if not board.is_valid():
        # Everything below assumes the engine is being asked real questions.
        # A board with adjacent kings or a side already in check out of turn
        # produces numbers that look plausible and mean nothing.
        raise ValueError(f"position is not legal: status {board.status()!r}")

    constants = constants or PieceValueConstants.load()
    depth = depth if depth is not None else constants.probe_depth
    multipv = multipv if multipv is not None else constants.probe_multipv
    to_material = constants.eval_scale.to_material

    analyses = 0
    eval_cp, spent = _white_cp(board, engine, depth, multipv, constants)
    analyses += spent
    eval_material_cp = to_material(eval_cp)

    material_cp = material_balance_cp(board, constants.values)
    targets = [
        (square, piece)
        for square, piece in sorted(board.piece_map().items())
        if piece.piece_type != chess.KING
    ]

    # --- Raw ablation ----------------------------------------------------- #
    raw: list[tuple[chess.Square, chess.Piece, float, str]] = []
    for square, piece in targets:
        static_cp = constants.values[piece.piece_type]
        probe = _without(board, square)
        if probe is None:
            # Unsalvageable board (vanishingly rare). Fall back to the static
            # value so the piece still participates in the anchoring identity
            # rather than silently vanishing from the reconciliation.
            raw.append((square, piece, float(static_cp), "static_fallback"))
            continue
        probe_board, method = probe
        without_cp, spent = _white_cp(probe_board, engine, depth, multipv, constants)
        analyses += spent
        without_material = to_material(without_cp)
        delta = (
            eval_material_cp - without_material
            if piece.color == chess.WHITE
            else without_material - eval_material_cp
        )
        delta *= constants.type_scale.get(piece.piece_type, 1.0)
        raw.append((square, piece, float(delta), method))

    # --- Anchoring -------------------------------------------------------- #
    def _sign(color: chess.Color) -> int:
        return 1 if color == chess.WHITE else -1

    signed_sum = sum(_sign(piece.color) * value for _, piece, value, _ in raw)
    residual = eval_material_cp - signed_sum
    total_magnitude = sum(abs(value) for _, _, value, _ in raw)
    if total_magnitude > 0:
        weights = [abs(value) / total_magnitude for _, _, value, _ in raw]
    elif raw:
        # Every marginal is zero (bare kings, or a fully decided position).
        # Spread the residual evenly rather than dividing by zero.
        weights = [1.0 / len(raw)] * len(raw)
    else:
        weights = []

    # --- Relocation ------------------------------------------------------- #
    # The k most consequential pieces get asked where they would rather be.
    # Pawns are excluded: teleporting one is a different position, not a
    # relocation.
    relocate_squares: set[chess.Square] = set()
    if relocate_top_k > 0:
        ranked = sorted(raw, key=lambda item: -abs(item[2]))
        relocate_squares = set(
            [
                square
                for square, piece, _, _ in ranked
                if piece.piece_type != chess.PAWN
            ][:relocate_top_k]
        )

    pieces: list[PieceValue] = []
    for (square, piece, raw_cp, method), weight in zip(raw, weights, strict=True):
        static_cp = constants.values[piece.piece_type]
        anchored = raw_cp + _sign(piece.color) * weight * residual
        features = piece_features(board, square, constants.values)

        relocation: Relocation | None = None
        if square in relocate_squares:
            best_gain = 0.0
            best_square: str | None = None
            for target in _relocation_candidates(board, square, constants):
                probe_board = _moved(board, square, target)
                if probe_board is None:
                    continue
                moved_cp, spent = _white_cp(
                    probe_board, engine, depth, multipv, constants
                )
                analyses += spent
                moved_material = to_material(moved_cp)
                gain = (
                    moved_material - eval_material_cp
                    if piece.color == chess.WHITE
                    else eval_material_cp - moved_material
                )
                if gain > best_gain:
                    best_gain, best_square = gain, chess.square_name(target)
            if best_square is not None and best_gain >= constants.relocate_min_gain_cp:
                relocation = Relocation(to_square=best_square, gain_cp=best_gain)

        pieces.append(
            PieceValue(
                square=chess.square_name(square),
                piece_type=piece.piece_type,
                symbol=piece.symbol(),
                color=piece.color,
                static_cp=static_cp,
                raw_cp=raw_cp,
                anchored_cp=anchored,
                premium_cp=anchored - static_cp,
                shrunk_cp=static_cp + constants.shrinkage * (anchored - static_cp),
                method=method,
                features=features,
                tags=features.tags if features else (),
                relocation=relocation,
            )
        )

    return PositionValues(
        fen=board.fen(),
        eval_cp=eval_cp,
        eval_material_cp=eval_material_cp,
        material_cp=material_cp,
        gap_cp=eval_material_cp - material_cp,
        residual_cp=residual,
        saturated=abs(eval_cp) >= constants.saturation_cp,
        pieces=pieces,
        analyses=analyses,
        depth=depth,
    )


__all__ = [
    "EvalScale",
    "PieceValue",
    "PieceValueConstants",
    "PositionValues",
    "Relocation",
    "compute_piece_values",
]
