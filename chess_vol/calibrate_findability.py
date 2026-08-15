"""Phase 3 findability calibration driver — DB-backed (Game Review 2.0 §4).

The Lichess puzzle database is a direct empirical measurement of findability: a
puzzle's Glicko rating *is* the rating at which players solve it ~50% of the
time. This driver reads the puzzles already imported into the trainer DB
(``positions`` table, ``classification='tactical'``) and checks how well the
human policy predicts that rating.

**FEN convention (the trap, spec §4.1):** ``pipeline.import_puzzles`` already
applies the opponent's setup move before storing, so ``positions.fen`` is the
position the solver faces and ``solution_moves[0]`` is the move to find. We must
therefore feed those directly and **not** re-run :func:`core.calibration.solver_position`
(which is for the raw CSV, where the FEN precedes the setup move).

This is a *driver*: it needs lc0 + Maia weights and the DB, so it lives outside
the unit suite. It builds on the pure primitives in :mod:`core.calibration`
(``pearson_r``, ``rating_band``). Run the **baseline first** (spec §4.3): if raw
``pi_1500(m*)`` alone does not clearly predict rating, the whole model is
suspect and there is no point running the expensive full fit.

Usage::

    python -m chess_vol.calibrate_findability --per-band 40 --net 1500
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import time
from dataclasses import dataclass

import chess

from core.calibration import pearson_r, rating_band
from core.engine import enrich_narr_q, multipv_move_evals
from core.findability import FindabilityConstants, invert_monotone, score_position
from core.human import Maia2Policy, MaiaPolicy, _lc0_command, _weight_path

# lc0 verbose-move-stats line: "<uci> (idx) N: .. (P: 12.34%) (WL: ..) ..."
_P_RE = re.compile(r"^(\S+)\s.*\(P:\s*([\d.]+)%\)")

# Per-rating Maia nets shipped in data/maia_weights.
_MAIA_NETS: tuple[int, ...] = (1100, 1300, 1500, 1700, 1900)


def _read_priors(engine: object, board: chess.Board) -> dict[chess.Move, float]:
    """Parse lc0 verbose-move-stats into ``{move: policy_prior}`` at ``nodes=1``."""
    import chess.engine

    out: dict[chess.Move, float] = {}
    with engine.analysis(board, chess.engine.Limit(nodes=1)) as analysis:  # type: ignore[attr-defined]
        for info in analysis:
            text = info.get("string")
            if not text:
                continue
            match = _P_RE.match(text)
            if not match:
                continue
            try:
                move = chess.Move.from_uci(match.group(1))
            except ValueError:
                continue
            if move in board.legal_moves:
                out[move] = float(match.group(2)) / 100.0
    return out


class MaiaPolicyHead:
    """Reads Maia's **policy-head** priors directly via lc0 verbose move stats.

    ``core.human.MaiaPolicy`` softmaxes the *value* head (a documented
    approximation the spec flags for replacement); this reads the *policy* head —
    ``P(move)``, the net's estimate that a human of this net's rating plays the
    move — which is precisely what findability's curves are built from. One lc0
    process, reused across positions; ``nodes=1`` so it is the prior, not a
    search.
    """

    def __init__(self, net: int = 1500) -> None:
        self._net = net
        self._engine = None  # type: ignore[assignment]

    def __enter__(self) -> "MaiaPolicyHead":
        import chess.engine

        cmd, weight = _lc0_command(), _weight_path(self._net)
        if not cmd or weight is None:
            raise RuntimeError(f"lc0 or maia-{self._net} weights not found.")
        engine = chess.engine.SimpleEngine.popen_uci([str(cmd), f"--weights={weight}"])
        try:
            engine.configure({"VerboseMoveStats": True})
        except Exception:  # noqa: BLE001 — some builds accept it only as a CLI flag
            pass
        self._engine = engine
        return self

    def __exit__(self, *_exc: object) -> bool:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._engine = None
        return False

    def priors(self, fen: str) -> dict[chess.Move, float]:
        return _read_priors(self._engine, chess.Board(fen))


class GridPolicyHead:
    """Policy-head :data:`core.findability.PolicyFn` over the whole rating grid.

    Keeps one lc0 process per Maia net (lazy), reused across positions, and for a
    requested rating dispatches to the nearest available net — the multi-net form
    ``score_position`` needs to build ``C_A(rating)`` across the grid. Returns the
    **policy-head** prior (``P:``) for the requested moves, not the value softmax.
    """

    def __init__(self, nets: tuple[int, ...] = _MAIA_NETS) -> None:
        self._nets = [n for n in nets if _weight_path(n) is not None]
        self._engines: dict[int, object] = {}

    @property
    def available(self) -> bool:
        return bool(self._nets)

    def __enter__(self) -> "GridPolicyHead":
        return self

    def __exit__(self, *_exc: object) -> bool:
        for engine in self._engines.values():
            try:
                engine.quit()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self._engines.clear()
        return False

    def _engine_for(self, net: int):  # noqa: ANN202
        import chess.engine

        engine = self._engines.get(net)
        if engine is not None:
            return engine
        cmd, weight = _lc0_command(), _weight_path(net)
        engine = chess.engine.SimpleEngine.popen_uci([str(cmd), f"--weights={weight}"])
        try:
            engine.configure({"VerboseMoveStats": True})
        except Exception:  # noqa: BLE001
            pass
        self._engines[net] = engine
        return engine

    def __call__(
        self, fen: str, rating: int, moves: list[chess.Move]
    ) -> dict[chess.Move, float]:
        if not self._nets:
            return {}
        net = min(self._nets, key=lambda n: abs(n - rating))
        board = chess.Board(fen)
        priors = _read_priors(self._engine_for(net), board)
        return {move: priors.get(move, 0.0) for move in moves}

# The trainer import clamps puzzles to this rating window.
_MIN_RATING = 1000
_MAX_RATING = 2400
# Deterministic "random" ordering: Knuth multiplicative hash over the row id.
_HASH_MULT = 2654435761


@dataclass(frozen=True)
class DbPuzzle:
    fen: str
    solver_move: chess.Move
    rating: int
    themes: tuple[str, ...]
    plies: int
    line: tuple[str, ...] = ()
    """Full solution line (UCI) from ``fen``: solver moves at even indices,
    forced opponent replies at odd indices."""


def _default_db_path() -> str:
    import os

    return os.environ.get("CHESS_TRAINER_DB", "data/trainer.db")


def sample_db_puzzles(
    db_path: str,
    *,
    per_band: int,
    width: int = 200,
    seed: int = 0,
    max_solution_plies: int = 3,
    drop_mate_in_1: bool = True,
) -> list[DbPuzzle]:
    """Stratified sample from the DB: up to ``per_band`` puzzles per rating band.

    Stops the dense ~1500 mass from dominating the fit (spec §4.1). Ordering is a
    deterministic hash of the row id (seeded), so a given ``seed`` reproduces the
    same sample without an ``ORDER BY RANDOM()`` full re-shuffle.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        out: list[DbPuzzle] = []
        band = rating_band(_MIN_RATING, width=width)
        while band <= _MAX_RATING:
            lo, hi = band, band + width
            rows = con.execute(
                """
                SELECT fen, solution_moves, rating, themes
                FROM positions
                WHERE classification = 'tactical'
                  AND rating >= ? AND rating < ?
                  AND solution_moves IS NOT NULL AND solution_moves != ''
                ORDER BY ((id * ? + ?) % 2147483647)
                LIMIT ?
                """,
                (lo, hi, _HASH_MULT, seed, per_band * 4),
            ).fetchall()
            kept = 0
            for row in rows:
                if kept >= per_band:
                    break
                puzzle = _row_to_puzzle(row, max_solution_plies, drop_mate_in_1)
                if puzzle is not None:
                    out.append(puzzle)
                    kept += 1
            band += width
        return out
    finally:
        con.close()


