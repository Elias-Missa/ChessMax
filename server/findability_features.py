"""Persist findability feature vectors so constants refits need no engine (B.3)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import chess

from chess_vol.findability_review import move_evals_from_ply
from core.evaluation import delta_w, win_prob_cp
from core.features import MoveEval, compute_features
from core.findability import FindabilityConstants, PolicyFn, score_position

_CONSTANTS_PATH = (
    Path(__file__).resolve().parent.parent / "core" / "constants" / "findability.json"
)


def constants_version(path: str | Path | None = None) -> str:
    """Stable short fingerprint of the findability constants file."""

    target = Path(path) if path is not None else _CONSTANTS_PATH
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return digest[:12]


def build_feature_payload(
    fen: str,
    move_evals: list[MoveEval],
    policy_fn: PolicyFn,
    constants: FindabilityConstants,
    *,
    volatility: float | None = None,
    m_star: chess.Move | None = None,
) -> dict[str, Any]:
    """Serialize engine/policy features needed to re-score without MultiPV."""

    if not move_evals:
        return {}
    if m_star is None:
        m_star = move_evals[0].move
    board = chess.Board(fen)
    best_wp = win_prob_cp(move_evals[0].cp or 0) if move_evals[0].cp is not None else 0.5
    if move_evals[0].mate is not None:
        best_wp = 1.0 if move_evals[0].mate > 0 else 0.0

    moves_out: list[dict[str, Any]] = []
    for me in move_evals:
        if me.cp is not None:
            wp = win_prob_cp(me.cp)
        elif me.mate is not None:
            wp = 1.0 if me.mate > 0 else 0.0
        else:
            wp = 0.5
        feats = compute_features(
            me,
            board,
            dep_offset=constants.dep_offset,
            dep_span=constants.dep_span,
            forc_plies=constants.forc_plies,
            w_dep=constants.w_dep,
            w_forc=constants.w_forc,
            w_narr=constants.w_narr,
            q_weight=constants.q_weight,
        )
        moves_out.append({
            "uci": me.move.uci(),
            "cp": me.cp,
            "mate": me.mate,
            "pv": [m.uci() for m in me.pv],
            "d_star": int(me.d_star),
            "narr": float(me.narr),
            "q": int(me.q),
            "forc": float(feats.forc),
            "delta_w": float(delta_w(best_wp, wp)),
        })

    moves = [me.move for me in move_evals]
    pi_r: dict[str, dict[str, float]] = {}
    for rating in constants.rating_grid:
        pi = policy_fn(fen, int(rating), moves)
        pi_r[str(rating)] = {m.uci(): float(pi.get(m, 0.0)) for m in moves}

    return {
        "fen": fen,
        "volatility": None if volatility is None else float(volatility),
        "m_star": m_star.uci(),
        "moves": moves_out,
        "pi_r": pi_r,
    }


def payload_from_ply(
    ply: Any,
    policy_fn: PolicyFn,
    constants: FindabilityConstants,
) -> dict[str, Any] | None:
    """Build a feature payload from an analyzed ply (reuse-mode MoveEvals)."""

    move_evals = move_evals_from_ply(ply)
    if len(move_evals) < 1:
        return None
    vol = float(ply.volatility.score) if ply.volatility else None
    return build_feature_payload(
        ply.fen_before,
        move_evals,
        policy_fn,
        constants,
        volatility=vol,
        m_star=move_evals[0].move,
    )


def _move_evals_from_payload(payload: dict[str, Any]) -> list[MoveEval]:
    evals: list[MoveEval] = []
    for raw in payload.get("moves") or []:
        try:
            move = chess.Move.from_uci(str(raw["uci"]))
        except (KeyError, ValueError):
            continue
        pv: list[chess.Move] = []
        for uci in raw.get("pv") or [raw["uci"]]:
            try:
                pv.append(chess.Move.from_uci(str(uci)))
            except ValueError:
                break
        evals.append(
            MoveEval(
                move=move,
                cp=raw.get("cp"),
                mate=raw.get("mate"),
                pv=pv or [move],
                d_star=int(raw.get("d_star") or 1),
                narr=float(raw.get("narr") or 0.0),
                q=int(raw.get("q") or 0),
            )
        )
    return evals


def policy_from_payload(payload: dict[str, Any]) -> PolicyFn:
    """Build a PolicyFn that returns cached ``pi_r`` (nearest rating band)."""

    pi_r = payload.get("pi_r") or {}
    ratings = sorted(int(r) for r in pi_r.keys())

    def policy(fen: str, rating: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
        if not ratings:
            n = max(1, len(moves))
            return {m: 1.0 / n for m in moves}
        nearest = min(ratings, key=lambda r: abs(r - rating))
        band = pi_r.get(str(nearest)) or {}
        return {m: float(band.get(m.uci(), 0.0)) for m in moves}

    return policy


def score_from_payload(
    payload: dict[str, Any],
    constants: FindabilityConstants,
    *,
    user_rating: int | None = None,
) -> Any:
    """Recompute findability from a stored feature payload (no engine)."""

    move_evals = _move_evals_from_payload(payload)
    if not move_evals:
        return None
    m_star = None
    if payload.get("m_star"):
        try:
            m_star = chess.Move.from_uci(str(payload["m_star"]))
        except ValueError:
            m_star = None
    return score_position(
        str(payload.get("fen") or chess.STARTING_FEN),
        move_evals,
        policy_from_payload(payload),
        constants,
        m_star=m_star,
        user_rating=user_rating,
        volatility=payload.get("volatility"),
    )


def recompute_review_findability(
    connection: sqlite3.Connection,
    review_id: str,
    *,
    constants: FindabilityConstants | None = None,
    version: str | None = None,
) -> bool:
    """Refresh findability columns from stored features when constants changed.

    Returns True if any move was recomputed.
    """

    consts = constants if constants is not None else FindabilityConstants.load()
    version = version or constants_version()
    review = connection.execute(
        "SELECT review_id, user_rating, depth_tier, constants_version, status "
        "FROM reviews WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    if review is None or review["status"] != "complete":
        return False
    if review["depth_tier"] != "full":
        return False
    if (review["constants_version"] or "") == version:
        return False

    moves = connection.execute(
        "SELECT ply, is_user_move, delta_w, detail FROM review_moves WHERE review_id = ?",
        (review_id,),
    ).fetchall()
    user_rating = review["user_rating"]
    try:
        user_rating = int(user_rating) if user_rating is not None else None
    except (TypeError, ValueError):
        user_rating = None

    changed = False
    fixable = 0.0
    for row in moves:
        detail_raw = row["detail"]
        if not detail_raw:
            continue
        try:
            detail = json.loads(detail_raw)
        except json.JSONDecodeError:
            continue
        payload = detail.get("findability_features")
        if not isinstance(payload, dict) or not payload.get("moves"):
            continue
        result = score_from_payload(payload, consts, user_rating=user_rating)
        findability = None if result is None else int(result.score)
        findability_personal = (
            None if result is None or result.personal is None else float(result.personal)
        )
        r_find = None if result is None or result.r_find is None else int(result.r_find)
        connection.execute(
            """
            UPDATE review_moves SET findability = ?, findability_personal = ?, r_find = ?
            WHERE review_id = ? AND ply = ?
            """,
            (findability, findability_personal, r_find, review_id, row["ply"]),
        )
        if row["is_user_move"] and findability is not None and findability > 60:
            fixable += float(row["delta_w"] or 0)
        changed = True

    if changed:
        connection.execute(
            """
            UPDATE reviews SET constants_version = ?, fixable_loss = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ?
            """,
            (version, fixable, review_id),
        )
        connection.commit()
    return changed


def ensure_fresh_findability(
    connection: sqlite3.Connection,
    review_id: str,
) -> bool:
    """No-op when versions match; otherwise recompute from features."""

    return recompute_review_findability(connection, review_id)


# Re-export forcingness for tests that want raw feature math.
__all__ = [
    "build_feature_payload",
    "constants_version",
    "ensure_fresh_findability",
    "payload_from_ply",
    "policy_from_payload",
    "recompute_review_findability",
    "score_from_payload",
]
