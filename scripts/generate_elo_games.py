"""Generate the Guess-the-Elo Duels game pool via Maia-2 self-play.

Each game is played by Maia-2 at a hidden true rating (both sides), so the pool
carries ground-truth Elo labels for the guessing game. Run offline:

    python -m scripts.generate_elo_games --per-elo 4
"""

from __future__ import annotations

import argparse
import time

from core.human import Maia2Policy
from server import guess_elo
from server.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-elo", type=int, default=4, help="games per rating in ELO_POOL")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--reset", action="store_true", help="clear existing pool + duels first")
    args = parser.parse_args()

    connection = connect()
    if args.reset:
        connection.execute("DELETE FROM elo_duels")
        connection.execute("DELETE FROM elo_games")
        connection.commit()

    print(f"pool before: {guess_elo.pool_size(connection)} game(s)", flush=True)
    with Maia2Policy() as policy:
        if not policy.available:
            raise SystemExit("Maia-2 not available (pip install maia2 + weights).")
        start = time.time()

        def on_game(elo: int, plies: int, made: int) -> None:
            print(f"  [{made}] elo={elo} plies={plies}  ({time.time() - start:.0f}s)", flush=True)

        made = guess_elo.generate_and_store_pool(
            connection, policy, per_elo=args.per_elo, seed=args.seed, on_game=on_game
        )
    print(f"generated {made}; pool now {guess_elo.pool_size(connection)} game(s)", flush=True)
    connection.close()


if __name__ == "__main__":
    main()
