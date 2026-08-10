"""Phase 1 — the evidence layer.

Every metric used to collapse ``review_moves`` into a number and throw away the
moves that produced it, so nothing downstream could show a user *why* a claim
was true. This module keeps the provenance.

Three pieces:

* :class:`Evidence` — one move, with everything needed to render and defend it.
* :func:`select_exemplars` — picks the *representative* moves, not the loudest
  ones, and never lets a single disastrous game fill the panel.
* :func:`persist_evidence` / :func:`load_evidence` — storage keyed by the dotted
  metric path, so the frontend resolves any metric generically and adding a
  metric never requires new evidence UI.

Support sets are deliberately **not** stored. Each metric carries a ``query``
predicate that regenerates them (:func:`support_query`, :func:`run_query`).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from server.insights_pro import parse_detail, parse_dt

#: Exemplars per metric, and the per-game cap that stops one blow-out game
#: filling the panel (spec 1.3).
MAX_EXEMPLARS = 5
MAX_PER_GAME = 2
MAX_COUNTER_EXEMPLARS = 3

#: Recency half-life used by the exemplar score, in days (spec 1.3).
EXEMPLAR_RECENCY_DAYS = 14.0

#: Plies of lead-in animated before the position (spec 1.6).
LEAD_IN_PLIES = 2

#: Features the typicality term is measured in. Kept explicit so the centroid
#: and the per-metric feature space can never drift apart.
FEATURE_KEYS = ("delta_w", "volatility", "findability", "time_spent", "ply")


@dataclass
class Evidence:
    """One move, with enough context to render a board and a sentence."""

    game_id: str
    ply: int
    delta_w: float
    findability: int | None
    volatility: float | None
    time_spent: float | None
    win_prob_before: float | None
    caption: str = ""
    score: float = 0.0
    san: str | None = None
    san_best: str | None = None
    move_uci: str | None = None
    best_uci: str | None = None
    fen_before: str | None = None
    lead_in: list[dict[str, Any]] = field(default_factory=list)
    opponent: str | None = None
    opponent_rating: int | None = None
    played_at: str | None = None
    user_color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Caption generation (spec 1.4) ─────────────────────────────────────────────


def _assessment(delta_w: float) -> str:
    """What the best move was worth, in words, from the Δw band."""

    if delta_w >= 50:
        return "winning"
    if delta_w >= 25:
        return "much better"
    if delta_w >= 10:
        return "clearly better"
    return "the accurate move"


def move_number(ply: int) -> int:
    return (int(ply) + 1) // 2


def build_caption(row: dict[str, Any]) -> str:
    """Generate from the row, not from a template bank.

    Clauses whose data is missing are dropped rather than filled with "unknown".
    """

    colour_suffix = "." if int(row.get("ply") or 0) % 2 == 1 else "…"
    head = f"Move {move_number(row.get('ply') or 0)}{colour_suffix}"

    opponent = row.get("opponent")
    if opponent:
        rating = row.get("opponent_rating")
        head += f" vs {opponent}" + (f" ({rating})" if rating else "")
    date = (row.get("played_at") or "")[:10]
    if date:
        head += f", {date}"

    played = row.get("san") or row.get("move_uci")
    clock = row.get("clock_remaining")
    played_clause = f" — you played {played}" if played else " — you played this"
    if clock is not None:
        played_clause += f" with {float(clock):.0f}s left"
    played_clause += "."

    best = row.get("san_best") or row.get("best_uci")
    best_clause = (
        f" {best} was {_assessment(float(row.get('delta_w') or 0))}." if best else ""
    )

    findability = row.get("findability")
    find_clause = f" Findability {int(findability)}." if findability is not None else ""

    return head + played_clause + best_clause + find_clause


# ── Exemplar scoring (spec 1.3) ───────────────────────────────────────────────


def _feature_vector(row: dict[str, Any]) -> list[float | None]:
    return [
        _as_float(row.get("delta_w")),
        _as_float(row.get("volatility")),
        _as_float(row.get("findability")),
        _as_float(row.get("time_spent")),
        _as_float(row.get("ply")),
    ]


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def centroid_and_spread(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[float | None], list[float]]:
    """Per-feature mean and standard deviation over the support set.

    A diagonal covariance stands in for the full Mahalanobis metric: the
    features are on wildly different scales (Δw 0–100, ply 1–200), which is the
    part that actually matters, and a diagonal estimate stays stable on the
    handful of rows a small bucket provides.
    """

    vectors = [_feature_vector(r) for r in rows]
    centre: list[float | None] = []
    spread: list[float] = []
    for i in range(len(FEATURE_KEYS)):
        values = [v[i] for v in vectors if v[i] is not None]
        if not values:
            centre.append(None)
            spread.append(1.0)
            continue
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        centre.append(mean)
        spread.append(math.sqrt(var) if var > 1e-9 else 1.0)
    return centre, spread


def scaled_distance(
    row: dict[str, Any], centre: Sequence[float | None], spread: Sequence[float]
) -> float:
    vector = _feature_vector(row)
    total = 0.0
    used = 0
    for value, mean, sd in zip(vector, centre, spread):
        if value is None or mean is None:
            continue
        total += ((value - mean) / (sd or 1.0)) ** 2
        used += 1
    return math.sqrt(total / used) if used else 0.0


def days_ago(played_at: Any, *, now: datetime | None = None) -> float:
    when = parse_dt(played_at, None)
    if when is None:
        return 0.0
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - when).total_seconds() / 86400.0)


def exemplar_score(
    row: dict[str, Any],
    centre: Sequence[float | None],
    spread: Sequence[float],
    *,
    now: datetime | None = None,
) -> float:
    """``impact * typicality * sqrt(recency)``.

    Ranking by Δw alone surfaces the most dramatic move, which is usually the
    least representative one; typicality pulls the selection back toward what
    the metric is actually made of.
    """

    impact = max(0.0, min(1.0, float(row.get("delta_w") or 0.0) / 25.0))
    typicality = 1.0 / (1.0 + scaled_distance(row, centre, spread))
    recency = math.exp(-days_ago(row.get("played_at"), now=now) / EXEMPLAR_RECENCY_DAYS)
    return impact * typicality * math.sqrt(recency)


def select_exemplars(
    rows: Sequence[dict[str, Any]],
    *,
    limit: int = MAX_EXEMPLARS,
    max_per_game: int = MAX_PER_GAME,
    now: datetime | None = None,
) -> list[Evidence]:
    """Top ``limit`` by exemplar score, at most ``max_per_game`` from one game."""

    if not rows:
        return []
    centre, spread = centroid_and_spread(rows)
    scored = sorted(
        ((exemplar_score(r, centre, spread, now=now), r) for r in rows),
        key=lambda pair: -pair[0],
    )

    picked: list[Evidence] = []
    per_game: dict[str, int] = {}
    for score, row in scored:
        game_id = str(row.get("game_id") or "")
        if per_game.get(game_id, 0) >= max_per_game:
            continue
        per_game[game_id] = per_game.get(game_id, 0) + 1
        picked.append(to_evidence(row, score))
        if len(picked) >= limit:
            break
    return picked


def select_counter_exemplars(
    rows: Sequence[dict[str, Any]],
    *,
    limit: int = MAX_COUNTER_EXEMPLARS,
    now: datetime | None = None,
) -> list[Evidence]:
    """Moves where the same situation was handled well (spec 1.5).

    The report reads as a prosecutor without these, and "5 failures against 40
    successes" communicates severity more honestly than any percentage.
    """

    if not rows:
        return []
    centre, spread = centroid_and_spread(rows)
    # Typicality and recency only — impact is near zero by construction here.
    scored = sorted(
        (
            (
                (1.0 / (1.0 + scaled_distance(r, centre, spread)))
                * math.sqrt(math.exp(-days_ago(r.get("played_at"), now=now) / EXEMPLAR_RECENCY_DAYS)),
                r,
            )
            for r in rows
        ),
        key=lambda pair: -pair[0],
    )
    picked: list[Evidence] = []
    seen_games: set[str] = set()
    for score, row in scored:
        game_id = str(row.get("game_id") or "")
        if game_id in seen_games:
            continue
        seen_games.add(game_id)
        picked.append(to_evidence(row, score))
        if len(picked) >= limit:
            break
    return picked


def to_evidence(row: dict[str, Any], score: float = 0.0) -> Evidence:
    return Evidence(
        game_id=str(row.get("game_id") or ""),
        ply=int(row.get("ply") or 0),
        delta_w=float(row.get("delta_w") or 0.0),
        findability=(
            int(row["findability"]) if row.get("findability") is not None else None
        ),
        volatility=_as_float(row.get("volatility")),
        time_spent=_as_float(row.get("time_spent")),
        win_prob_before=_as_float(row.get("win_prob")),
        caption=build_caption(row),
        score=score,
        san=row.get("san"),
        san_best=row.get("san_best"),
        move_uci=row.get("move_uci"),
        best_uci=row.get("best_uci"),
        fen_before=row.get("fen_before"),
        lead_in=list(row.get("lead_in") or []),
        opponent=row.get("opponent"),
        opponent_rating=row.get("opponent_rating"),
        played_at=row.get("played_at"),
        user_color=row.get("user_color"),
    )


# ── Support-set queries (spec 1.1 ``query``) ──────────────────────────────────


def support_query(**predicate: Any) -> dict[str, Any]:
    """Serialize a filter predicate that regenerates a metric's support set.

    Recognized keys: ``phase``, ``min_volatility``, ``max_volatility``,
    ``min_delta_w``, ``max_delta_w``, ``max_clock``, ``min_clock``,
    ``classification``, ``is_user_move``, ``min_findability``, ``max_time_spent``,
    ``min_time_spent``, ``min_win_prob``, ``max_win_prob``.
    """

    return {k: v for k, v in predicate.items() if v is not None}


def matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    """Evaluate a serialized predicate against one enriched move row."""

    def num(key: str) -> float | None:
        return _as_float(row.get(key))

    checks = (
        ("phase", lambda v: str(row.get("phase") or "") == v),
        ("classification", lambda v: str(row.get("classification") or "") == v),
        ("is_user_move", lambda v: bool(row.get("is_user_move")) == bool(v)),
        ("min_volatility", lambda v: (num("volatility") or -1e9) >= v),
        ("max_volatility", lambda v: (num("volatility") if num("volatility") is not None else 1e9) <= v),
        ("min_delta_w", lambda v: (num("delta_w") or 0.0) >= v),
        ("max_delta_w", lambda v: (num("delta_w") or 0.0) <= v),
        ("min_clock", lambda v: num("clock_remaining") is not None and num("clock_remaining") >= v),
        ("max_clock", lambda v: num("clock_remaining") is not None and num("clock_remaining") <= v),
        ("min_time_spent", lambda v: num("time_spent") is not None and num("time_spent") >= v),
        ("max_time_spent", lambda v: num("time_spent") is not None and num("time_spent") <= v),
        ("min_findability", lambda v: num("findability") is not None and num("findability") >= v),
        ("min_win_prob", lambda v: num("win_prob") is not None and num("win_prob") >= v),
        ("max_win_prob", lambda v: num("win_prob") is not None and num("win_prob") <= v),
        ("min_ply", lambda v: (num("ply") or 0) >= v),
        ("max_ply", lambda v: (num("ply") or 0) <= v),
    )
    for key, test in checks:
        if key in query and not test(query[key]):
            return False
    return True


def run_query(rows: Iterable[dict[str, Any]], query: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in rows if matches(r, query)]


# ── Enriched move rows ────────────────────────────────────────────────────────


def enrich_moves(
    move_rows: Sequence[Any],
    game_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten ``review_moves`` + game metadata into evidence-ready dicts.

    Done once per run: every metric then filters this list with its predicate
    instead of re-reading the database.
    """

    by_review: dict[str, list[dict[str, Any]]] = {}
    enriched: list[dict[str, Any]] = []
    for m in move_rows:
        review_id = str(m["review_id"])
        detail = parse_detail(m["detail"])
        lines = detail.get("top_lines") or []
        meta = game_meta.get(review_id) or {}
        row = {
            "review_id": review_id,
            "game_id": meta.get("game_id"),
            "ply": int(m["ply"] or 0),
            "san": m["san"],
            "is_user_move": bool(m["is_user_move"]),
            "phase": m["phase"],
            "is_book": bool(m["is_book"]),
            "classification": m["classification"],
            "win_prob": _as_float(m["win_prob"]),
            "delta_w": _as_float(m["delta_w"]) or 0.0,
            "volatility": _as_float(m["volatility"]),
            "findability": m["findability"],
            "time_spent": _as_float(m["time_spent"]),
            "clock_remaining": _as_float(m["clock_remaining"]),
            "tactic_tags": m["tactic_tags"],
            "fen_before": detail.get("fen_before"),
            "move_uci": detail.get("move_uci"),
            "best_uci": (lines[0] or {}).get("uci") if lines else None,
            "san_best": (lines[0] or {}).get("san") if lines else None,
            "top_lines": lines,
            "opponent": meta.get("opponent"),
            "opponent_rating": meta.get("opponent_rating"),
            "played_at": meta.get("played_at"),
            "user_color": meta.get("user_color"),
        }
        enriched.append(row)
        by_review.setdefault(review_id, []).append(row)

    # Lead-in plies: the move before is usually what makes a blunder
    # comprehensible, so the inspector animates into the position (spec 1.6).
    for rows in by_review.values():
        rows.sort(key=lambda r: r["ply"])
        for idx, row in enumerate(rows):
            start = max(0, idx - LEAD_IN_PLIES)
            row["lead_in"] = [
                {
                    "ply": prior["ply"],
                    "san": prior["san"],
                    "fen_before": prior["fen_before"],
                    "move_uci": prior["move_uci"],
                }
                for prior in rows[start:idx]
            ]
    return enriched


