"""Heuristic tactic tags from board + best-move / PV shape (Insights.md C.4)."""

from __future__ import annotations

import chess

TAG_ORDER = (
    "fork",
    "pin",
    "skewer",
    "discovered_attack",
    "back_rank",
    "overloaded_defender",
    "zwischenzug",
    "deflection",
)


def tag_tactics(
    board_before: chess.Board,
    best_uci: str | None,
    *,
    played_uci: str | None = None,
    pv_uci: list[str] | None = None,
) -> list[str]:
    """Return zero or more tactic tags for the best move from ``board_before``."""

    if not best_uci:
        return []
    try:
        move = chess.Move.from_uci(best_uci)
    except ValueError:
        return []
    if move not in board_before.legal_moves:
        return []

    tags: list[str] = []
    if _is_fork(board_before, move):
        tags.append("fork")
    if _is_pin(board_before, move):
        tags.append("pin")
    if _is_skewer(board_before, move):
        tags.append("skewer")
    if _is_discovered(board_before, move):
        tags.append("discovered_attack")
    if _is_back_rank(board_before, move, pv_uci or []):
        tags.append("back_rank")
    if _is_overload(board_before, move):
        tags.append("overloaded_defender")
    if _is_zwischenzug(board_before, move, played_uci):
        tags.append("zwischenzug")
    if _is_deflection(board_before, move):
        tags.append("deflection")
    return tags


def _is_fork(board: chess.Board, move: chess.Move) -> bool:
    b = board.copy(stack=False)
    b.push(move)
    piece = b.piece_at(move.to_square)
    if piece is None:
        return False
    targets = []
    for sq in b.attacks(move.to_square):
        victim = b.piece_at(sq)
        if victim is None or victim.color == piece.color:
            continue
        if victim.piece_type == chess.KING or victim.piece_type >= piece.piece_type:
            targets.append(sq)
    return len(targets) >= 2


def _is_pin(board: chess.Board, move: chess.Move) -> bool:
    """True if the move creates a new absolute pin of an enemy piece."""

    enemy = not board.turn
    before = {sq for sq in chess.SQUARES if _absolutely_pinned(board, sq, enemy)}
    b = board.copy(stack=False)
    b.push(move)
    after = {sq for sq in chess.SQUARES if _absolutely_pinned(b, sq, enemy)}
    return bool(after - before)


def _absolutely_pinned(board: chess.Board, square: int, color: chess.Color) -> bool:
    piece = board.piece_at(square)
    if piece is None or piece.color != color:
        return False
    if piece.piece_type == chess.KING:
        return False
    return bool(board.is_pinned(color, square))


def _is_skewer(board: chess.Board, move: chess.Move) -> bool:
    """Slider check that also attacks a piece behind the king on the same ray."""

    b = board.copy(stack=False)
    b.push(move)
    if not b.is_check():
        return False
    piece = b.piece_at(move.to_square)
    if piece is None or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        return False
    king_sq = b.king(not piece.color)
    if king_sq is None:
        return False
    if king_sq not in b.attacks(move.to_square):
        return False
    # Walk beyond the king along the ray from attacker through king.
    af, ar = chess.square_file(move.to_square), chess.square_rank(move.to_square)
    kf, kr = chess.square_file(king_sq), chess.square_rank(king_sq)
    df = 0 if kf == af else (1 if kf > af else -1)
    dr = 0 if kr == ar else (1 if kr > ar else -1)
    if df == 0 and dr == 0:
        return False
    f, r = kf + df, kr + dr
    while 0 <= f <= 7 and 0 <= r <= 7:
        sq = chess.square(f, r)
        victim = b.piece_at(sq)
        if victim is not None:
            return victim.color != piece.color and victim.piece_type != chess.KING
        f += df
        r += dr
    return False


def _is_discovered(board: chess.Board, move: chess.Move) -> bool:
    b = board.copy(stack=False)
    checkers_before = board.checkers() if board.is_check() else chess.SquareSet()
    b.push(move)
    if not b.is_check():
        return False
    # A discovery: check that isn't delivered solely by the moved piece.
    checkers = b.checkers()
    return any(sq != move.to_square for sq in checkers) or (
        move.to_square not in checkers and bool(checkers - checkers_before)
    )


def _is_back_rank(board: chess.Board, move: chess.Move, pv_uci: list[str]) -> bool:
    b = board.copy(stack=False)
    b.push(move)
    enemy = not board.turn
    back = 0 if enemy == chess.WHITE else 7
    king = b.king(enemy)
    if king is None or chess.square_rank(king) != back:
        return False
    # Mate in PV or mating attack on back rank
    if b.is_checkmate():
        return True
    for uci in pv_uci[:4]:
        try:
            m = chess.Move.from_uci(uci)
        except ValueError:
            break
        if m not in b.legal_moves:
            break
        b.push(m)
        if b.is_checkmate() and chess.square_rank(b.king(enemy) or 0) == back:
            return True
    return False


def _is_overload(board: chess.Board, move: chess.Move) -> bool:
    """Capture a piece that defends two or more hanging/attacked units."""

    if board.piece_at(move.to_square) is None:
        return False
    victim_sq = move.to_square
    enemy = not board.turn
    defended = 0
    for sq in board.attacks(victim_sq):
        piece = board.piece_at(sq)
        if piece is None or piece.color != enemy:
            continue
        # Count if that square is also attacked by us
        if board.is_attacked_by(board.turn, sq):
            defended += 1
    return defended >= 2


def _is_zwischenzug(board: chess.Board, move: chess.Move, played_uci: str | None) -> bool:
    """Best move is a check (or capture) while the played move was a recapture elsewhere."""

    if not played_uci or played_uci == move.uci():
        return False
    try:
        played = chess.Move.from_uci(played_uci)
    except ValueError:
        return False
    b = board.copy(stack=False)
    b.push(move)
    if not (b.is_check() or board.is_capture(move)):
        return False
    # Played looked like a capture/recapture on a different square
    return board.is_capture(played) and played.to_square != move.to_square


def _is_deflection(board: chess.Board, move: chess.Move) -> bool:
    """Check that forces the king or a defender off a key square (simplified)."""

    b = board.copy(stack=False)
    b.push(move)
    if not b.is_check():
        return False
    # Only one legal response and it is a king move or capture of checker —
    # and we attack something the responder previously defended.
    legal = list(b.legal_moves)
    if not legal or len(legal) > 3:
        return False
    return any(b.piece_at(m.from_square) and b.piece_at(m.from_square).piece_type == chess.KING for m in legal)
