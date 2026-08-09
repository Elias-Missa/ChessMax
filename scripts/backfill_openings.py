"""Repair ``games.opening_name`` and ``games.eco`` from stored PGNs.

Two ingest bugs left the Insights opening tree unusable for chess.com games:

* ``opening_name`` only read the ``[Opening]`` header, which chess.com does not
  send — it ships ``[ECOUrl]``. Those rows are NULL, so every game grouped under
  "Unknown opening".
* ``eco`` preferred the API's ``eco`` field, which chess.com populates with the
  opening *URL* rather than the code, so ``games.eco`` held URLs instead of
  ``A40``-style codes.

Pure parsing over PGNs already in the database: no engine, no network. Only
missing names and malformed codes are rewritten, so re-running is a no-op.

    python -m scripts.backfill_openings [--db data/trainer.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import io
import os

import chess.pgn

from server import db
from server.reviews import _ECO_CODE_RE, eco_code, opening_name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("CHESS_TRAINER_DB", "data/trainer.db"),
        help="SQLite path (default: $CHESS_TRAINER_DB or data/trainer.db)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    connection = db.connect(args.db)
    rows = connection.execute(
        "SELECT game_id, pgn, eco, opening_name FROM games WHERE pgn IS NOT NULL"
    ).fetchall()

    named = 0
    coded = 0
    unresolved = 0
    for row in rows:
        game = chess.pgn.read_game(io.StringIO(row["pgn"]))
        headers = game.headers if game is not None else {}

        needs_name = not (row["opening_name"] or "").strip()
        current_eco = (row["eco"] or "").strip().upper()
        needs_eco = not _ECO_CODE_RE.match(current_eco)
        if not needs_name and not needs_eco:
            continue

        name = opening_name(headers) if needs_name else None
        code = eco_code(headers) if needs_eco else None
        if not name and not code:
            unresolved += 1
            continue

        if name:
            if not args.dry_run:
                connection.execute(
                    "UPDATE games SET opening_name = ? WHERE game_id = ?",
                    (name, row["game_id"]),
                )
            named += 1
        if code:
            if not args.dry_run:
                connection.execute(
                    "UPDATE games SET eco = ? WHERE game_id = ?", (code, row["game_id"])
                )
            coded += 1

    if not args.dry_run:
        connection.commit()

    suffix = " (dry run — nothing written)" if args.dry_run else ""
    print(f"scanned      : {len(rows)}")
    print(f"names set    : {named}{suffix}")
    print(f"eco fixed    : {coded}{suffix}")
    print(f"unresolvable : {unresolved}")
    if named or coded:
        sample = connection.execute(
            "SELECT eco, opening_name, COUNT(*) n FROM games WHERE opening_name IS NOT NULL "
            "GROUP BY opening_name ORDER BY n DESC LIMIT 8"
        ).fetchall()
        print("\ntop openings now on record:")
        for entry in sample:
            print(f"  {entry['n']:4}  {entry['eco'] or '---'}  {entry['opening_name']}")


if __name__ == "__main__":
    main()