def _row_to_puzzle(
    row: sqlite3.Row, max_solution_plies: int, drop_mate_in_1: bool
) -> DbPuzzle | None:
    moves = str(row["solution_moves"]).split()
    if not moves:
        return None
    if len(moves) > max_solution_plies:
        return None
    themes = tuple(t for t in str(row["themes"] or "").split(",") if t)
    if drop_mate_in_1 and "mateIn1" in themes:
        return None
    try:
        board = chess.Board(row["fen"])
        solver_move = chess.Move.from_uci(moves[0])
    except ValueError:
        return None
    if solver_move not in board.legal_moves:
        return None
    return DbPuzzle(
        fen=row["fen"],
        solver_move=solver_move,
        rating=int(row["rating"]),
        themes=themes,
        plies=len(moves),
        line=tuple(moves),
    )


def baseline_pi(policy: MaiaPolicy, puzzle: DbPuzzle, *, rating: int) -> float | None:
    """``pi_rating(m*)`` — the policy's probability of the solver's move.

    Distributes over *all* legal moves so the softmax normalizes correctly, then
    reads the mass on the move that had to be found. ``None`` if the policy
    returned nothing (engine failure) so the caller can skip, not poison the set.
    """
    board = chess.Board(puzzle.fen)
    legal = list(board.legal_moves)
    dist = policy(puzzle.fen, rating, legal)
    if not dist:
        return None
    return float(dist.get(puzzle.solver_move, 0.0))


