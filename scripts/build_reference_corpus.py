"""Phase 4 — build the static reference corpus that turns metrics into percentiles.

"Your endgame Δw/move is 0.9" means nothing. "…the median 1500 is 0.62, you're in
the 18th percentile" is a diagnosis. This job produces the distributions that
make that sentence possible.

It runs the **shallow-tier** pipeline over games stratified by rating band
(100-point buckets) and time control, then stores per-metric quantiles. It is a
one-time, offline cost: the corpus is static, versioned, and shipped with the
app — never recomputed per run.

Two sources:

* ``--from-db`` aggregates reviews already stored locally. Cheap, and useful for
  a smoke test, but only representative if the database holds many players.
* ``--pgn`` ingests a Lichess database dump (``.pgn`` or ``.pgn.zst``) and
  analyzes it with Stockfish. This is the real corpus and it takes hours.

    python -m scripts.build_reference_corpus --from-db --time-class rapid
    python -m scripts.build_reference_corpus --pgn lichess_2026-01.pgn.zst \\
        --time-class blitz --per-cell 2000
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
from collections import defaultdict
from typing import Any, Iterable, Iterator

from server import db
from server.insights_reference import BAND_MAX, BAND_MIN, BAND_WIDTH, rating_band

CORPUS_VERSION = "v1"


# ── Metric extraction ─────────────────────────────────────────────────────────


def metrics_for_review(connection: Any, review_id: str) -> dict[str, float]:
    """The corpus metric vector for a single analyzed game.

    Deliberately computed from ``review_moves`` directly rather than through
    ``compute_tier1_metrics``: the corpus needs per-game values to build a
    distribution, not one aggregate over a player's window.
    """

    rows = connection.execute(
        "SELECT ply, phase, is_user_move, is_book, delta_w, volatility, time_spent, "
        "clock_remaining FROM review_moves WHERE review_id = ? ORDER BY ply",
        (review_id,),
    ).fetchall()
    user = [r for r in rows if r["is_user_move"]]
    if len(user) < 10:
        return {}

    from server.insights_pro import (
        BLUNDER_DELTA_W,
        CRITICAL_VOLATILITY,
        MISTAKE_DELTA_W,
        QUIET_VOLATILITY,
        accuracy_for_delta_w,
    )

    out: dict[str, float] = {}
    for phase in ("opening", "middlegame", "endgame"):
        subset = [r for r in user if (r["phase"] or "") == phase]
        if subset:
            out[f"move_quality.{phase}.delta_w_per_move"] = sum(
                float(r["delta_w"] or 0) for r in subset
            ) / len(subset)

    total_moves = len(user)
    out["headline.error_rates.blunders_per_100"] = 100 * sum(
        1 for r in user if float(r["delta_w"] or 0) >= BLUNDER_DELTA_W
    ) / total_moves
    out["headline.error_rates.mistakes_per_100"] = 100 * sum(
        1 for r in user if MISTAKE_DELTA_W <= float(r["delta_w"] or 0) < BLUNDER_DELTA_W
    ) / total_moves
    out["headline.loss.total_per_game"] = sum(float(r["delta_w"] or 0) for r in user)

    critical = [
        r for r in user
        if r["volatility"] is not None and float(r["volatility"]) >= CRITICAL_VOLATILITY
    ]
    quiet = [
        r for r in user
        if r["volatility"] is not None and float(r["volatility"]) <= QUIET_VOLATILITY
    ]
    if len(critical) >= 3 and len(quiet) >= 3:
        crit_acc = statistics.fmean(
            accuracy_for_delta_w(float(r["delta_w"] or 0), is_book=bool(r["is_book"]))
            for r in critical
        )
        quiet_acc = statistics.fmean(
            accuracy_for_delta_w(float(r["delta_w"] or 0), is_book=bool(r["is_book"]))
            for r in quiet
        )
        out["critical_moments.criticality_gap"] = quiet_acc - crit_acc

    endgame = [r for r in user if (r["phase"] or "") == "endgame"]
    if endgame:
        out["endgame.delta_w_per_move"] = sum(
            float(r["delta_w"] or 0) for r in endgame
        ) / len(endgame)

    # Punish rate needs both sides, so it is computed over the full ply list.
    from server.insights_pro import CRITICAL_HANDLED_DELTA_W

    opportunities = punished = 0
    for idx, row in enumerate(rows):
        if row["is_user_move"] or float(row["delta_w"] or 0) <= MISTAKE_DELTA_W:
            continue
        reply = rows[idx + 1] if idx + 1 < len(rows) else None
        if reply is None or not reply["is_user_move"]:
            continue
        opportunities += 1
        given = float(reply["delta_w"] or 0)
        if given <= CRITICAL_HANDLED_DELTA_W and given <= 0.5 * float(row["delta_w"] or 0):
            punished += 1
    if opportunities >= 3:
        out["measures.punish.punish_rate"] = punished / opportunities

    unhurried = [
        r for r in user
        if r["time_spent"] is not None
        and (r["clock_remaining"] is None or float(r["clock_remaining"]) >= 10)
    ]
    if len(unhurried) >= 10:
        out["measures.impulsivity.impulse_rate"] = sum(
            1 for r in unhurried if float(r["time_spent"]) < 3.0
        ) / len(unhurried)
    return out


# ── Sources ───────────────────────────────────────────────────────────────────


def iter_pgn_games(
    path: str, time_class: str | None
) -> Iterator[tuple[int, str, str, Any]]:
    """Stream ``(rating, time_class, game_id, game)`` from a Lichess dump.

    Handles plain ``.pgn`` and zstd-compressed ``.pgn.zst`` (the format Lichess
    actually publishes). Rating is the *average* of the two players, since a
    corpus cell describes a level of play rather than one seat.
    """

    import chess.pgn

    if path.endswith(".zst"):
        try:
            import pyzstd
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise SystemExit(
                "Reading .pgn.zst needs pyzstd: pip install pyzstd "
                "(or decompress the dump first)."
            ) from exc
        handle = io.TextIOWrapper(pyzstd.ZstdFile(path, "rb"), encoding="utf-8", errors="replace")
    else:
        handle = open(path, "r", encoding="utf-8", errors="replace")

    with handle:
        index = 0
        while True:
            game = chess.pgn.read_game(handle)
            if game is None:
                return
            index += 1
            headers = game.headers
            tc = _lichess_time_class(headers.get("TimeControl", ""))
            if time_class and tc != time_class:
                continue
            try:
                white = int(headers.get("WhiteElo", "") or 0)
                black = int(headers.get("BlackElo", "") or 0)
            except ValueError:
                continue
            if not white or not black:
                continue
            yield (white + black) // 2, tc, headers.get("Site", f"pgn-{index}"), game


def _lichess_time_class(time_control: str) -> str:
    """Lichess encodes the control as ``base+increment``; bucket it like chess.com."""

    try:
        base, _, inc = time_control.partition("+")
        total = int(base) + 40 * int(inc or 0)
    except ValueError:
        return "unknown"
    if total < 179:
        return "bullet"
    if total < 479:
        return "blitz"
    if total < 1499:
        return "rapid"
    return "classical"


def iter_local_reviews(
    connection: Any, time_class: str | None
) -> Iterator[tuple[int, str, str]]:
    """(rating, time_class, review_id) for every shallow review on record."""

    sql = (
        "SELECT r.review_id, r.user_color, g.white_rating, g.black_rating, g.time_class "
        "FROM reviews r JOIN games g ON g.game_id = r.game_id "
        "WHERE r.status = 'complete'"
    )
    params: list[Any] = []
    if time_class:
        sql += " AND g.time_class = ?"
        params.append(time_class)
    for row in connection.execute(sql, params):
        rating = (
            row["white_rating"] if (row["user_color"] or "white") == "white"
            else row["black_rating"]
        )
        if rating is None or row["time_class"] is None:
            continue
        yield int(rating), str(row["time_class"]), str(row["review_id"])


# ── Aggregation ───────────────────────────────────────────────────────────────


def quantiles(values: list[float]) -> dict[str, float] | None:
    if len(values) < 20:
        return None
    ordered = sorted(values)

    def q(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return ordered[idx]

    return {
        "p10": q(0.10), "p25": q(0.25), "p50": q(0.50),
        "p75": q(0.75), "p90": q(0.90),
        "mean": statistics.fmean(ordered),
        "sd": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
    }


def write_corpus(
    connection: Any,
    samples: dict[tuple[str, int, str], list[float]],
    *,
    version: str,
    dry_run: bool,
) -> int:
    written = 0
    for (metric_key, band, time_class), values in sorted(samples.items()):
        stats = quantiles(values)
        if stats is None:
            continue
        if not dry_run:
            connection.execute(
                """
                INSERT OR REPLACE INTO reference_distribution (
                    metric_key, rating_band, time_class, n_games,
                    p10, p25, p50, p75, p90, mean, sd, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric_key, band, time_class, len(values),
                    stats["p10"], stats["p25"], stats["p50"], stats["p75"],
                    stats["p90"], stats["mean"], stats["sd"], version,
                ),
            )
        written += 1
    if not dry_run:
        connection.commit()
    return written


