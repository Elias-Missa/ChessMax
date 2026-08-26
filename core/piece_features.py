"""Tier-0 per-piece board features — engine-free, microseconds, pure ``chess``.

These features are the **explanation**, never the number. The contextual value of
a piece is measured by ablation in :mod:`core.piece_values` (remove it, re-search,
read the eval drop); what lives here is the vocabulary that says *why* the number
came out the way it did — "worth 4.4 because it's an outpost hitting the king
zone", "worth 0.6 because it has one safe square".

That split is deliberate, and it is the same one findability already makes:
hand-weighted feature sums are exactly the classical evaluation function that took
Stockfish a decade to tune and that nobody can validate. A counterfactual can be
validated. So the features describe, and the engine decides.

Everything here is a pure function of a :class:`chess.Board`; no engine, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

# Classic values in centipawns. Kept classic (not the modern N=305/B=333/R=563)
# so that `chess_vol.game_review._material` keeps returning the same pawn units
# it always has — its sacrifice thresholds are calibrated against them. Whether
# the modern table describes real positions better is a question for the
# `recover` mode of the calibration driver, not an assumption to bake in here.
CLASSIC_VALUES_CP: dict[chess.PieceType, int] = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

# A knight or bishop with this many safe squares or fewer is trapped. One square
# is the threshold rather than zero because a piece whose only refuge is a single
# square is, practically, already caught.
TRAPPED_SAFE_MOBILITY = 1

# Own pawns on the bishop's own colour complex before it reads as "bad".
BAD_BISHOP_PAWNS = 4

# Squares of the enemy king ring a piece must hit to count as an attacker.
KING_ATTACKER_SQUARES = 2

# Display order, mirroring the `TAG_ORDER` idiom in server/tactic_tags.py: most
# consequential first, so a truncated list still leads with what matters.
TAG_ORDER: tuple[str, ...] = (
    "hanging",
    "trapped",
    "pinned",
    "protected_passer",
    "passed",
    "outpost",
    "king_attacker",
    "bad_bishop",
    "blockaded",
    "seventh_rank",
    "open_file",
    "semi_open_file",
    "king_defender",
    "isolated",
    "doubled",
)


# --------------------------------------------------------------------------- #
# Material                                                                     #
# --------------------------------------------------------------------------- #


def material_cp(
    board: chess.Board,
    color: chess.Color,
    values: dict[chess.PieceType, int] | None = None,
) -> int:
    """Static material for one side, in centipawns."""

    table = values or CLASSIC_VALUES_CP
    return sum(
        len(board.pieces(kind, color)) * value
        for kind, value in table.items()
        if kind != chess.KING
    )


def material_balance_cp(
    board: chess.Board,
    values: dict[chess.PieceType, int] | None = None,
) -> int:
    """Static material balance, White POV, in centipawns."""

    return material_cp(board, chess.WHITE, values) - material_cp(
        board, chess.BLACK, values
    )


# --------------------------------------------------------------------------- #
# Square helpers                                                               #
# --------------------------------------------------------------------------- #


def relative_rank(color: chess.Color, square: chess.Square) -> int:
    """Rank from ``color``'s point of view: 0 is its back rank, 7 the promotion
    rank. Lets one piece of pawn logic serve both colours."""

    rank = chess.square_rank(square)
    return rank if color == chess.WHITE else 7 - rank


def king_ring(board: chess.Board, color: chess.Color) -> chess.SquareSet:
    """``color``'s king square plus its eight neighbours.

    Empty when that king is missing, which only happens in synthetic positions —
    returning empty keeps every caller total instead of raising on them.
    """

    king_square = board.king(color)
    if king_square is None:
        return chess.SquareSet()
    return chess.SquareSet(
        chess.BB_KING_ATTACKS[king_square] | chess.BB_SQUARES[king_square]
    )


def _front_span(color: chess.Color, square: chess.Square, files: list[int]) -> chess.SquareSet:
    """Every square on ``files`` strictly ahead of ``square`` for ``color``."""

    mask = chess.SquareSet()
    rank = chess.square_rank(square)
    ranks = range(rank + 1, 8) if color == chess.WHITE else range(0, rank)
    for f in files:
        if 0 <= f <= 7:
            for r in ranks:
                mask.add(chess.square(f, r))
    return mask


def _adjacent_files(square: chess.Square) -> list[int]:
    f = chess.square_file(square)
    return [f - 1, f + 1]


def cheapest_attacker_cp(
    board: chess.Board,
    by_color: chess.Color,
    square: chess.Square,
    values: dict[chess.PieceType, int] | None = None,
) -> int | None:
    """Value of the least valuable ``by_color`` piece attacking ``square``.

    ``None`` when the square is not attacked. The king counts as an attacker but
    is priced at :data:`CLASSIC_VALUES_CP`'s 0, so it never reads as "cheap" —
    it is filtered out instead, since a king cannot capture a defended piece.
    """

    table = values or CLASSIC_VALUES_CP
    best: int | None = None
    for attacker_square in board.attackers(by_color, square):
        piece = board.piece_at(attacker_square)
        if piece is None or piece.piece_type == chess.KING:
            continue
        value = table[piece.piece_type]
        if best is None or value < best:
            best = value
    return best


def square_is_safe_for(
    board: chess.Board,
    square: chess.Square,
    piece: chess.Piece,
    values: dict[chess.PieceType, int] | None = None,
) -> bool:
    """Would ``piece`` survive on ``square``? A cheap stand-in for SEE.

    Unsafe when a cheaper enemy piece covers the square (the classic losing
    trade), or when the square is attacked more times than it is defended. This
    is deliberately an approximation — a real static exchange evaluation is
    Phase-4 precision for a feature whose job is to label, not to score.
    """

    table = values or CLASSIC_VALUES_CP
    attackers = board.attackers(not piece.color, square)
    if not attackers:
        return True
    cheapest = cheapest_attacker_cp(board, not piece.color, square, table)
    if cheapest is not None and cheapest < table[piece.piece_type]:
        return False
    defenders = board.attackers(piece.color, square)
    return len(defenders) >= len(attackers)


# --------------------------------------------------------------------------- #
# Features                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PieceFeatures:
    """Everything Tier-0 can say about one piece, without an engine."""

    square: chess.Square
    piece_type: chess.PieceType
    color: chess.Color

    # Reach
    mobility: int = 0
    """Squares the piece attacks (pseudo-legal, own pieces included)."""
    safe_mobility: int = 0
    """Attacked squares it could actually stand on — see :func:`square_is_safe_for`."""
    legal_moves: int = 0
    """Legal moves originating from this square. Unlike ``mobility`` this counts
    pawn pushes, and it is 0 for every piece when the side is not to move."""

    # Safety
    attacked_by: int = 0
    defended_by: int = 0
    hanging: bool = False
    pinned: bool = False

    # Influence
    targets_cp: int = 0
    """Total value of enemy material the piece attacks (kings excluded)."""
    king_zone_pressure: int = 0
    """Squares of the enemy king ring the piece covers."""
    own_king_shield: int = 0
    """Squares of its own king ring the piece covers."""

    # Pawn structure (all False/0 for non-pawns)
    passed: bool = False
    protected_passed: bool = False
    connected: bool = False
    blockaded: bool = False
    isolated: bool = False
    doubled: bool = False
    rank_advance: int = 0
    """Relative rank minus one: 0 on the home rank, 5 one step from promoting."""

    # Piece-specific
    outpost: bool = False
    """Knight/bishop, past the middle, pawn-protected, unevictable by a pawn."""
    bad_bishop_pawns: int = 0
    """Own pawns on the bishop's own colour complex."""
    open_file: bool = False
    semi_open_file: bool = False
    seventh_rank: bool = False
    behind_passer: bool = False

    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly view for the API and the stored feature vector."""

        return {
            "square": chess.square_name(self.square),
            "piece_type": self.piece_type,
            "color": "white" if self.color else "black",
            "mobility": self.mobility,
            "safe_mobility": self.safe_mobility,
            "legal_moves": self.legal_moves,
            "attacked_by": self.attacked_by,
            "defended_by": self.defended_by,
            "hanging": self.hanging,
            "pinned": self.pinned,
            "targets_cp": self.targets_cp,
            "king_zone_pressure": self.king_zone_pressure,
            "own_king_shield": self.own_king_shield,
            "passed": self.passed,
            "protected_passed": self.protected_passed,
            "connected": self.connected,
            "blockaded": self.blockaded,
            "isolated": self.isolated,
            "doubled": self.doubled,
            "rank_advance": self.rank_advance,
            "outpost": self.outpost,
            "bad_bishop_pawns": self.bad_bishop_pawns,
            "open_file": self.open_file,
            "semi_open_file": self.semi_open_file,
            "seventh_rank": self.seventh_rank,
            "behind_passer": self.behind_passer,
            "tags": list(self.tags),
        }


def _pawn_features(
    board: chess.Board, square: chess.Square, color: chess.Color
) -> dict[str, object]:
    """Passed / connected / blockaded / isolated / doubled for one pawn."""

    file_index = chess.square_file(square)
    own_pawns = board.pieces(chess.PAWN, color)
    enemy_pawns = board.pieces(chess.PAWN, not color)

    # Passed: no enemy pawn ahead on this file or either neighbour.
    blockers = _front_span(color, square, [file_index, *_adjacent_files(square)])
    passed = not (blockers & enemy_pawns)

    # Protected: an own pawn defends it. `attackers` on the pawn's own square
    # gives exactly the pieces guarding it; filter to pawns.
    protected = any(
        board.piece_at(sq) is not None and board.piece_at(sq).piece_type == chess.PAWN
        for sq in board.attackers(color, square)
    )

    # Connected: an own pawn on a neighbouring file within one rank — either
    # abreast (phalanx) or supporting from behind.
    rank = chess.square_rank(square)
    connected = False
    for f in _adjacent_files(square):
        if not 0 <= f <= 7:
            continue
        for r in (rank - 1, rank, rank + 1):
            if 0 <= r <= 7 and chess.square(f, r) in own_pawns:
                connected = True
                break

    forward = 8 if color == chess.WHITE else -8
    ahead = square + forward
    blockaded = False
    if 0 <= ahead <= 63:
        occupant = board.piece_at(ahead)
        blockaded = occupant is not None and occupant.color != color

    isolated = not any(
        chess.SquareSet(chess.BB_FILES[f]) & own_pawns
        for f in _adjacent_files(square)
        if 0 <= f <= 7
    )
    doubled = len(chess.SquareSet(chess.BB_FILES[file_index]) & own_pawns) > 1

    return {
        "passed": passed,
        "protected_passed": passed and protected,
        "connected": connected,
        "blockaded": blockaded,
        "isolated": isolated,
        "doubled": doubled,
        "rank_advance": max(0, relative_rank(color, square) - 1),
    }


def _is_outpost(board: chess.Board, square: chess.Square, color: chess.Color) -> bool:
    """A knight or bishop that cannot be chased away by a pawn.

    Requires: past the middle of the board, defended by an own pawn, and no
    enemy pawn still able to reach a square attacking it. The last condition is
    what separates a real outpost from a square the opponent simply hasn't
    attacked yet.
    """

    if relative_rank(color, square) < 3:
        return False
    defended_by_pawn = any(
        board.piece_at(sq) is not None and board.piece_at(sq).piece_type == chess.PAWN
        for sq in board.attackers(color, square)
    )
    if not defended_by_pawn:
        return False
    enemy_pawns = board.pieces(chess.PAWN, not color)
    # An enemy pawn evicts us by arriving on an adjacent file, ahead of us.
    evictors = _front_span(color, square, _adjacent_files(square))
    return not (evictors & enemy_pawns)


def piece_features(
    board: chess.Board,
    square: chess.Square,
    values: dict[chess.PieceType, int] | None = None,
) -> PieceFeatures | None:
    """Every Tier-0 feature for the piece on ``square``, or ``None`` if empty."""

    piece = board.piece_at(square)
    if piece is None:
        return None

    table = values or CLASSIC_VALUES_CP
    color = piece.color
    attacks = board.attacks(square)

    own_occupied = board.occupied_co[color]
    safe_mobility = sum(
        1
        for target in attacks
        if not (chess.BB_SQUARES[target] & own_occupied)
        and square_is_safe_for(board, target, piece, table)
    )

    targets_cp = 0
    for target in attacks:
        occupant = board.piece_at(target)
        if occupant is not None and occupant.color != color:
            targets_cp += table[occupant.piece_type]

    attackers = board.attackers(not color, square)
    defenders = board.attackers(color, square)
    cheapest = cheapest_attacker_cp(board, not color, square, table)
    hanging = bool(attackers) and (
        not defenders or (cheapest is not None and cheapest < table[piece.piece_type])
    )

    # `legal_moves` is only meaningful for the side to move; for the other side
    # it is structurally 0, which is why `mobility` is the comparable number.
    legal_moves = (
        sum(1 for move in board.legal_moves if move.from_square == square)
        if board.turn == color
        else 0
    )

    pawn: dict[str, object] = {}
    if piece.piece_type == chess.PAWN:
        pawn = _pawn_features(board, square, color)

    outpost = piece.piece_type in (chess.KNIGHT, chess.BISHOP) and _is_outpost(
        board, square, color
    )

    bad_bishop_pawns = 0
    if piece.piece_type == chess.BISHOP:
        complex_mask = (
            chess.BB_DARK_SQUARES
            if chess.BB_SQUARES[square] & chess.BB_DARK_SQUARES
            else chess.BB_LIGHT_SQUARES
        )
        bad_bishop_pawns = len(
            chess.SquareSet(complex_mask) & board.pieces(chess.PAWN, color)
        )

    open_file = semi_open_file = seventh_rank = behind_passer = False
    if piece.piece_type in (chess.ROOK, chess.QUEEN):
        file_mask = chess.SquareSet(chess.BB_FILES[chess.square_file(square)])
        own_pawns_on_file = file_mask & board.pieces(chess.PAWN, color)
        enemy_pawns_on_file = file_mask & board.pieces(chess.PAWN, not color)
        open_file = not own_pawns_on_file and not enemy_pawns_on_file
        semi_open_file = not own_pawns_on_file and bool(enemy_pawns_on_file)
        seventh_rank = relative_rank(color, square) == 6
        ahead = _front_span(color, square, [chess.square_file(square)])
        behind_passer = any(
            (features := _pawn_features(board, pawn_square, color))["passed"]
            for pawn_square in (ahead & board.pieces(chess.PAWN, color))
        )

    features = PieceFeatures(
        square=square,
        piece_type=piece.piece_type,
        color=color,
        mobility=len(attacks),
        safe_mobility=safe_mobility,
        legal_moves=legal_moves,
        attacked_by=len(attackers),
        defended_by=len(defenders),
        hanging=hanging,
        pinned=board.is_pinned(color, square),
        targets_cp=targets_cp,
        king_zone_pressure=len(attacks & king_ring(board, not color)),
        own_king_shield=len(attacks & king_ring(board, color)),
        outpost=outpost,
        bad_bishop_pawns=bad_bishop_pawns,
        open_file=open_file,
        semi_open_file=semi_open_file,
        seventh_rank=seventh_rank,
        behind_passer=behind_passer,
        **pawn,  # type: ignore[arg-type]
    )
    return _with_tags(features)


def _with_tags(features: PieceFeatures) -> PieceFeatures:
    """Attach the display vocabulary, in :data:`TAG_ORDER`."""

    present: set[str] = set()
    if features.hanging:
        present.add("hanging")
    if (
        features.piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
        and features.safe_mobility <= TRAPPED_SAFE_MOBILITY
    ):
        present.add("trapped")
    if features.pinned:
        present.add("pinned")
    if features.protected_passed:
        present.add("protected_passer")
    elif features.passed:
        present.add("passed")
    if features.outpost:
        present.add("outpost")
    if features.king_zone_pressure >= KING_ATTACKER_SQUARES:
        present.add("king_attacker")
    if features.bad_bishop_pawns >= BAD_BISHOP_PAWNS:
        present.add("bad_bishop")
    if features.blockaded:
        present.add("blockaded")
    if features.seventh_rank:
        present.add("seventh_rank")
    if features.open_file:
        present.add("open_file")
    elif features.semi_open_file:
        present.add("semi_open_file")
    if features.own_king_shield >= KING_ATTACKER_SQUARES:
        present.add("king_defender")
    if features.isolated:
        present.add("isolated")
    if features.doubled:
        present.add("doubled")

    ordered = tuple(tag for tag in TAG_ORDER if tag in present)
    return PieceFeatures(**{**features.__dict__, "tags": ordered})


def board_features(
    board: chess.Board,
    values: dict[chess.PieceType, int] | None = None,
) -> dict[chess.Square, PieceFeatures]:
    """Tier-0 features for every non-king piece on the board."""

    return {
        square: features
        for square, piece in board.piece_map().items()
        if piece.piece_type != chess.KING
        and (features := piece_features(board, square, values)) is not None
    }


__all__ = [
    "BAD_BISHOP_PAWNS",
    "CLASSIC_VALUES_CP",
    "KING_ATTACKER_SQUARES",
    "TAG_ORDER",
    "TRAPPED_SAFE_MOBILITY",
    "PieceFeatures",
    "board_features",
    "cheapest_attacker_cp",
    "king_ring",
    "material_balance_cp",
    "material_cp",
    "piece_features",
    "relative_rank",
    "square_is_safe_for",
]
