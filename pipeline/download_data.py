"""Download Lichess datasets used by the MVP pipelines."""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path


PUZZLE_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
STANDARD_2026_04_URL = (
    "https://database.lichess.org/standard/lichess_db_standard_rated_2026-04.pgn.zst"
)
DEFAULT_RAW_DIR = Path("data") / "raw"


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        print(f"Already exists: {destination}")
        return destination

    print(f"Downloading {url}")
    print(f"       into {destination}")
    urllib.request.urlretrieve(url, destination)
    return destination


def _main() -> None:
    parser = argparse.ArgumentParser(description="Download Lichess MVP datasets.")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--games-url",
        default=STANDARD_2026_04_URL,
        help="Standard rated monthly PGN .zst URL",
    )
    parser.add_argument(
        "--skip-games",
        action="store_true",
        help="Only download the puzzle CSV",
    )
    args = parser.parse_args()

    download(PUZZLE_URL, args.raw_dir / "lichess_db_puzzle.csv.zst")
    if not args.skip_games:
        download(
            args.games_url,
            args.raw_dir / Path(args.games_url).name,
        )


if __name__ == "__main__":
    _main()
