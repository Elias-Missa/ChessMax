"""Engine adapter for findability: MultiPV + iterative-deepening PV capture.

Game Review 2.0 spec §3.3 Step 2: run Stockfish at ``MultiPV=8`` and a **fixed
node count** (nodes are reproducible across hardware; depth is not), and capture
the root PV at *every* iterative-deepening iteration. That last part gives
``d_star(m)`` — the depth at which each candidate first surfaces — for free,
with no extra searches.

The stream-reduction math (:func:`reduce_analysis_stream`) is **pure** and unit
-tested with synthetic ``info`` dicts. The engine driving (:func:`multipv_move_evals`)
and the ``narr``/``q`` enrichment are integration-only — they need a real
Stockfish binary and are exercised under the ``integration`` marker.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import chess

from core.evaluation import Wdl, clamp_mate_cp, delta_w, win_prob_cp
from core.features import MoveEval


def _score_to_cp_mate(score: Any, turn: chess.Color) -> tuple[int | None, int | None]:
    pov = score.pov(turn)
    mate = pov.mate()
    if mate is not None:
        return None, int(mate)
    cp = pov.score()
    return (int(cp) if cp is not None else None), None


def _wdl_tuple(info: dict[str, Any], turn: chess.Color) -> Wdl | None:
    raw = info.get("wdl")
    if raw is None:
        return None
    try:
        wdl = raw.pov(turn)
        return (int(wdl.wins), int(wdl.draws), int(wdl.losses))
    except Exception:  # noqa: BLE001 — engine may not report WDL
        return None


def reduce_analysis_stream(
    board: chess.Board,
    infos: Iterable[dict[str, Any]],
) -> list[MoveEval]:
    """Reduce a stream of iterative-deepening ``info`` dicts to :class:`MoveEval`s.

    Each ``info`` carries ``multipv`` (1-based slot), ``depth``, ``pv`` (list of
    :class:`chess.Move`), ``score`` (a python-chess ``PovScore``), and optionally
    ``wdl``. The *final* info per slot supplies cp/mate/pv/wdl; ``d_star`` for a
    move is the **first** depth at which it appeared as the head of any line.
    Result is ordered best-first (MultiPV slot 1 first).
    """
    turn = board.turn
    final_by_slot: dict[int, dict[str, Any]] = {}
    first_depth: dict[chess.Move, int] = {}
    for info in infos:
        pv = info.get("pv")
        if not pv:
            continue
        slot = int(info.get("multipv", 1))
        depth = int(info.get("depth", 1))
        head = pv[0]
        final_by_slot[slot] = info
        if head not in first_depth or depth < first_depth[head]:
            first_depth[head] = depth

    evals: list[MoveEval] = []
    for slot in sorted(final_by_slot):
        info = final_by_slot[slot]
        pv = list(info["pv"])
        head = pv[0]
        cp, mate = _score_to_cp_mate(info["score"], turn)
        evals.append(
            MoveEval(
                move=head,
                cp=cp,
                mate=mate,
                pv=pv,
                d_star=first_depth.get(head, int(info.get("depth", 1))),
                wdl=_wdl_tuple(info, turn),
            )
        )
    return evals


def multipv_move_evals(
    engine: Any,
    board: chess.Board,
    *,
    multipv: int = 8,
    nodes: int = 2_500_000,
) -> list[MoveEval]:
    """Drive a ``chess.engine.SimpleEngine`` and capture per-iteration PVs.

    ``engine`` must expose ``.analysis(board, limit, multipv=...)`` (python-chess
    ``SimpleEngine``). Uses a fixed **node** limit for reproducibility. Requests
    ``UCI_ShowWDL`` when supported so :func:`core.evaluation.win_prob` can use
    the WDL route. Integration-only.
    """
    import chess.engine

    want = max(1, min(multipv, board.legal_moves.count()))
    infos: list[dict[str, Any]] = []
    with engine.analysis(
        board, chess.engine.Limit(nodes=nodes), multipv=want, info=chess.engine.INFO_ALL
    ) as analysis:
        for info in analysis:
            if info.get("pv"):
                infos.append(dict(info))
    return reduce_analysis_stream(board, infos)


def enrich_narr_q(
    engine: Any,
    board: chess.Board,
    move_evals: list[MoveEval],
    *,
    tau: float,
    opponent_nodes: int = 3,
    q_min_pv_ply: int = 3,
    q_delta_mult: float = 3.0,
    child_multipv: int = 6,
    child_nodes: int = 400_000,
) -> None:
    """Fill in ``narr`` and ``q`` on each :class:`MoveEval` (spec §3.3 Step 5).

    ``narr`` = ``1 / (1 + mean opponent replies within tau)`` over the first
    ``opponent_nodes`` opponent nodes of the PV. ``q`` flags a *quiet* move at
    PV ply ``>= q_min_pv_ply`` that is uniquely winning at its node
    (``delta_w`` to second best there ``> q_delta_mult * tau``). Both require
    extra shallow searches along the PV — integration-only, and mutates in place.
    """
    import chess.engine

    def _multipv_probs(node: chess.Board) -> list[float]:
        want = max(1, min(child_multipv, node.legal_moves.count()))
        raw = engine.analyse(node, chess.engine.Limit(nodes=child_nodes), multipv=want)
        rows = raw if isinstance(raw, list) else [raw]
        probs: list[float] = []
        for row in rows:
            score = row.get("score")
            if score is None:
                continue
            pov = score.pov(node.turn)
            mate = pov.mate()
            cp = pov.score()
            probs.append(win_prob_cp(clamp_mate_cp(None if cp is None else cp, mate)))
        return sorted(probs, reverse=True)

    for me in move_evals:
        walk = board.copy(stack=True)
        replies_within: list[int] = []
        q_flag = 0
        for ply_index, mv in enumerate(me.pv):
            if mv not in walk.legal_moves:
                break
            is_opponent_node = (ply_index % 2 == 1)
            if is_opponent_node and len(replies_within) < opponent_nodes:
                probs = _multipv_probs(walk)
                if probs:
                    best = probs[0]
                    within = sum(1 for p in probs if delta_w(best, p) <= tau)
                    replies_within.append(within)
            if (not is_opponent_node) and ply_index >= q_min_pv_ply and q_flag == 0:
                is_quiet = not walk.is_capture(mv) and not walk.gives_check(mv) and mv.promotion is None
                if is_quiet:
                    probs = _multipv_probs(walk)
                    if len(probs) >= 2 and delta_w(probs[0], probs[1]) > q_delta_mult * tau:
                        q_flag = 1
            walk.push(mv)
        me.narr = 1.0 / (1.0 + (sum(replies_within) / len(replies_within))) if replies_within else 0.0
        me.q = q_flag


__all__ = [
    "enrich_narr_q",
    "multipv_move_evals",
    "reduce_analysis_stream",
]