# ── Bundling: metric key -> exemplars + counter-examples ──────────────────────

#: Δw at or below which a move counts as "handled correctly" for counters.
HANDLED_DELTA_W = 5.0

#: Δw at or above which a move is a failure worth exemplifying.
FAILURE_DELTA_W = 10.0


def query_for_leak(leak: dict[str, Any]) -> dict[str, Any] | None:
    """The predicate whose support set backs a given leak.

    Leaks whose unit is the *game* rather than the move (conversion, opening,
    tilt) have no move-level predicate; they return ``None`` and are backed by
    their own evidence elsewhere.
    """

    leak_id = str(leak.get("id") or "")
    evidence = leak.get("evidence") or {}

    if leak_id == "phase" and evidence.get("phase"):
        return support_query(is_user_move=True, phase=evidence["phase"])
    if leak_id == "critical":
        return support_query(is_user_move=True, min_volatility=60.0)
    if leak_id == "clock":
        return support_query(is_user_move=True, max_clock=10.0)
    if leak_id == "time_allocation":
        return support_query(is_user_move=True, min_volatility=60.0, max_time_spent=5.0)
    if leak_id == "game_window":
        key = str(evidence.get("key") or "")
        if "-" in key:
            lo, hi = key.split("-", 1)
            if lo.isdigit() and hi.isdigit():
                return support_query(is_user_move=True, min_ply=int(lo) * 2 - 1, max_ply=int(hi) * 2)
        if key.endswith("+") and key[:-1].isdigit():
            return support_query(is_user_move=True, min_ply=int(key[:-1]) * 2 - 1)
    if leak_id == "impulse":
        return support_query(is_user_move=True, min_clock=10.0, max_time_spent=3.0)
    if leak_id == "trades":
        return support_query(is_user_move=True)
    return None


