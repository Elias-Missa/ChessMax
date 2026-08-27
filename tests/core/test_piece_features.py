"""Tier-0 per-piece features (core/piece_features.py). Pure, no engine."""

from __future__ import annotations

import chess

from core.piece_features import (
    CLASSIC_VALUES_CP,
    board_features,
    king_ring,
    material_balance_cp,
    material_cp,
    piece_features,
    relative_rank,
    square_is_safe_for,
)


def test_material_matches_the_classic_table() -> None:
    board = chess.Board()
    assert material_cp(board, chess.WHITE) == 8 * 100 + 2 * 300 + 2 * 300 + 2 * 500 + 900
    assert material_balance_cp(board) == 0
    board.remove_piece_at(chess.D1)
    assert material_balance_cp(board) == -900


def test_material_table_still_backs_the_game_review_helper() -> None:
    """``game_review._material`` reports pawn units off the same table."""

    from chess_vol.game_review import _material

    board = chess.Board()
    assert _material(board, chess.WHITE) == material_cp(board, chess.WHITE) // 100


def test_relative_rank_flips_for_black() -> None:
    assert relative_rank(chess.WHITE, chess.A2) == 1
    assert relative_rank(chess.BLACK, chess.A2) == 6
    assert relative_rank(chess.BLACK, chess.A7) == 1


def test_king_ring_is_the_king_plus_neighbours() -> None:
    board = chess.Board()
    ring = king_ring(board, chess.WHITE)
    assert chess.E1 in ring
    assert chess.D2 in ring
    assert chess.A5 not in ring
    # A board with no such king yields an empty ring rather than raising.
    bare = chess.Board(None)
    assert len(king_ring(bare, chess.WHITE)) == 0


def test_empty_square_has_no_features() -> None:
    assert piece_features(chess.Board(), chess.E4) is None


def test_board_features_skips_kings() -> None:
    features = board_features(chess.Board())
    assert len(features) == 30  # 32 pieces minus the two kings
    assert all(f.piece_type != chess.KING for f in features.values())


def test_hanging_detects_an_undefended_attacked_piece() -> None:
    # Black knight on e5 attacked by the d4 pawn, undefended.
    board = chess.Board("4k3/8/8/4n3/3P4/8/8/4K3 w - - 0 1")
    knight = piece_features(board, chess.E5)
    assert knight is not None
    assert knight.hanging
    assert "hanging" in knight.tags


def test_a_defended_piece_attacked_only_by_equals_is_not_hanging() -> None:
    # Rooks trade evenly on e5 and the black rook is defended by its king.
    board = chess.Board("8/8/4k3/4r3/4R3/8/8/4K3 w - - 0 1")
    rook = piece_features(board, chess.E5)
    assert rook is not None
    assert not rook.hanging


def test_safe_mobility_excludes_squares_a_pawn_covers() -> None:
    # White knight on e4. Black pawns on b6 and d6 both cover c5, which is one
    # of the knight's eight destinations.
    board = chess.Board("4k3/8/1p1p4/8/4N3/8/8/4K3 w - - 0 1")
    knight = piece_features(board, chess.E4)
    assert knight is not None
    assert knight.mobility == 8
    assert knight.safe_mobility < knight.mobility
    assert not square_is_safe_for(
        board, chess.C5, chess.Piece(chess.KNIGHT, chess.WHITE), CLASSIC_VALUES_CP
    )


def test_trapped_tags_a_piece_with_no_safe_squares() -> None:
    # Bishop entombed in the corner: a8 sees only b7, which is defended by c6.
    board = chess.Board("B3k3/1p6/2p5/8/8/8/8/4K3 w - - 0 1")
    bishop = piece_features(board, chess.A8)
    assert bishop is not None
    assert bishop.safe_mobility <= 1
    assert "trapped" in bishop.tags


def test_outpost_needs_a_pawn_guard_and_no_pawn_that_can_evict() -> None:
    board = chess.Board("r1bqkb1r/pp3ppp/2n2n2/3N4/4P3/8/PPP2PPP/R1BQKB1R b KQkq - 0 1")
    knight = piece_features(board, chess.D5)
    assert knight is not None
    assert knight.outpost
    assert "outpost" in knight.tags


