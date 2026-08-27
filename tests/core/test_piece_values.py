"""Contextual piece values (core/piece_values.py). Engine-free.

Every test here drives a scripted fake engine, so the *math* is pinned without a
Stockfish binary. The question of whether the numbers describe chess is a
separate one, answered by ``chess_vol/calibrate_piece_values.py`` and by the
``@integration`` tests alongside this file.
"""

from __future__ import annotations

import chess
import chess.engine
import pytest

from core.piece_features import CLASSIC_VALUES_CP
from core.piece_values import (
    EvalScale,
    PieceValueConstants,
    _repair,
    _without,
    compute_piece_values,
)


class ScriptedEngine:
    """Returns a fixed White-POV eval per position, defaulting to ``base``.

    ``overrides`` maps a board FEN's piece-placement field to a White-POV cp
    value, which is enough to script "this position without that piece is worth
    X" without caring about move numbers or castling flags.
    """

    def __init__(self, base: int = 0, overrides: dict[str, int] | None = None) -> None:
        self.base = base
        self.overrides = overrides or {}
        self.calls = 0

    def analyse(
        self, board: chess.Board, depth: int = 18, multipv: int = 6
    ) -> list[dict[str, object]]:
        self.calls += 1
        placement = board.board_fen()
        white_cp = self.overrides.get(placement, self.base)
        return [
            {
                "score": chess.engine.PovScore(chess.engine.Cp(white_cp), chess.WHITE),
                "multipv": 1,
                "pv": [],
            }
        ]


IDENTITY = PieceValueConstants(eval_scale=EvalScale(((0.0, 0.0), (10000.0, 10000.0))))


def sign_of(color: bool) -> int:
    return 1 if color else -1


# --------------------------------------------------------------------------- #
# The two identities                                                           #
# --------------------------------------------------------------------------- #


def test_anchored_values_sum_to_the_eval() -> None:
    board = chess.Board()
    engine = ScriptedEngine(base=40)
    result = compute_piece_values(board, engine, depth=4, constants=IDENTITY)

    total = sum(sign_of(p.color) * p.anchored_cp for p in result.pieces)
    assert total == pytest.approx(result.eval_material_cp, abs=1e-6)


def test_premiums_sum_to_the_material_gap() -> None:
    board = chess.Board("r1bqkb1r/pp3ppp/2n2n2/3N4/4P3/8/PPP2PPP/R1BQKB1R b KQkq - 0 1")
    engine = ScriptedEngine(base=-120)
    result = compute_piece_values(board, engine, depth=4, constants=IDENTITY)

    total = sum(sign_of(p.color) * p.premium_cp for p in result.pieces)
    assert total == pytest.approx(result.gap_cp, abs=1e-6)
    assert result.gap_cp == pytest.approx(result.eval_material_cp - result.material_cp)


def test_the_identity_survives_an_all_zero_ablation() -> None:
    """Every marginal zero must spread the residual, not divide by it."""

    board = chess.Board()
    engine = ScriptedEngine(base=0)  # every probe returns the same eval
    result = compute_piece_values(board, engine, depth=4, constants=IDENTITY)

    assert all(p.raw_cp == 0 for p in result.pieces)
    total = sum(sign_of(p.color) * p.anchored_cp for p in result.pieces)
    assert total == pytest.approx(result.eval_material_cp, abs=1e-6)


# --------------------------------------------------------------------------- #
# Ablation direction                                                           #
# --------------------------------------------------------------------------- #


def test_a_piece_whose_loss_hurts_reads_positive() -> None:
    board = chess.Board("4k3/8/8/8/8/8/3Q4/4K3 w - - 0 1")
    without_queen = "4k3/8/8/8/8/8/8/4K3"
    engine = ScriptedEngine(base=900, overrides={without_queen: 0})
    result = compute_piece_values(board, engine, depth=4, constants=IDENTITY)

    queen = next(p for p in result.pieces if p.square == "d2")
    assert queen.raw_cp == pytest.approx(900)


