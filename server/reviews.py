"""Persist game reviews (shallow/full) for Insights and Game Review."""

from __future__ import annotations

import io
import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import chess
import chess.pgn

from chess_vol.analyze import analyze_pgn
from chess_vol.game_review import build_game_review_summary
from core.evaluation import win_prob_cp
from core.findability import FindabilityConstants
from server import game_identity
from server.findability_features import constants_version, payload_from_ply
from server.position_cache import CachingEngine, put_features, get_features, params_nodes
from server.tactic_tags import tag_tactics

# Insights / placeholder reviews — keep MultiPV=3 (spec) but a modest depth so
# 30-day windows finish in a reasonable time on one Stockfish process.
SHALLOW_DEPTH = 10
SHALLOW_MULTIPV = 3
FULL_DEPTH = 18
FULL_MULTIPV = 6
ENGINE_VERSION = "stockfish"

_CLK_RE = re.compile(r"\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]")


EventFn = Callable[[str, dict[str, Any]], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_game(
    connection: Any,
    *,
    game_id: str,
    source: str,
    pgn: str,
    meta: dict[str, Any] | None = None,
) -> None:
    meta = meta or {}
    game = chess.pgn.read_game(io.StringIO(pgn))
    headers = game.headers if game is not None else {}
    result = (
        game_identity.result_from_chesscom(meta)
        or headers.get("Result")
        or meta.get("result")
    )
    connection.execute(
        """
        INSERT INTO games (
            game_id, source, pgn, white_name, black_name, white_rating, black_rating,
            result, time_class, time_control, eco, opening_name, played_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            pgn=excluded.pgn,
            white_name=COALESCE(excluded.white_name, games.white_name),
            black_name=COALESCE(excluded.black_name, games.black_name),
            white_rating=COALESCE(excluded.white_rating, games.white_rating),
            black_rating=COALESCE(excluded.black_rating, games.black_rating),
            result=COALESCE(excluded.result, games.result),
            time_class=COALESCE(excluded.time_class, games.time_class),
            time_control=COALESCE(excluded.time_control, games.time_control),
            eco=COALESCE(excluded.eco, games.eco),
            opening_name=COALESCE(excluded.opening_name, games.opening_name),
            played_at=COALESCE(excluded.played_at, games.played_at)
        """,
        (
            game_id,
            source,
            pgn,
            meta.get("white_username") or headers.get("White"),
            meta.get("black_username") or headers.get("Black"),
            _int_or_none(meta.get("white_rating") or headers.get("WhiteElo")),
            _int_or_none(meta.get("black_rating") or headers.get("BlackElo")),
            result,
            meta.get("time_class") or None,
            meta.get("time_control") or headers.get("TimeControl"),
            meta.get("eco") or headers.get("ECO"),
            headers.get("Opening"),
            meta.get("date") or headers.get("Date") or headers.get("UTCDate"),
        ),
    )
    connection.commit()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def find_review(
    connection: Any,
    *,
    user_id: int,
    game_id: str,
    depth_tier: str,
) -> Any | None:
    return connection.execute(
        "SELECT * FROM reviews WHERE user_id = ? AND game_id = ? AND depth_tier = ?",
        (user_id, game_id, depth_tier),
    ).fetchone()


def get_review(connection: Any, review_id: str, user_id: int | None = None) -> Any | None:
    if user_id is None:
        return connection.execute(
            "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
    return connection.execute(
        "SELECT * FROM reviews WHERE review_id = ? AND user_id = ?",
        (review_id, user_id),
    ).fetchone()


def create_pending_review(
    connection: Any,
    *,
    user_id: int,
    game_id: str,
    user_color: str,
    depth_tier: str,
    user_rating: int | None = None,
    nodes: int | None = None,
    force_new: bool = False,
) -> str:
    existing = find_review(
        connection, user_id=user_id, game_id=game_id, depth_tier=depth_tier
    )
    if existing is not None:
        if not force_new and existing["status"] == "complete":
            return str(existing["review_id"])
        if not force_new and existing["status"] in ("pending", "running"):
            return str(existing["review_id"])
        # Replace errored / forced rows.
        connection.execute(
            "DELETE FROM review_moves WHERE review_id = ?", (existing["review_id"],)
        )
        connection.execute(
            "DELETE FROM reviews WHERE review_id = ?", (existing["review_id"],)
        )
        connection.commit()
    review_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO reviews (
            review_id, user_id, game_id, user_color, user_rating, depth_tier,
            status, progress, engine_version, constants_version, nodes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
        """,
        (
            review_id,
            user_id,
            game_id,
            user_color,
            user_rating,
            depth_tier,
            ENGINE_VERSION,
            constants_version() if depth_tier == "full" else None,
            nodes,
            _now_iso(),
        ),
    )
    connection.commit()
    return review_id


def _parse_clocks(pgn: str) -> list[float | None]:
    """Per-ply clock remaining (seconds) from ``[%clk]`` comments, if present."""

    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        return []
    clocks: list[float | None] = []
    node = game
    while node.variations:
        node = node.variation(0)
        comment = node.comment or ""
        match = _CLK_RE.search(comment)
        if match:
            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
            clocks.append(h * 3600 + m * 60 + s)
        else:
            clocks.append(None)
    return clocks


def _phase_for_ply(ply: int, board_after: chess.Board) -> str:
    if ply <= 20:
        return "opening"
    majors = 0
    for piece_type in (chess.QUEEN, chess.ROOK):
        majors += len(board_after.pieces(piece_type, chess.WHITE))
        majors += len(board_after.pieces(piece_type, chess.BLACK))
    if majors <= 2 or ply >= 60:
        return "endgame"
    return "middlegame"


def _time_spent(clocks: list[float | None], ply_index: int) -> float | None:
    """Seconds spent on this move from clock deltas (same side, two plies back)."""

    if ply_index < 0 or ply_index >= len(clocks):
        return None
    cur = clocks[ply_index]
    prev_idx = ply_index - 2
    if cur is None or prev_idx < 0:
        return None
    prev = clocks[prev_idx]
    if prev is None:
        return None
    spent = prev - cur
    return spent if spent >= 0 else None


def analyze_and_store(
    connection: Any,
    *,
    review_id: str,
    pgn: str,
    user_color: str,
    depth_tier: str,
    engine: Any,
    on_progress: Callable[[float], None] | None = None,
    attach_findability_fn: Callable[..., Any] | None = None,
    policy_fn: Callable[..., Any] | None = None,
    user_rating: int | None = None,
) -> dict[str, Any]:
    """Run analysis and write ``review_moves`` + summary fields on ``reviews``."""

    connection.execute(
        "UPDATE reviews SET status = 'running', progress = 0, updated_at = ? WHERE review_id = ?",
        (_now_iso(), review_id),
    )
    connection.commit()

    depth = SHALLOW_DEPTH if depth_tier == "shallow" else FULL_DEPTH
    multipv = SHALLOW_MULTIPV if depth_tier == "shallow" else FULL_MULTIPV
    const_ver = constants_version()
    findability_constants = (
        FindabilityConstants.load() if depth_tier == "full" else None
    )
    # Shared Zobrist MultiPV cache — two users reviewing the same opening
    # share engine work across review_ids.
    cached_engine = CachingEngine(engine, connection)

    def progress_cb(done: int, total: int, _ply: Any) -> None:
        if total <= 0:
            return
        frac = min(1.0, done / total)
        connection.execute(
            "UPDATE reviews SET progress = ?, updated_at = ? WHERE review_id = ?",
            (frac * 0.9, _now_iso(), review_id),
        )
        connection.commit()
        if on_progress:
            on_progress(frac * 0.9)

    plies = analyze_pgn(
        pgn,
        cached_engine,
        depth=depth,
        multipv=multipv,
        recurse_depth=0,
        progress=progress_cb,
    )
    if depth_tier == "full" and attach_findability_fn is not None:
        try:
            attach_findability_fn(plies, user_rating=user_rating)
        except Exception:  # noqa: BLE001 — findability is best-effort
            pass

    clocks = _parse_clocks(pgn)
    user_is_white = user_color == "white"
    connection.execute("DELETE FROM review_moves WHERE review_id = ?", (review_id,))

    total_loss = 0.0
    fixable_loss = 0.0
    user_deltas: list[float] = []
    board = chess.Board()
    game = chess.pgn.read_game(io.StringIO(pgn))
    mainline = list(game.mainline_moves()) if game is not None else []

    for idx, ply in enumerate(plies):
        is_white_move = ply.ply % 2 == 1
        is_user = is_white_move == user_is_white
        # Reconstruct board after move for phase.
        if idx < len(mainline):
            board.push(mainline[idx])
        phase = _phase_for_ply(ply.ply, board)

        wp_before = win_prob_cp(ply.eval_cp)
        # Approx played win% from eval drop if review has expected points.
        dw = None
        if ply.review is not None:
            before = ply.review.expected_points_before
            after = ply.review.expected_points_after
            if before is not None and after is not None:
                # expected points are side-to-move / white-normalized in game_review —
                # use loss field when present.
                loss = getattr(ply.review, "expected_points_loss", None)
                if loss is not None:
                    dw = float(loss) * 100.0
                else:
                    dw = max(0.0, (before - after) * 100.0)
                wp_before = float(before) if before <= 1.0 else before / 100.0

        findability = None
        findability_personal = None
        r_find = None
        if ply.findability is not None:
            findability = int(ply.findability.score)
            if ply.findability.personal is not None:
                findability_personal = float(ply.findability.personal)
            if ply.findability.r_find is not None:
                r_find = int(ply.findability.r_find)

        if is_user and dw is not None:
            total_loss += dw
            user_deltas.append(dw)
            if findability is not None and findability > 60:
                fixable_loss += dw

        classification = None
        is_book = 0
        if ply.review is not None:
            classification = ply.review.classification
            is_book = 1 if classification == "book" else 0

        top_lines = [
            {"uci": t.uci, "san": t.san, "eval_cp": t.eval_cp}
            for t in (ply.volatility.top_lines or [])
        ] if ply.volatility else []
        detail: dict[str, Any] = {
            "fen_before": ply.fen_before,
            "fen_after": ply.fen_after,
            "move_uci": ply.move_uci,
            "eval_cp": ply.eval_cp,
            "top_lines": top_lines,
            "cache_hits": getattr(cached_engine, "hits", None),
        }
        if (
            depth_tier == "full"
            and policy_fn is not None
            and findability_constants is not None
            and ply.volatility is not None
            and ply.volatility.reason is None
        ):
            try:
                feat = payload_from_ply(ply, policy_fn, findability_constants)
                if feat:
                    detail["findability_features"] = feat
                    # Share feature vectors across users via position_cache
                    # (only when MultiPV lines are already cached).
                    board_before = chess.Board(ply.fen_before)
                    existing = get_features(
                        connection,
                        board_before,
                        depth=depth,
                        multipv=multipv,
                    )
                    if existing is not None:
                        merged = dict(existing)
                        merged["findability"] = feat
                        put_features(
                            connection,
                            board_before,
                            depth=depth,
                            multipv=multipv,
                            features=merged,
                        )
            except Exception:  # noqa: BLE001 — features are best-effort
                pass
        tactic_tags = None
        if is_user and top_lines:
            try:
                before = chess.Board(ply.fen_before)
                tags = tag_tactics(
                    before,
                    top_lines[0].get("uci"),
                    played_uci=ply.move_uci,
                    pv_uci=[top_lines[0]["uci"]] + [
                        t["uci"] for t in top_lines[1:3] if t.get("uci")
                    ],
                )
                # Only keep tags on costly misses — noise otherwise
                if tags and (dw or 0) >= 10:
                    tactic_tags = json.dumps(tags)
            except Exception:  # noqa: BLE001
                tactic_tags = None
        connection.execute(
            """
            INSERT INTO review_moves (
                review_id, ply, san, is_user_move, phase, is_book, classification,
                win_prob, delta_w, volatility, findability, findability_personal,
                r_find, time_spent, clock_remaining, tactic_tags, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                ply.ply,
                ply.san,
                1 if is_user else 0,
                phase,
                is_book,
                classification,
                wp_before,
                dw,
                (
                    float(ply.volatility.score)
                    if ply.volatility is not None and ply.volatility.score is not None
                    else None
                ),
                findability,
                findability_personal,
                r_find,
                _time_spent(clocks, idx),
                clocks[idx] if idx < len(clocks) else None,
                tactic_tags,
                json.dumps(detail),
            ),
        )

    summary = build_game_review_summary(plies, pgn) if plies else None
    accuracy = None
    if summary is not None:
        acc = summary.get("accuracy") or {}
        accuracy = acc.get("white") if user_is_white else acc.get("black")

    loss_type = classify_loss_type(connection, review_id, user_color)

    connection.execute(
        """
        UPDATE reviews SET
            status = 'complete', progress = 1, accuracy = ?, total_loss = ?,
            fixable_loss = ?, loss_type = ?, nodes = ?, detail_json = ?,
            engine_version = ?, constants_version = ?, updated_at = ?
        WHERE review_id = ?
        """,
        (
            accuracy,
            total_loss,
            fixable_loss if depth_tier == "full" else None,
            loss_type,
            params_nodes(depth, multipv),
            json.dumps({"summary": summary}, default=str) if summary else None,
            ENGINE_VERSION,
            const_ver if depth_tier == "full" else None,
            _now_iso(),
            review_id,
        ),
    )
    connection.commit()
    if on_progress:
        on_progress(1.0)
    return {
        "review_id": review_id,
        "total_loss": total_loss,
        "fixable_loss": fixable_loss if depth_tier == "full" else None,
        "loss_type": loss_type,
        "accuracy": accuracy,
    }


def classify_loss_type(connection: Any, review_id: str, user_color: str) -> str:
    """Assign a single loss taxonomy label (Insights.md C.4 precedence)."""

    rows = connection.execute(
        "SELECT ply, win_prob, delta_w, clock_remaining, is_user_move "
        "FROM review_moves WHERE review_id = ? ORDER BY ply",
        (review_id,),
    ).fetchall()
    if not rows:
        return "bleed"

    user_rows = [r for r in rows if r["is_user_move"]]
    deltas = [float(r["delta_w"] or 0) for r in user_rows]
    total_delta = sum(deltas) or 1.0

    reached_winning = any((r["win_prob"] or 0) > 0.8 for r in user_rows)
    # Did the user lose? Infer from final win_prob if available.
    final_wp = user_rows[-1]["win_prob"] if user_rows else None
    converted_then_lost = bool(reached_winning and final_wp is not None and final_wp < 0.4)

    early = [r for r in user_rows if r["ply"] <= 30]
    never_in_it = bool(early) and all((r["win_prob"] or 0.5) < 0.35 for r in early[:8])

    cliff = any(
        float(r["delta_w"] or 0) > 25 and (r["win_prob"] or 0.5) >= 0.35
        for r in user_rows
    )

    clocks = [r["clock_remaining"] for r in user_rows if r["clock_remaining"] is not None]
    scramble = False
    if clocks and total_delta > 0:
        threshold = sorted(clocks)[max(0, len(clocks) // 10)]
        bottom = sum(
            float(r["delta_w"] or 0)
            for r in user_rows
            if r["clock_remaining"] is not None and r["clock_remaining"] <= threshold
        )
        scramble = bottom / total_delta > 0.5

    if converted_then_lost:
        return "converted_then_lost"
    if cliff:
        return "cliff"
    if scramble:
        return "scramble"
    if never_in_it:
        return "never_in_it"
    return "bleed"


def mark_review_error(connection: Any, review_id: str, message: str) -> None:
    connection.execute(
        "UPDATE reviews SET status = 'error', detail_json = ?, updated_at = ? WHERE review_id = ?",
        (json.dumps({"error": message}), _now_iso(), review_id),
    )
    connection.commit()


def list_reviews(
    connection: Any,
    user_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT r.*, g.white_name, g.black_name, g.white_rating, g.black_rating,
               g.result, g.played_at, g.time_class, g.source, g.pgn
        FROM reviews r
        JOIN games g ON g.game_id = r.game_id
        WHERE r.user_id = ? AND r.status = 'complete'
        ORDER BY r.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (user_id, limit, offset),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        spark = connection.execute(
            "SELECT win_prob FROM review_moves WHERE review_id = ? AND is_user_move = 1 "
            "ORDER BY ply",
            (row["review_id"],),
        ).fetchall()
        ply_count = connection.execute(
            "SELECT COUNT(*) AS n FROM review_moves WHERE review_id = ?",
            (row["review_id"],),
        ).fetchone()
        avg_v = connection.execute(
            "SELECT AVG(volatility) AS v FROM review_moves WHERE review_id = ?",
            (row["review_id"],),
        ).fetchone()
        blunders = connection.execute(
            "SELECT COUNT(*) AS n FROM review_moves WHERE review_id = ? "
            "AND classification = 'blunder'",
            (row["review_id"],),
        ).fetchone()
        out.append({
            **dict(row),
            "opponent": (
                row["black_name"] if row["user_color"] == "white" else row["white_name"]
            ),
            "sparkline": [r["win_prob"] for r in spark if r["win_prob"] is not None][:40],
            "ply_count": int(ply_count["n"] or 0) if ply_count else 0,
            "avg_v": float(avg_v["v"]) if avg_v and avg_v["v"] is not None else None,
            "blunders": int(blunders["n"] or 0) if blunders else 0,
        })
    return out