def run_baseline(
    puzzles: list[DbPuzzle],
    *,
    net_rating: int,
    mode: str = "policy",
    policy_obj: object | None = None,
    progress_every: int = 100,
) -> dict[str, object]:
    """Spec §4.3 baseline: correlate ``pi(m*)`` with puzzle rating.

    ``mode="policy"`` reads a policy head via ``policy_obj`` (Maia-1 single net or
    Maia-2); ``mode="value"`` uses the ``MaiaPolicy`` value-head approximation for
    comparison. Reports Pearson r on the raw probability and on ``-log pi``
    (monotone in difficulty, usually more linear against rating). The bar:
    ``|r| > 0.7`` means the signal is defensible; ``~0.3`` means redesign.
    """
    pis: list[float] = []
    neglogs: list[float] = []
    ratings: list[float] = []
    skipped = 0
    start = time.time()

    def _record(pi: float | None, rating: int) -> None:
        nonlocal skipped
        if pi is None:
            skipped += 1
            return
        pis.append(pi)
        neglogs.append(-math.log(max(pi, 1e-6)))
        ratings.append(float(rating))

    def _tick(i: int) -> None:
        if progress_every and i % progress_every == 0:
            rate = i / max(1e-9, time.time() - start)
            print(f"  {i}/{len(puzzles)}  ({rate:.1f} puzzles/s)", flush=True)

    if mode == "policy":
        policy = policy_obj if policy_obj is not None else MaiaPolicyHead(net=net_rating)
        with policy as pol:
            for i, puzzle in enumerate(puzzles, 1):
                board = chess.Board(puzzle.fen)
                dist = pol(puzzle.fen, net_rating, list(board.legal_moves))
                _record(dist.get(puzzle.solver_move) if dist else None, puzzle.rating)
                _tick(i)
    else:
        with MaiaPolicy() as policy:
            if not policy.available:
                raise RuntimeError("MaiaPolicy has no nets — check lc0 + maia_weights.")
            for i, puzzle in enumerate(puzzles, 1):
                _record(baseline_pi(policy, puzzle, rating=net_rating), puzzle.rating)
                _tick(i)

    elapsed = time.time() - start
    return {
        "n": len(ratings),
        "skipped": skipped,
        "mode": mode,
        "seconds": round(elapsed, 1),
        "per_puzzle_s": round(elapsed / max(1, len(puzzles)), 3),
        "net_rating": net_rating,
        "r_pi_vs_rating": round(pearson_r(pis, ratings), 4),
        "r_neglogpi_vs_rating": round(pearson_r(neglogs, ratings), 4),
        "mean_pi": round(sum(pis) / len(pis), 4) if pis else None,
    }


def open_stockfish():  # noqa: ANN201
    """Open a python-chess Stockfish engine (STOCKFISH_PATH or PATH)."""
    import os
    import shutil

    import chess.engine

    path = os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish")
    if not path:
        raise RuntimeError("Stockfish not found (set STOCKFISH_PATH).")
    engine = chess.engine.SimpleEngine.popen_uci(path)
    try:
        engine.configure({"UCI_ShowWDL": True})
    except Exception:  # noqa: BLE001 — not all builds expose it
        pass
    return engine


