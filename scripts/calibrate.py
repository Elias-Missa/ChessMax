"""Calibration CLI (README §7 / Phase 2.5).

Three subcommands:

* ``dump``    — run Stockfish on every position in a corpus and write the
                MultiPV results to a JSON cache. Slow; do this once per
                corpus + (depth, multipv) combination.
* ``tune``    — load the cache, optimise the 8 volatility constants under
                a chosen loss (expert / distributional / blended), and
                print a suggested replacement for ``chess_vol.config``.
* ``report``  — load the cache, recompute V under either the current
                ``chess_vol.config`` constants or a named override, and
                print a per-category breakdown of bucket distributions
                + label MSE so you can compare before/after.

Usage::

    # 1. Curate / write a corpus JSON. Starter at:
    #    tests/fixtures/calibration_corpus.json
    #
    # 2. Run Stockfish on the corpus (slow, can be hours for large corpora):
    python scripts/calibrate.py dump tests/fixtures/calibration_corpus.json \\
        --out cache/analyses.json --depth 18 --multipv 6
    #
    # 3. See where the current constants land:
    python scripts/calibrate.py report tests/fixtures/calibration_corpus.json \\
        --analyses cache/analyses.json
    #
    # 4. Tune:
    python scripts/calibrate.py tune tests/fixtures/calibration_corpus.json \\
        --analyses cache/analyses.json --mode blended
    #
    # 5. Compare (re-run report with the suggested constants — see --constants):
    python scripts/calibrate.py report tests/fixtures/calibration_corpus.json \\
        --analyses cache/analyses.json --constants tuned.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import chess

from chess_vol.calibrate import (
    CachedAnalysis,
    Constants,
    CorpusEntry,
    LossMode,
    RawScore,
    analyses_from_json,
    analyses_to_json,
    build_report,
    corpus_from_json,
    tune_constants,
)

# --------------------------------------------------------------------------- #
# Loaders                                                                     #
# --------------------------------------------------------------------------- #


def load_corpus(path: Path) -> list[CorpusEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"corpus must be a JSON list, got {type(payload).__name__}")
    return corpus_from_json(payload)


def load_analyses(path: Path) -> dict[str, CachedAnalysis]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(
            f"analyses cache must be a JSON object, got {type(payload).__name__}"
        )
    return analyses_from_json(payload)


def load_constants(path: Path | None) -> Constants:
    """Load constants from a JSON file, or return defaults if path is None."""
    if path is None:
        return Constants()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Constants(**payload)


def write_constants(path: Path, c: Constants) -> None:
    path.write_text(json.dumps(asdict(c), indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# `dump` — slow Stockfish pass                                                #
# --------------------------------------------------------------------------- #


def cmd_dump(args: argparse.Namespace) -> None:
    """Run Stockfish on every corpus position; save raw MultiPV to disk."""
    # Lazy import — keeps `report` / `tune` usable without Stockfish.
    from chess_vol.engine import Engine

    corpus = load_corpus(Path(args.corpus))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    analyses: dict[str, CachedAnalysis] = {}

    print(f"Dumping {len(corpus)} positions at depth={args.depth}, multipv={args.multipv}")
    print(f"  → {out_path}")
    t0 = time.perf_counter()

    with Engine() as engine:
        for i, entry in enumerate(corpus, start=1):
            board = chess.Board(entry.fen)

            if board.is_checkmate() or board.is_stalemate():
                analyses[entry.id] = CachedAnalysis(
                    fen=entry.fen, lines=[], legal_count=0, is_terminal=True
                )
                _emit_progress(i, len(corpus), entry.id, "TERMINAL")
                continue

            legal_count = sum(1 for _ in board.legal_moves)
            multipv = max(1, min(args.multipv, legal_count))
            infos = engine.analyse(board, depth=args.depth, multipv=multipv)

            lines: list[RawScore] = []
            turn = board.turn
            for info in infos:
                pov = info["score"].pov(turn)
                mate = pov.mate()
                if mate is not None:
                    lines.append(RawScore(mate=int(mate)))
                else:
                    cp = pov.score()
                    if cp is None:
                        # Unknown — skip; the recompute path is robust to
                        # missing entries because it sorts by cp, but log it.
                        print(f"  ! {entry.id}: skipped a line with no cp/mate")
                        continue
                    lines.append(RawScore(cp=int(cp)))

            analyses[entry.id] = CachedAnalysis(
                fen=entry.fen,
                lines=lines,
                legal_count=legal_count,
                is_terminal=False,
            )
            _emit_progress(i, len(corpus), entry.id, f"{len(lines)} lines")

    elapsed = time.perf_counter() - t0
    out_path.write_text(
        json.dumps(analyses_to_json(analyses), indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nDone in {elapsed:.1f}s — {len(analyses)} analyses written.")


def _emit_progress(i: int, n: int, eid: str, note: str) -> None:
    print(f"  [{i:3d}/{n}] {eid:32}  {note}")


# --------------------------------------------------------------------------- #
# `tune`                                                                      #
# --------------------------------------------------------------------------- #


def cmd_tune(args: argparse.Namespace) -> None:
    corpus = load_corpus(Path(args.corpus))
    analyses = load_analyses(Path(args.analyses))
    initial = load_constants(Path(args.initial)) if args.initial else None
    mode: LossMode = args.mode

    print(f"Tuning under mode='{mode}' on {len(corpus)} corpus entries.")
    print("Starting constants:")
    _print_constants(initial or Constants(), indent="  ")

    result = tune_constants(corpus, analyses, mode=mode, initial=initial, max_iter=args.max_iter)

    print(f"\n{'Converged' if result.converged else 'Did NOT converge'} after "
          f"{result.iterations} iterations. Final loss = {result.loss:.6f}")
    print("\nTuned constants:")
    _print_constants(result.constants, indent="  ")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_constants(out_path, result.constants)
        print(f"\nWritten to {out_path}")
    print(_render_constants_py_diff(result.constants))


def _print_constants(c: Constants, indent: str = "") -> None:
    for k, v in asdict(c).items():
        print(f"{indent}{k:<20} = {v:.4f}")


def _render_constants_py_diff(c: Constants) -> str:
    """Suggested patch text for ``chess_vol/config.py``."""
    return (
        "\n--- Suggested replacement for chess_vol/config.py ---\n"
        f"K_SHALLOW: Final[float]        = {c.k_shallow:.2f}\n"
        f"K_DEEP:    Final[float]        = K_SHALLOW   # tune separately when deep corpus exists\n"
        f"EVAL_SCALE_GRACE: Final[float] = {c.eval_scale_grace:.2f}\n"
        f"EVAL_SCALE_WIDTH: Final[float] = {c.eval_scale_width:.2f}\n"
        f"EVAL_SCALE_MAX:   Final[float] = {c.eval_scale_max:.2f}\n"
        f"MATE_BASE:        Final[int]   = {round(c.mate_base)}\n"
        f"MATE_STEP:        Final[int]   = {round(c.mate_step)}\n"
        f"DECIDED_BEST_CP:  Final[float] = {c.decided_best_cp:.2f}\n"
        f"DECIDED_ALT_CP:   Final[float] = {c.decided_alt_cp:.2f}\n"
        "----\n"
    )


# --------------------------------------------------------------------------- #
# `report`                                                                    #
# --------------------------------------------------------------------------- #


def cmd_report(args: argparse.Namespace) -> None:
    corpus = load_corpus(Path(args.corpus))
    analyses = load_analyses(Path(args.analyses))
    constants = load_constants(Path(args.constants)) if args.constants else None
    report = build_report(corpus, analyses, constants=constants)

    print("Constants used:")
    _print_constants(report.constants, indent="  ")
    print(f"\nTotal positions    : {report.n_total}")
    print(f"  with V undefined : {report.n_undefined}")
    print(f"  scored mean V    : {report.overall_mean_v:.2f}")
    print(
        f"  buckets          : low {report.overall_low_share:.0%}, "
        f"med {report.overall_medium_share:.0%}, high {report.overall_high_share:.0%}"
    )

    if report.expert_mse is not None:
        print(f"\nExpert MSE         : {report.expert_mse:.2f}  "
              f"(RMSE ≈ {report.expert_mse ** 0.5:.2f})")
    if report.distributional_kl is not None:
        print(f"Distributional KL  : {report.distributional_kl:.4f}")

    if report.categories:
        print("\nBy category:")
        for cat in report.categories:
            tgt_str = ""
            if cat.target is not None:
                t = cat.target
                tgt_str = (
                    f"  target [low {t.low:.0%}, med {t.medium:.0%}, high {t.high:.0%}]"
                )
            print(
                f"  {cat.name:<14} n={cat.n:3d}  meanV={cat.mean_v:5.1f}  "
                f"low {cat.low_share:.0%}, med {cat.medium_share:.0%}, "
                f"high {cat.high_share:.0%}{tgt_str}"
            )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(_report_to_dict(report), indent=2) + "\n",
                            encoding="utf-8")
        print(f"\nWritten machine-readable report to {out_path}")


def _report_to_dict(r: Any) -> dict[str, Any]:
    """asdict() round-trip for the report — avoids name collisions in printing."""
    return asdict(r)


# --------------------------------------------------------------------------- #
# Arg parser                                                                  #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="calibrate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="Run Stockfish on the corpus, cache results.")
    d.add_argument("corpus", help="Path to corpus JSON.")
    d.add_argument("--out", required=True, help="Output cache JSON path.")
    d.add_argument("--depth", type=int, default=18)
    d.add_argument("--multipv", type=int, default=6)
    d.set_defaults(func=cmd_dump)

    t = sub.add_parser("tune", help="Optimise constants from a cached dump.")
    t.add_argument("corpus", help="Path to corpus JSON.")
    t.add_argument("--analyses", required=True, help="Path to cache JSON from `dump`.")
    t.add_argument("--mode", choices=["expert", "distributional", "blended"],
                   default="blended")
    t.add_argument("--initial", default=None,
                   help="Optional Constants JSON to start from. Defaults to current config.")
    t.add_argument("--out", default=None, help="Optional path to write the tuned Constants JSON.")
    t.add_argument("--max-iter", type=int, default=200)
    t.set_defaults(func=cmd_tune)

    r = sub.add_parser("report", help="Show how a constants set performs on the corpus.")
    r.add_argument("corpus", help="Path to corpus JSON.")
    r.add_argument("--analyses", required=True, help="Path to cache JSON from `dump`.")
    r.add_argument("--constants", default=None,
                   help="Optional Constants JSON. Defaults to current chess_vol.config.")
    r.add_argument("--out", default=None,
                   help="Optional path for a JSON report.")
    r.set_defaults(func=cmd_report)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
