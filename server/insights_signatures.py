"""Phase 6 — error signatures and recurrence, plus the classifiers they need.

A mistake that happens once is noise; the same mistake six times is a leak.
Recurrence is more persuasive than any aggregate and gives an honest filter — a
"leak" that never recurs probably isn't one.

Signatures are built from a move's motif, the piece moved, the piece lost, the
phase, the **geometry** of the best move that was missed, and the opponent piece
involved. The geometry and piece classifiers (spec 10.2 / 10.3) live here
because the signature depends on them, and they carry their own cards:

* **Geometric blind spots** — humans systematically miss backward moves,
  retreats and long diagonals. Measured *within findability deciles*, because
  otherwise you are only rediscovering that backward moves tend to be harder.
* **Piece-specific attribution** — which piece you hang, whose tactics you miss.

Signatures persist in their own table so recurrence survives run immutability.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, Sequence

import chess

from server.insights_pro import MISTAKE_DELTA_W

#: A signature must recur at least this often in the window to be leak-eligible.
RECURRENCE_MIN = 3

#: Window either side of a solved practice set, in days (spec 6.4).
EFFICACY_WINDOW_DAYS = 30

PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


# ── Geometry (spec 10.2) ──────────────────────────────────────────────────────


def classify_geometry(fen: str | None, uci: str | None) -> dict[str, Any] | None:
    """Direction, distance, piece and king-relation of a move.

    Direction is expressed from the mover's point of view, so "backward" means
    the same thing for both colours.
    """

    if not fen or not uci or len(uci) < 4:
        return None
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
    except (ValueError, IndexError):
        return None
    piece = board.piece_at(move.from_square)
    if piece is None:
        return None

    from_rank, to_rank = chess.square_rank(move.from_square), chess.square_rank(move.to_square)
    from_file, to_file = chess.square_file(move.from_square), chess.square_file(move.to_square)

    forward = (to_rank - from_rank) if piece.color == chess.WHITE else (from_rank - to_rank)
    if forward > 0:
        direction = "forward"
    elif forward < 0:
        direction = "backward"
    else:
        direction = "lateral"

    steps = max(abs(to_rank - from_rank), abs(to_file - from_file))
    distance = "adjacent" if steps <= 1 else "medium" if steps <= 3 else "long"

    king_square = board.king(not piece.color)
    toward_king = None
    if king_square is not None:
        before = chess.square_distance(move.from_square, king_square)
        after = chess.square_distance(move.to_square, king_square)
        toward_king = "toward" if after < before else "away" if after > before else "level"

    is_diagonal = abs(to_rank - from_rank) == abs(to_file - from_file) and steps > 0
    return {
        "direction": direction,
        "distance": distance,
        "piece": PIECE_NAMES.get(piece.piece_type, "?"),
        "king_relation": toward_king,
        "diagonal": is_diagonal,
        "class": f"{direction}-{distance}-{PIECE_NAMES.get(piece.piece_type, '?')}",
    }


def piece_captured_by(fen_after_error: str | None, reply_uci: str | None) -> str | None:
    """Which piece the opponent's reply took — i.e. what the user hung."""

    if not fen_after_error or not reply_uci or len(reply_uci) < 4:
        return None
    try:
        board = chess.Board(fen_after_error)
        move = chess.Move.from_uci(reply_uci)
    except (ValueError, IndexError):
        return None
    if board.is_en_passant(move):
        return "pawn"
    victim = board.piece_at(move.to_square)
    return PIECE_NAMES.get(victim.piece_type) if victim else None


def moving_piece(fen: str | None, uci: str | None) -> str | None:
    if not fen or not uci or len(uci) < 4:
        return None
    try:
        board = chess.Board(fen)
        piece = board.piece_at(chess.Move.from_uci(uci).from_square)
    except (ValueError, IndexError):
        return None
    return PIECE_NAMES.get(piece.piece_type) if piece else None


# ── Signatures (spec 6.1) ─────────────────────────────────────────────────────