def run_full(
    puzzles: list[DbPuzzle],
    constants: FindabilityConstants,
    *,
    nodes: int,
    multipv: int,
    enrich: bool,
    policy: object | None = None,
    dump_path: str | None = None,
    progress_every: int = 25,
) -> dict[str, object]:
    """The full model (spec §3.3): Stockfish acceptable-set + calc features +
    Maia policy-head over the grid -> ``R_find``, correlated with puzzle rating.

    Primary metric is ``score`` (0-100, defined for every non-gated puzzle;
    lower = harder), expected to correlate *negatively* with rating. ``R_find``
    (positive) is reported too, with a 2800 sentinel for engine-only positions.
    """
    scores: list[float] = []
    rfinds: list[float] = []
    ratings: list[float] = []
    curves: list[tuple[float, list[float]]] = []  # (rating, C_A over grid) — censoring-free
    gated = engine_only = forced = skipped = 0
    start = time.time()

    stockfish = open_stockfish()
    policy_cm = policy if policy is not None else GridPolicyHead()
    try:
        with policy_cm as pol:
            if hasattr(pol, "available") and not pol.available:
                raise RuntimeError("Policy backend unavailable (check Maia install).")
            for i, puzzle in enumerate(puzzles, 1):
                board = chess.Board(puzzle.fen)
                try:
                    move_evals = multipv_move_evals(
                        stockfish, board, multipv=multipv, nodes=nodes
                    )
                except Exception:  # noqa: BLE001 — a bad position must not abort the sweep
                    skipped += 1
                    continue
                if enrich and len(move_evals) >= 2:
                    try:
                        enrich_narr_q(
                            stockfish,
                            board,
                            move_evals,
                            tau=constants.tau,
                            opponent_nodes=constants.narr_opponent_nodes,
                            q_min_pv_ply=constants.q_min_pv_ply,
                            q_delta_mult=constants.q_delta_mult,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                mes_moves = {me.move for me in move_evals}
                m_star = puzzle.solver_move if puzzle.solver_move in mes_moves else None
                pf = score_position(
                    puzzle.fen, move_evals, pol, constants, m_star=m_star
                )
                if pf is None:
                    gated += 1
                    continue
                if pf.forced:
                    forced += 1
                scores.append(float(pf.score))
                ratings.append(float(puzzle.rating))
                curves.append((float(puzzle.rating), [v for _, v in pf.curve]))
                if pf.r_find is None:
                    engine_only += 1
                    rfinds.append(2800.0)
                else:
                    rfinds.append(float(pf.r_find))
                if progress_every and i % progress_every == 0:
                    rate = i / max(1e-9, time.time() - start)
                    print(f"  {i}/{len(puzzles)}  ({rate:.2f} puzzles/s, {gated} gated)", flush=True)
    finally:
        try:
            stockfish.quit()
        except Exception:  # noqa: BLE001
            pass

    elapsed = time.time() - start
    from collections import defaultdict

    band_scores: dict[int, list[float]] = defaultdict(list)
    band_eo: dict[int, int] = defaultdict(int)
    for s, rt, rf in zip(scores, ratings, rfinds, strict=True):
        band = rating_band(int(rt))
        band_scores[band].append(s)
        if rf >= 2800.0:
            band_eo[band] += 1
    by_band = {
        band: {
            "n": len(vals),
            "mean_score": round(sum(vals) / len(vals), 1),
            "engine_only": band_eo[band],
        }
        for band, vals in sorted(band_scores.items())
    }

    # Censoring-free difficulty signals: continuous, defined for every puzzle, so
    # they measure the model's ranking power without the R_find sentinel artifact.
    grid = list(constants.rating_grid)
    curve_ratings = [r for r, _ in curves]
    mean_ca = [sum(c) / len(c) for _, c in curves]
    top_ca = [c[-1] for _, c in curves]
    # Threshold sweep: R_find = C_A^-1(theta) for various theta (the 0.5 in the
    # spec is itself a free constant given Maia's move-1 policy entropy).
    sweep: dict[str, float] = {}
    for theta in (0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
        rfs = [
            (2800.0 if (v := invert_monotone(grid, c, theta)) is None else float(v))
            for _, c in curves
        ]
        sweep[f"theta={theta}"] = round(pearson_r(rfs, curve_ratings), 4)

    if dump_path:
        import json

        with open(dump_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"grid": grid, "curves": [{"rating": r, "c_a": c} for r, c in curves]},
                handle,
            )

    return {
        "r_meanCA_vs_rating": round(pearson_r(mean_ca, curve_ratings), 4),
        "r_topCA_vs_rating": round(pearson_r(top_ca, curve_ratings), 4),
        "rfind_threshold_sweep": sweep,
        "n": len(ratings),
        "gated_decided": gated,
        "engine_only": engine_only,
        "forced": forced,
        "skipped": skipped,
        "seconds": round(elapsed, 1),
        "per_puzzle_s": round(elapsed / max(1, len(puzzles)), 3),
        "nodes": nodes,
        "multipv": multipv,
        "enrich": enrich,
        "r_score_vs_rating": round(pearson_r(scores, ratings), 4),
        "r_rfind_vs_rating": round(pearson_r(rfinds, ratings), 4),
        "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
        "by_band": by_band,
    }


def line_ca_curve(
    stockfish,  # noqa: ANN001
    pol,  # noqa: ANN001
    constants: FindabilityConstants,
    fen: str,
    line_ucis: tuple[str, ...],
    *,
    nodes: int,
    multipv: int,
) -> tuple[list[float] | None, int]:
    """Per-solver-node ``C_A`` curves along the puzzle line, multiplied into one.

    Only the solver's own moves (even indices) are scored — opponent replies are
    given. A node that is forced or already *decided* contributes ``C_A = 1`` (it
    adds no difficulty). The product of the per-node curves is the probability of
    finding the *whole* line. Returns ``(C_A_line over grid, n_solver_nodes)`` or
    ``(None, 0)`` if nothing was scorable.
    """
    grid_len = len(constants.rating_grid)
    board = chess.Board(fen)
    ca_line = [1.0] * grid_len
    n_solver = 0
    for i, uci in enumerate(line_ucis):
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        if i % 2 == 0:  # solver node — the moves that have to be found
            move_evals = multipv_move_evals(stockfish, board, multipv=multipv, nodes=nodes)
            if len(move_evals) >= 2:
                pf = score_position(board.fen(), move_evals, pol, constants)
                curve = (
                    [1.0] * grid_len
                    if (pf is None or pf.forced)
                    else [v for _, v in pf.curve]
                )
            else:
                curve = [1.0] * grid_len  # single legal move — forced
            ca_line = [a * b for a, b in zip(ca_line, curve, strict=True)]
            n_solver += 1
        board.push(move)
    if n_solver == 0:
        return None, 0
    return ca_line, n_solver


def run_line(
    puzzles: list[DbPuzzle],
    constants: FindabilityConstants,
    *,
    nodes: int,
    multipv: int,
    policy: object | None = None,
    dump_path: str | None = None,
    progress_every: int = 25,
) -> dict[str, object]:
    """Multi-move whole-line findability: ``R_find`` from the PRODUCT of the
    per-solver-node ``C_A`` curves, correlated with puzzle rating. Deeper puzzles
    accumulate more sub-1 factors, so this breaks the move-1 ceiling by design."""
    from collections import defaultdict

    from core.findability import pava

    scores: list[float] = []
    rfinds: list[float] = []
    ratings: list[float] = []
    curves: list[tuple[float, list[float]]] = []
    nsolver_hist: dict[int, int] = defaultdict(int)
    engine_only = skipped = 0
    grid = list(constants.rating_grid)
    start = time.time()

    stockfish = open_stockfish()
    policy_cm = policy if policy is not None else GridPolicyHead()
    try:
        with policy_cm as pol:
            if hasattr(pol, "available") and not pol.available:
                raise RuntimeError("Policy backend unavailable.")
            for i, puzzle in enumerate(puzzles, 1):
                try:
                    ca_line, n_solver = line_ca_curve(
                        stockfish, pol, constants, puzzle.fen, puzzle.line,
                        nodes=nodes, multipv=multipv,
                    )
                except Exception:  # noqa: BLE001 — a bad line must not abort the sweep
                    skipped += 1
                    continue
                if ca_line is None:
                    skipped += 1
                    continue
                nsolver_hist[n_solver] += 1
                ca_iso = [max(0.0, min(1.0, v)) for v in pava(ca_line)]
                ratings.append(float(puzzle.rating))
                curves.append((float(puzzle.rating), ca_iso))
                rf = invert_monotone(grid, ca_iso, 0.5)
                if rf is None:
                    engine_only += 1
                    rfinds.append(2800.0)
                    scores.append(0.0)
                else:
                    rfinds.append(float(rf))
                    frac = 1.0 - (rf - constants.score_floor_rating) / constants.score_span
                    scores.append(round(100.0 * max(0.0, min(1.0, frac))))
                if progress_every and i % progress_every == 0:
                    rate = i / max(1e-9, time.time() - start)
                    print(f"  {i}/{len(puzzles)}  ({rate:.2f} puzzles/s, {engine_only} eo)", flush=True)
    finally:
        try:
            stockfish.quit()
        except Exception:  # noqa: BLE001
            pass

    elapsed = time.time() - start
    curve_ratings = [r for r, _ in curves]
    mean_ca = [sum(c) / len(c) for _, c in curves]
    top_ca = [c[-1] for _, c in curves]
    sweep: dict[str, float] = {}
    for theta in (0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
        rfs = [
            (2800.0 if (v := invert_monotone(grid, c, theta)) is None else float(v))
            for _, c in curves
        ]
        sweep[f"theta={theta}"] = round(pearson_r(rfs, curve_ratings), 4)
    band_scores: dict[int, list[float]] = defaultdict(list)
    band_eo: dict[int, int] = defaultdict(int)
    for s, rt, rf in zip(scores, ratings, rfinds, strict=True):
        band = rating_band(int(rt))
        band_scores[band].append(s)
        if rf >= 2800.0:
            band_eo[band] += 1
    by_band = {
        band: {"n": len(vals), "mean_score": round(sum(vals) / len(vals), 1), "engine_only": band_eo[band]}
        for band, vals in sorted(band_scores.items())
    }
    if dump_path:
        import json

        with open(dump_path, "w", encoding="utf-8") as handle:
            json.dump({"grid": grid, "curves": [{"rating": r, "c_a": c} for r, c in curves]}, handle)

    return {
        "n": len(ratings),
        "engine_only": engine_only,
        "skipped": skipped,
        "seconds": round(elapsed, 1),
        "per_puzzle_s": round(elapsed / max(1, len(puzzles)), 3),
        "nodes": nodes,
        "n_solver_nodes_hist": dict(sorted(nsolver_hist.items())),
        "r_meanCA_vs_rating": round(pearson_r(mean_ca, curve_ratings), 4),
        "r_topCA_vs_rating": round(pearson_r(top_ca, curve_ratings), 4),
        "rfind_threshold_sweep": sweep,
        "r_score_vs_rating": round(pearson_r(scores, ratings), 4),
        "r_rfind_vs_rating": round(pearson_r(rfinds, ratings), 4),
        "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
        "by_band": by_band,
    }


def run_calibrate(
    puzzles: list[DbPuzzle],
    constants: FindabilityConstants,
    *,
    nodes: int,
    multipv: int,
    policy: object | None = None,
    progress_every: int = 25,
) -> dict[str, object]:
    """Fit ``calibration.c0`` / ``c1`` against the Lichess Elo expectancy.

    A puzzle rated ``R`` is solved by a player rated ``r`` with probability
    ``1 / (1 + 10 ** ((R - r) / 400))``. Walking a puzzle's whole solution line
    and multiplying the per-node find-probabilities gives the model's answer to
    the same question, so the two constants are whatever makes those agree.
    Reports the residual (MAE against the expectancy) and the ranking power
    against puzzle rating; write the fitted pair into
    ``core/constants/findability.json``.

    Deliberately a grid search over two constants rather than an optimiser:
    the surface is shallow, it needs no extra dependency, and the printed table
    shows how flat it is.
    """
    from core.findability import ELO_LOGIT_SCALE, _logit

    grid = list(constants.rating_grid)
    stockfish = open_stockfish()
    policy_cm = policy if policy is not None else GridPolicyHead()
    per_puzzle: list[tuple[float, list[list[float]]]] = []
    skipped = 0
    start = time.time()
    try:
        with policy_cm as pol:
            for i, puzzle in enumerate(puzzles, 1):
                board = chess.Board(puzzle.fen)
                curves: list[list[float]] = []
                try:
                    for j, uci in enumerate(puzzle.line):
                        move = chess.Move.from_uci(uci)
                        if move not in board.legal_moves:
                            break
                        if j % 2 == 0:
                            mes = multipv_move_evals(
                                stockfish, board, multipv=multipv, nodes=nodes
                            )
                            if len(mes) >= 2:
                                pf = score_position(board.fen(), mes, pol, constants)
                                if pf is not None and not pf.forced:
                                    curves.append([v for _, v in pf.curve])
                        board.push(move)
                except Exception:  # noqa: BLE001 — one bad line must not abort the sweep
                    skipped += 1
                    continue
                if curves:
                    per_puzzle.append((float(puzzle.rating), curves))
                if progress_every and i % progress_every == 0:
                    rate = i / max(1e-9, time.time() - start)
                    print(f"  {i}/{len(puzzles)}  ({rate:.2f} puzzles/s)", flush=True)
    finally:
        try:
            stockfish.quit()
        except Exception:  # noqa: BLE001
            pass

    # ``score_position`` already applied the *current* calibration, so undo it
    # back to a difficulty per node before refitting.
    base = constants.calibration
    nodes_d: list[tuple[float, list[float]]] = []
    for rating, curves in per_puzzle:
        ds = []
        for curve in curves:
            implied = [
                r - ELO_LOGIT_SCALE * _logit(v, base.clamp)
                for v, r in zip(curve, grid, strict=True)
            ]
            mean = sum(implied) / len(implied)
            ds.append((mean - base.c0) / base.c1 if base.enabled and base.c1 else mean)
        nodes_d.append((rating, ds))

    def expectancy(difficulty: float, rating: float) -> float:
        return 1.0 / (1.0 + 10 ** ((difficulty - rating) / 400.0))

    def residual(c0: float, c1: float) -> float:
        total = 0.0
        n = 0
        for rating, ds in nodes_d:
            for r in grid:
                model = 1.0
                for d in ds:
                    model *= expectancy(c0 + c1 * d, r)
                total += abs(model - expectancy(rating, r))
                n += 1
        return total / max(1, n)

    best = None
    table: list[tuple[float, float, float]] = []
    for c1 in [0.8 + 0.04 * k for k in range(16)]:
        for c0 in [-200.0 + 25.0 * k for k in range(17)]:
            mae = residual(c0, c1)
            table.append((mae, c0, c1))
            if best is None or mae < best[0]:
                best = (mae, c0, c1)
    table.sort()

    mae, c0, c1 = best  # type: ignore[misc]
    return {
        "n_puzzles": len(nodes_d),
        "skipped": skipped,
        "seconds": round(time.time() - start, 1),
        "c0": round(c0, 2),
        "c1": round(c1, 4),
        "line_mae": round(mae, 4),
        "top_5": [(round(m, 4), c_0, round(c_1, 3)) for m, c_0, c_1 in table[:5]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=_default_db_path(), help="trainer.db path")
    parser.add_argument("--per-band", type=int, default=40, help="puzzles per rating band")
    parser.add_argument("--width", type=int, default=200, help="rating band width")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--net", type=int, default=1500, help="Maia net rating for the baseline")
    parser.add_argument(
        "--mode",
        choices=("policy", "value", "full", "line", "calibrate"),
        default="policy",
        help=(
            "policy/value = single-move baseline; full = single-position model; "
            "line = multi-move whole-line; calibrate = refit calibration.c0/c1 "
            "against the Elo expectancy"
        ),
    )
    parser.add_argument(
        "--max-plies",
        type=int,
        default=None,
        help="max solution plies to keep (default 3; 7 in line mode so deeper puzzles survive)",
    )
    parser.add_argument("--nodes", type=int, default=1_000_000, help="Stockfish nodes/puzzle (full mode)")
    parser.add_argument("--multipv", type=int, default=8, help="Stockfish MultiPV (full mode)")
    parser.add_argument("--enrich", action="store_true", help="compute narr/q features (slow; full mode)")
    parser.add_argument("--dump", default=None, help="write per-puzzle C_A curves to JSON (full mode)")
    parser.add_argument(
        "--policy",
        choices=("maia1", "maia2", "maia3"),
        default="maia1",
        help=(
            "human model: maia1 (per-rating lc0 nets), maia2 (rating-conditioned "
            "policy head) or maia3 (Chessformer, raw-Elo conditioned — the backend "
            "best_available_policy() actually returns)"
        ),
    )
    args = parser.parse_args()

    max_plies = args.max_plies if args.max_plies is not None else (7 if args.mode == "line" else 3)
    print(
        f"Sampling from {args.db} (per_band={args.per_band}, width={args.width}, max_plies={max_plies})...",
        flush=True,
    )
    puzzles = sample_db_puzzles(
        args.db,
        per_band=args.per_band,
        width=args.width,
        seed=args.seed,
        max_solution_plies=max_plies,
    )
    by_band: dict[int, int] = {}
    for puzzle in puzzles:
        by_band[rating_band(puzzle.rating, width=args.width)] = (
            by_band.get(rating_band(puzzle.rating, width=args.width), 0) + 1
        )
    print(f"Sampled {len(puzzles)} puzzles across {len(by_band)} bands: {dict(sorted(by_band.items()))}", flush=True)

    # Calibration must be able to measure the backend that actually ships.
    if args.policy == "maia3":
        from core.human import Maia3Policy
        policy_obj = Maia3Policy()
    elif args.policy == "maia2":
        policy_obj = Maia2Policy()
    else:
        policy_obj = None

    if args.mode == "full":
        constants = FindabilityConstants.load()
        print(
            f"Running FULL model (Stockfish nodes={args.nodes}, multipv={args.multipv}, "
            f"enrich={args.enrich}) + {args.policy} policy...",
            flush=True,
        )
        metrics = run_full(
            puzzles,
            constants,
            nodes=args.nodes,
            multipv=args.multipv,
            enrich=args.enrich,
            policy=policy_obj if policy_obj is not None else GridPolicyHead(),
            dump_path=args.dump,
        )
        print("\n=== Full model (spec section 3.3) ===")
        for key, value in metrics.items():
            print(f"  {key}: {value}")
        r = abs(float(metrics["r_score_vs_rating"]))
        verdict = "DEFENSIBLE (>0.7)" if r > 0.7 else "WEAK - redesign (spec 4.3)" if r < 0.4 else "MARGINAL"
        print(f"\n  verdict: |r(score, rating)|={r:.3f} -> {verdict}")
        return

    if args.mode == "calibrate":
        constants = FindabilityConstants.load()
        print(
            f"Refitting calibration (Stockfish nodes={args.nodes}, multipv={args.multipv}) "
            f"+ {args.policy} policy, grid={list(constants.rating_grid)}...",
            flush=True,
        )
        metrics = run_calibrate(
            puzzles,
            constants,
            nodes=args.nodes,
            multipv=args.multipv,
            policy=policy_obj if policy_obj is not None else GridPolicyHead(),
        )
        print("\n=== Calibration refit (write c0/c1 into core/constants/findability.json) ===")
        for key, value in metrics.items():
            print(f"  {key}: {value}")
        return

    if args.mode == "line":
        constants = FindabilityConstants.load()
        print(
            f"Running LINE model (multi-move; Stockfish nodes={args.nodes}, "
            f"multipv={args.multipv}) + {args.policy} policy...",
            flush=True,
        )
        metrics = run_line(
            puzzles,
            constants,
            nodes=args.nodes,
            multipv=args.multipv,
            policy=policy_obj if policy_obj is not None else GridPolicyHead(),
            dump_path=args.dump,
        )
        print("\n=== Line model (multi-move findability) ===")
        for key, value in metrics.items():
            print(f"  {key}: {value}")
        r = abs(float(metrics["r_score_vs_rating"]))
        verdict = "DEFENSIBLE (>0.7)" if r > 0.7 else "WEAK - redesign (spec 4.3)" if r < 0.4 else "MARGINAL"
        print(f"\n  verdict: |r(score, rating)|={r:.3f} -> {verdict}")
        return

    print(f"Running baseline ({args.policy}, {args.mode} head, net/elo={args.net})...", flush=True)
    metrics = run_baseline(
        puzzles, net_rating=args.net, mode=args.mode, policy_obj=policy_obj
    )
    print("\n=== Baseline (spec section 4.3) ===")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    r = abs(float(metrics["r_neglogpi_vs_rating"]))
    verdict = "DEFENSIBLE (>0.7)" if r > 0.7 else "WEAK - redesign (spec 4.3)" if r < 0.4 else "MARGINAL"
    print(f"\n  verdict: |r|={r:.3f} -> {verdict}")


if __name__ == "__main__":
    main()