def test_black_pieces_are_measured_from_their_owners_side() -> None:
    board = chess.Board("4k3/3q4/8/8/8/8/8/4K3 w - - 0 1")
    without_queen = "4k3/8/8/8/8/8/8/4K3"
    engine = ScriptedEngine(base=-900, overrides={without_queen: 0})
    result = compute_piece_values(board, engine, depth=4, constants=IDENTITY)

    queen = next(p for p in result.pieces if p.square == "d7")
    # Owner-POV: removing Black's queen costs Black 900, so the value is +900.
    assert queen.raw_cp == pytest.approx(900)


# --------------------------------------------------------------------------- #
# Board surgery                                                                #
# --------------------------------------------------------------------------- #


def test_removing_a_corner_rook_drops_the_castling_right() -> None:
    board = chess.Board()
    probe = _without(board, chess.H1)
    assert probe is not None
    probe_board, method = probe
    assert method == "ablation"
    assert probe_board.is_valid()
    assert not probe_board.has_kingside_castling_rights(chess.WHITE)
    assert probe_board.has_queenside_castling_rights(chess.WHITE)


def test_removing_the_double_pushed_pawn_clears_en_passant() -> None:
    board = chess.Board(
        "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"
    )
    probe = _without(board, chess.F5)
    assert probe is not None
    probe_board, method = probe
    assert method == "ablation"
    assert probe_board.is_valid()
    assert probe_board.ep_square is None


def test_exposing_the_owners_king_out_of_turn_flips_the_side_to_move() -> None:
    # White's e2 rook shields e1 from the h1 rook along the first rank... use a
    # file pin instead: black rook e8, white rook e2, white king e1, Black to move.
    board = chess.Board("4r2k/8/8/8/8/8/4R3/4K3 b - - 0 1")
    assert board.is_valid()
    probe = _without(board, chess.E2)
    assert probe is not None
    probe_board, method = probe
    assert method == "turn_flipped"
    assert probe_board.turn == chess.WHITE
    assert probe_board.is_valid()


def test_exposing_the_owners_king_in_turn_is_left_alone() -> None:
    """Being in check on your own move is legal — and is the honest answer."""

    board = chess.Board("4r2k/8/8/8/8/8/4R3/4K3 w - - 0 1")
    probe = _without(board, chess.E2)
    assert probe is not None
    probe_board, method = probe
    assert method == "ablation"
    assert probe_board.is_check()


def test_repair_reports_failure_rather_than_returning_a_broken_board() -> None:
    board = chess.Board(None)
    board.set_piece_at(chess.E1, chess.Piece(chess.KING, chess.WHITE))
    board.set_piece_at(chess.E2, chess.Piece(chess.KING, chess.BLACK))
    assert _repair(board, chess.WHITE) is None


# --------------------------------------------------------------------------- #
# Scale, saturation, shrinkage                                                 #
# --------------------------------------------------------------------------- #


def test_eval_scale_interpolates_and_is_odd_symmetric() -> None:
    scale = EvalScale(((0.0, 0.0), (100.0, 50.0), (200.0, 200.0)))
    assert scale.to_material(0) == pytest.approx(0)
    assert scale.to_material(100) == pytest.approx(50)
    assert scale.to_material(50) == pytest.approx(25)
    assert scale.to_material(150) == pytest.approx(125)
    assert scale.to_material(-150) == pytest.approx(-125)


def test_eval_scale_extrapolates_past_the_table() -> None:
    scale = EvalScale(((0.0, 0.0), (100.0, 100.0), (200.0, 300.0)))
    # Final segment runs 2 material per 1 eval.
    assert scale.to_material(300) == pytest.approx(500)


def test_the_shipped_scale_is_monotone() -> None:
    """A non-monotone table would not be invertible, and the fit enforces it."""

    constants = PieceValueConstants.load()
    materials = [constants.eval_scale.to_material(cp) for cp in range(0, 1000, 25)]
    assert materials == sorted(materials)


