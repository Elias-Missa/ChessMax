"""Endgame Arena rules — which positions qualify, who you face, when a draw sticks.

The mode drills the three endgames that actually decide games, and it picks your
opponent from which one you got:

* **Drawn** — play your own level. A draw is a pass.
* **Winning** — play the level *above*. Converting against someone stronger is
  the skill worth drilling; converting against someone weaker is not practice.
* **Losing** — play the level *below*, and try to hold. A lost endgame against a
  peer is a formality; against someone weaker it is a real save.

The draw offer is the teaching mechanism. Maia accepts only when it is **not
winning**, so claiming a draw you actually hold ends the game — and claiming one
you threw away does not. You cannot offer your way out of a blunder; you have to
sit in the position you made.

Everything here is pure: no engine, no database, no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chess

from core.piece_features import CLASSIC_VALUES_CP, material_cp

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "endgame.json"

DRAWN = "drawn"
WINNING = "winning"
LOSING = "losing"
BUCKETS: tuple[str, ...] = (DRAWN, WINNING, LOSING)

#: Maia ships one net per rung. One step is 200 Elo; the ladder clamps rather
#: than wraps, so a 1900 player converting still faces 1900 and not 1100.
MAIA_NETS: tuple[int, ...] = (1100, 1300, 1500, 1700, 1900)


@dataclass(frozen=True)
class EndgameConstants:
    max_pieces: int = 10
    max_non_king_material_cp: int = 1500
    allow_queens_below_pieces: int = 6
    drawn_cp: int = 60
    decided_cp: int = 200
    max_decided_cp: int = 900
    ladder_steps: dict[str, int] = field(
        default_factory=lambda: {DRAWN: 0, WINNING: 1, LOSING: -1}
    )
    draw_accept_max_cp: int = 90
    min_plies_before_offer: int = 6
    blunder_cp: int = 250

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EndgameConstants":
        base = cls()
        steps = dict(base.ladder_steps)
        for key, value in (data.get("ladder_steps") or {}).items():
            if key in BUCKETS:
                steps[key] = int(value)
        return cls(
            max_pieces=int(data.get("max_pieces", base.max_pieces)),
            max_non_king_material_cp=int(
                data.get("max_non_king_material_cp", base.max_non_king_material_cp)
            ),
            allow_queens_below_pieces=int(
                data.get("allow_queens_below_pieces", base.allow_queens_below_pieces)
            ),
            drawn_cp=int(data.get("drawn_cp", base.drawn_cp)),
            decided_cp=int(data.get("decided_cp", base.decided_cp)),
            max_decided_cp=int(data.get("max_decided_cp", base.max_decided_cp)),
            ladder_steps=steps,
            draw_accept_max_cp=int(
                data.get("draw_accept_max_cp", base.draw_accept_max_cp)
            ),
            min_plies_before_offer=int(
                data.get("min_plies_before_offer", base.min_plies_before_offer)
            ),
            blunder_cp=int(data.get("blunder_cp", base.blunder_cp)),
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "EndgameConstants":
        target = Path(path) if path is not None else _CONSTANTS_PATH
        with open(target, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


# --------------------------------------------------------------------------- #
# Which positions qualify                                                      #
# --------------------------------------------------------------------------- #


def is_endgame(board: chess.Board, constants: EndgameConstants | None = None) -> bool:
    """Is this an endgame worth drilling?

    Piece count is the honest signal, with one carve-out: a queen still on the
    board carries middlegame character however few pawns are left, so queens
    disqualify unless almost nothing else remains (a queen ending proper).
    """

    constants = constants or EndgameConstants.load()
    pieces = chess.popcount(board.occupied)
    if pieces > constants.max_pieces:
        return False

    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(
        board.pieces(chess.QUEEN, chess.BLACK)
    )
    if queens and pieces > constants.allow_queens_below_pieces:
        return False

    non_king = sum(
        material_cp(board, color, CLASSIC_VALUES_CP)
        for color in (chess.WHITE, chess.BLACK)
    )
    return non_king <= constants.max_non_king_material_cp


# --------------------------------------------------------------------------- #
# Which bucket, and who you face                                               #
# --------------------------------------------------------------------------- #


def bucket_for_eval(
    user_eval_cp: int | None, constants: EndgameConstants | None = None
) -> str | None:
    """Bucket a position by how the **user** stands in it.

    ``None`` for the dead zone between ``drawn_cp`` and ``decided_cp``, and for
    anything past ``max_decided_cp``. Both exclusions are deliberate: a +90cp
    endgame is neither a draw to hold nor a win to convert, and calling it
    either teaches the wrong lesson; a +1200cp one is already over.
    """

    if user_eval_cp is None:
        return None
    constants = constants or EndgameConstants.load()
    magnitude = abs(int(user_eval_cp))

    if magnitude <= constants.drawn_cp:
        return DRAWN
    if magnitude < constants.decided_cp or magnitude > constants.max_decided_cp:
        return None
    return WINNING if user_eval_cp > 0 else LOSING


def maia_rating_for(
    bucket: str,
    user_rating: int,
    constants: EndgameConstants | None = None,
    nets: tuple[int, ...] = MAIA_NETS,
) -> int:
    """The Maia net to face, laddered off the bucket.

    The user's rating is first snapped to the nearest rung so the step is a real
    step — off-rung ratings otherwise round in a way that makes ``winning`` and
    ``drawn`` land on the same net.
    """

    constants = constants or EndgameConstants.load()
    nearest = min(range(len(nets)), key=lambda i: abs(nets[i] - user_rating))
    step = constants.ladder_steps.get(bucket, 0)
    return nets[max(0, min(len(nets) - 1, nearest + step))]


# --------------------------------------------------------------------------- #
# The draw offer                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DrawVerdict:
    accepted: bool
    reason: str
    """``held`` | ``too_early`` | ``still_winning`` — what to tell the player."""
    message: str


def draw_offer_verdict(
    *,
    maia_eval_cp: int | None,
    plies_played: int,
    constants: EndgameConstants | None = None,
) -> DrawVerdict:
    """Does Maia take the draw?

    Only when it is **not winning**, judged from *its* side. That is the whole
    mechanism: a position you genuinely hold ends when you claim it, and one you
    blundered keeps going, because offering a draw is not a way out of a
    position you threw.
    """

    constants = constants or EndgameConstants.load()

    if plies_played < constants.min_plies_before_offer:
        return DrawVerdict(
            accepted=False,
            reason="too_early",
            message="Play it out a little first — the draw has to be earned.",
        )

    if maia_eval_cp is not None and maia_eval_cp > constants.draw_accept_max_cp:
        return DrawVerdict(
            accepted=False,
            reason="still_winning",
            message=(
                "Declined — this is winning now, so there is nothing to agree to. "
                "You have to hold the position you made."
            ),
        )

    return DrawVerdict(
        accepted=True,
        reason="held",
        message="Draw agreed. The position was holdable and you claimed it.",
    )


# --------------------------------------------------------------------------- #
# Grading the finished game                                                    #
# --------------------------------------------------------------------------- #

#: ``(bucket, result) -> (outcome, passed)``. ``result`` is from the user's
#: point of view: ``win`` / ``draw`` / ``loss``.
_OUTCOMES: dict[tuple[str, str], tuple[str, bool]] = {
    (DRAWN, "win"): ("won_a_draw", True),
    (DRAWN, "draw"): ("held", True),
    (DRAWN, "loss"): ("lost_a_draw", False),
    (WINNING, "win"): ("converted", True),
    (WINNING, "draw"): ("let_it_slip", False),
    (WINNING, "loss"): ("threw_it", False),
    (LOSING, "win"): ("turned_it_around", True),
    (LOSING, "draw"): ("saved", True),
    (LOSING, "loss"): ("lost_as_expected", False),
}

_OUTCOME_TEXT: dict[str, str] = {
    "won_a_draw": "You won a position that started level.",
    "held": "Held. That is the pass mark from a level start.",
    "lost_a_draw": "Lost from a level start — the draw was there.",
    "converted": "Converted, against a stronger opponent. That is the whole exercise.",
    "let_it_slip": "Drawn from a winning start. The win was there to be taken.",
    "threw_it": "Lost from a winning start.",
    "turned_it_around": "Won a position that started lost.",
    "saved": "Saved the half point from a losing start — that is a real result.",
    "lost_as_expected": "Lost from a losing start. The save was the target.",
}


def grade_outcome(bucket: str, result: str) -> tuple[str, bool, str]:
    """``(outcome, passed, message)`` for a finished arena game."""

    outcome, passed = _OUTCOMES.get((bucket, result), ("unknown", False))
    return outcome, passed, _OUTCOME_TEXT.get(outcome, "")


__all__ = [
    "BUCKETS",
    "DRAWN",
    "LOSING",
    "MAIA_NETS",
    "WINNING",
    "DrawVerdict",
    "EndgameConstants",
    "bucket_for_eval",
    "draw_offer_verdict",
    "grade_outcome",
    "is_endgame",
    "maia_rating_for",
]
