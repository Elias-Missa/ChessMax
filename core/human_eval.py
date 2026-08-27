"""Human Eval — what the position is worth to a player, not to Stockfish.

Stockfish's evaluation assumes both sides find every move. That is the right
answer to "is this objectively winning" and the wrong answer to "am I winning".
A position where the only refutation is a quiet queen retreat eight plies deep
is lost on the engine's terms and perfectly fine on a 1400's, because nobody at
1400 is finding it.

So this takes the same MultiPV the volatility pass already produced and asks a
rating-conditioned human policy how likely each of those moves actually is::

    human_cp = Σ π_R(m) · eval(m)   +   tail · eval(worst known line)

The tail matters. MultiPV covers the top handful of moves, so the policy's
remaining probability mass sits on moves we have no evaluation for.
Renormalising over the covered set would pretend the player only ever chooses
among the engine's top lines — which biases the number upward precisely in the
messy positions the bar exists for. Valuing the uncovered mass at the *worst*
line we do have is optimistic but bounded, and it errs in a direction we can
name rather than one we cannot.

**One ply, not a playout.** This measures "who is better after one realistic
move", which is what makes an engine-only refutation stop dominating the
number. It does not simulate the rest of the game; a tactic that takes four
accurate moves to punish still shows up as bad here if its first move is
findable. A Maia self-play rollout would answer the deeper question and costs
several orders of magnitude more.

Null-safe by the same rule findability uses: no policy, too little coverage, or
no candidate moves yields ``None``, never ``0``. Zero is a real evaluation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chess

from core.findability import PolicyFn

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "review_advice.json"


@dataclass(frozen=True)
class HumanEvalConstants:
    """Tunables, loaded from JSON so a refit never touches code."""

    rating_offset: int = 500
    rating_default: int = 1500
    rating_min: int = 600
    rating_max: int = 2600
    min_coverage: float = 0.35

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HumanEvalConstants":
        base = cls()
        return cls(
            rating_offset=int(data.get("rating_offset", base.rating_offset)),
            rating_default=int(data.get("rating_default", base.rating_default)),
            rating_min=int(data.get("rating_min", base.rating_min)),
            rating_max=int(data.get("rating_max", base.rating_max)),
            min_coverage=float(data.get("min_coverage", base.min_coverage)),
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "HumanEvalConstants":
        target = Path(path) if path is not None else _CONSTANTS_PATH
        with open(target, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def rating_for(self, user_rating: int | None) -> int:
        """The rating the policy is asked at: the user's, plus the offset."""

        base = self.rating_default if user_rating is None else int(user_rating)
        return max(self.rating_min, min(self.rating_max, base + self.rating_offset))


@dataclass(frozen=True)
class HumanEval:
    """One position's human evaluation, alongside the engine's."""

    cp: int
    """Human-expected evaluation, **White POV**, centipawns."""
    engine_cp: int
    """Stockfish's evaluation of the same position, White POV."""
    delta_cp: int
    """``cp − engine_cp``. Positive means the position is friendlier to a human
    than the engine says — the usual case, since engine-only refutations get
    discounted. Large negative means the *opponent's* best try is easy to find."""
    rating: int
    """The rating the policy was queried at (user rating + offset)."""
    coverage: float
    """Share of the policy's probability mass the MultiPV actually covered."""
    top_move_uci: str | None
    """The move the human policy likes most among the candidates — not
    necessarily the engine's. This is what the review draws as the pink arrow."""
    top_move_p: float
    """That move's probability under the policy."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cp": self.cp,
            "engine_cp": self.engine_cp,
            "delta_cp": self.delta_cp,
            "rating": self.rating,
            "coverage": round(self.coverage, 3),
            "top_move_uci": self.top_move_uci,
            "top_move_p": round(self.top_move_p, 4),
        }


def human_eval(
    fen: str,
    move_evals: list[tuple[chess.Move, int]],
    policy_fn: PolicyFn,
    *,
    user_rating: int | None = None,
    constants: HumanEvalConstants | None = None,
) -> HumanEval | None:
    """Policy-weighted evaluation of ``fen``, or ``None`` when not measurable.

    ``move_evals`` is ``[(move, cp_side_to_move)]`` best-first — exactly the
    shape ``ply.volatility.top_lines`` already carries, so a review costs no
    extra engine work.
    """

    constants = constants or HumanEvalConstants.load()
    if not move_evals:
        return None

    board = chess.Board(fen)
    rating = constants.rating_for(user_rating)
    moves = [move for move, _ in move_evals]

    try:
        policy = policy_fn(fen, rating, moves)
    except Exception:  # noqa: BLE001 — an absent human model is not an error
        return None
    if not policy:
        return None

    # The policy is queried over the candidate moves but returns each move's
    # probability among ALL legal moves, so the mass need not sum to 1 — the
    # shortfall is exactly the tail we have no evaluation for.
    coverage = float(sum(max(0.0, policy.get(move, 0.0)) for move in moves))
    if coverage < constants.min_coverage:
        return None

    engine_stm = int(move_evals[0][1])
    tail_stm = int(move_evals[-1][1])
    tail_mass = max(0.0, 1.0 - coverage)

    expected = sum(
        max(0.0, policy.get(move, 0.0)) * float(cp) for move, cp in move_evals
    )
    expected += tail_mass * float(tail_stm)

    best_move, best_p = max(
        ((move, max(0.0, policy.get(move, 0.0))) for move in moves),
        key=lambda item: item[1],
    )

    # Everything above is side-to-move POV, which is what the engine reports.
    # The bar is White POV, like the eval bar it sits beside.
    flip = 1 if board.turn == chess.WHITE else -1
    human_cp = int(round(expected)) * flip
    engine_cp = engine_stm * flip

    return HumanEval(
        cp=human_cp,
        engine_cp=engine_cp,
        delta_cp=human_cp - engine_cp,
        rating=rating,
        coverage=coverage,
        top_move_uci=best_move.uci() if best_p > 0 else None,
        top_move_p=best_p,
    )


__all__ = ["HumanEval", "HumanEvalConstants", "human_eval"]
