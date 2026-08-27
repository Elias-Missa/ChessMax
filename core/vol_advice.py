"""Steering advice: is this position as sharp as you want it to be?

Volatility says how much the evaluation swings across the candidate moves. That
number is neutral on its own — whether you *want* it high depends entirely on
who is winning:

* **Winning and sharp is bad for you.** Every extra chance in the position is
  another chance for *you* to be the one who goes wrong, and you are the one
  with something to lose. Trade, simplify, take the boring line.
* **Losing and quiet is bad for you.** A stable position converts your
  opponent's advantage for free — they have nothing to solve. You want mess,
  because mess is where a lost game becomes a drawn one.

The second is the counter-intuitive half and the one players get wrong: the
instinct when losing is to trade into an endgame and "hold", which is exactly
how a lost middlegame becomes a lost endgame with no chances left in it.

Read against the state you steered *into* — the position after the move — and
against how much the move itself moved volatility, so a quiet move in a long-won
position does not nag on every ply.

Pure: no engine, no policy, no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "review_advice.json"

KEEP_IT_SIMPLE = "keep_it_simple"
MAKE_IT_MESSY = "make_it_messy"


@dataclass(frozen=True)
class VolAdviceConstants:
    winning_win_prob: float = 0.75
    losing_win_prob: float = 0.25
    high_volatility: float = 55.0
    low_volatility: float = 25.0
    swing_volatility: float = 12.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VolAdviceConstants":
        base = cls()
        return cls(
            winning_win_prob=float(data.get("winning_win_prob", base.winning_win_prob)),
            losing_win_prob=float(data.get("losing_win_prob", base.losing_win_prob)),
            high_volatility=float(data.get("high_volatility", base.high_volatility)),
            low_volatility=float(data.get("low_volatility", base.low_volatility)),
            swing_volatility=float(data.get("swing_volatility", base.swing_volatility)),
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "VolAdviceConstants":
        target = Path(path) if path is not None else _CONSTANTS_PATH
        with open(target, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


@dataclass(frozen=True)
class VolAdvice:
    """One ply's steering note. ``kind`` names which of the two rules fired."""

    kind: str
    severity: str
    """``"warn"`` when the move itself caused the swing, ``"info"`` when the
    position simply is that way."""
    headline: str
    detail: str
    win_prob: float
    volatility: float
    volatility_delta: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "headline": self.headline,
            "detail": self.detail,
            "win_prob": round(self.win_prob, 4),
            "volatility": round(self.volatility, 1),
            "volatility_delta": (
                round(self.volatility_delta, 1)
                if self.volatility_delta is not None
                else None
            ),
        }


def vol_advice(
    *,
    win_prob: float | None,
    volatility: float | None,
    volatility_before: float | None = None,
    constants: VolAdviceConstants | None = None,
) -> VolAdvice | None:
    """Steering note for one ply, or ``None`` when the position is unremarkable.

    ``win_prob`` and ``volatility`` describe the position **after** the move,
    from the **mover's** point of view. ``volatility_before`` is the position
    the mover chose from, and only affects severity.
    """

    if win_prob is None or volatility is None:
        return None
    constants = constants or VolAdviceConstants.load()

    delta = (
        volatility - volatility_before if volatility_before is not None else None
    )

    if win_prob >= constants.winning_win_prob and volatility >= constants.high_volatility:
        caused = delta is not None and delta >= constants.swing_volatility
        return VolAdvice(
            kind=KEEP_IT_SIMPLE,
            severity="warn" if caused else "info",
            headline=(
                "This sharpened a won position"
                if caused
                else "Winning, but the position is sharp"
            ),
            detail=(
                "You are winning, so every extra chance in the position is a "
                "chance for you to be the one who goes wrong. Trading pieces and "
                "taking the boring line costs you nothing here and takes their "
                "counterplay away."
            ),
            win_prob=win_prob,
            volatility=volatility,
            volatility_delta=delta,
        )

    if win_prob <= constants.losing_win_prob and volatility <= constants.low_volatility:
        caused = delta is not None and delta <= -constants.swing_volatility
        return VolAdvice(
            kind=MAKE_IT_MESSY,
            severity="warn" if caused else "info",
            headline=(
                "This calmed a position you needed messy"
                if caused
                else "Losing, and the position is quiet"
            ),
            detail=(
                "You are worse, and a stable position converts their advantage "
                "for free — there is nothing left for them to get wrong. Keep "
                "pieces on and pick the line with the most ways to go astray, "
                "even at some objective cost."
            ),
            win_prob=win_prob,
            volatility=volatility,
            volatility_delta=delta,
        )

    return None


__all__ = [
    "KEEP_IT_SIMPLE",
    "MAKE_IT_MESSY",
    "VolAdvice",
    "VolAdviceConstants",
    "vol_advice",
]
