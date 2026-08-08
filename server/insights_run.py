"""Insights run orchestration: chess.com ingest → shallow reviews → metrics → mistakes."""

from __future__ import annotations

import io
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Callable

import chess
import chess.pgn

from pipeline import chesscom, lichess
from server import game_identity
from server.insights_metrics import (
    compute_run_trend,
    compute_tier1_metrics,
    persist_practice_flags,
)
from server.mistakes import detect_mistakes
from server.mistakes_run import _insert_puzzle, _start_run, _update_run
from server.reviews import analyze_and_store, create_pending_review, find_review, upsert_game

EventFn = Callable[[str, dict[str, Any]], None]
MAX_RUNS_KEPT = 10
FULL_TIER_QUEUE_CAP = 5


def _queue_full_tier_for_flags(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    practice: dict[str, Any],
    engine: Any,
    policy_fn: Callable[..., Any] | None = None,
) -> int:
    """Run full-tier analysis for up to N flagged games that lack findability."""

    attach_fn = None
    if policy_fn is not None:
        from chess_vol.findability_review import attach_findability

        def attach_fn(plies: Any, user_rating: int | None = None) -> None:  # noqa: ANN401
            attach_findability(plies, policy_fn, user_rating=user_rating)

    queued = 0
    seen: set[str] = set()
    for item in practice.get("items") or []:
        if not item.get("needs_full"):
            continue
        game_id = item.get("game_id")
        if not game_id or game_id in seen:
            continue
        seen.add(str(game_id))
        existing = find_review(
            connection, user_id=user_id, game_id=str(game_id), depth_tier="full"
        )
        if existing is not None and existing["status"] == "complete":
            continue
        row = connection.execute(
            "SELECT pgn FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        if row is None or not row["pgn"]:
            continue
        color = item.get("user_color") or "white"
        review_id = create_pending_review(
            connection,
            user_id=user_id,
            game_id=str(game_id),
            user_color=color,
            depth_tier="full",
            force_new=existing is not None and existing["status"] == "error",
        )
        try:
            analyze_and_store(
                connection,
                review_id=review_id,
                pgn=row["pgn"],
                user_color=color,
                depth_tier="full",
                engine=engine,
                attach_findability_fn=attach_fn,
                policy_fn=policy_fn,
            )
            queued += 1
        except Exception:  # noqa: BLE001 — best-effort upgrade
            continue
        if queued >= FULL_TIER_QUEUE_CAP:
            break
    return queued


def _trim_old_runs(connection: sqlite3.Connection, user_id: int) -> None:
    rows = connection.execute(
        "SELECT run_id FROM insight_runs WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    for row in rows[MAX_RUNS_KEPT:]:
        rid = row["run_id"]
        connection.execute("DELETE FROM insight_run_games WHERE run_id = ?", (rid,))
        connection.execute("DELETE FROM insight_runs WHERE run_id = ?", (rid,))
    connection.commit()


def run_insights(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    username: str,
    window_days: int,
    time_class: str,
    engine: Any,
    analysis_fn: Callable[..., Any] | None = None,
    maia_topk_fn: Callable[[str], list[str] | None] | None = None,
    volatility_fn: Callable[[str], float | None] | None = None,
    fetch_json: chesscom.FetchJson | None = None,
    fetch_text: lichess.FetchText | None = None,
    on_event: EventFn | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    max_games: int = chesscom.MAX_GAMES,
    run_id: str | None = None,
    source: str = "chesscom",
    policy_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute one immutable Insights run (shallow tier + Mistakes bridge)."""

    emit = on_event or (lambda _e, _p: None)
    cancelled = is_cancelled or (lambda: False)
    since = chesscom.default_since(window_days)
    run_id = run_id or str(uuid.uuid4())
    source = (source or "chesscom").strip().lower()
    if source not in ("chesscom", "lichess"):
        raise ValueError(f"unsupported insights source: {source!r}")

    existing = connection.execute(
        "SELECT run_id FROM insight_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO insight_runs (
                run_id, user_id, chesscom_handle, source, window_days, time_class,
                status, progress, games_analyzed, games_capped
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', 0, 0, 0)
            """,
            (run_id, user_id, username, source, window_days, time_class),
        )
    else:
        connection.execute(
            """
            UPDATE insight_runs SET status = 'running', progress = 0,
                chesscom_handle = ?, source = ?, window_days = ?, time_class = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (username, source, window_days, time_class, run_id),
        )
    connection.commit()
    emit("start", {"run_id": run_id, "username": username, "source": source})

    review_ids: list[str] = []
    analyzed = 0
    skipped_cached = 0
    games_capped = False
    mistake_run_id: int | None = None

    try:
        if source == "lichess":
            games, games_capped = lichess.collect_games(
                username,
                since,
                time_class=time_class,
                max_games=max_games,
                fetch_text=fetch_text,
            )
        else:
            games, games_capped = chesscom.collect_games(
                username,
                since,
                time_class=time_class,
                max_games=max_games,
                fetch_json=fetch_json,
            )
        connection.execute(
            "UPDATE insight_runs SET games_capped = ? WHERE run_id = ?",
            (1 if games_capped else 0, run_id),
        )
        connection.commit()

        total = max(1, len(games))
        if analysis_fn is not None:
            mistake_run_id = _start_run(connection, user_id, username, since)

        for idx, (pgn, meta) in enumerate(games):
            if cancelled():
                break
            game_id = game_identity.resolve_game_id(
                source=source, pgn=pgn, meta=meta
            )
            upsert_game(connection, game_id=game_id, source=source, pgn=pgn, meta=meta)
            connection.execute(
                "INSERT OR IGNORE INTO insight_run_games (run_id, game_id) VALUES (?, ?)",
                (run_id, game_id),
            )
            connection.commit()

            user_color = meta.get("user_color") or "white"
            user_rating = meta.get("user_rating")
            try:
                user_rating = int(user_rating) if user_rating is not None else None
            except (TypeError, ValueError):
                user_rating = None

            existing = find_review(
                connection,
                user_id=user_id,
                game_id=game_id,
                depth_tier="shallow",
            )
            if existing is not None and existing["status"] == "complete":
                review_ids.append(str(existing["review_id"]))
                skipped_cached += 1
            else:
                review_id = create_pending_review(
                    connection,
                    user_id=user_id,
                    game_id=game_id,
                    user_color=user_color,
                    depth_tier="shallow",
                    user_rating=user_rating,
                )
                analyze_and_store(
                    connection,
                    review_id=review_id,
                    pgn=pgn,
                    user_color=user_color,
                    depth_tier="shallow",
                    engine=engine,
                    user_rating=user_rating,
                )
                review_ids.append(review_id)
                analyzed += 1

            # Mistakes bridge — mine practice puzzles from this game.
            if mistake_run_id is not None and analysis_fn is not None:
                game = chess.pgn.read_game(io.StringIO(pgn))
                if game is not None:
                    color = chess.WHITE if user_color == "white" else chess.BLACK
                    for puzzle in detect_mistakes(
                        game,
                        color,
                        analysis_fn=analysis_fn,
                        maia_topk_fn=maia_topk_fn,
                        volatility_fn=volatility_fn,
                        meta=meta,
                    ):
                        _insert_puzzle(connection, user_id, mistake_run_id, puzzle)

            progress = (idx + 1) / total
            connection.execute(
                "UPDATE insight_runs SET progress = ?, games_analyzed = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ?",
                (progress, analyzed + skipped_cached, run_id),
            )
            connection.commit()
            emit("progress", {
                "run_id": run_id,
                "progress": progress,
                "games_analyzed": analyzed + skipped_cached,
                "newly_analyzed": analyzed,
                "cached": skipped_cached,
                "games_capped": games_capped,
            })

        if mistake_run_id is not None:
            _update_run(
                connection,
                mistake_run_id,
                status="interrupted" if cancelled() else "done",
                games_scanned=len(games),
                games_eligible=len(games),
            )

        metrics = compute_tier1_metrics(connection, review_ids=review_ids)
        flags_written = persist_practice_flags(
            connection,
            run_id=run_id,
            user_id=user_id,
            practice=metrics.get("practice_flags") or {},
            mistake_run_id=mistake_run_id,
        )
        # Queue a small set of flagged games for full-tier upgrade so findability
        # becomes available when the user opens them / next Insights refresh.
        full_queued = _queue_full_tier_for_flags(
            connection,
            user_id=user_id,
            practice=metrics.get("practice_flags") or {},
            engine=engine,
            policy_fn=policy_fn,
        )
        metrics["practice_flags"] = {
            **(metrics.get("practice_flags") or {}),
            "persisted": flags_written,
            "full_tier_queued": full_queued,
        }
        trend = compute_run_trend(
            connection,
            user_id=user_id,
            run_id=run_id,
            handle=username,
            window_days=window_days,
            time_class=time_class,
            source=source,
            current_metrics=metrics,
        )
        if trend is not None:
            metrics["trend"] = trend
        status = "interrupted" if cancelled() else "complete"
        connection.execute(
            """
            UPDATE insight_runs SET
                status = ?, progress = 1, games_analyzed = ?, games_capped = ?,
                metrics = ?, updated_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (
                status,
                analyzed + skipped_cached,
                1 if games_capped else 0,
                json.dumps(metrics),
                run_id,
            ),
        )
        connection.commit()
        _trim_old_runs(connection, user_id)

        payload = {
            "run_id": run_id,
            "status": status,
            "games_analyzed": analyzed + skipped_cached,
            "newly_analyzed": analyzed,
            "cached": skipped_cached,
            "games_capped": games_capped,
            "metrics": metrics,
        }
        if status == "complete":
            emit("done", payload)
        return payload

    except Exception as exc:  # noqa: BLE001
        connection.execute(
            "UPDATE insight_runs SET status = 'error', detail = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE run_id = ?",
            (repr(exc), run_id),
        )
        connection.commit()
        raise
