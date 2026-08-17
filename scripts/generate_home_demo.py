"""Regenerate the home-page hero demo data (``frontend/home/opera.js``).

The landing page replays Morphy vs Duke Karl & Count Isouard (Paris Opera,
1858) with live volatility / findability / win% readouts. Those numbers are
produced by the same code paths the product runs, not authored by hand:

* ``vol``  — :func:`core.volatility.compute_volatility` (Stockfish, depth 20,
  MultiPV 6), the volatility of the position *before* the move.
* ``win``  — :func:`core.evaluation.win_prob_cp` of the same position, White's
  point of view.
* ``find`` / ``band`` / ``best`` — :func:`core.findability.score_position` over
  :func:`core.engine.multipv_move_evals`, against whichever human policy
  :func:`core.human.best_available_policy` finds (Maia-3 preferred).

Needs Stockfish (``STOCKFISH_PATH`` or on PATH) and, for the findability
columns, an installed Maia backend. Without a policy the script still runs and
writes ``find``/``band``/``best`` as ``null``; the panel degrades to showing an
em dash rather than a wrong number.

    python -m scripts.generate_home_demo            # rewrite opera.js in place
    python -m scripts.generate_home_demo --stdout   # print it instead
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import chess
import chess.engine

from chess_vol.engine import Engine, _resolve_path
from core.engine import multipv_move_evals
from core.evaluation import win_prob_cp
from core.findability import FindabilityConstants, score_position
from core.human import best_available_policy
from core.volatility import compute_volatility, info_to_cp

OUT_PATH = Path(__file__).resolve().parents[1] / "frontend" / "home" / "opera.js"

SAN_MOVES = (
    "e4 e5 Nf3 d6 d4 Bg4 dxe5 Bxf3 Qxf3 dxe5 Bc4 Nf6 Qb3 Qe7 Nc3 c6 Bg5 b5 "
    "Nxb5 cxb5 Bxb5+ Nbd7 O-O-O Rd8 Rxd7 Rxd7 Rd1 Qe6 Bxd7+ Nxd7 Qb8+ Nxb8 Rd8#"
).split()

DEPTH = 20
MULTIPV = 6
FIND_MULTIPV = 6
FIND_NODES = 400_000

HEADER = '''// Hero demo data — Paul Morphy vs Duke Karl / Count Isouard, Paris Opera, 1858.
//
// GENERATED — do not hand-edit. Run `python -m scripts.generate_home_demo`.
//
// These are not marketing numbers. Every row was produced by the same code
// paths the product runs: `core.volatility.compute_volatility` (Stockfish
// depth {depth}, MultiPV {multipv}) for `vol`, `core.evaluation.win_prob_cp` for
// `win`, and `core.findability.score_position` against the {policy} policy head
// for `find` / `band` / `best`. All three describe the position *before* the
// move on that row — i.e. the decision the player was actually facing.

window.HM_OPERA = {{
  white: "Paul Morphy",
  black: "Duke Karl / Count Isouard",
  event: "Paris Opera",
  year: 1858,
  plies: [
'''


def _js(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = parser.parse_args(argv)

    policy = best_available_policy()
    policy_name = type(policy).__name__ if policy is not None else "no (unavailable)"
    if policy is None:
        print("no human policy available — findability columns will be null", file=sys.stderr)
    consts = FindabilityConstants.load()

    rows: list[dict[str, object]] = []
    board = chess.Board()
    # Findability needs its own node-limited handle, but it must be the same
    # binary the vol pass resolved — hence the shared resolver rather than a
    # second, independently-guessed path.
    sf_path = _resolve_path(None)

    with Engine() as vol_engine:
        find_engine = chess.engine.SimpleEngine.popen_uci(sf_path)
        try:
            for san in SAN_MOVES:
                move = board.parse_san(san)

                vol = compute_volatility(board, vol_engine, depth=DEPTH, multipv=MULTIPV)
                infos = vol_engine.analyse(board, depth=DEPTH, multipv=1)
                cp_stm = info_to_cp(infos[0], board.turn)
                cp_white = cp_stm if board.turn == chess.WHITE else -cp_stm

                find = band = best = None
                if policy is not None:
                    evals = multipv_move_evals(
                        find_engine, board, multipv=FIND_MULTIPV, nodes=FIND_NODES
                    )
                    if len(evals) >= 2:
                        scored = score_position(
                            board.fen(), evals, policy, consts, m_star=evals[0].move
                        )
                        if scored is not None:
                            find = round(scored.score)
                            band = scored.band
                            best = board.san(evals[0].move)

                rows.append(
                    {
                        "uci": move.uci(),
                        "san": san,
                        "vol": None if vol.score is None else round(vol.score, 1),
                        "win": round(win_prob_cp(cp_white) * 100, 1),
                        "find": find,
                        "band": band,
                        "best": best,
                    }
                )
                print(f"{len(rows):>2} {san:<7} vol={rows[-1]['vol']} "
                      f"win={rows[-1]['win']} find={find} ({band})", file=sys.stderr)
                board.push(move)
        finally:
            find_engine.quit()

    body = "".join(
        "    { "
        + ", ".join(f'{key}: {_js(row[key])}' for key in ("uci", "san", "vol", "win", "find", "band", "best"))
        + " },\n"
        for row in rows
    )
    text = HEADER.format(depth=DEPTH, multipv=MULTIPV, policy=policy_name) + body + "  ],\n};\n"

    if args.stdout:
        print(text)
    else:
        OUT_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {OUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
