"""Human Eval (core/human_eval.py). Engine-free and Maia-free.

The policy is a plain dict here, which is the whole point of the ``PolicyFn``
contract: the arithmetic can be pinned exactly without lc0 or torch installed.
"""

from __future__ import annotations

import chess
import pytest

from core.human_eval import HumanEval, HumanEvalConstants, human_eval

START = chess.Board().fen()
CONSTANTS = HumanEvalConstants()


def policy_of(mapping: dict[str, float]):
    """A PolicyFn serving fixed probabilities keyed by UCI."""

    def fn(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
        return {m: mapping.get(m.uci(), 0.0) for m in moves}

    return fn


def evals(pairs: list[tuple[str, int]]) -> list[tuple[chess.Move, int]]:
    return [(chess.Move.from_uci(u), cp) for u, cp in pairs]


# --------------------------------------------------------------------------- #
# The expectation                                                              #
# --------------------------------------------------------------------------- #


def test_full_coverage_is_a_plain_weighted_average() -> None:
    me = evals([("e2e4", 100), ("d2d4", 0)])
    result = human_eval(
        START, me, policy_of({"e2e4": 0.5, "d2d4": 0.5}), constants=CONSTANTS
    )
    assert result is not None
    assert result.cp == 50
    assert result.engine_cp == 100
    assert result.delta_cp == -50


def test_uncovered_mass_is_valued_at_the_worst_known_line() -> None:
    """Renormalising instead would pretend the tail plays like the top moves."""

    me = evals([("e2e4", 100), ("d2d4", -100)])
    # Half the mass sits on moves we have no evaluation for.
    result = human_eval(
        START, me, policy_of({"e2e4": 0.5, "d2d4": 0.0}), constants=CONSTANTS
    )
    assert result is not None
    # 0.5*100 + 0.5*(-100) = 0, not the 100 a renormalised average would give.
    assert result.cp == 0


def test_an_engine_only_refutation_barely_moves_the_bar() -> None:
    """The motivating case: Stockfish sees a loss nobody at this level finds."""

    # Black to move. Their one saving resource is worth -800 to White; every
    # human continuation leaves White comfortable.
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
    me = evals([("g8f6", 800), ("b8c6", -100), ("d7d5", -120)])
    # A human at this level plays the natural developing moves.
    result = human_eval(
        fen, me, policy_of({"g8f6": 0.03, "b8c6": 0.5, "d7d5": 0.42}), constants=CONSTANTS
    )
    assert result is not None
    # Engine (side-to-move Black, so White POV flips): the best line reads -800.
    assert result.engine_cp == -800
    # The human bar is nowhere near it, because the refutation is not played.
    assert result.cp > -200
    assert result.delta_cp > 500


def test_white_pov_flips_for_black_to_move() -> None:
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
    me = evals([("e7e5", 100)])
    result = human_eval(fen, me, policy_of({"e7e5": 1.0}), constants=CONSTANTS)
    assert result is not None
    # +100 for the side to move (Black) is -100 for White.
    assert result.cp == -100
    assert result.engine_cp == -100


# --------------------------------------------------------------------------- #
# Null-safety — zero is a real evaluation, so absence must not read as zero     #
# --------------------------------------------------------------------------- #


def test_no_policy_yields_none_not_zero() -> None:
    me = evals([("e2e4", 100)])
    assert human_eval(START, me, policy_of({}), constants=CONSTANTS) is None


def test_a_raising_policy_yields_none_rather_than_sinking_the_review() -> None:
    def boom(fen, rating, moves):
        raise RuntimeError("lc0 died")

    assert human_eval(START, evals([("e2e4", 5)]), boom, constants=CONSTANTS) is None


def test_no_candidates_yields_none() -> None:
    assert human_eval(START, [], policy_of({"e2e4": 1.0}), constants=CONSTANTS) is None


def test_thin_coverage_is_refused() -> None:
    """Below min_coverage the tail dominates and the number means little."""

    me = evals([("e2e4", 100), ("d2d4", 90)])
    thin = human_eval(
        START, me, policy_of({"e2e4": 0.05}), constants=CONSTANTS
    )
    assert thin is None
    ok = human_eval(START, me, policy_of({"e2e4": 0.9}), constants=CONSTANTS)
    assert ok is not None


# --------------------------------------------------------------------------- #
# Rating plumbing                                                              #
# --------------------------------------------------------------------------- #


def test_the_policy_is_asked_at_the_users_rating_plus_the_offset() -> None:
    seen: list[int] = []

    def spy(fen, rating, moves):
        seen.append(rating)
        return {m: 1.0 / len(moves) for m in moves}

    human_eval(START, evals([("e2e4", 0)]), spy, user_rating=1200, constants=CONSTANTS)
    assert seen == [1200 + CONSTANTS.rating_offset]


def test_a_missing_user_rating_falls_back_to_the_default() -> None:
    seen: list[int] = []

    def spy(fen, rating, moves):
        seen.append(rating)
        return {m: 1.0 for m in moves}

    human_eval(START, evals([("e2e4", 0)]), spy, user_rating=None, constants=CONSTANTS)
    assert seen == [CONSTANTS.rating_default + CONSTANTS.rating_offset]


def test_the_queried_rating_is_clamped_to_the_models_range() -> None:
    assert CONSTANTS.rating_for(3000) == CONSTANTS.rating_max
    assert CONSTANTS.rating_for(10) == CONSTANTS.rating_min


# --------------------------------------------------------------------------- #
# The human's own favourite                                                    #
# --------------------------------------------------------------------------- #


def test_the_top_human_move_can_differ_from_the_engines() -> None:
    """This is what the review draws as the pink 'strong at your level' arrow."""

    me = evals([("e2e4", 100), ("d2d4", 40)])
    result = human_eval(
        START, me, policy_of({"e2e4": 0.1, "d2d4": 0.8}), constants=CONSTANTS
    )
    assert result is not None
    assert result.top_move_uci == "d2d4"
    assert result.top_move_p == pytest.approx(0.8)


def test_shipped_constants_load() -> None:
    loaded = HumanEvalConstants.load()
    assert loaded.rating_offset == 500
    assert 0.0 < loaded.min_coverage < 1.0


def test_as_dict_is_json_shaped() -> None:
    result = human_eval(START, evals([("e2e4", 30)]), policy_of({"e2e4": 1.0}))
    assert result is not None
    payload = result.as_dict()
    assert set(payload) == {
        "cp", "engine_cp", "delta_cp", "rating", "coverage",
        "top_move_uci", "top_move_p",
    }
