"""Attach the Human Eval bar and the steering advice to an analysed game.

Mirrors :mod:`chess_vol.findability_review`: both reuse the MultiPV the
volatility pass already produced, so neither costs an extra engine search, and
both mutate the ply list in place and never raise — an absent human model
leaves the fields ``None`` rather than sinking a review.

The two live together because they answer the same question from opposite
sides. Human Eval asks *whose position is this really, for a player of this
strength*; the steering advice asks *given that, is it as sharp as you want it
to be*.
"""

from __future__ import annotations

from typing import Any

import chess

from chess_vol.analyze import PlyResult
from chess_vol.findability_review import move_evals_from_ply
from core.evaluation import win_prob_cp
from core.findability import PolicyFn
from core.human_eval import HumanEvalConstants, human_eval
from core.vol_advice import VolAdviceConstants, vol_advice


def attach_human_eval(
    results: list[PlyResult],
    policy_fn: PolicyFn,
    constants: HumanEvalConstants | None = None,
    *,
    user_rating: int | None = None,
) -> None:
    """Set ``ply.human_eval`` on every scorable ply, in place."""

    consts = constants if constants is not None else HumanEvalConstants.load()
    for ply in results:
        if ply.volatility is None or ply.volatility.reason is not None:
            continue
        move_evals = [
            (me.move, int(me.cp))
            for me in move_evals_from_ply(ply)
            if me.cp is not None
        ]
        if not move_evals:
            continue
        try:
            ply.human_eval = human_eval(
                ply.fen_before,
                move_evals,
                policy_fn,
                user_rating=user_rating,
                constants=consts,
            )
        except Exception:  # noqa: BLE001 — an enrichment must never sink a review
            ply.human_eval = None


def attach_vol_advice(
    results: list[PlyResult],
    constants: VolAdviceConstants | None = None,
) -> None:
    """Set ``ply.vol_advice`` on every ply whose consequences we can see.

    The advice is about the position the move *steered into*, so it reads the
    **next** ply — which is why the last ply of a game never gets one. The next
    ply's evaluation is from the opponent's point of view (they are to move
    there), so it is flipped back to the mover's before being judged.
    """

    consts = constants if constants is not None else VolAdviceConstants.load()
    for index, ply in enumerate(results):
        nxt = results[index + 1] if index + 1 < len(results) else None
        if nxt is None or nxt.volatility is None or ply.volatility is None:
            continue
        # `nxt.eval_cp` is side-to-move POV at the next ply, i.e. the opponent's.
        mover_win_prob = 1.0 - win_prob_cp(nxt.eval_cp)
        try:
            ply.vol_advice = vol_advice(
                win_prob=mover_win_prob,
                volatility=nxt.volatility.score,
                volatility_before=ply.volatility.score,
                constants=consts,
            )
        except Exception:  # noqa: BLE001
            ply.vol_advice = None


def attach_all(
    results: list[PlyResult],
    policy_fn: PolicyFn | None,
    *,
    user_rating: int | None = None,
) -> None:
    """Both enrichments. ``policy_fn=None`` still gets the steering advice,
    which needs no human model at all."""

    if policy_fn is not None:
        attach_human_eval(results, policy_fn, user_rating=user_rating)
    attach_vol_advice(results)


__all__ = ["attach_all", "attach_human_eval", "attach_vol_advice"]
