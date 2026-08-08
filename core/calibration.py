"""Phase 3 calibration harness for findability (Game Review 2.0 §4).

The Lichess puzzle database is a direct empirical measurement of the thing
findability computes: each puzzle's Glicko rating is derived from how often
players at each rating actually solve it — that *is* ``R_find``. This module is
the harness that turns that database into a fit/validation set.

What lives here is the **pure, testable** scaffolding — puzzle parsing (with the
FEN-convention trap pinned by a test), filtering, stratified sampling, and the
metrics (Pearson r, Brier score, linear fit with intercept for the documented
puzzle-rating bias). The engine feature-extraction and the constant-fitting loop
(Optuna / Nelder-Mead) are drivers that need the ~50k-row CSV, a Stockfish
binary, and a Maia model — they consume these primitives but cannot run in a
unit test, so they are kept thin and clearly marked.

**Baseline first (spec §4.3):** before trusting the full pipeline, check how
well raw ``pi_1500(m*)`` *alone* predicts puzzle rating. :func:`pearson_r` over
that single feature is the bar the full model must clearly beat.
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import chess

# Lichess `lichess_db_puzzle.csv` column order.
_COLUMNS = (
    "PuzzleId",
    "FEN",
    "Moves",
    "Rating",
    "RatingDeviation",
    "Popularity",
    "NbPlays",
    "Themes",
    "GameUrl",
    "OpeningTags",
)


@dataclass(frozen=True)
class PuzzleRow:
    """One row of the Lichess puzzle DB."""

    puzzle_id: str
    fen: str
    """Position **before** the opponent's setup move (Lichess convention)."""
    moves: tuple[str, ...]
    """UCI moves. ``moves[0]`` is the opponent's setup move; the solver replies
    with ``moves[1]`` — getting this backwards silently poisons the dataset."""
    rating: int
    rating_deviation: int
    nb_plays: int
    themes: tuple[str, ...] = field(default_factory=tuple)


def parse_puzzle_row(row: Sequence[str]) -> PuzzleRow:
    """Parse one CSV record into a :class:`PuzzleRow`."""
    data = dict(zip(_COLUMNS, row, strict=False))
    return PuzzleRow(
        puzzle_id=data.get("PuzzleId", ""),
        fen=data["FEN"],
        moves=tuple(data["Moves"].split()),
        rating=int(data["Rating"]),
        rating_deviation=int(data.get("RatingDeviation", 0) or 0),
        nb_plays=int(data.get("NbPlays", 0) or 0),
        themes=tuple(t for t in data.get("Themes", "").split() if t),
    )


def iter_puzzles(path: str | Path) -> Iterable[PuzzleRow]:
    """Stream :class:`PuzzleRow`s from a Lichess puzzle CSV (header auto-skipped)."""
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or row[0] == "PuzzleId":
                continue
            yield parse_puzzle_row(row)


def solver_position(row: PuzzleRow) -> tuple[str, chess.Move]:
    """Return ``(fen_the_solver_faces, solver_first_move)`` — the FEN-convention.

    **The trap (spec §4.1):** the ``FEN`` column is the position *before* the
    opponent's setup move. Applying ``moves[0]`` yields the position the solver
    actually sees, and ``moves[1]`` is the solver's move. Scoring findability on
    the raw ``FEN`` (or treating ``moves[0]`` as the solve) is the silent error
    that poisons the whole set — hence :func:`solver_position` is the only
    supported way to get the position to score, and a test pins its behavior.
    """
    board = chess.Board(row.fen)
    setup = chess.Move.from_uci(row.moves[0])
    board.push(setup)
    solver_move = chess.Move.from_uci(row.moves[1])
    return board.fen(), solver_move


def solution_plies(row: PuzzleRow) -> int:
    """Number of half-moves after the setup move (the solver's line + replies)."""
    return max(0, len(row.moves) - 1)


def keep_puzzle(
    row: PuzzleRow,
    *,
    min_plays: int = 1000,
    max_solution_plies: int = 3,
    drop_mate_in_1: bool = True,
) -> bool:
    """Filtering rule (spec §4.1): well-measured, short, non-trivial puzzles."""
    if row.nb_plays <= min_plays:
        return False
    if len(row.moves) < 2:
        return False
    if drop_mate_in_1 and "mateIn1" in row.themes:
        return False
    if solution_plies(row) > max_solution_plies:
        return False
    return True


def rating_band(rating: int, *, width: int = 200, floor: int = 600) -> int:
    """Bucket a rating into a band label (its lower edge) for stratification."""
    return floor + ((rating - floor) // width) * width


def stratified_sample(
    rows: Sequence[PuzzleRow],
    per_band: int,
    *,
    width: int = 200,
    seed: int = 0,
) -> list[PuzzleRow]:
    """Sample up to ``per_band`` puzzles per rating band (spec §4.1).

    Stops the dense ~1500 mass from dominating the fit. Deterministic given
    ``seed``.
    """
    rng = random.Random(seed)
    by_band: dict[int, list[PuzzleRow]] = {}
    for row in rows:
        by_band.setdefault(rating_band(row.rating, width=width), []).append(row)
    out: list[PuzzleRow] = []
    for band in sorted(by_band):
        bucket = by_band[band]
        if len(bucket) <= per_band:
            out.extend(bucket)
        else:
            out.extend(rng.sample(bucket, per_band))
    return out


# --------------------------------------------------------------------------- #
# Metrics                                                                       #
# --------------------------------------------------------------------------- #


def pearson_r(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation. Returns 0.0 for a degenerate (zero-variance) input.

    Success bar (spec §4.3): ``r > 0.7`` on the tactical set means the metric is
    defensible; ``r ~ 0.3`` means stop and redesign.
    """
    n = len(xs)
    if n == 0 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of probabilistic predictions (0 = perfect).

    Used on ``C_A`` versus the binary solve outcome per rating band (spec §4.3).
    """
    n = len(probabilities)
    if n == 0 or n != len(outcomes):
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes, strict=True)) / n


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """Ordinary least squares ``y ~ slope*x + intercept``.

    The intercept models the documented puzzle-rating bias (spec §4.1): puzzle
    ratings overstate findability because the solver knows a win exists, so the
    ``R_find -> puzzle_rating`` map is not the identity.
    """
    n = len(xs)
    if n == 0 or n != len(ys):
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope, my - slope * mx


__all__ = [
    "PuzzleRow",
    "brier_score",
    "iter_puzzles",
    "keep_puzzle",
    "linear_fit",
    "parse_puzzle_row",
    "pearson_r",
    "rating_band",
    "solution_plies",
    "solver_position",
    "stratified_sample",
]
