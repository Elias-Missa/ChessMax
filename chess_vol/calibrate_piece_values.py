"""Calibration + validation driver for contextual piece values.

A *driver*, not a unit test: it needs a real Stockfish and (for some modes) the
trainer DB, so it lives outside the pytest suite in the same way
``chess_vol/calibrate_findability.py`` does.

Modes
-----

``scale``
    **Fits** :class:`core.piece_values.EvalScale`. This is the mode that makes
    the whole algorithm mean something. Raw ablation in centipawns is *not* a
    piece value — removing a queen from the opening moves the eval 773cp while
    removing a bishop moves it 613, because both land where the engine's eval
    has stopped being linear in material. This mode measures the eval reached by
    positions with a known material deficit, inverts that into an
    eval → material-equivalent table, and prints JSON to paste into
    ``core/constants/piece_values.json``.

``recover``
    The check that the fit worked: mean contextual value per piece type over
    sampled positions. **The classical values are the average of the contextual
    ones over the position distribution**, so the means must land near
    1/3/3/5/9. If they don't, the scale or the anchoring is wrong.

``oracle``
    Hand-built positions with a known verdict (trapped bishop, knight outpost,
    protected passer). Prints value and tags for eyeballing; the assertions live
    in ``tests/core/test_piece_values_integration.py``.

``trades``
    **The falsifiable bar.** A trade is a natural experiment in piece value: for
    every real capture-and-recapture, predict the eval change from the two
    contextual values and compare with what actually happened. Run the same
    prediction with static 1/3/3/5/9. If contextual values do not beat static
    values, this algorithm adds nothing and should not ship.

Usage::

    python -m chess_vol.calibrate_piece_values --mode scale --positions 60
    python -m chess_vol.calibrate_piece_values --mode recover --positions 40
    python -m chess_vol.calibrate_piece_values --mode trades --samples 150
"""

from __future__ import annotations

import argparse
import io
import json
import random
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn

from chess_vol.engine import Engine
from core.piece_features import CLASSIC_VALUES_CP, material_balance_cp
from core.piece_values import (
    EvalScale,
    PieceValueConstants,
    compute_piece_values,
)
from core.calibration import pearson_r
from core.findability import pava
from core.volatility import info_to_cp

_REPO_ROOT = Path(__file__).resolve().parents[1]

PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
}

# Openings deep enough to have structure but not so deep they are decided.
# Used when the DB has no stored reviews to sample from.
_SEED_PGNS = [
    "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O",
    "1. d4 Nf6 2. c4 e6 3. Nc3 Bb4 4. e3 O-O 5. Bd3 d5 6. Nf3 c5 7. O-O Nc6 8. a3 Bxc3",
    "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 6. Be3 e5 7. Nb3 Be6 8. f3 Be7",
    "1. d4 d5 2. c4 c6 3. Nf3 Nf6 4. Nc3 dxc4 5. a4 Bf5 6. e3 e6 7. Bxc4 Bb4 8. O-O O-O",
    "1. e4 e6 2. d4 d5 3. Nc3 Bb4 4. e5 c5 5. a3 Bxc3+ 6. bxc3 Ne7 7. Qg4 O-O 8. Bd3 Nbc6",
    "1. c4 e5 2. Nc3 Nf6 3. Nf3 Nc6 4. g3 d5 5. cxd5 Nxd5 6. Bg2 Nb6 7. O-O Be7 8. a3 O-O",
    "1. e4 c6 2. d4 d5 3. Nc3 dxe4 4. Nxe4 Bf5 5. Ng3 Bg6 6. h4 h6 7. Nf3 Nd7 8. h5 Bh7",
    "1. d4 Nf6 2. c4 g6 3. Nc3 Bg7 4. e4 d6 5. Nf3 O-O 6. Be2 e5 7. O-O Nc6 8. d5 Ne7",
]


def _default_db_path() -> str:
    return str(_REPO_ROOT / "data" / "trainer.db")