def _first_tag(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        tags = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None
    return str(tags[0]) if isinstance(tags, list) and tags else None


def error_signature(parts: dict[str, Any]) -> str:
    """Stable short hash of the error's shape.

    Truncated SHA-1 rather than Python's ``hash`` because these are stored and
    compared across processes and runs, where ``hash`` is not stable.
    """

    payload = "|".join(
        str(parts.get(key) or "-")
        for key in (
            "motif", "piece_moved", "piece_lost", "phase", "geometry", "opponent_piece"
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def describe_signature(parts: dict[str, Any]) -> str:
    """Human sentence for a signature, built from whichever parts are known."""

    motif = (parts.get("motif") or "").replace("_", " ")
    piece = parts.get("piece_moved")
    lost = parts.get("piece_lost")
    geometry = parts.get("geometry") or ""
    phase = parts.get("phase")

    if lost and motif:
        head = f"losing a {lost} to a {motif}"
    elif lost:
        head = f"hanging a {lost}"
    elif motif:
        head = f"missing a {motif}"
    elif piece:
        head = f"a {piece} error"
    else:
        head = "an error"

    tail = []
    if geometry.startswith("backward"):
        tail.append("after a backward move was needed")
    if phase:
        tail.append(f"in the {phase}")
    return head + (" " + " ".join(tail) if tail else "")


def build_signatures(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One signature row per user error, ready for storage and recurrence."""

    by_review: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_review[str(row["review_id"])].append(row)
    for moves in by_review.values():
        moves.sort(key=lambda r: r["ply"])

    out: list[dict[str, Any]] = []
    for moves in by_review.values():
        for idx, move in enumerate(moves):
            if not move["is_user_move"]:
                continue
            if float(move["delta_w"] or 0.0) < MISTAKE_DELTA_W:
                continue

            reply = moves[idx + 1] if idx + 1 < len(moves) else None
            geometry = classify_geometry(move.get("fen_before"), move.get("best_uci"))
            # A piece counts as *hung* only when the capture was not simply the
            # other half of an exchange the user started. Without this, ordinary
            # recaptures dominate and every signature reads "hanging a pawn".
            user_captured = "x" in str(move.get("san") or "")
            piece_lost = (
                piece_captured_by(reply.get("fen_before"), reply.get("move_uci"))
                if reply is not None and not reply["is_user_move"] and not user_captured
                else None
            )
            parts = {
                "motif": _first_tag(move.get("tactic_tags")),
                "piece_moved": moving_piece(move.get("fen_before"), move.get("move_uci")),
                "piece_lost": piece_lost,
                "phase": move.get("phase"),
                "geometry": (geometry or {}).get("class"),
                "opponent_piece": (
                    moving_piece(reply.get("fen_before"), reply.get("move_uci"))
                    if reply is not None and not reply["is_user_move"]
                    else None
                ),
            }
            out.append({
                **move,
                "signature": error_signature(parts),
                "signature_parts": parts,
                "signature_label": describe_signature(parts),
                "geometry": geometry,
            })
    return out


# ── Recurrence (spec 6.3) ─────────────────────────────────────────────────────


def compute_recurrence(signed: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Group the window's errors by signature and rank by total cost."""

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in signed:
        groups[row["signature"]].append(row)

    clusters = []
    for signature, items in groups.items():
        dates = sorted(str(i.get("played_at") or "")[:10] for i in items if i.get("played_at"))
        clusters.append({
            "signature": signature,
            "label": items[0]["signature_label"],
            "parts": items[0]["signature_parts"],
            "n": len(items),
            "total_delta_w": sum(float(i["delta_w"] or 0) for i in items),
            "mean_delta_w": fmean([float(i["delta_w"] or 0) for i in items]),
            "first_seen": dates[0] if dates else None,
            "last_seen": dates[-1] if dates else None,
            "games": sorted({str(i.get("game_id")) for i in items}),
            "moves": items,
        })
    clusters.sort(key=lambda c: (-c["n"], -c["total_delta_w"]))
    return {
        # Only the head of the tail is worth storing: a long list of one-off
        # signatures is noise and would bloat the run payload.
        "clusters": clusters[:40],
        "recurring": [c for c in clusters if c["n"] >= RECURRENCE_MIN],
        "distinct": len(clusters),
        "singletons": sum(1 for c in clusters if c["n"] == 1),
        "threshold": RECURRENCE_MIN,
    }


# ── Geometric blind spots (spec 10.2) ─────────────────────────────────────────


def compute_geometry_blind_spots(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Miss rate by move geometry, **controlled for difficulty**.

    Comparing raw miss rates would only rediscover that backward moves tend to
    be harder. Rates are computed inside difficulty deciles and then averaged
    across them, so each comparison is like-for-like.

    Difficulty is findability where it exists and volatility otherwise; the
    control actually used is reported so the card can say so.
    """

    candidates = []
    for row in rows:
        if not row["is_user_move"] or row["is_book"] or not row.get("best_uci"):
            continue
        difficulty = (
            100.0 - float(row["findability"]) if row["findability"] is not None
            else float(row["volatility"]) if row["volatility"] is not None
            else None
        )
        if difficulty is None:
            continue
        geometry = classify_geometry(row.get("fen_before"), row["best_uci"])
        if not geometry:
            continue
        candidates.append({
            "difficulty": difficulty,
            "missed": (row.get("move_uci") or "") != row["best_uci"],
            "geometry": geometry,
            "row": row,
        })

    if len(candidates) < 20:
        return {"n": len(candidates), "directions": [], "control": None}

    used_findability = any(r["row"]["findability"] is not None for r in candidates)
    candidates.sort(key=lambda c: c["difficulty"])
    decile_size = max(1, len(candidates) // 10)
    deciles = [candidates[i:i + decile_size] for i in range(0, len(candidates), decile_size)]

    def controlled_rate(key: str, value: str) -> tuple[float | None, int]:
        """Mean of within-decile miss rates, so difficulty cannot drive it."""

        rates, total = [], 0
        for decile in deciles:
            subset = [c for c in decile if c["geometry"][key] == value]
            if len(subset) < 3:
                continue
            rates.append(sum(1 for c in subset if c["missed"]) / len(subset))
            total += len(subset)
        return (fmean(rates) if rates else None), total

    directions = []
    for value in ("forward", "backward", "lateral"):
        rate, n = controlled_rate("direction", value)
        if rate is not None:
            directions.append({"direction": value, "miss_rate": rate, "n": n})

    pieces = []
    for value in ("knight", "bishop", "rook", "queen", "pawn", "king"):
        rate, n = controlled_rate("piece", value)
        if rate is not None:
            pieces.append({"piece": value, "miss_rate": rate, "n": n})
    pieces.sort(key=lambda p: -p["miss_rate"])

    note = None
    by_direction = {d["direction"]: d for d in directions}
    if "backward" in by_direction and "forward" in by_direction:
        back, fwd = by_direction["backward"]["miss_rate"], by_direction["forward"]["miss_rate"]
        if fwd > 0.01 and back / fwd >= 1.4:
            note = (
                f"Controlling for difficulty, you miss backward moves "
                f"{back / fwd:.1f}× more often than forward ones."
            )

    return {
        "n": len(candidates),
        "control": "findability" if used_findability else "volatility",
        "directions": directions,
        "pieces": pieces,
        "note": note,
    }


def compute_piece_attribution(signed: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Which piece you hang, and which piece's errors cost most (spec 10.3)."""

    hung: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "delta_w": 0.0})
    moved: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "delta_w": 0.0})
    for row in signed:
        parts = row["signature_parts"]
        dw = float(row["delta_w"] or 0.0)
        if parts.get("piece_lost"):
            entry = hung[parts["piece_lost"]]
            entry["n"] += 1
            entry["delta_w"] += dw
        if parts.get("piece_moved"):
            entry = moved[parts["piece_moved"]]
            entry["n"] += 1
            entry["delta_w"] += dw

    def rows_of(source: dict[str, dict[str, Any]], key: str) -> list[dict[str, Any]]:
        return sorted(
            ({key: name, **stats} for name, stats in source.items()),
            key=lambda r: -r["n"],
        )

    hung_rows = rows_of(hung, "piece")
    return {
        "hung": hung_rows,
        "moved": rows_of(moved, "piece"),
        "most_hung": hung_rows[0] if hung_rows else None,
    }


# ── Persistence ───────────────────────────────────────────────────────────────


def persist_signatures(
    connection: Any, *, user_id: int, signed: Sequence[dict[str, Any]]
) -> int:
    """Upsert signatures so recurrence survives immutable runs."""

    written = 0
    for row in signed:
        if not row.get("game_id"):
            continue
        parts = row["signature_parts"]
        connection.execute(
            """
            INSERT OR REPLACE INTO error_signatures (
                user_id, signature, game_id, ply, delta_w, motif, piece_moved,
                piece_lost, phase, geometry, played_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                row["signature"],
                row["game_id"],
                int(row["ply"]),
                float(row["delta_w"] or 0.0),
                parts.get("motif"),
                parts.get("piece_moved"),
                parts.get("piece_lost"),
                parts.get("phase"),
                parts.get("geometry"),
                row.get("played_at"),
            ),
        )
        written += 1
    connection.commit()
    return written


def signature_history(
    connection: Any, *, user_id: int, signature: str
) -> list[dict[str, Any]]:
    """Every stored instance of a signature, oldest first — the cross-run view."""

    rows = connection.execute(
        "SELECT game_id, ply, delta_w, played_at FROM error_signatures "
        "WHERE user_id = ? AND signature = ? ORDER BY played_at, ply",
        (user_id, signature),
    ).fetchall()
    return [dict(r) for r in rows]


def record_practice_efficacy(
    connection: Any,
    *,
    user_id: int,
    signature: str,
    solved_at: datetime | None = None,
    window_days: int = EFFICACY_WINDOW_DAYS,
) -> dict[str, Any]:
    """Measure the signature's error rate either side of a solved practice set.

    If the rate drops, the tool provably works. If it doesn't, that is more
    valuable than any metric on the page — so it is recorded either way.
    """

    solved_at = solved_at or datetime.now(timezone.utc)
    start = (solved_at - timedelta(days=window_days)).isoformat()
    end = (solved_at + timedelta(days=window_days)).isoformat()
    pivot = solved_at.isoformat()

    def window(lo: str, hi: str) -> tuple[float, int]:
        rows = connection.execute(
            "SELECT delta_w FROM error_signatures WHERE user_id = ? AND signature = ? "
            "AND played_at >= ? AND played_at < ?",
            (user_id, signature, lo, hi),
        ).fetchall()
        return sum(float(r["delta_w"] or 0) for r in rows), len(rows)

    before_loss, before_n = window(start, pivot)
    after_loss, after_n = window(pivot, end)

    connection.execute(
        """
        INSERT OR REPLACE INTO practice_efficacy (
            user_id, signature, solved_at, delta_w_before, delta_w_after,
            n_before, n_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, signature, pivot, before_loss, after_loss, before_n, after_n),
    )
    connection.commit()
    return {
        "signature": signature,
        "solved_at": pivot,
        "delta_w_before": before_loss,
        "delta_w_after": after_loss,
        "n_before": before_n,
        "n_after": after_n,
        "improved": after_n < before_n if (before_n or after_n) else None,
    }
