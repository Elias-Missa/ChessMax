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

## The `core/` package (shared primitives — Game Review 2.0)

`core/` holds primitives shared by the review and (eventually) Puzzles 2.0, with **no FastAPI or route imports** (enforced by design; see [`game-review-2.0-spec.md`](game-review-2.0-spec.md) §1). The volatility algorithm was **moved** here — `core/volatility.py` is the implementation and `chess_vol/volatility.py` is a pure re-export shim, so there is exactly one copy. Do not re-add logic to the shim.

- `core/evaluation.py` — cp ↔ WDL ↔ win% (`win_prob`, `win_prob_cp`, `delta_w`). **Never compare centipawns directly**; reason in win probability.
- `core/acceptable.py` — `acceptable_set()` and `tau_for()`, the **single site** for the acceptable-move threshold. Phase 4 makes `tau = f(volatility)` here and nowhere else. (The trainer's cp-based `server/evalcheck.py` is a *separate* rule and is intentionally not yet migrated — that's the Puzzles 2.0 semantic redesign, not a mechanical move.)
- `core/features.py` (`dep`/`forc`/`narr`/`q`/`calc`, `reweight`), `core/findability.py` (curves + PAVA isotonic + `R_find` inversion + bands + alternate move), `core/human.py` (rating-conditioned policy: `uniform_policy` fallback + `MaiaPolicy`), `core/engine.py` (fixed-node MultiPV + iterative-deepening `d_star` capture), `core/cache.py` (Zobrist feature cache), `core/calibration.py` (Phase 3 harness).
- **Findability constants live in JSON** (`core/constants/findability.json`), loaded by `FindabilityConstants.load()`, so Phase 3 refitting never touches code. They are placeholders until calibrated.
- Findability is wired **opt-in and null-safe**: `chess_vol/findability_review.py:attach_findability` reuses the volatility MultiPV to build `MoveEval`s; the SSE endpoint enables it via the `chess_vol.server.POLICY_FACTORY` seam (defaults to Maia; the vol test-suite's autouse fixture forces it **off** so reviews stay deterministic). `findability` is `null` (never `0`) when gated out; `0` means "engine-only".
- `MaiaPolicy` selects the nearest per-rating net and derives a distribution from lc0's value head — a **documented approximation** that Phase 3 calibration found to be near-noise (r≈0.04). **`core.human.Maia2Policy` is the real backend** the spec wanted: CSSLab's single rating-conditioned Maia-2 policy head (coherent across rating), matching the same `PolicyFn` contract. It's an **optional dep** — `pip install --no-deps maia2` then `pip install gdown einops pyzstd` (maia2's numpy pin tries to build from source on Py3.14; the already-installed torch/numpy work). Weights auto-download to `maia2_models/` (gitignored) and are memoized (`_load_maia2`, load-once). **`core.human.Maia3Policy` is now the strongest backend** and the only one that reaches master strength: CSSLab's **Maia-3** (Chessformer, ICLR 2026) conditions on *raw* Elo across ~600-2600, whereas Maia-2's buckets cap at 2000 (everything ≥2000 collapses into one bucket). Install: `python -m pip install --no-deps ./data/maia3_src` (repo cloned from github.com/CSSLab/maia3 into gitignored `data/`) + `pip install huggingface-hub`; the 5M/23M/79M checkpoints auto-download from HF to `~/.cache/huggingface` (we default to **maia3-5m** for CPU speed). `Maia3Policy` calls the policy head in-process via `maia3.uci.Maia3UCIEngine` (not UCI): softmax of legal-masked move logits from `model(tokens, self_elos, oppo_elos)`. `core.human.best_available_policy()` now returns **Maia-3 > Maia-2 > None**; wire it into `chess_vol.server.POLICY_FACTORY`. When Maia-3 is the live backend, widen `core/constants/findability.json:rating_grid` to span 600-2600 (kept at 1100-2000 while Maia-2 is live so it doesn't clamp both ends).
- **Phase 3 calibration driver:** `chess_vol/calibrate_findability.py` reads the 4.29M Lichess puzzles already imported in `data/trainer.db` (they are stored *in solver-position form* — `positions.fen` is the position to score, `solution_moves[0]` the move to find, so do **not** re-run `core.calibration.solver_position`). Modes: `policy`/`value` (single-move baseline), `full` (single-position model), `line` (**multi-move whole-line** — `C_A_line = ∏ C_A(each solver move)`, the model that actually tracks puzzle difficulty). Correlation with puzzle rating climbs Maia-1 single (~0.2) → Maia-2 single (~0.3) → **Maia-2 line (~0.5)**. Run e.g. `python -m chess_vol.calibrate_findability --mode line --policy maia2 --nodes 500000 --max-plies 7`.

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

## Guess the Elo Duels

A head-to-head guessing game (its own third app root `#elo-root`, alongside puzzles + vol, registered in `frontend/shell.js` under app `"elo"`). Two players watch the **same** game and guess its hidden rating within 2 minutes; closest wins. Logic in `server/guess_elo.py` (pure scoring `decide_winner`/`guess_points`/`bot_guess`, the `elo_games` pool + `elo_duels` records, and an **in-process matchmaking waiting room with a bot fallback** after ~6s so a duel is always available), routes in `server/guess_elo_api.py:build_guess_elo_router` (mounted under `/api/elo`, `current_user`-gated, poll-based). Games are **Maia-2 self-play at a hidden true rating** — `generate_elo_game` samples the `core.human.Maia2Policy` head; pre-generate the pool offline with `python -m scripts.generate_elo_games --per-elo 5` (needs Maia-2; `pick_random_game` filters to ≥20 plies). Frontend: `frontend/elo/app.js` (lobby → matchmaking → board replay + countdown + guess → result) + `frontend/elo/styles.css`. Engine-free tests in `tests/puzzles/test_guess_elo.py`.

## Insights & game-review persistence

Spec: [`Insights.md`](Insights.md). Five top-level shell tabs (Puzzles / Training / Game Review / Insights / Guess the Elo) with History-API routes in `frontend/shell.js`; SPA fallback routes live in `server/main.py`.

**Persistence (Phase B).** Normalized tables in `server/db.py`: `games`, `reviews`, `review_moves`, `position_cache` (shared Zobrist MultiPV + optional findability features — no `user_id`). Async review jobs: `POST/GET /api/review`, `GET /api/reviews` (`server/reviews.py` + `reviews_api.py`). Game Review opens always request **full** tier, using a completed shallow row as a placeholder while it upgrades (`frontend/vol/app.js`). Anonymous `/analyze/*` SSE still works and is **not** persisted; durable reviews require an account. Changing findability constants recomputes scores from stored feature vectors via `server/findability_features.py` (no engine) — stamped with `constants_version` on each full review.

**Insights (Phase C).** `POST/GET /api/insights`, refresh, flags (`server/insights_api.py` + `insights_run.py` + `insights_metrics.py`). Ingest is chess.com **or** Lichess (`pipeline/chesscom.py` / `pipeline/lichess.py`); handle is per-run, not a profile field. Runs are immutable snapshots (keep last 10); refresh analyzes only games not already cached at shallow tier. Metrics cover Tier 1–3, practice flags (Δw≥15, findability>60 when known → Mistakes bridge + up to 5 full-tier upgrades), missed-tactic tags, and trend vs the previous matching run.

**The pro layer (`server/insights_pro.py`).** `insights_metrics` owns the Tier 1–3 catalogue; `insights_pro` owns everything a player reads first and hangs off `metrics["pro"]`: headline KPIs (record, rating delta, performance rating, Elo expectancy, accuracy spread, **rating left on the board**), move-quality mix, **critical-moment** performance bucketed by volatility, timeline, opening tree, endgame conversion, resilience, blunder timing, and a ranked **leak board**. The dependency runs one way — `insights_metrics` imports `insights_pro`, never the reverse — so the shared row helpers (`user_won`, `parse_dt`, `parse_detail`, `castle_side`, …) have exactly one definition. Per-move accuracy comes from `chess_vol.game_review.move_accuracy` so Insights and the review tab can't disagree. Every leak is scored in **win% lost per game** and capped at the loss actually observed; leaks overlap by construction, so they are ranked, never summed. The coach takeaways are just the top three leaks.

**Opening identity per source.** `server/reviews.py:opening_name` / `eco_code` normalize what each source actually sends: lichess has a real `[Opening]` header, chess.com has neither — it ships `[ECOUrl]` (parsed to a name, cutting the slug at the move continuation) and puts the *opening URL* in its API `eco` field, so the code must come from the PGN's `[ECO]`. Getting either wrong makes the Insights opening tree collapse into one "Unknown opening" row. `python -m scripts.backfill_openings` repairs already-ingested rows from their stored PGNs (no engine, no network, idempotent); follow it with `insights_metrics.recompute_run_metrics` to fold the names into saved runs.

**`metrics["game_explorer"]` is a contract, not a list view.** It is a per-game fact table (`insights_pro.build_game_facts`) carrying per-phase moves/loss/accuracy, classification counts, critical/quiet splits, scramble counts, castling, rating band and the biggest miss. The dashboard's colour/result/opponent filters **re-aggregate the whole page client-side from these rows** — `test_insights_pro.py::test_game_facts_reaggregate_to_the_server_totals` pins that summing the facts reproduces the server aggregates. Panels whose inputs only exist per move (loss taxonomy, scramble decay, session tilt, steering) carry an "All games" badge instead of silently ignoring the filter.

**Insights 3.0 (evidence, rigour, new measurement).** Spec: [`insights-3.0-spec.md`](insights-3.0-spec.md). Five modules, all pure over stored rows and all imported *by* `insights_metrics` (they import constants *from* `insights_pro`, so the dependency runs one way and there is no cycle):

- `server/insights_stats.py` — expectation adjustment (`performance_gap`), recency weighting (14-day half-life; note it uses `exp(-ln2·t/H)`, since the spec's `exp(-t/H)` gives 1/e rather than a half), Wilson intervals for proportions, seeded bootstrap for means (immutable runs must not jitter), partial pooling (`shrink_buckets`), sample-size guidance, and `MetricResult`. `DISPLAY_FLOOR_N = 8` gates the leak board.
- `server/insights_evidence.py` — Phase 1. `enrich_moves` flattens `review_moves` + game meta **once**; every metric then filters that list. Exemplars are scored `impact × typicality × √recency` with **max 2 per game**, counter-examples come from the same predicate at `Δw ≤ 5`, captions are generated from the row, and `strip_support` keeps raw support sets out of the metrics blob (they live in `metric_evidence`, regenerable via each metric's `query` predicate).
- `server/insights_measures.py` — Phases 5 and 11: punish rate, offensive/defensive blindness, fast/slow blunders, trade quality, impulsivity, metacognition, state-conditioned risk, stubbornness, tilt significance, move-time shape.
- `server/insights_signatures.py` — Phase 6 + the 10.2/10.3 classifiers it needs: move geometry, piece attribution, error signatures (truncated SHA-1, so they're stable across processes), recurrence, and practice efficacy.
- `server/insights_reference.py` + `scripts/build_reference_corpus.py` — Phase 4 percentiles and the rating-implied profile. The corpus is static and versioned; everything degrades to "unavailable" without one.

The later phases add four more modules, same one-way dependency: `server/insights_irt.py` (Phase 8 — 2PL over `r_find`, so θ is already in rating points), `server/insights_shapley.py` (Phase 9 — exact Shapley over the conditional-mean model; the budget closes because cell means reproduce the observed total, and a test asserts it), `server/insights_structure.py` (Phase 10 — pawn-structure families, endgame material signatures with optional Syzygy ground truth via `CHESS_TRAINER_SYZYGY`, hand-rolled deterministic k-means for blunder clusters), and `server/insights_export.py` (Phase 12 — greatest hits, coach memo, annotated PGN).

Two things that look like bugs but are not: SHAP values sum to **zero** per feature across a dataset (they are deviations from the mean), so attribution reports the positive and negative halves separately rather than the net; and `chess.pgn.read_game` returns an **empty Game, not None**, for unparseable input, so the PGN exporter checks `variations` before emitting.

Four measurement traps found by running these on real data, all now guarded: comparing win-probability swings classifies **every** error as defensive (the opponent gains what you lost by construction — use move *shape* instead); matching impulsive moves against a volatility min/max band is no control at all (match within deciles); counting any capture-reply as a hung piece makes every signature "hanging a pawn" (exclude exchanges the user initiated); and persisting with a piece is only stubbornness if the persistence keeps costing.

**UI (`frontend/insights/`).** The tab is a *launcher* — run form, prior runs, and a ready card with an **Open Insights** button; the report itself is a full-screen fixed overlay (`#insights-dashboard`) with a section rail, seven sections, Esc-to-close and a body scroll lock. Two traps that already bit once: `#insights-root > *` sets `position: relative` at ID specificity, so the overlays need their own `#insights-root > .insights-dashboard` rule to stay `fixed`; and the first render must be **synchronous** (`void offsetWidth`, not `requestAnimationFrame`) because rAF never fires in a backgrounded tab.

## Volatility algorithm

`chess_vol/volatility.py:compute_volatility` is pure given an engine. The math (one-ply move-choice volatility, optional recursive reply volatility via `recurse_depth`, mate-to-cp mapping, eval-aware scaling, the `decided` flag, normalization constants) is fully specified in `docs/VOL_README.md` §3 with worked examples in §3.7 and tuning constants in `chess_vol/config.py`. Two correctness traps the tests pin down: `recurse_depth=0` must stay bit-identical to Phase 1 behavior, and centipawn conversions must use the **child board's** side-to-move at every recursion level. The JSON report schema is shared between CLI and server via `chess_vol/cli_report.py` (single source of truth).

## Layout quick reference

| Path | What |
|------|------|
| `server/` | Trainer backend: `main.py` (combined app), `db.py`, `engine.py`, `grading.py`, `selection.py`, `stats.py`, `modes.py` + `modes_api.py`, `playout.py`, `maia.py`, `replies.py`, `evalcheck.py`; accounts: `auth.py` + `auth_api.py`, `deps.py` (shared `get_connection` + `current_user`), `vol_games_api.py` (per-user saved games); "Your Mistakes": `mistakes.py` + `mistakes_run.py` + `mistakes_api.py`; Guess the Elo Duels: `guess_elo.py` + `guess_elo_api.py`; Insights/reviews: `reviews.py` + `reviews_api.py`, `insights_api.py` + `insights_run.py` + `insights_metrics.py` (Tier 1–3) + `insights_pro.py` (headline/leaks/game facts), `position_cache.py`, `findability_features.py`, `tactic_tags.py`, `game_identity.py` |
| `pipeline/` | Offline puzzle data: `import_puzzles.py` (Lichess CSV → DB, also owns the `positions` schema), `mine_quiet.py` (PGN → quiet positions via Stockfish), `seed_demo.py`, `download_data.py`, `chesscom.py` / `lichess.py` (Insights ingest) |
| `chess_vol/` | Vol package: `volatility.py` (re-export shim → `core.volatility`), `engine.py`, `analyze.py`, `config.py`, `cli.py`, `server.py`, `calibrate.py`, `classify.py`, `explain.py`, `game_review.py` (expected-points review + opening/key-moments), `findability_review.py` (attaches findability), `calibrate_findability.py` (Phase 3 driver: DB puzzles → full/line calibration) |
| `core/` | Shared, FastAPI-free primitives (Game Review 2.0): `volatility.py`, `evaluation.py`, `acceptable.py`, `features.py`, `findability.py`, `human.py`, `engine.py`, `cache.py`, `calibration.py`, `constants/findability.json` |
| `frontend/` | Single page: `index.html` + `shell.js`/`shell.css` (tab shell), `auth.js` (login/signup overlay gate), `app.js` (puzzles), `vol/` (vol UI; `vol/library.js` merges `/api/reviews` + `/api/vol/games`), `insights/` (Insights UI), `elo/`, `vendor/` (vol's vendored chessground bundle) |
| `tests/puzzles/`, `tests/vol/`, `tests/core/` | The three suites; `tests/vol/conftest.py` holds `FakeEngine` and fixtures; `tests/core/` covers the shared primitives + findability (engine-free, plus `@integration` real-engine capture tests) |
| `data/` | Runtime only (gitignored): `trainer.db`, Stockfish/lc0 binaries, Maia weights, raw downloads |

`positions` table schema is created by `pipeline/import_puzzles.py:ensure_positions_schema` (imported by `db.py`), while the app-side tables (`users`, `sessions`, `attempts`, `playouts`, `playout_sessions`, `vol_games`, mode + mistakes + Insights tables) are in `server/db.py:APP_SCHEMA`. Columns added after a table's first creation go in `server/db.py:_migrate_add_columns` (idempotent `ALTER TABLE`), since the shipped `data/trainer.db` predates them — that's how `email`/`password_hash`/`password_salt`/`chesscom_username` / `insight_runs.source` reach the existing DB.

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