def test_saturation_is_flagged_not_hidden() -> None:
    board = chess.Board("4k3/8/8/8/8/8/3Q4/4K3 w - - 0 1")
    quiet = compute_piece_values(
        board, ScriptedEngine(base=10), depth=4, constants=IDENTITY
    )
    assert not quiet.saturated
    decided = compute_piece_values(
        board, ScriptedEngine(base=2000), depth=4, constants=IDENTITY
    )
    assert decided.saturated


def test_shrinkage_pulls_toward_the_static_value() -> None:
    board = chess.Board("4k3/8/8/8/8/8/3Q4/4K3 w - - 0 1")
    without_queen = "4k3/8/8/8/8/8/8/4K3"
    engine = ScriptedEngine(base=1500, overrides={without_queen: 0})
    constants = PieceValueConstants(
        eval_scale=EvalScale(((0.0, 0.0), (10000.0, 10000.0))),
        shrinkage=0.5,
        saturation_cp=100_000,
    )
    result = compute_piece_values(board, engine, depth=4, constants=constants)
    queen = next(p for p in result.pieces if p.square == "d2")
    midpoint = queen.static_cp + 0.5 * (queen.anchored_cp - queen.static_cp)
    assert queen.shrunk_cp == pytest.approx(midpoint)


def test_type_scale_rescales_the_raw_measurement() -> None:
    board = chess.Board("4k3/8/8/8/8/8/3Q4/4K3 w - - 0 1")
    without_queen = "4k3/8/8/8/8/8/8/4K3"
    engine = ScriptedEngine(base=900, overrides={without_queen: 0})
    constants = PieceValueConstants(
        eval_scale=EvalScale(((0.0, 0.0), (10000.0, 10000.0))),
        type_scale={**dict.fromkeys(CLASSIC_VALUES_CP, 1.0), chess.QUEEN: 0.5},
        saturation_cp=100_000,
    )
    result = compute_piece_values(board, engine, depth=4, constants=constants)
    queen = next(p for p in result.pieces if p.square == "d2")
    assert queen.raw_cp == pytest.approx(450)


# --------------------------------------------------------------------------- #
# Guards                                                                       #
# --------------------------------------------------------------------------- #


def test_an_illegal_position_is_refused() -> None:
    """Adjacent kings produce plausible-looking numbers that mean nothing."""

    board = chess.Board("8/1P6/P7/8/8/8/6k1/6K1 w - - 0 1")
    assert not board.is_valid()
    with pytest.raises(ValueError, match="not legal"):
        compute_piece_values(board, ScriptedEngine(), depth=4, constants=IDENTITY)


def test_terminal_probes_cost_no_engine_call() -> None:
    """An ablation that leaves the owner mated is answered without searching."""

    # Black king h8, white queen g7 supported by the king: removing the queen
    # ends the mate; removing nothing leaves mate on the board.
    board = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")
    assert board.is_checkmate()
    engine = ScriptedEngine(base=0)
    result = compute_piece_values(board, engine, depth=4, constants=IDENTITY)
    # The baseline is terminal, so it never reaches the engine. Black is to
    # move and mated, so White POV is a full mate score.
    assert result.eval_cp == 1950
    assert result.analyses < len(result.pieces) + 1


def test_kings_are_never_valued() -> None:
    result = compute_piece_values(
        chess.Board(), ScriptedEngine(base=0), depth=4, constants=IDENTITY
    )
    assert len(result.pieces) == 30
    assert all(p.piece_type != chess.KING for p in result.pieces)


def test_as_dict_is_json_shaped() -> None:
    result = compute_piece_values(
        chess.Board(), ScriptedEngine(base=25), depth=4, constants=IDENTITY
    )
    payload = result.as_dict()
    assert payload["eval_cp"] == 25
    assert len(payload["pieces"]) == 30
    first = payload["pieces"][0]
    assert {"square", "raw_cp", "anchored_cp", "premium_cp", "shrunk_cp", "tags"} <= set(
        first
    )