def _split(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Failures worth showing, and successes under the same predicate."""

    failures = [r for r in rows if float(r.get("delta_w") or 0) >= FAILURE_DELTA_W]
    successes = [r for r in rows if float(r.get("delta_w") or 0) <= HANDLED_DELTA_W]
    return failures, successes


def _bundle(
    failures: Sequence[dict[str, Any]],
    successes: Sequence[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, list[Evidence]]:
    return {
        "exemplar": select_exemplars(failures, now=now),
        "counter": select_counter_exemplars(successes, now=now),
    }


def build_bundles(
    enriched: Sequence[dict[str, Any]],
    *,
    leaks: Sequence[dict[str, Any]],
    measures: dict[str, Any] | None = None,
    recurrence: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, list[Evidence]]]:
    """Evidence for every leak and every new measurement, keyed by dotted path.

    The key is the contract the frontend resolves generically, so adding a
    metric never requires new evidence UI.
    """

    measures = measures or {}
    bundles: dict[str, dict[str, list[Evidence]]] = {}

    for leak in leaks:
        query = query_for_leak(leak)
        if not query:
            continue
        failures, successes = _split(run_query(enriched, query))
        if failures or successes:
            bundles[f"pro.leaks.{leak['id']}"] = _bundle(failures, successes, now=now)

    # Measurements carry their own support sets already.
    punish = measures.get("punish") or {}
    if punish.get("missed") or punish.get("taken"):
        bundles["pro.measures.punish"] = _bundle(
            punish.get("missed") or [], punish.get("taken") or [], now=now
        )

    tempo = measures.get("blunder_tempo") or {}
    for kind in ("fast", "slow"):
        moves = tempo.get(f"{kind}_moves") or []
        if moves:
            bundles[f"pro.measures.blunder_tempo.{kind}"] = {
                "exemplar": select_exemplars(moves, now=now),
                "counter": [],
            }

    blindness = measures.get("blindness") or {}
    for kind in ("defensive", "offensive"):
        moves = blindness.get(f"{kind}_moves") or []
        if moves:
            bundles[f"pro.measures.blindness.{kind}"] = {
                "exemplar": select_exemplars(moves, now=now),
                "counter": [],
            }

    for key in ("impulsivity", "stubbornness"):
        moves = (measures.get(key) or {}).get("moves") or []
        if moves:
            bundles[f"pro.measures.{key}"] = {
                "exemplar": select_exemplars(moves, now=now),
                "counter": [],
            }

    trades = measures.get("trades") or {}
    if trades.get("worst"):
        failures, successes = _split(trades["worst"])
        bundles["pro.measures.trades"] = _bundle(failures, successes, now=now)

    for cluster in (recurrence or {}).get("recurring", []):
        bundles[f"pro.recurrence.{cluster['signature']}"] = {
            # Every instance matters here — that is the whole claim — so the
            # per-game cap is relaxed for recurrence.
            "exemplar": select_exemplars(
                cluster["moves"], limit=6, max_per_game=6, now=now
            ),
            "counter": [],
        }

    return bundles


#: Heavy support lists that must never reach ``insight_runs.metrics``.
SUPPORT_KEYS = (
    "moves", "missed", "taken", "fast_moves", "slow_moves",
    "defensive_moves", "offensive_moves", "worst",
)


def strip_support(payload: Any) -> Any:
    """Recursively drop raw support sets from a metrics payload.

    Exemplars live in ``metric_evidence``; the predicate in ``query``
    regenerates the rest. Storing hundreds of plies per metric in the blob is an
    explicit anti-goal.
    """

    if isinstance(payload, dict):
        return {
            k: strip_support(v)
            for k, v in payload.items()
            if not (k in SUPPORT_KEYS and isinstance(v, list))
        }
    if isinstance(payload, list):
        return [strip_support(v) for v in payload]
    return payload


# ── Persistence ───────────────────────────────────────────────────────────────


def persist_evidence(
    connection: Any,
    *,
    run_id: str,
    bundles: dict[str, dict[str, list[Evidence]]],
) -> int:
    """Write exemplars and counter-examples for every metric key of a run."""

    connection.execute("DELETE FROM metric_evidence WHERE run_id = ?", (run_id,))
    written = 0
    for metric_key, kinds in bundles.items():
        for kind, items in kinds.items():
            for rank, item in enumerate(items):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO metric_evidence (
                        run_id, metric_key, kind, rank, game_id, ply, score,
                        caption, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        metric_key,
                        kind,
                        rank,
                        item.game_id,
                        item.ply,
                        item.score,
                        item.caption,
                        json.dumps(item.to_dict()),
                    ),
                )
                written += 1
    connection.commit()
    return written


def load_evidence(
    connection: Any, *, run_id: str, metric_key: str | None = None
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Read evidence back, grouped ``metric_key -> kind -> [evidence]``."""

    sql = (
        "SELECT metric_key, kind, rank, game_id, ply, score, caption, detail "
        "FROM metric_evidence WHERE run_id = ?"
    )
    params: list[Any] = [run_id]
    if metric_key:
        sql += " AND metric_key = ?"
        params.append(metric_key)
    sql += " ORDER BY metric_key, kind, rank"

    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in connection.execute(sql, params):
        detail = parse_detail(row["detail"])
        detail.setdefault("game_id", row["game_id"])
        detail.setdefault("ply", row["ply"])
        detail.setdefault("caption", row["caption"])
        out.setdefault(row["metric_key"], {}).setdefault(row["kind"], []).append(detail)
    return out
