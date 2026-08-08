# ChessMax

A single local website combining two projects:

- **Chess Trainer** (puzzles): tactical + quiet-position training, Maia playouts, stats dashboard. Spec: [`chess_trainer_spec.md`](chess_trainer_spec.md) (source of truth for trainer behavior).
- **Chess Volatility Bar** (vol): PGN game analyzer, position editor, saved-game library, volatility algorithm explainer. Algorithm docs: [`docs/VOL_README.md`](docs/VOL_README.md).

One FastAPI app, one page, twelve tabs in two groups:

- **Train**: `Puzzles | Eval Hold | Defense Gym | Forced Lines | Guess | Your Mistakes | Play Out | Stats`
- **Analyze**: `Game Analyzer | Position Editor | Library | Why`

## Training modes

| Mode | Goal |
|------|------|
| **Puzzles** | Classic tactical + quiet-position puzzles with Elo-style rating |
| **Eval Hold** | Survive N consecutive moves in a quiet-ish position without ever dropping the eval more than a configurable threshold (default 100cp). The engine answers with a human-like reply (Maia or top-3 sample). Tracks a survival streak. |
| **Defense Gym** | Start from a worse position (roughly -1 to -2) and *hold* it: pass if the eval doesn't collapse below your starting baseline minus the threshold over N moves, or if you reach a draw. |
| **Forced Lines** | Enter the entire line — your moves *and* the opponent's replies — before anything animates. The full sequence is validated ply-by-ply against the engine's principal variation (exact match or within eval tolerance). |
| **Guess** | Eval + sharpness guessing: both bars hidden, you estimate the eval (cp) and volatility (0-100), then the actuals are revealed and your error is scored. Calibration charts plot guessed vs actual over time. |

All four new modes share two backend utilities: `server/replies.py` (engine reply service: best move, top-N sample, or Maia) and `server/evalcheck.py` (eval-drop checker).

## Layout

| Path | What |
|------|------|
| `server/` | Puzzles backend (API, db, grading, playout, maia, selection, stats, training modes) + app entrypoint |
| `pipeline/` | Puzzle data pipeline (import, mine_quiet, seed_demo, download) |
| `chess_vol/` | Volatility package (algorithm, engine wrapper, cli, server routes) |
| `frontend/` | Merged single-page frontend (shell tab bar + both apps) |
| `data/` | Runtime data: `trainer.db`, optional engine binaries, Maia weights |
| `tests/puzzles/`, `tests/vol/` | Both test suites |

## Setup

```powershell
pip install -r requirements.txt
npm install          # chessground + chess.js for the trainer board
```

Stockfish is required for grading/analysis. Both backends resolve the binary
the same way: `STOCKFISH_PATH` env var, then PATH, then common install
locations (the trainer additionally accepts a binary dropped into `data/`).

Optional (human-like playouts): lc0 + Maia weights — see env vars below.

## Run

Seed a demo database (first time only):

```powershell
python -m pipeline.seed_demo
```

Start the app (from the repo root):

```powershell
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — you'll be asked to **sign in or create an account** (email + password). Each account keeps its own puzzle rating, attempt history/stats, mined "Your Mistakes" puzzles, saved analyzed games, and openings.

## Accounts

Email + password, hashed with the standard library's `scrypt` (no extra dependencies); the session is an httpOnly cookie. There is no email verification — accounts work immediately (intended for a self-hosted app on a trusted LAN). The **first** account you create adopts any pre-existing history from the original single-user database (rating, attempts, saved games carry over); later signups start fresh. Saved analyzed games (the vol Library) live on the server per account; the first time you log in, games previously stored in your browser are uploaded automatically.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHESS_TRAINER_DB` | `data/trainer.db` | Trainer SQLite path |
| `STOCKFISH_PATH` | auto-detect | Stockfish binary (trainer + vol analysis) |
| `CHESS_TRAINER_LC0` | `lc0` (or `data/lc0.exe`) | lc0 binary for Maia playouts |
| `CHESS_TRAINER_MAIA_WEIGHTS_DIR` | `data/maia_weights` | Maia weight files |
| `CHESS_TRAINER_MAIA_NODES` | `800` | lc0 node budget (1900 bucket) |

## Tests

```powershell
pytest tests
```

Engine-dependent tests skip automatically when Stockfish is not installed.

## Known gaps (inherited from source repos)

- Sound files now ship in `frontend/sounds/` (Lichess standard `Move`/`Capture`/`GenericNotify` plus a synthesized `Check` cue, in `.mp3` and `.ogg`). Both the trainer and the vol tab load from `/static/sounds/`.
- Stockfish / lc0 binaries and Maia weights are user-provided.
