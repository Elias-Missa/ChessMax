"""Endgame Arena rules (core/endgame.py). Pure — no engine, no DB."""

from __future__ import annotations

import chess

from core.endgame import (
    DRAWN,
    LOSING,
    MAIA_NETS,
    WINNING,
    EndgameConstants,
    bucket_for_eval,
    draw_offer_verdict,
    grade_outcome,
    is_endgame,
    maia_rating_for,
)

C = EndgameConstants()


# --------------------------------------------------------------------------- #
# What counts as an endgame                                                    #
# --------------------------------------------------------------------------- #


def test_the_starting_position_is_not_an_endgame() -> None:
    assert not is_endgame(chess.Board(), C)


def test_a_rook_and_pawn_ending_qualifies() -> None:
    board = chess.Board("8/5pk1/8/8/8/8/5PK1/4R3 w - - 0 1")
    assert is_endgame(board, C)


def test_a_bare_king_and_pawn_ending_qualifies() -> None:
    assert is_endgame(chess.Board("8/8/4k3/8/4P3/4K3/8/8 w - - 0 1"), C)


def test_a_queen_on_a_full_board_disqualifies() -> None:
    """A queen carries middlegame character however few pawns are left."""

    board = chess.Board("3qk3/pppppppp/8/8/8/8/PPPPPPPP/3QK3 w - - 0 1")
    assert not is_endgame(board, C)


def test_a_bare_queen_ending_still_qualifies() -> None:
    board = chess.Board("4k3/8/8/8/8/8/5Q2/4K3 w - - 0 1")
    assert is_endgame(board, C)


def test_too_many_pieces_disqualifies_even_when_light() -> None:
    board = chess.Board("4k3/pppppppp/8/8/8/8/PPPPPPPP/4K3 w - - 0 1")
    assert not is_endgame(board, C)


# --------------------------------------------------------------------------- #
# Buckets                                                                      #
# --------------------------------------------------------------------------- #


def test_a_level_position_is_drawn() -> None:
    assert bucket_for_eval(0, C) == DRAWN
    assert bucket_for_eval(55, C) == DRAWN
    assert bucket_for_eval(-55, C) == DRAWN


def test_a_clear_plus_is_winning_and_a_clear_minus_is_losing() -> None:
    assert bucket_for_eval(400, C) == WINNING
    assert bucket_for_eval(-400, C) == LOSING


def test_the_dead_zone_is_discarded_rather_than_assigned() -> None:
    """+90cp is neither a draw to hold nor a win to convert."""

    assert bucket_for_eval(90, C) is None
    assert bucket_for_eval(-90, C) is None


def test_an_already_over_position_is_discarded() -> None:
    assert bucket_for_eval(2000, C) is None
    assert bucket_for_eval(-2000, C) is None


def test_a_missing_eval_yields_no_bucket() -> None:
    assert bucket_for_eval(None, C) is None


# --------------------------------------------------------------------------- #
# The Maia ladder — the point of the mode                                      #
# --------------------------------------------------------------------------- #


def test_a_drawn_position_is_played_against_your_own_level() -> None:
    assert maia_rating_for(DRAWN, 1500, C) == 1500


def test_a_won_position_is_played_against_the_level_above() -> None:
    """Converting against someone weaker is not practice."""

    assert maia_rating_for(WINNING, 1500, C) == 1700


def test_a_lost_position_is_played_against_the_level_below() -> None:
    """Holding against a peer is a formality; against someone weaker it is real."""

    assert maia_rating_for(LOSING, 1500, C) == 1300


def test_the_ladder_clamps_rather_than_wraps() -> None:
    assert maia_rating_for(WINNING, 1900, C) == MAIA_NETS[-1]
    assert maia_rating_for(LOSING, 1100, C) == MAIA_NETS[0]


def test_an_off_rung_rating_snaps_before_stepping() -> None:
    """Without snapping first, 1580 would round in a way that collapses two
    buckets onto the same net."""

    assert maia_rating_for(DRAWN, 1580, C) == 1500
    assert maia_rating_for(WINNING, 1580, C) == 1700
    assert maia_rating_for(LOSING, 1580, C) == 1300


# --------------------------------------------------------------------------- #
# The draw offer                                                               #
# --------------------------------------------------------------------------- #


def test_a_held_position_is_agreed() -> None:
    verdict = draw_offer_verdict(maia_eval_cp=10, plies_played=20, constants=C)
    assert verdict.accepted
    assert verdict.reason == "held"


def test_a_blundered_position_is_declined_and_play_continues() -> None:
    """The whole mechanism: you cannot offer your way out of what you threw."""

    verdict = draw_offer_verdict(maia_eval_cp=600, plies_played=20, constants=C)
    assert not verdict.accepted
    assert verdict.reason == "still_winning"


def test_maia_being_worse_still_accepts() -> None:
    """Only *winning* declines — Maia does not play on from a lost position."""

    verdict = draw_offer_verdict(maia_eval_cp=-500, plies_played=20, constants=C)
    assert verdict.accepted


def test_a_draw_cannot_be_claimed_on_move_one() -> None:
    verdict = draw_offer_verdict(maia_eval_cp=0, plies_played=1, constants=C)
    assert not verdict.accepted
    assert verdict.reason == "too_early"


def test_an_unknown_eval_does_not_block_the_draw() -> None:
    """No engine is not a reason to trap someone in a held position."""

    verdict = draw_offer_verdict(maia_eval_cp=None, plies_played=20, constants=C)
    assert verdict.accepted


# --------------------------------------------------------------------------- #
# Grading                                                                      #
# --------------------------------------------------------------------------- #


def test_a_draw_from_level_is_a_pass_and_from_winning_is_not() -> None:
    _, held, _ = grade_outcome(DRAWN, "draw")
    _, slipped, _ = grade_outcome(WINNING, "draw")
    assert held
    assert not slipped


def test_a_draw_from_a_losing_start_is_a_pass() -> None:
    outcome, passed, message = grade_outcome(LOSING, "draw")
    assert outcome == "saved"
    assert passed
    assert message


def test_every_bucket_and_result_pair_is_graded() -> None:
    for bucket in (DRAWN, WINNING, LOSING):
        for result in ("win", "draw", "loss"):
            outcome, _, message = grade_outcome(bucket, result)
            assert outcome != "unknown", (bucket, result)
            assert message


def test_shipped_constants_load_and_are_ordered() -> None:
    loaded = EndgameConstants.load()
    assert loaded.drawn_cp < loaded.decided_cp < loaded.max_decided_cp
    assert loaded.ladder_steps[WINNING] > 0 > loaded.ladder_steps[LOSING]
