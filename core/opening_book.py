"""Ranked candidate moves for one position, with the evidence behind each.

This is the builder's engine room: given a position, produce the list of moves
worth considering, each carrying *why* — how often masters play it, how people
at your rating score with it, what the engine thinks, and which of the three
dots it lights (:mod:`core.opening_signals`).

The ranking follows the clone spec's §6.1 formula::

    rank_score = w_masters·popularity + w_peer·peer + w_engine·quality
                 + w_recency·recency − obscure_penalty

Three things about it are deliberate and easy to get wrong:

* **Popularity is normalized within the position, not absolute.** ``log(1 + n)``
  on raw counts makes move one of the game outrank every move after it forever,
  because the opening position has more games behind it than any position
  downstream. Dividing by the busiest move at *this* position asks the question
  that matters — is this the main line here — and makes scores comparable across
  plies.
* **Engine quality is centipawn *loss*, not evaluation.** Near the start every
  reasonable move evaluates within a few centipawns; ranking on raw eval ranks
  on search noise. Loss against the position's own best move is the signal.
* **No legal move is ever suppressed.** The spec is explicit (§2.3, §10) that
  "Something else…" must work: ranking decides order and what is shown by
  default, never what is *allowed*. :func:`rank_candidates` will happily return
  a move with no explorer data at all, flagged ``low_sample``.

Pure: no engine, no network, no database. The server assembles the inputs.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import chess

from core.opening_signals import MoveSignals, row_games, row_score

_CONSTANTS_PATH = Path(__file__).resolve().parent / "constants" / "opening_book.json"


@dataclass(frozen=True)
class OpeningBookConstants:
    w_masters: float = 0.45
    w_peer: float = 0.25
    w_engine: float = 0.20
    w_recency: float = 0.10
    engine_loss_scale_cp: float = 60.0
    peer_prior_games: float = 12.0
    obscure_min_share: float = 0.02
    obscure_penalty: float = 0.25
    obscure_forgive_cp: float = 30.0
    min_candidate_games: int = 5
    default_multipv: int = 6
    default_depth: int = 18
    maia_rating: int = 1900

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OpeningBookConstants":
        base = cls()
        kwargs: dict[str, Any] = {}
        for f in base.__dataclass_fields__:  # noqa: SLF001 — dataclass API
            if f in data:
                current = getattr(base, f)
                kwargs[f] = type(current)(data[f])
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Path | None = None) -> "OpeningBookConstants":
        target = path or _CONSTANTS_PATH
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_dict({k: v for k, v in raw.items() if not k.startswith("_")})


@dataclass
class Candidate:
    """One legal move at a position, with everything known about it."""

    uci: str
    san: str

    # Masters corpus
    master_games: int = 0
    master_share: float = 0.0

    # Peer corpus (the user's own rating band)
    peer_games: int = 0
    peer_score: float | None = None
    peer_wins: int = 0
    peer_draws: int = 0
    peer_losses: int = 0
    peer_share: float = 0.0
    average_rating: int | None = None

    # Engine
    eval_cp: int | None = None
    loss_cp: int | None = None

    # Derived
    rank_score: float = 0.0
    signals: MoveSignals = field(default_factory=MoveSignals)
    opening_name: str | None = None
    opening_eco: str | None = None
    low_sample: bool = True
    obscure: bool = False
    in_repertoire: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "uci": self.uci,
            "san": self.san,
            "master_games": self.master_games,
            "master_share": round(self.master_share, 4),
            "peer_games": self.peer_games,
            "peer_score": None if self.peer_score is None else round(self.peer_score, 4),
            "peer_wins": self.peer_wins,
            "peer_draws": self.peer_draws,
            "peer_losses": self.peer_losses,
            "peer_share": round(self.peer_share, 4),
            "average_rating": self.average_rating,
            "eval_cp": self.eval_cp,
            "loss_cp": self.loss_cp,
            "rank_score": round(self.rank_score, 4),
            "signals": self.signals.as_dict(),
            "opening_name": self.opening_name,
            "opening_eco": self.opening_eco,
            "low_sample": self.low_sample,
            "obscure": self.obscure,
            "in_repertoire": self.in_repertoire,
        }


def shrunk_score(
    wins: int, draws: int, losses: int, *, prior_games: float
) -> float | None:
    """Peer score shrunk toward 0.5, or ``None`` with no games at all.

    A beta-style prior rather than a hard minimum: 2 games at 100% lands near
    0.57 instead of 1.0, so it sorts below a 200-game 54% without being hidden.
    """

    games = wins + draws + losses
    if games <= 0:
        return None
    points = wins + 0.5 * draws
    return (points + prior_games * 0.5) / (games + prior_games)


def engine_quality(loss_cp: int | None, *, scale_cp: float) -> float | None:
    """Bounded transform of centipawn loss into [0, 1]; ``None`` if unanalysed.

    ``exp(-loss/scale)`` rather than a linear ramp: the difference between 0 and
    30cp of concession matters far more than between 300 and 330, and a linear
    term would rank a dubious gambit and a losing blunder as nearly equal.
    """

    if loss_cp is None:
        return None
    return math.exp(-max(0.0, float(loss_cp)) / max(1e-6, scale_cp))


def _normalized(values: Mapping[str, float]) -> dict[str, float]:
    """Scale a mapping to [0, 1] by its own maximum; all-zero stays all-zero."""

    peak = max(values.values()) if values else 0.0
    if peak <= 0:
        return {k: 0.0 for k in values}
    return {k: v / peak for k, v in values.items()}


def build_candidates(
    fen: str,
    *,
    masters: Mapping[str, Any] | None = None,
    peers: Mapping[str, Any] | None = None,
    engine_top_moves: Sequence[Mapping[str, Any]] | None = None,
    signals: Mapping[str, MoveSignals] | None = None,
    repertoire_ucis: Sequence[str] = (),
    constants: OpeningBookConstants | None = None,
) -> list[Candidate]:
    """Assemble one :class:`Candidate` per move any source mentions.

    The union of the three sources, not the intersection — a move the engine
    likes that nobody has played is exactly the kind of thing the builder should
    surface, and so is a move that scores well and the engine never searched.
    Every move is validated against ``fen`` before it is returned, so a stale
    cache entry for a different position cannot inject an illegal move.
    """

    constants = constants or OpeningBookConstants.load()
    board = chess.Board(fen)
    legal = {move.uci(): move for move in board.legal_moves}

    master_rows = {
        str(r["uci"]): r for r in (masters or {}).get("moves", []) if r.get("uci")
    }
    peer_rows = {
        str(r["uci"]): r for r in (peers or {}).get("moves", []) if r.get("uci")
    }
    engine_rows = {
        str(r["move"]): r
        for r in (engine_top_moves or [])
        if r.get("move")
    }

    best_eval: int | None = None
    if engine_top_moves:
        first = engine_top_moves[0].get("eval")
        best_eval = int(first) if first is not None else None

    master_total = sum(row_games(r) for r in master_rows.values())
    peer_total = sum(row_games(r) for r in peer_rows.values())
    for_white = board.turn == chess.WHITE

    ucis = [u for u in (master_rows | peer_rows | engine_rows) if u in legal]
    # Keep the explorer's popularity order as the stable tiebreak before scoring.
    ordered = sorted(ucis, key=lambda u: -row_games(peer_rows.get(u, {})))

    out: list[Candidate] = []
    for uci in ordered:
        move = legal[uci]
        m_row = master_rows.get(uci, {})
        p_row = peer_rows.get(uci, {})
        e_row = engine_rows.get(uci, {})

        m_games = row_games(m_row)
        p_games = row_games(p_row)
        p_wins = int(p_row.get("white" if for_white else "black") or 0)
        p_losses = int(p_row.get("black" if for_white else "white") or 0)
        p_draws = int(p_row.get("draws") or 0)

        eval_cp = int(e_row["eval"]) if e_row.get("eval") is not None else None
        loss_cp = None if (eval_cp is None or best_eval is None) else max(0, best_eval - eval_cp)

        out.append(
            Candidate(
                uci=uci,
                san=board.san(move),
                master_games=m_games,
                master_share=(m_games / master_total) if master_total else 0.0,
                peer_games=p_games,
                peer_score=shrunk_score(
                    p_wins, p_draws, p_losses, prior_games=constants.peer_prior_games
                ),
                peer_wins=p_wins,
                peer_draws=p_draws,
                peer_losses=p_losses,
                peer_share=(p_games / peer_total) if peer_total else 0.0,
                average_rating=p_row.get("average_rating"),
                eval_cp=eval_cp,
                loss_cp=loss_cp,
                signals=(signals or {}).get(uci, MoveSignals()),
                low_sample=max(m_games, p_games) < constants.min_candidate_games,
                in_repertoire=uci in set(repertoire_ucis),
            )
        )
    return out


def rank_candidates(
    candidates: Sequence[Candidate],
    *,
    constants: OpeningBookConstants | None = None,
) -> list[Candidate]:
    """Score and sort in place-ish (returns a new list, mutates ``rank_score``).

    Sorting is by score, then by peer sample size, then SAN — deterministic, so
    the same position always presents the same order and a test can pin it.
    """

    constants = constants or OpeningBookConstants.load()
    if not candidates:
        return []

    popularity = _normalized(
        {c.uci: math.log1p(c.master_games) for c in candidates}
    )
    # Recency stands in as "who plays this" — see the constants file's note.
    recency = _normalized(
        {c.uci: float(c.average_rating or 0) for c in candidates}
    )

    best_loss = min(
        (c.loss_cp for c in candidates if c.loss_cp is not None), default=None
    )

    for c in candidates:
        quality = engine_quality(c.loss_cp, scale_cp=constants.engine_loss_scale_cp)
        score = (
            constants.w_masters * popularity.get(c.uci, 0.0)
            + constants.w_peer * (c.peer_score if c.peer_score is not None else 0.5)
            + constants.w_engine * (quality if quality is not None else 0.5)
            + constants.w_recency * recency.get(c.uci, 0.0)
        )
        # The obscure penalty, and its escape hatch: a rare move that IS the
        # engine's best is a find, not an obscurity. `best_loss == 0` identifies
        # the engine's own top line among the candidates we scored.
        share = max(c.master_share, c.peer_share)
        c.obscure = bool(
            share < constants.obscure_min_share
            and not c.in_repertoire
            and not (
                c.loss_cp is not None
                and best_loss is not None
                and c.loss_cp <= best_loss
                and c.loss_cp <= constants.obscure_forgive_cp
            )
        )
        if c.obscure:
            score -= constants.obscure_penalty
        c.rank_score = score

    return sorted(
        candidates,
        key=lambda c: (-c.rank_score, -c.peer_games, c.san),
    )


__all__ = [
    "Candidate",
    "OpeningBookConstants",
    "build_candidates",
    "engine_quality",
    "rank_candidates",
    "shrunk_score",
]
