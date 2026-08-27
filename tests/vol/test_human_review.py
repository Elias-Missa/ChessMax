"""Attaching Human Eval + steering advice to an analysed game. Engine-free."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import chess

from chess_vol.human_review import attach_all, attach_human_eval, attach_vol_advice
from core.vol_advice import KEEP_IT_SIMPLE, MAKE_IT_MESSY


@dataclass
class FakeTopLine:
    uci: str
    san: str
    eval_cp: int
    pv_san: list[str] = field(default_factory=list)


@dataclass
class FakeVol:
    score: float | None
    reason: str | None = None
    top_lines: list[FakeTopLine] = field(default_factory=list)


@dataclass
class FakePly:
    ply: int
    fen_before: str
    eval_cp: int
    volatility: Any
    review: Any = None
    findability: Any = None
    human_eval: Any = None
    vol_advice: Any = None


def start_ply(volatility: float, eval_cp: int, lines: list[tuple[str, str, int]]):
    return FakePly(
        ply=1,
        fen_before=chess.Board().fen(),
        eval_cp=eval_cp,
        volatility=FakeVol(
            score=volatility,
            top_lines=[FakeTopLine(u, s, cp) for u, s, cp in lines],
        ),
    )


def uniform_policy(fen, rating, moves):
    return {m: 1.0 / max(1, len(moves)) for m in moves}


# --------------------------------------------------------------------------- #
# Human Eval                                                                   #
# --------------------------------------------------------------------------- #


def test_human_eval_is_attached_from_the_existing_multipv() -> None:
    plies = [start_ply(30.0, 100, [("e2e4", "e4", 100), ("d2d4", "d4", 0)])]
    attach_human_eval(plies, uniform_policy)
    assert plies[0].human_eval is not None
    assert plies[0].human_eval.engine_cp == 100
    # Half on each covered move, and no tail mass left over.
    assert plies[0].human_eval.cp == 50


def test_a_gated_ply_is_left_alone() -> None:
    plies = [start_ply(30.0, 100, [("e2e4", "e4", 100)])]
    plies[0].volatility.reason = "only_move"
    attach_human_eval(plies, uniform_policy)
    assert plies[0].human_eval is None


def test_no_policy_leaves_it_null_never_zero() -> None:
    plies = [start_ply(30.0, 100, [("e2e4", "e4", 100)])]
    attach_human_eval(plies, lambda fen, rating, moves: {})
    assert plies[0].human_eval is None


def test_a_broken_policy_does_not_sink_the_review() -> None:
    def boom(fen, rating, moves):
        raise RuntimeError("lc0 died mid-game")

    plies = [start_ply(30.0, 100, [("e2e4", "e4", 100)])]
    attach_human_eval(plies, boom)
    assert plies[0].human_eval is None


# --------------------------------------------------------------------------- #
# Steering advice                                                              #
# --------------------------------------------------------------------------- #


def test_sharpening_a_won_position_warns() -> None:
    """Ply 0 is winning; the move it made lands in a much sharper position."""

    first = start_ply(20.0, 600, [("e2e4", "e4", 600)])
    # Next ply is the opponent to move at -600 (so the mover is still winning),
    # and the position is now volatile.
    second = start_ply(75.0, -600, [("e7e5", "e5", -600)])
    plies = [first, second]
    attach_vol_advice(plies)

    assert plies[0].vol_advice is not None
    assert plies[0].vol_advice.kind == KEEP_IT_SIMPLE
    assert plies[0].vol_advice.severity == "warn"
    # The last ply has no successor, so it never gets advice.
    assert plies[1].vol_advice is None


def test_trading_down_while_losing_warns() -> None:
    first = start_ply(60.0, -600, [("a2a3", "a3", -600)])
    # Opponent to move at +600 -> the mover is losing; and it is now quiet.
    second = start_ply(5.0, 600, [("a7a6", "a6", 600)])
    plies = [first, second]
    attach_vol_advice(plies)

    assert plies[0].vol_advice is not None
    assert plies[0].vol_advice.kind == MAKE_IT_MESSY
    assert plies[0].vol_advice.severity == "warn"


def test_an_equal_game_gets_no_advice() -> None:
    plies = [start_ply(40.0, 10, [("e2e4", "e4", 10)]),
             start_ply(40.0, -10, [("e7e5", "e5", -10)])]
    attach_vol_advice(plies)
    assert all(p.vol_advice is None for p in plies)


def test_attach_all_still_gives_advice_without_a_human_model() -> None:
    """The steering rule needs no policy — it must survive a Maia-less box."""

    plies = [start_ply(20.0, 600, [("e2e4", "e4", 600)]),
             start_ply(75.0, -600, [("e7e5", "e5", -600)])]
    attach_all(plies, None)
    assert plies[0].human_eval is None
    assert plies[0].vol_advice is not None
