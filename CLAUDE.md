# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ChessMax merges two originally-separate projects into **one FastAPI app served on one page** (`frontend/index.html`), with a flat tab bar across both:

- **Puzzles trainer** (`server/`, `pipeline/`) — API under `/api/*`. Source of truth for behavior: [`chess_trainer_spec.md`](chess_trainer_spec.md).
- **Chess Volatility Bar** (`chess_vol/`) — API under `/analyze/*`. Algorithm + phase docs: [`docs/VOL_README.md`](docs/VOL_README.md).

`server/main.py:create_app` wires them together: it builds the trainer routes inline, mounts the vol `api_router` (plus the vol package's CORS policy), and serves `frontend/` at `/static` and `node_modules/` at `/vendor`. The module-level `app = create_app()` is what `uvicorn server.main:app` runs.

## Commands

```powershell
pip install -r requirements.txt
npm install                              # chessground + chess.js, served from /vendor

python -m pipeline.seed_demo             # first-time: seed data/trainer.db with demo positions
uvicorn server.main:app --host 0.0.0.0 --port 8000   # serves everything at http://localhost:8000

pytest tests                             # whole suite
pytest tests/puzzles                     # trainer only
pytest tests/vol                         # volatility only
pytest tests/vol/test_volatility.py::test_name   # single test
pytest -m integration                    # the Stockfish-dependent tests (skipped if no binary)
```

Engine-dependent tests **skip automatically** when Stockfish isn't installed — a green run does not mean engine paths were exercised. To actually run them, install Stockfish and set `STOCKFISH_PATH` (vol) / put `stockfish` on PATH (trainer).

There is no `pyproject.toml`, so the `chess-vol` console entry point referenced in `docs/VOL_README.md` is **not installed**. The Typer CLI lives at `chess_vol/cli.py` (`app`); exercise it via `typer.testing.CliRunner` (see `tests/vol/test_cli.py`) or the `scripts/` helpers. The vol CLI/server are not the normal entry — everything ships through the combined `server.main` app.

## The two engines are separate

There are **two independent Stockfish wrappers with different path resolution** — do not unify them blindly:

- `server/engine.py` — trainer grading/analysis. Expects `stockfish` on PATH.
- `chess_vol/engine.py` — `Engine` context manager. Resolves explicit arg → `STOCKFISH_PATH` → `shutil.which` → known install locations; raises `EngineNotFoundError`. The vol code requires the engine always be context-managed and reused across recursion (a leaked process is a bug — see VOL_README §10).

Maia (human-like playout) is a third engine: `server/maia.py` drives lc0 with Maia weights over UCI, configured via `CHESS_TRAINER_LC0` / `CHESS_TRAINER_MAIA_WEIGHTS_DIR` / `CHESS_TRAINER_MAIA_NODES`.

## Engine functions are injected via `app.state` (key testing seam)

`create_app` seeds `app.state.analyze_fn`, `playout_move_fn`, `reply_fn`, and `guess_actuals_fn` with the real engine-backed implementations. Routes call **through** `app.state`, never the imports directly. Tests swap these for fakes to stay engine-free:

- Trainer tests (`tests/puzzles/test_api.py`) set `client.app.state.analyze_fn = FakeAnalyzer(...)` / `ShouldNotBeCalled()` — the latter asserts Stockfish is *never* hit for tactical attempts (a spec invariant).
- Vol tests inject via the module-level `chess_vol.cli.ENGINE_FACTORY` / `chess_vol.server.ENGINE_FACTORY`, monkey-patched to a scripted `FakeEngine` (`tests/vol/conftest.py`). `FakeEngine` replays pre-seeded MultiPV `info` dicts; `evals_to_infos` / `make_info` build them from plain cp/mate values.

When adding an engine-touching route, route the call through `app.state` (or an injectable factory) so it remains testable without a binary.

## Trainer invariants (from the spec — easy to break)

- **No tell, ever.** `GET /api/puzzle/next` must return identical shape for tactical and quiet positions (`position_id`, `fen`, `side_to_move` only — never `classification`). The whole product thesis depends on the user not knowing which kind they got until after they move. `test_next_puzzle_hides_classification` guards this.
- **Tactical vs quiet grade differently** behind that identical shape (`server/grading.py`): tactical = step-driven UCI match against `solution_moves`, **no Stockfish**; quiet = Stockfish eval-loss with multipv-3 top lines. The `/attempt` response distinguishes four statuses: `continue` (no rating delta / no `attempts` row), `solved`, `failed`, `graded`.
- **Accounts (multi-user).** Email + password (scrypt hash in `server/auth.py`, no extra deps), opaque session token in an httpOnly `chessmax_session` cookie backed by the `sessions` table. Every `/api/*` user route resolves the caller via `server/deps.py:current_user` (cookie → `sessions` → `users`); **do not** use `get_singleton_user` in routes (it remains only as a test/provisioning helper). `/analyze/*` (vol analysis, used by the chess.com extension) stays unauthenticated. The **first** account registered claims the legacy `username='default'` row (`server/auth.py:register_user`), so pre-accounts history carries over; later signups are fresh. `selected_openings` is JSON on the user row; valid values are `("london", "caro-kann")`. Tests authenticate by registering inside `make_client` (TestClient persists the cookie).
- **Selection mix** (`server/selection.py`): 50/50 tactical/quiet with no openings; 40/20/40 when openings active. Fallback chain `tactical_general → quiet → tactical_opening`; excludes the user's last 50 attempts.

## Extra training modes

The four modes beyond Puzzles (Eval Hold, Defense Gym, Forced Lines, Guess) live in `server/modes.py` with routes built by `server/modes_api.py:build_modes_router` (mounted under `/api`). Eval Hold and Defense Gym share one "hold session" state machine and differ only in position-picking and the per-move fail rule. All four are built on the two shared utilities `server/replies.py` (engine reply: best / top-N sample / Maia) and `server/evalcheck.py` (eval-drop checker). Session state for playouts and hold sessions persists in SQLite tables (`playout_sessions`, etc.), not in memory.

## Volatility algorithm

`chess_vol/volatility.py:compute_volatility` is pure given an engine. The math (one-ply move-choice volatility, optional recursive reply volatility via `recurse_depth`, mate-to-cp mapping, eval-aware scaling, the `decided` flag, normalization constants) is fully specified in `docs/VOL_README.md` §3 with worked examples in §3.7 and tuning constants in `chess_vol/config.py`. Two correctness traps the tests pin down: `recurse_depth=0` must stay bit-identical to Phase 1 behavior, and centipawn conversions must use the **child board's** side-to-move at every recursion level. The JSON report schema is shared between CLI and server via `chess_vol/cli_report.py` (single source of truth).

## Layout quick reference

| Path | What |
|------|------|
| `server/` | Trainer backend: `main.py` (combined app), `db.py`, `engine.py`, `grading.py`, `selection.py`, `stats.py`, `modes.py` + `modes_api.py`, `playout.py`, `maia.py`, `replies.py`, `evalcheck.py`; accounts: `auth.py` + `auth_api.py`, `deps.py` (shared `get_connection` + `current_user`), `vol_games_api.py` (per-user saved games); "Your Mistakes": `mistakes.py` + `mistakes_run.py` + `mistakes_api.py` |
| `pipeline/` | Offline puzzle data: `import_puzzles.py` (Lichess CSV → DB, also owns the `positions` schema), `mine_quiet.py` (PGN → quiet positions via Stockfish), `seed_demo.py`, `download_data.py` |
| `chess_vol/` | Vol package: `volatility.py`, `engine.py`, `analyze.py`, `config.py`, `cli.py`, `server.py`, `calibrate.py`, `classify.py`, `explain.py` |
| `frontend/` | Single page: `index.html` + `shell.js`/`shell.css` (tab shell), `auth.js` (login/signup overlay gate), `app.js` (puzzles), `vol/` (vol UI; `vol/library.js` is server-backed via `/api/vol/games`), `vendor/` (vol's vendored chessground bundle) |
| `tests/puzzles/`, `tests/vol/` | The two suites; `tests/vol/conftest.py` holds `FakeEngine` and fixtures |
| `data/` | Runtime only (gitignored): `trainer.db`, Stockfish/lc0 binaries, Maia weights, raw downloads |

`positions` table schema is created by `pipeline/import_puzzles.py:ensure_positions_schema` (imported by `db.py`), while the app-side tables (`users`, `sessions`, `attempts`, `playouts`, `playout_sessions`, `vol_games`, mode + mistakes tables) are in `server/db.py:APP_SCHEMA`. Columns added after a table's first creation go in `server/db.py:_migrate_add_columns` (idempotent `ALTER TABLE`), since the shipped `data/trainer.db` predates them — that's how `email`/`password_hash`/`password_salt`/`chesscom_username` reach the existing DB.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHESS_TRAINER_DB` | `data/trainer.db` | Trainer SQLite path |
| `STOCKFISH_PATH` | auto-detect | Stockfish for vol analysis |
| `CHESS_TRAINER_LC0` | `lc0` (or `data/lc0.exe`) | lc0 binary for Maia playouts |
| `CHESS_TRAINER_MAIA_WEIGHTS_DIR` | `data/maia_weights` | Maia weight files |
| `CHESS_TRAINER_MAIA_NODES` | `800` | lc0 node budget |

## Known gaps (inherited from source repos)

- Sound files ship in `frontend/sounds/` (Lichess standard `Move`/`Capture`/`GenericNotify` plus a synthesized `Check` cue, `.mp3` + `.ogg`); both the trainer (`frontend/app.js`) and the vol tab (`frontend/vol/audio.js`) load from `/static/sounds/`.
- Stockfish / lc0 binaries and Maia weights are user-provided; nothing in the repo downloads them automatically.