def _white_cp(engine: Engine, board: chess.Board, depth: int) -> int:
    infos = engine.analyse(board, depth=depth, multipv=1)
    cp = info_to_cp(infos[0], board.turn)
    return cp if board.turn == chess.WHITE else -cp


# --------------------------------------------------------------------------- #
# Position sources                                                             #
# --------------------------------------------------------------------------- #


def _seed_positions(count: int, rng: random.Random) -> list[chess.Board]:
    """Middlegame positions from the built-in opening list."""

    out: list[chess.Board] = []
    for pgn in _SEED_PGNS:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if game is None:
            continue
        board = game.board()
        moves = list(game.mainline_moves())
        for move in moves:
            board.push(move)
        out.append(board.copy(stack=False))
    while len(out) < count and out:
        # Walk a few random legal moves off a seed to widen the sample.
        base = rng.choice(out[: len(_SEED_PGNS)]).copy(stack=False)
        for _ in range(rng.randint(1, 6)):
            legal = list(base.legal_moves)
            if not legal:
                break
            base.push(rng.choice(legal))
        if base.is_valid() and not base.is_game_over():
            out.append(base.copy(stack=False))
    return out[:count]


def _db_positions(
    db_path: str, count: int, rng: random.Random, *, max_abs_eval: int = 600
) -> list[chess.Board]:
    """Stored review positions, quiet ones preferred. Empty list if no DB."""

    if not Path(db_path).exists():
        return []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT detail FROM review_moves WHERE detail IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (count * 4,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    out: list[chess.Board] = []
    for row in rows:
        try:
            detail = json.loads(row["detail"])
            fen = detail.get("fen_before")
            eval_cp = detail.get("eval_cp")
        except (json.JSONDecodeError, TypeError):
            continue
        if not fen or eval_cp is None or abs(int(eval_cp)) > max_abs_eval:
            continue
        try:
            board = chess.Board(fen)
        except ValueError:
            continue
        if board.is_valid() and not board.is_game_over():
            out.append(board)
        if len(out) >= count:
            break
    rng.shuffle(out)
    return out


def _positions(args: argparse.Namespace, rng: random.Random) -> list[chess.Board]:
    boards = _db_positions(args.db, args.positions, rng)
    if len(boards) < args.positions:
        boards += _seed_positions(args.positions - len(boards), rng)
    return boards[: args.positions]


# --------------------------------------------------------------------------- #
# Mode: scale                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class _ScaleSample:
    eval_cp: float
    material_cp: float


def mode_scale(args: argparse.Namespace) -> None:
    """Fit eval → material-equivalent by measuring known material deficits.

    **Pawns are the yardstick, not pieces.** The obvious version of this — remove
    one piece at a time and record the eval reached — samples the curve at only
    four material levels (100/300/500/900), and measurement showed a rook and a
    knight land almost on top of each other (590cp vs 534cp). Inverting a curve
    across a 56cp-wide gap that spans 200cp of material amplifies noise by 3.6x.

    Removing *k pawns* instead samples every 100cp from one mechanism, which
    both fills the curve in and keeps each step inside the region where the
    engine's eval still discriminates. Beyond the pawns available we fall back to
    pawn-plus-piece combinations to extend the range.
    """

    rng = random.Random(args.seed)
    boards = _positions(args, rng)
    print(f"scale: {len(boards)} positions, depth {args.depth}, pawn-yardstick")

    # material_removed_cp -> list of |eval| reached by the side that lost it.
    buckets: dict[int, list[float]] = {}
    calls = 0
    started = time.time()

    def _strip(
        board: chess.Board, squares: list[chess.Square]
    ) -> chess.Board | None:
        probe = board.copy(stack=False)
        for square in squares:
            probe.remove_piece_at(square)
        probe.castling_rights = probe.clean_castling_rights()
        if probe.status() & chess.STATUS_INVALID_EP_SQUARE:
            probe.ep_square = None
        if not probe.is_valid() or probe.is_game_over():
            return None
        return probe

    with Engine() as engine:
        for index, board in enumerate(boards, 1):
            base = _white_cp(engine, board, args.depth)
            calls += 1
            if abs(base) > args.max_abs_eval:
                continue

            for color in (chess.WHITE, chess.BLACK):
                pawns = list(board.pieces(chess.PAWN, color))
                rng.shuffle(pawns)
                others = [
                    square
                    for square, piece in board.piece_map().items()
                    if piece.color == color and piece.piece_type not in (chess.KING, chess.PAWN)
                ]
                rng.shuffle(others)

                # k pawns, k = 1..len(pawns): one step of 100cp each.
                ladder: list[tuple[int, list[chess.Square]]] = []
                for k in range(1, min(len(pawns), args.max_pawn_steps) + 1):
                    ladder.append((100 * k, pawns[:k]))
                # Extend past the pawns with pawns-plus-a-piece.
                for piece_square in others[:2]:
                    piece = board.piece_at(piece_square)
                    if piece is None:
                        continue
                    for k in (0, 2):
                        if k > len(pawns):
                            continue
                        material = CLASSIC_VALUES_CP[piece.piece_type] + 100 * k
                        ladder.append((material, [piece_square, *pawns[:k]]))

                for material, squares in ladder:
                    probe = _strip(board, squares)
                    if probe is None:
                        continue
                    after = _white_cp(engine, probe, args.depth)
                    calls += 1
                    deficit = (base - after) if color == chess.WHITE else (after - base)
                    buckets.setdefault(material, []).append(deficit)

            if index % 5 == 0:
                print(f"  {index}/{len(boards)} · {calls} calls · {time.time()-started:.0f}s")

    print("\n  material_cp   n     mean eval reached   median    isotonic")
    materials: list[float] = [0.0]
    medians: list[float] = [0.0]
    weights: list[float] = [1.0]
    rows: list[tuple[int, int, float, float]] = []
    for material in sorted(buckets):
        deltas = buckets[material]
        if len(deltas) < args.min_bucket:
            print(f"  {material:>10}  {len(deltas):>4}   (skipped, too few)")
            continue
        # The median is the robust choice: an individual removal occasionally
        # lands on a tactic worth far more than the material, and those tails
        # would drag a mean upward without describing the typical position.
        median = statistics.median(deltas)
        rows.append((material, len(deltas), statistics.fmean(deltas), median))
        materials.append(float(material))
        medians.append(median)
        weights.append(float(len(deltas)))

    # More material must never evaluate to less. Raw medians violate that around
    # 500-600cp, where the engine's eval has so little resolution left that
    # bucket noise outweighs a whole pawn. `pava` is the same isotonic
    # regression findability uses on its rating curves, weighted by sample size.
    fitted = pava(medians, weights)
    for (material, n, mean, median), fit in zip(rows, fitted[1:], strict=True):
        flag = "" if abs(fit - median) < 0.5 else "  <- pooled"
        print(
            f"  {material:>10}  {n:>4}   {mean:>17.1f}   {median:>7.1f}   {fit:>9.1f}{flag}"
        )

    # Collapse pooled ties: equal evals cannot be inverted, so keep the widest
    # material span at each distinct eval.
    points: list[_ScaleSample] = []
    for material, fit in zip(materials, fitted, strict=True):
        if points and fit <= points[-1].eval_cp:
            points[-1] = _ScaleSample(points[-1].eval_cp, material)
            continue
        points.append(_ScaleSample(fit, material))

    anchors = [[round(p.eval_cp, 1), round(p.material_cp, 1)] for p in points]
    if len(anchors) >= 2:
        # Extend past the measured range along the final slope so mate-adjacent
        # evals do not clip to the last anchor.
        (x0, y0), (x1, y1) = anchors[-2], anchors[-1]
        slope = (y1 - y0) / (x1 - x0) if x1 != x0 else 1.0
        anchors.append([10000.0, round(y1 + (10000.0 - x1) * slope, 1)])

    print("\n  Paste into core/constants/piece_values.json as eval_scale_anchors:")
    print("  " + json.dumps(anchors))

    scale = EvalScale(anchors=tuple((a[0], a[1]) for a in anchors))
    print("\n  Sanity — eval → material-equivalent:")
    for cp in (0, 100, 200, 400, 600, 800, 1200):
        print(f"    {cp:>5} cp  ->  {scale.to_material(cp):>8.0f}")


# --------------------------------------------------------------------------- #
# Mode: recover                                                                #
# --------------------------------------------------------------------------- #


def mode_recover(args: argparse.Namespace) -> None:
    """Mean contextual value per piece type. Must land near the static table."""

    rng = random.Random(args.seed)
    boards = _positions(args, rng)
    constants = PieceValueConstants.load()
    print(
        f"recover: {len(boards)} positions, depth {args.depth}, "
        f"{len(constants.eval_scale.anchors)} scale anchors"
    )

    raw: dict[int, list[float]] = {}
    anchored: dict[int, list[float]] = {}
    calls = 0
    started = time.time()

    with Engine() as engine:
        for index, board in enumerate(boards, 1):
            try:
                result = compute_piece_values(
                    board, engine, depth=args.depth, constants=constants
                )
            except ValueError:
                continue
            calls += result.analyses
            if result.saturated:
                continue
            for piece in result.pieces:
                if piece.method == "static_fallback":
                    continue
                raw.setdefault(piece.piece_type, []).append(piece.raw_cp)
                anchored.setdefault(piece.piece_type, []).append(piece.anchored_cp)
            if index % 5 == 0:
                print(f"  {index}/{len(boards)} · {calls} calls · {time.time()-started:.0f}s")

    print("\n  piece      n     static    mean raw   mean anchored   median raw")
    suggested: dict[str, float] = {}
    for piece_type in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        values = raw.get(piece_type, [])
        if not values:
            continue
        anc = anchored.get(piece_type, [])
        mean_raw = statistics.fmean(values)
        print(
            f"  {PIECE_NAMES[piece_type]:<9} {len(values):>4}   "
            f"{constants.values[piece_type]:>6}   {mean_raw:>9.0f}   "
            f"{statistics.fmean(anc):>13.0f}   {statistics.median(values):>10.0f}"
        )
        if mean_raw != 0:
            # Compose with whatever correction is already applied, so re-running
            # this mode converges rather than oscillating.
            current = constants.type_scale.get(piece_type, 1.0)
            suggested[PIECE_NAMES[piece_type]] = round(
                current * constants.values[piece_type] / mean_raw, 4
            )

    print(
        "\n  The classical values ARE the mean of the contextual ones over the\n"
        "  position distribution. Means far from the static column mean the\n"
        "  eval scale (--mode scale) or the anchoring is wrong, not that the\n"
        "  positions were unusual."
    )
    if suggested:
        print("\n  Paste into core/constants/piece_values.json as type_scale:")
        print("  " + json.dumps(suggested))


# --------------------------------------------------------------------------- #
# Mode: oracle                                                                 #
# --------------------------------------------------------------------------- #

ORACLE_POSITIONS: list[tuple[str, str, str]] = [
    (
        "Knight outpost on d5",
        "r1bqkb1r/pp3ppp/2n2n2/3N4/4P3/8/PPP2PPP/R1BQKB1R b KQkq - 0 1",
        "d5",
    ),
    (
        "Protected passer on the 7th",
        "8/1P6/P7/8/6k1/8/8/6K1 w - - 0 1",
        "b7",
    ),
    (
        "Bishop entombed by its own pawns",
        "4k3/8/8/8/8/2PPP3/2PBP3/4K3 w - - 0 1",
        "d2",
    ),
    (
        "Rook on the seventh",
        "4k3/1R6/8/8/8/8/8/4K3 w - - 0 1",
        "b7",
    ),
]


def mode_oracle(args: argparse.Namespace) -> None:
    constants = PieceValueConstants.load()
    with Engine() as engine:
        for name, fen, square in ORACLE_POSITIONS:
            board = chess.Board(fen)
            if not board.is_valid():
                print(f"\n=== {name} ===\n  SKIPPED: illegal FEN ({board.status()!r})")
                continue
            result = compute_piece_values(
                board, engine, depth=args.depth, constants=constants
            )
            focus = next((p for p in result.pieces if p.square == square), None)
            print(f"\n=== {name} ===")
            print(
                f"  eval {result.eval_cp}cp -> material-equiv "
                f"{result.eval_material_cp:.0f}  ·  static {result.material_cp}  "
                f"·  gap {result.gap_cp:+.0f}  ·  {result.analyses} calls"
                + ("  ·  SATURATED (values unreliable)" if result.saturated else "")
            )
            if focus is None:
                print(f"  no piece on {square}")
                continue
            print(
                f"  {focus.symbol}{focus.square}: static {focus.static_cp/100:.1f}  "
                f"raw {focus.raw_cp/100:+.2f}  anchored {focus.anchored_cp/100:+.2f}  "
                f"premium {focus.premium_cp/100:+.2f}  [{', '.join(focus.tags) or '—'}]"
            )


# --------------------------------------------------------------------------- #
# Mode: trades                                                                 #
# --------------------------------------------------------------------------- #


def mode_trades(args: argparse.Namespace) -> None:
    """Do contextual values predict real trades better than 1/3/3/5/9?"""

    rng = random.Random(args.seed)
    boards = _positions(args, rng)
    constants = PieceValueConstants.load()

    contextual_errors: list[float] = []
    static_errors: list[float] = []
    raw_errors: list[float] = []
    series: list[tuple[float, float, float, float]] = []
    samples = 0
    started = time.time()

    with Engine() as engine:
        for board in boards:
            if samples >= args.samples:
                break
            captures = [m for m in board.legal_moves if board.is_capture(m)]
            rng.shuffle(captures)
            for move in captures[:2]:
                if samples >= args.samples:
                    break
                victim = board.piece_at(move.to_square)
                attacker = board.piece_at(move.from_square)
                if victim is None or attacker is None:
                    continue

                static_gap = (
                    CLASSIC_VALUES_CP[victim.piece_type]
                    - CLASSIC_VALUES_CP[attacker.piece_type]
                )
                if args.equal_only and static_gap != 0:
                    continue

                after = board.copy(stack=False)
                after.push(move)
                # Only completed trades: the opponent must recapture on the square.
                recaptures = [
                    m
                    for m in after.legal_moves
                    if m.to_square == move.to_square and after.is_capture(m)
                ]
                if not recaptures:
                    continue
                # Recapture with the cheapest piece, as a player would. Taking an
                # arbitrary legal recapture pollutes `actual` with the cost of a
                # bad choice that has nothing to do with either piece's value.
                recaptures.sort(
                    key=lambda m: CLASSIC_VALUES_CP[
                        after.piece_at(m.from_square).piece_type
                    ]
                )
                after.push(recaptures[0])
                if not after.is_valid() or after.is_game_over():
                    continue

                try:
                    values = compute_piece_values(
                        board, engine, depth=args.depth, constants=constants
                    )
                except ValueError:
                    continue
                if values.saturated:
                    continue

                by_square = {p.square: p for p in values.pieces}
                victim_value = by_square.get(chess.square_name(move.to_square))
                attacker_value = by_square.get(chess.square_name(move.from_square))
                if victim_value is None or attacker_value is None:
                    continue

                after_material = constants.eval_scale.to_material(
                    _white_cp(engine, after, args.depth)
                )
                actual = after_material - values.eval_material_cp
                sign = 1.0 if attacker.color == chess.WHITE else -1.0

                predicted_ctx = sign * (
                    victim_value.anchored_cp - attacker_value.anchored_cp
                )
                predicted_raw = sign * (victim_value.raw_cp - attacker_value.raw_cp)
                predicted_static = sign * (
                    CLASSIC_VALUES_CP[victim.piece_type]
                    - CLASSIC_VALUES_CP[attacker.piece_type]
                )
                contextual_errors.append(abs(predicted_ctx - actual))
                static_errors.append(abs(predicted_static - actual))
                raw_errors.append(abs(predicted_raw - actual))
                series.append((predicted_ctx, predicted_raw, predicted_static, actual))
                samples += 1
                if samples % 10 == 0:
                    print(f"  {samples}/{args.samples} · {time.time()-started:.0f}s")

    if not contextual_errors:
        print("trades: no completed trades found in the sample")
        return

    actual = [s[3] for s in series]
    print(f"\n  trades measured : {len(contextual_errors)}")
    print("\n  predictor      MAE     bias        r     (bias = mean signed error)")
    for label, index, errors in (
        ("anchored", 0, contextual_errors),
        ("raw", 1, raw_errors),
        ("static", 2, static_errors),
    ):
        predicted = [s[index] for s in series]
        bias = statistics.fmean(p - a for p, a in zip(predicted, actual, strict=True))
        print(
            f"  {label:<10} {statistics.fmean(errors):>8.1f} {bias:>8.1f} "
            f"{pearson_r(predicted, actual):>8.3f}"
        )

    # Shrinkage sweep. The contextual predictor above is near-unbiased and
    # correlated with the truth, but noisier than a constant — two depth-limited
    # ablations enter every prediction. Blending it back toward the static value
    # trades a little of that signal for a lot of variance, which is the
    # James-Stein bargain. Sweeping here is free: the series is already
    # collected, so one run prices every blend instead of one per engine pass.
    print("\n  shrinkage  lambda   MAE      (v = static + lambda*(contextual - static))")
    best = (0.0, float("inf"))
    for step in range(11):
        lam = step / 10.0
        blended = [lam * s[0] + (1.0 - lam) * s[2] for s in series]
        mae = statistics.fmean(
            abs(p - a) for p, a in zip(blended, actual, strict=True)
        )
        marker = ""
        if mae < best[1]:
            best = (lam, mae)
            marker = "  <-"
        print(f"             {lam:>5.1f}  {mae:>7.1f}{marker}")
    print(f"  best lambda     : {best[0]:.1f}  (MAE {best[1]:.1f})")

    ctx_mae = statistics.fmean(contextual_errors)
    static_mae = statistics.fmean(static_errors)
    delta = static_mae - ctx_mae
    verdict = (
        "PASS — contextual beats static"
        if delta > 0
        else "FAIL — static is as good or better"
    )
    print(f"\n  improvement     : {delta:+8.1f} cp   {verdict}")
    print(
        "\n  This is the bar. If contextual values do not predict real trades\n"
        "  better than 1/3/3/5/9, the algorithm adds nothing.\n"
        "  Read MAE against r: a predictor can carry real signal (higher r) and\n"
        "  still lose on MAE if it is noisier, since a constant has no variance\n"
        "  to be punished for."
    )


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("scale", "recover", "oracle", "trades"),
        default="oracle",
    )
    parser.add_argument("--db", default=_default_db_path())
    parser.add_argument("--depth", type=int, default=12)
    parser.add_argument("--positions", type=int, default=20)
    parser.add_argument("--samples", type=int, default=100, help="trades mode")
    parser.add_argument("--min-bucket", type=int, default=8, help="scale mode")
    parser.add_argument(
        "--max-pawn-steps", type=int, default=6,
        help="scale mode: how far up the k-pawn ladder to climb",
    )
    parser.add_argument("--max-abs-eval", type=int, default=600)
    parser.add_argument(
        "--equal-only", action="store_true",
        help="trades mode: only trades where the static values MATCH (N-for-B, "
             "R-for-R). Static then predicts 0 every time, so the whole signal "
             "under test is contextual.",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    {
        "scale": mode_scale,
        "recover": mode_recover,
        "oracle": mode_oracle,
        "trades": mode_trades,
    }[args.mode](args)


if __name__ == "__main__":
    main()
