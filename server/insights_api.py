"""API routes for Insights runs (POST/GET /api/insights*)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from pipeline import chesscom
from server import db
from server.deps import current_user, get_connection
from server.insights_run import run_insights

logger = logging.getLogger("server.insights")

VALID_SOURCES = frozenset({"chesscom", "lichess"})


class InsightsStartRequest(BaseModel):
    chesscom_handle: str | None = Field(default=None, max_length=64)
    handle: str | None = Field(default=None, max_length=64)
    source: Literal["chesscom", "lichess"] = "chesscom"
    window_days: Literal[7, 30] = 30
    time_class: str = Field(default="blitz", max_length=16)

    def resolved_handle(self) -> str:
        raw = (self.handle or self.chesscom_handle or "").strip()
        if not raw:
            raise ValueError("handle is required")
        return raw


def _default_run(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    username: str,
    window_days: int,
    time_class: str,
    on_event: Any,
    is_cancelled: Any,
    fetch_json: chesscom.FetchJson | None,
    fetch_text: Any = None,
    run_id: str | None = None,
    source: str = "chesscom",
) -> dict[str, Any]:
    from chess_vol.engine import Engine
    from core.human import best_available_policy

    # Shallow reviews only need the vol Engine. Mistakes puzzles are created from
    # practice flags after metrics (no per-game Stockfish/Maia mining here).
    with Engine() as vol_engine:
        return run_insights(
            connection,
            user_id=user_id,
            username=username,
            window_days=window_days,
            time_class=time_class,
            engine=vol_engine,
            analysis_fn=None,
            maia_topk_fn=None,
            volatility_fn=None,
            fetch_json=fetch_json,
            fetch_text=fetch_text,
            on_event=on_event,
            is_cancelled=is_cancelled,
            run_id=run_id,
            source=source,
            policy_fn=best_available_policy(),
        )


def build_insights_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/api")
    running_users: set[int] = set()
    lock = threading.Lock()

    def _start(body: InsightsStartRequest, user: sqlite3.Row) -> dict[str, object]:
        try:
            handle = body.resolved_handle()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        source = (body.source or "chesscom").strip().lower()
        if source not in VALID_SOURCES:
            raise HTTPException(
                status_code=422,
                detail=f"source must be one of {sorted(VALID_SOURCES)}",
            )
        time_class = body.time_class.strip().lower()
        if time_class not in chesscom.VALID_TIME_CLASSES:
            raise HTTPException(
                status_code=422,
                detail=f"time_class must be one of {sorted(chesscom.VALID_TIME_CLASSES)}",
            )
        user_id = int(user["id"])
        with lock:
            if user_id in running_users:
                raise HTTPException(
                    status_code=409,
                    detail="An insights run is already in progress.",
                )
            running_users.add(user_id)

        run_id = str(uuid.uuid4())
        bootstrap = db.connect(app.state.db_path)
        try:
            bootstrap.execute(
                """
                INSERT INTO insight_runs (
                    run_id, user_id, chesscom_handle, source, window_days, time_class,
                    status, progress
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)
                """,
                (run_id, user_id, handle, source, int(body.window_days), time_class),
            )
            bootstrap.commit()
        finally:
            bootstrap.close()

        run_fn = getattr(app.state, "insights_run_fn", None)
        fetch_json = getattr(app.state, "fetch_json", None)
        fetch_text = getattr(app.state, "fetch_text", None)

        def worker() -> None:
            connection = db.connect(app.state.db_path)
            try:
                kwargs = dict(
                    user_id=user_id,
                    username=handle,
                    window_days=int(body.window_days),
                    time_class=time_class,
                    on_event=lambda _e, _p: None,
                    is_cancelled=lambda: False,
                    fetch_json=fetch_json,
                    fetch_text=fetch_text,
                    run_id=run_id,
                    source=source,
                )
                if run_fn is not None:
                    run_fn(connection, **kwargs)
                else:
                    _default_run(connection, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.exception("insights run failed")
                connection.execute(
                    "UPDATE insight_runs SET status = 'error', detail = ? WHERE run_id = ?",
                    (repr(exc), run_id),
                )
                connection.commit()
            finally:
                connection.close()
                with lock:
                    running_users.discard(user_id)

        threading.Thread(target=worker, daemon=True).start()
        return {"run_id": run_id}

    @router.post("/insights")
    def start_insights(
        body: InsightsStartRequest,
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        return _start(body, user)

    @router.get("/insights/{run_id}")
    def get_insights_run(
        run_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM insight_runs WHERE run_id = ? AND user_id = ?",
            (run_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Insights run not found")
        metrics = None
        if row["metrics"]:
            try:
                metrics = json.loads(row["metrics"])
            except json.JSONDecodeError:
                metrics = None
        source = row["source"] if "source" in row.keys() else "chesscom"
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "progress": row["progress"],
            "games_analyzed": row["games_analyzed"],
            "games_capped": bool(row["games_capped"]),
            "chesscom_handle": row["chesscom_handle"],
            "handle": row["chesscom_handle"],
            "source": source or "chesscom",
            "window_days": row["window_days"],
            "time_class": row["time_class"],
            "metrics": metrics,
            "detail": row["detail"],
            "created_at": row["created_at"],
        }

    @router.get("/insights")
    def list_insights(
        handle: str | None = None,
        window_days: int | None = None,
        time_class: str | None = None,
        limit: int = 10,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user["id"]]
        if handle:
            clauses.append("chesscom_handle = ?")
            params.append(handle.strip())
        if window_days is not None:
            clauses.append("window_days = ?")
            params.append(int(window_days))
        if time_class:
            clauses.append("time_class = ?")
            params.append(time_class.strip().lower())
        params.append(max(1, min(50, int(limit))))
        rows = connection.execute(
            f"SELECT * FROM insight_runs WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        runs = []
        for row in rows:
            metrics = None
            if row["metrics"]:
                try:
                    metrics = json.loads(row["metrics"])
                except json.JSONDecodeError:
                    metrics = None
            runs.append({
                **dict(row),
                "metrics": metrics,
                "games_capped": bool(row["games_capped"]),
            })
        return {"runs": runs}

    @router.post("/insights/{run_id}/refresh")
    def refresh_insights(
        run_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM insight_runs WHERE run_id = ? AND user_id = ?",
            (run_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Insights run not found")
        source = row["source"] if "source" in row.keys() else "chesscom"
        return _start(
            InsightsStartRequest(
                chesscom_handle=row["chesscom_handle"],
                source=source or "chesscom",  # type: ignore[arg-type]
                window_days=int(row["window_days"]),  # type: ignore[arg-type]
                time_class=row["time_class"],
            ),
            user,
        )

    @router.get("/insights/{run_id}/flags")
    def list_insight_flags(
        run_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT run_id FROM insight_runs WHERE run_id = ? AND user_id = ?",
            (run_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Insights run not found")
        flags = connection.execute(
            "SELECT * FROM insight_flags WHERE run_id = ? ORDER BY delta_w DESC",
            (run_id,),
        ).fetchall()
        return {"run_id": run_id, "flags": [dict(f) for f in flags]}

    def _owned_run(connection: sqlite3.Connection, run_id: str, user: sqlite3.Row) -> None:
        row = connection.execute(
            "SELECT run_id FROM insight_runs WHERE run_id = ? AND user_id = ?",
            (run_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Insights run not found")

    @router.get("/insights/{run_id}/evidence")
    def get_evidence(
        run_id: str,
        metric_key: str | None = None,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        """Phase 1: exemplars and counter-examples backing a metric.

        Keyed by the dotted metric path, so one frontend component resolves any
        metric and a new metric never needs new evidence UI.
        """

        from server.insights_evidence import load_evidence

        _owned_run(connection, run_id, user)
        return {
            "run_id": run_id,
            "evidence": load_evidence(connection, run_id=run_id, metric_key=metric_key),
        }

    class DisputeRequest(BaseModel):
        metric_key: str = Field(max_length=200)
        agreed: bool

    @router.post("/insights/{run_id}/dispute")
    def record_dispute(
        run_id: str,
        body: DisputeRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        """Phase 1.7: a user disagreeing *while looking at the evidence*.

        Far cleaner as a calibration signal than raw outcome data, and it is the
        direct answer to "leak thresholds are reasoned, not calibrated".
        """

        _owned_run(connection, run_id, user)
        connection.execute(
            "INSERT OR REPLACE INTO leak_disputes (user_id, run_id, metric_key, agreed) "
            "VALUES (?, ?, ?, ?)",
            (int(user["id"]), run_id, body.metric_key, 1 if body.agreed else 0),
        )
        connection.commit()
        return {"run_id": run_id, "metric_key": body.metric_key, "agreed": body.agreed}

    @router.get("/insights/{run_id}/disputes")
    def list_disputes(
        run_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        _owned_run(connection, run_id, user)
        rows = connection.execute(
            "SELECT metric_key, agreed FROM leak_disputes WHERE user_id = ? AND run_id = ?",
            (int(user["id"]), run_id),
        ).fetchall()
        return {"disputes": {r["metric_key"]: bool(r["agreed"]) for r in rows}}

    class SelfAssessmentRequest(BaseModel):
        answers: dict[str, str]

    @router.post("/insights/{run_id}/self-assessment")
    def save_self_assessment(
        run_id: str,
        body: SelfAssessmentRequest,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        """Phase 12.2: the gap between self-perception and data is the insight."""

        _owned_run(connection, run_id, user)
        connection.execute(
            "INSERT OR REPLACE INTO self_assessments (user_id, run_id, answers) "
            "VALUES (?, ?, ?)",
            (int(user["id"]), run_id, json.dumps(body.answers)),
        )
        connection.commit()
        return {"run_id": run_id, "answers": body.answers}

    @router.get("/insights/{run_id}/self-assessment")
    def get_self_assessment(
        run_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        _owned_run(connection, run_id, user)
        row = connection.execute(
            "SELECT answers FROM self_assessments WHERE user_id = ? AND run_id = ?",
            (int(user["id"]), run_id),
        ).fetchone()
        answers = {}
        if row is not None:
            try:
                answers = json.loads(row["answers"])
            except json.JSONDecodeError:
                answers = {}
        return {"run_id": run_id, "answers": answers}

    @router.post("/insights/{run_id}/recompute")
    def recompute_insights(
        run_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
        user: sqlite3.Row = Depends(current_user),
    ) -> dict[str, object]:
        from server.insights_metrics import recompute_run_metrics

        row = connection.execute(
            "SELECT run_id FROM insight_runs WHERE run_id = ? AND user_id = ?",
            (run_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Insights run not found")
        metrics = recompute_run_metrics(connection, run_id)
        return {"run_id": run_id, "status": "complete", "metrics": metrics}

    return router