def _ingest_pgn(
    connection: Any,
    path: str,
    *,
    time_class: str | None,
    per_cell: int,
    samples: dict[tuple[str, int, str], list[float]],
    per_cell_counts: dict[tuple[int, str], int],
) -> int:
    """Analyze dump games with Stockfish and fold them into the corpus.

    Each game is reviewed at shallow tier and stored like any other, so the
    position cache is shared and a re-run costs nothing for games already seen.
    This is the slow path — hours for a real corpus — and it is meant to be run
    once, offline.
    """

    import io as _io

    import chess.pgn

    from chess_vol.engine import Engine
    from server import game_identity
    from server.reviews import analyze_and_store, create_pending_review, upsert_game

    corpus_user = _corpus_user_id(connection)
    analyzed = 0

    with Engine() as engine:
        for rating, tc, site, game in iter_pgn_games(path, time_class):
            band = rating_band(rating)
            if band is None or per_cell_counts[(band, tc)] >= per_cell:
                continue

            exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
            pgn_text = game.accept(exporter)
            meta = {"time_class": tc, "url": site}
            game_id = game_identity.resolve_game_id(source="pgn", pgn=pgn_text, meta=meta)
            upsert_game(connection, game_id=game_id, source="pgn", pgn=pgn_text, meta=meta)

            review_id = create_pending_review(
                connection, user_id=corpus_user, game_id=game_id,
                user_color="white", depth_tier="shallow",
            )
            try:
                analyze_and_store(
                    connection, review_id=review_id, pgn=pgn_text,
                    user_color="white", depth_tier="shallow", engine=engine,
                )
            except Exception:  # noqa: BLE001 — one bad game must not stop a long job
                continue

            values = metrics_for_review(connection, review_id)
            if not values:
                continue
            per_cell_counts[(band, tc)] += 1
            analyzed += 1
            for metric_key, value in values.items():
                samples[(metric_key, band, tc)].append(float(value))

            if analyzed % 50 == 0:
                print(f"  … {analyzed} games analyzed", flush=True)
    return analyzed