def test_a_pawn_that_can_evict_denies_the_outpost() -> None:
    # Same knight, but Black keeps a c-pawn that can play ...c6.
    board = chess.Board("r1bqkb1r/ppp2ppp/5n2/3N4/4P3/8/PPP2PPP/R1BQKB1R b KQkq - 0 1")
    knight = piece_features(board, chess.D5)
    assert knight is not None
    assert not knight.outpost


def test_passed_and_protected_passer() -> None:
    board = chess.Board("8/1P6/P7/8/6k1/8/8/6K1 w - - 0 1")
    assert board.is_valid()
    pawn = piece_features(board, chess.B7)
    assert pawn is not None
    assert pawn.passed and pawn.protected_passed
    assert "protected_passer" in pawn.tags
    # Relative rank 7 means one square from promoting.
    assert pawn.rank_advance == 5


def test_doubled_and_isolated_pawns() -> None:
    # White pawns doubled on the d-file with no c- or e-pawn to support them.
    board = chess.Board("4k3/8/8/8/8/3P4/3P4/4K3 w - - 0 1")
    lower = piece_features(board, chess.D2)
    upper = piece_features(board, chess.D3)
    assert lower is not None and upper is not None
    assert lower.doubled and lower.isolated
    assert upper.doubled and upper.isolated
    assert "doubled" in lower.tags and "isolated" in lower.tags


def test_blockaded_pawn() -> None:
    # A black knight sits directly in front of the d3 pawn.
    board = chess.Board("4k3/8/8/8/3n4/3P4/8/4K3 w - - 0 1")
    pawn = piece_features(board, chess.D3)
    assert pawn is not None
    assert pawn.blockaded
    assert "blockaded" in pawn.tags
    # An unobstructed pawn is not blockaded.
    clear = piece_features(chess.Board(), chess.E2)
    assert clear is not None and not clear.blockaded


def test_bad_bishop_counts_pawns_on_its_own_complex() -> None:
    # Dark-squared bishop on d2 with every pawn on a dark square.
    board = chess.Board("4k3/8/8/8/8/2P1P3/3B4/4K3 w - - 0 1")
    bishop = piece_features(board, chess.D2)
    assert bishop is not None
    assert bishop.bad_bishop_pawns == 2  # c3 and e3 are dark, like d2


def test_rook_file_and_rank_features() -> None:
    board = chess.Board("4k3/1R6/8/8/8/8/8/4K3 w - - 0 1")
    rook = piece_features(board, chess.B7)
    assert rook is not None
    assert rook.open_file
    assert rook.seventh_rank
    assert "seventh_rank" in rook.tags and "open_file" in rook.tags


def test_semi_open_file_needs_an_enemy_pawn_and_none_of_ours() -> None:
    board = chess.Board("4k3/1p6/8/8/8/8/8/1R2K3 w - - 0 1")
    rook = piece_features(board, chess.B1)
    assert rook is not None
    assert rook.semi_open_file and not rook.open_file
    assert "semi_open_file" in rook.tags


def test_king_zone_pressure_counts_squares_around_the_enemy_king() -> None:
    board = chess.Board("6k1/8/8/8/8/8/8/4K1R1 w - - 0 1")
    rook = piece_features(board, chess.G1)
    assert rook is not None
    assert rook.king_zone_pressure >= 1
    assert "king_attacker" in rook.tags or rook.king_zone_pressure < 2


def test_pinned_is_reported() -> None:
    # White knight on e4 pinned to its king on e1 by a rook on e8.
    board = chess.Board("4r2k/8/8/8/4N3/8/8/4K3 w - - 0 1")
    knight = piece_features(board, chess.E4)
    assert knight is not None
    assert knight.pinned
    assert "pinned" in knight.tags


def test_legal_moves_is_zero_for_the_side_not_to_move() -> None:
    board = chess.Board()
    white_knight = piece_features(board, chess.G1)
    black_knight = piece_features(board, chess.G8)
    assert white_knight is not None and black_knight is not None
    assert white_knight.legal_moves == 2
    assert black_knight.legal_moves == 0
    # Mobility is the comparable number, and it is symmetric here.
    assert white_knight.mobility == black_knight.mobility


def test_as_dict_is_json_shaped() -> None:
    features = piece_features(chess.Board(), chess.G1)
    assert features is not None
    payload = features.as_dict()
    assert payload["square"] == "g1"
    assert payload["color"] == "white"
    assert isinstance(payload["tags"], list)
