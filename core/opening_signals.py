"""The three dots: engine, human, and results — do they agree on this move?

A repertoire choice is only interesting where the three ways of being right
disagree. So every candidate move carries three independent signals:

* ``engine``  — is this Stockfish's top move?
* ``human``   — is this the move the strongest Maia net plays?
* ``results`` — does this move score best in the Lichess game database?

A move all three light up is the easy pick and needs no thought. A move the
engine likes and nobody wins with is a memorised line you will not hold. A move
that scores well and the engine dislikes is a practical try — worth knowing
*that* it is one. The dots exist to make that disagreement visible at a glance
rather than buried in three numbers on three rows.

**Every signal is tri-state, and the third state is the important one.**
``True`` lit, ``False`` unlit, ``None`` *unknown* — no engine installed, no Maia
weights, no explorer data for this position. ``None`` is never rendered as
``False``: "Stockfish does not like this" and "we never asked Stockfish" are
different statements and conflating them is the one way this display can lie.
The rest of the repo already draws this line for findability (``null`` never
``0``) and human eval; the same rule applies here.

Pure: no engine, no network, no database. The caller assembles the three inputs
and this module only decides who wins each of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

ENGINE = "engine"
HUMAN = "human"
RESULTS = "results"
SIGNALS: tuple[str, ...] = (ENGINE, HUMAN, RESULTS)

#: Below this many games an explorer row is noise, not a result. A move played
#: three times with two wins is not "the best scoring move"; it is three games.
#: PLACEHOLDER — the explorer reports exact counts, so this only needs to be
#: high enough that one lucky game cannot win the dot.
MIN_RESULTS_GAMES = 20


@dataclass(frozen=True)
class MoveSignals:
    """The three dots for one move, each independently tri-state."""

    engine: bool | None = None
    human: bool | None = None
    results: bool | None = None

    @property
    def lit(self) -> int:
        """How many dots are lit. Unknown counts as unlit here, by design —
        this is a display ordering, not a claim about the unknown signals."""

        return sum(1 for value in (self.engine, self.human, self.results) if value)

    @property
    def known(self) -> int:
        """How many of the three we actually have an answer for."""

        return sum(
            1
            for value in (self.engine, self.human, self.results)
            if value is not None
        )

    @property
    def unanimous(self) -> bool:
        """All three known *and* all three lit — the move nothing argues with."""

        return self.known == len(SIGNALS) and self.lit == len(SIGNALS)

    def as_dict(self) -> dict[str, bool | None]:
        return {ENGINE: self.engine, HUMAN: self.human, RESULTS: self.results}


def engine_best(top_moves: Sequence[Mapping[str, Any]] | None) -> str | None:
    """The UCI move Stockfish ranks first, or ``None`` if we have no analysis.

    Takes ``analyze()``'s ``top_moves`` list directly (``{"move", "eval", ...}``
    entries, already side-to-move POV and MultiPV-ordered), so the caller does
    not have to reshape it. An empty list means the position is terminal or the
    search returned nothing — either way, unknown rather than "no move is best".
    """

    if not top_moves:
        return None
    first = top_moves[0]
    move = first.get("move")
    return str(move) if move else None


def human_best(top_moves: Sequence[str] | None) -> str | None:
    """The UCI move the Maia net plays, from ``maia_topk_fn``'s policy order.

    ``None`` (no Maia assets, or terminal position) propagates as unknown — the
    whole point of the lenient Maia gate everywhere else in this repo.
    """

    if not top_moves:
        return None
    return str(top_moves[0]) or None


def results_best(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    min_games: int = MIN_RESULTS_GAMES,
    for_white: bool,
) -> str | None:
    """The UCI move with the best score in the explorer rows, or ``None``.

    ``rows`` are normalized explorer entries carrying ``uci``, ``white``,
    ``draws`` and ``black`` counts (see :mod:`pipeline.lichess_explorer`).
    Score is from the *mover's* side — the explorer always reports raw W/D/L
    from White's perspective, so asking "which move wins most" without flipping
    for Black returns the move Black's opponent scores best against, which is
    exactly backwards.

    Draws count a half point: a move that draws every game outscores one that
    wins a third and loses two thirds, and for a repertoire that is the right
    ordering.

    Rows under ``min_games`` are dropped rather than shrunk. Shrinking toward
    0.5 is the right move for *ranking* candidates (``core.opening_book`` does
    exactly that) but the dot is a single winner-take-all claim, and awarding it
    to a four-game sample makes it noise.
    """

    if not rows:
        return None
    best_uci: str | None = None
    best_score = -1.0
    total_considered = 0
    for row in rows:
        uci = str(row.get("uci") or "")
        if not uci:
            continue
        score = row_score(row, for_white=for_white)
        if score is None:
            continue
        if row_games(row) < min_games:
            continue
        total_considered += 1
        # Ties go to the move seen first: the explorer returns rows in
        # popularity order, so the more common of two equal scorers wins.
        if score > best_score:
            best_score = score
            best_uci = uci
    if not total_considered:
        return None
    return best_uci


def row_games(row: Mapping[str, Any]) -> int:
    """Total games behind one explorer row."""

    return sum(int(row.get(key) or 0) for key in ("white", "draws", "black"))


def row_score(row: Mapping[str, Any], *, for_white: bool) -> float | None:
    """Score for the side to move in [0, 1], or ``None`` when the row is empty."""

    games = row_games(row)
    if games <= 0:
        return None
    wins = int(row.get("white" if for_white else "black") or 0)
    draws = int(row.get("draws") or 0)
    return (wins + 0.5 * draws) / games


def compute_signals(
    ucis: Sequence[str],
    *,
    engine_top: str | None,
    human_top: str | None,
    results_top: str | None,
    engine_known: bool | None = None,
    human_known: bool | None = None,
    results_known: bool | None = None,
) -> dict[str, MoveSignals]:
    """Light the dots for every move in ``ucis``.

    A signal whose ``*_top`` is ``None`` is unknown for *every* move — we did
    not ask, so no move can be said to have failed it. The ``*_known`` overrides
    exist for the one case that is not self-describing: a source that answered
    but named a move outside ``ucis`` (the engine's best is a move the user has
    no branch for, say). There the signal *is* known and every listed move is a
    genuine ``False``, which ``*_top is None`` alone cannot express.
    """

    def resolve(top: str | None, known: bool | None) -> tuple[bool, str | None]:
        if known is None:
            return top is not None, top
        return bool(known), top

    engine_ok, engine_move = resolve(engine_top, engine_known)
    human_ok, human_move = resolve(human_top, human_known)
    results_ok, results_move = resolve(results_top, results_known)

    out: dict[str, MoveSignals] = {}
    for uci in ucis:
        out[uci] = MoveSignals(
            engine=(uci == engine_move) if engine_ok else None,
            human=(uci == human_move) if human_ok else None,
            results=(uci == results_move) if results_ok else None,
        )
    return out


def signals_for_position(
    ucis: Sequence[str],
    *,
    engine_top_moves: Sequence[Mapping[str, Any]] | None = None,
    maia_top_moves: Sequence[str] | None = None,
    explorer_rows: Iterable[Mapping[str, Any]] | None = None,
    for_white: bool,
    min_results_games: int = MIN_RESULTS_GAMES,
) -> dict[str, MoveSignals]:
    """One call from raw sources to dots — what the API route actually wants.

    Each source is optional and missing sources stay unknown, so a box with no
    Stockfish, no Maia and no network still returns a well-formed answer with
    three grey dots rather than an error or three wrong ones.
    """

    rows = list(explorer_rows) if explorer_rows is not None else None
    return compute_signals(
        ucis,
        engine_top=engine_best(engine_top_moves),
        human_top=human_best(maia_top_moves),
        results_top=results_best(
            rows, min_games=min_results_games, for_white=for_white
        ),
        # An explorer that answered with rows but none over the threshold has
        # genuinely told us "no move scores best here"; that is known-and-unlit,
        # not unknown. Same for an engine or Maia that named a move we are not
        # listing — handled by `compute_signals`'s default when top is present.
        results_known=None if rows is None else True,
    )


__all__ = [
    "ENGINE",
    "HUMAN",
    "MIN_RESULTS_GAMES",
    "RESULTS",
    "SIGNALS",
    "MoveSignals",
    "compute_signals",
    "engine_best",
    "human_best",
    "results_best",
    "row_games",
    "row_score",
    "signals_for_position",
]