def _corpus_user_id(connection: Any) -> int:
    """A dedicated owner row, so corpus reviews never mix with a real account."""

    row = connection.execute(
        "SELECT id FROM users WHERE username = ?", ("__reference_corpus__",)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    connection.execute(
        "INSERT INTO users (username, selected_openings) VALUES (?, '[]')",
        ("__reference_corpus__",),
    )
    connection.commit()
    return int(
        connection.execute(
            "SELECT id FROM users WHERE username = ?", ("__reference_corpus__",)
        ).fetchone()["id"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("CHESS_TRAINER_DB", "data/trainer.db"))
    parser.add_argument("--from-db", action="store_true", help="aggregate local reviews")
    parser.add_argument("--pgn", help="Lichess dump to ingest (not yet implemented)")
    parser.add_argument("--time-class", default=None, help="restrict to one time control")
    parser.add_argument("--per-cell", type=int, default=2000, help="target games per cell")
    parser.add_argument("--version", default=CORPUS_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.from_db and not args.pgn:
        raise SystemExit("Choose a source: --from-db or --pgn <dump>.")

    connection = db.connect(args.db)
    samples: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    per_cell: dict[tuple[int, str], int] = defaultdict(int)
    scanned = 0

    if args.pgn:
        scanned = _ingest_pgn(
            connection, args.pgn,
            time_class=args.time_class, per_cell=args.per_cell, samples=samples,
            per_cell_counts=per_cell,
        )
    else:
        for rating, time_class, review_id in iter_local_reviews(connection, args.time_class):
            band = rating_band(rating)
            if band is None:
                continue
            if per_cell[(band, time_class)] >= args.per_cell:
                continue
            values = metrics_for_review(connection, review_id)
            if not values:
                continue
            per_cell[(band, time_class)] += 1
            scanned += 1
            for metric_key, value in values.items():
                samples[(metric_key, band, time_class)].append(float(value))

    written = write_corpus(connection, samples, version=args.version, dry_run=args.dry_run)

    print(f"games sampled : {scanned}")
    print(f"cells filled  : {len(per_cell)}")
    print(f"rows written  : {written}{' (dry run)' if args.dry_run else ''}")
    if written == 0:
        print(
            "\nNo cell reached the 20-game minimum. A corpus built from one "
            "player's games is not a reference — ingest a multi-player dump "
            "before relying on percentiles."
        )
    else:
        thin = [
            f"{band} {tc}" for (band, tc), n in sorted(per_cell.items()) if n < 100
        ]
        if thin:
            print(f"\nThin cells (<100 games): {', '.join(thin[:10])}")


if __name__ == "__main__":
    main()
