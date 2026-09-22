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
- **Findability constants live in JSON** (`core/constants/findability.json`), loaded by `FindabilityConstants.load()`, so refitting never touches code. The visibility weights, the calibration and the band thresholds are **fitted/measured**; tau, the gates and the alternate-move constants are still the spec's placeholders. Every measured constant carries a `_comment` in the JSON recording the number that justifies it.
- **The score is a calibrated probability, and `score_mode` is `"calibrated"`.** Two things were wrong with the original model and both are fixed in `core/findability.py` + `core/features.py`:
  - *The reweight discriminated nothing.* `calc = w_dep·dep + w_forc·forc + w_narr·narr` leans on `d_star` and `narr`, but the review reuses the volatility pass's MultiPV, so `chess_vol/findability_review.py` builds every `MoveEval` with `d_star=1, narr=0, q=0` — a near-constant multiplier. Supplying the *real* values changes |r(score, puzzle rating)| by 0.004, because measured against 272 rated puzzles `d_star` and `narr` have spearman **0.00**. `core.features.visibility` replaces it with properties of the move itself — gives check (−0.41), quiet (+0.32), PV forcingness (−0.30) — **centred** across the candidate set (`core.features.centered`) so it reallocates attention instead of stealing mass from `reweight`'s tail bucket. Measured and rejected: the move hanging itself (−0.12, wrong sign), backward moves, edge squares, PV length.
  - *The number meant nothing.* `100·mean(C_A)` on Maia's raw policy is "what a human plays in one second", so 74% of rated puzzles landed in Hard/Engine-only and nothing ever reached Natural. `core.findability.Calibration` inverts the Elo expectancy instead: each grid rating implies `D_r = r − 173.7·logit(C_A(r))`, `D` is their mean, and the curve is replaced by the expectancy for `D`. So **`r_find` is now a difficulty rating on the same scale as a Lichess puzzle rating**, and the score is `100·mean` of that expectancy — the share of players across the grid who find an acceptable move. An unconstrained affine map in logit space fit the puzzles marginally better but extrapolated the wrong way onto easy positions it had never seen (scoring a wide-open game position 61 where the raw model said 79); the Elo shape can't do that.
  - Result on an independent 320-puzzle holdout: |r| **0.23 → 0.39**, line probability vs Elo expectancy MAE **0.170** (r 0.741), and the fitted `c1 = 1.028` says the reweighted curve was already on the Lichess rating scale to within 3%. End-to-end with Stockfish + Maia-3: a forced recapture scores 96, a free rook 98, a free queen 95, the Ruy Exchange `dxc6` (a real choice) 72, and the `only_quiet_move` bucket of real game positions has median 25.
  - Refit with `python -m chess_vol.calibrate_findability --mode calibrate --policy maia3`. Behaviour is pinned engine-free by `tests/core/test_core_findability_calibrated.py`.
- Findability is wired **opt-in and null-safe**: `chess_vol/findability_review.py:attach_findability` reuses the volatility MultiPV to build `MoveEval`s; the SSE endpoint enables it via the `chess_vol.server.POLICY_FACTORY` seam (defaults to Maia; the vol test-suite's autouse fixture forces it **off** so reviews stay deterministic). `findability` is `null` (never `0`) when gated out; `0` means "engine-only".
- **The findability panel scores the *best* move, not the move played** — the single most misreadable thing in the review UI. `frontend/vol/review-ui.js:renderFindability` therefore leads with the best move (purple, glowing) and a one-line scope note, and `frontend/vol/app.js` draws the board arrow for that same move in the same purple (`BEST_ARROW_COLOR`, hover for score + band). The `#findModeSelect` drawer control decides *when* the panel appears: always, or only when the best move was worth ≥2/5/10 win% more than the move played (`shouldShowFindability`, persisted to `localStorage`) — otherwise an excellent move gets narrated with a difficulty score for a move the player already found. On a real 163-ply game those thresholds surface the panel on 121 / 43 / 23 / 16 plies.
- `MaiaPolicy` selects the nearest per-rating net and derives a distribution from lc0's value head — a **documented approximation** that Phase 3 calibration found to be near-noise (r≈0.04). **`core.human.Maia2Policy` is the real backend** the spec wanted: CSSLab's single rating-conditioned Maia-2 policy head (coherent across rating), matching the same `PolicyFn` contract. It's an **optional dep** — `pip install --no-deps maia2` then `pip install gdown einops pyzstd` (maia2's numpy pin tries to build from source on Py3.14; the already-installed torch/numpy work). Weights auto-download to `maia2_models/` (gitignored) and are memoized (`_load_maia2`, load-once). **`core.human.Maia3Policy` is now the strongest backend** and the only one that reaches master strength: CSSLab's **Maia-3** (Chessformer, ICLR 2026) conditions on *raw* Elo across ~600-2600, whereas Maia-2's buckets cap at 2000 (everything ≥2000 collapses into one bucket). Install: `python -m pip install --no-deps ./data/maia3_src` (repo cloned from github.com/CSSLab/maia3 into gitignored `data/`) + `pip install huggingface-hub`; the 5M/23M/79M checkpoints auto-download from HF to `~/.cache/huggingface` (we default to **maia3-5m** for CPU speed). `Maia3Policy` calls the policy head in-process via `maia3.uci.Maia3UCIEngine` (not UCI): softmax of legal-masked move logits from `model(tokens, self_elos, oppo_elos)`. `core.human.best_available_policy()` now returns **Maia-3 > Maia-2 > None**; wire it into `chess_vol.server.POLICY_FACTORY`. **Do not widen `core/constants/findability.json:rating_grid` to Maia-3's full 600-2600 range** — that was the plan while Maia-2 was live, and measuring it against 72 stratified Lichess puzzles (`--mode line --policy maia3`) showed it makes the *shipped* score worse: with `score_mode: "mean_ca"` the score is the mean of `C_A` over the grid, so adding ratings nobody in the sample plays at compresses easy and hard positions together. |r(score, puzzle rating)| falls 0.603 → 0.577 as the grid widens 1100-2000 → 600-2600, and narrowing further does not help either (0.602 at 1200-1950). Widening only helps the *unused* `r_find`-crossing scoring. The measurement is recorded in the JSON next to the constant. `--policy maia3` was added to the driver for exactly this — it previously could only measure Maia-1/2, which is how the unverified instruction survived.
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

## Puzzles feed layout (`frontend/style.css`) — three specificity traps

The trainer's CSS is scoped under `#puzzles-root`, which is an **ID selector**, so any bare-class rule that tries to restyle an element loses to it. Three consequences worth knowing before touching this file:

- **`#puzzles-root button` beats any `.my-button` rule.** A new button styled by class alone still renders as the accent-green gradient pill. Scope the override (`#puzzles-root .puzzle-hero-toggle`).
- **`.hidden` must be ID-scoped too.** Plain `.hidden { display: none }` lost to any later single-class rule setting `display` — `.playout-start { display: grid }` is defined further down the file, so the "Play it out" control was visible on every puzzle even though the JS adds `.hidden` to it. The rule is now `.hidden, #puzzles-root .hidden`.
- **The board sizes off viewport height, so anything stacked above it must be measured.** `.board` is `clamp(330px, 100dvh − header − var(--puzzle-hero-h) − 10.5rem, 640px)`; `--puzzle-hero-h` is published by `app.js:publishHeroHeight` (ResizeObserver + `switchTab`, 0 on every view but the feed). Without it the expanded Puzzles-2.0 hero pushed the board off the bottom of a 1440×900 laptop. `.layout`'s board column is `auto`, not a fixed 640px track, so the panel hugs the board when height is the binding constraint instead of leaving it adrift in a half-empty card.

## Home page (`frontend/home/`)

The landing page at `/`. Three files: the markup lives inline in `index.html` under `#home-root`, styling in `home/styles.css` (all `#home-root`-scoped), behaviour in `home/app.js`. Four things about it are load-bearing:

- **The hero board is real, and so are its numbers.** It replays Morphy's Opera Game on a live board with volatility / findability / win% readouts stepping alongside. The data is `frontend/home/opera.js`, **generated** by `python -m scripts.generate_home_demo` from `core.volatility` + `core.evaluation` + `core.findability` against Maia-3 — not authored. Do not hand-edit it, and do not "adjust" a number to look better; regenerate. Every row describes the position *before* its move, which is why the replay loop advances board and readouts **together** (`showAt`) — showing ply N's volatility over ply N's *resulting* position is the easy way to make the panel quietly wrong.
- **Pieces are chessground's.** The board is `<div class="hm-board cg-wrap">` containing bare `<piece class="queen white">` elements; the vendored `chessground.cburnett.css` + `chessground.base.css` supply the sprites and the `12.5%`/`transform` geometry for free. That is the only reason a hand-rolled board looks like the rest of the app.
- **Motion is opt-in, via `.hm-anim` on `#home-root`.** Everything that animates in is authored in its *final* state; the entry state (opacity 0, zero-height bars, dashed-out arcs) is scoped under `#home-root.hm-anim`, which `app.js` adds only when `prefers-reduced-motion` is not set. The failure mode of the usual `opacity: 0` default is a blank landing page if one script throws.
- **A hidden tab runs no rAF callbacks.** `__homeSetActive` therefore calls its activation work directly when `document.hidden`, and re-runs it on `visibilitychange`; deferring unconditionally to `requestAnimationFrame` left the segmented control unpositioned and the counters at zero for anyone opening the site in a background tab.

## Extra training modes

The four modes beyond Puzzles (Eval Hold, Defense Gym, Forced Lines, Guess) live in `server/modes.py` with routes built by `server/modes_api.py:build_modes_router` (mounted under `/api`). Eval Hold and Defense Gym share one "hold session" state machine and differ only in position-picking and the per-move fail rule. All four are built on the two shared utilities `server/replies.py` (engine reply: best / top-N sample / Maia) and `server/evalcheck.py` (eval-drop checker). Session state for playouts and hold sessions persists in SQLite tables (`playout_sessions`, etc.), not in memory.

## Endgame Arena (`core/endgame.py`, `server/endgame.py`)

The sixth training mode: mined endgame positions played out against Maia. Route `/training/endgame`, tab `endgame`, panel `#view-endgame`, all inside the existing `puzzles` app root — it is a Training sub-tab, not a seventh app.

**The Maia ladder is the whole idea.** The position is bucketed from the *user's* point of view at the start (`core/endgame.py:bucket_for_eval`), and the bucket picks the opponent's strength (`maia_rating_for`): drawn → your own level, **winning → one level up**, **losing → one level down**. Converting a won endgame against someone better is the skill worth drilling; holding a lost one is only a lesson if the draw is actually reachable. Steps are in Maia nets (1100/1300/1500/1700/1900, so one step is 200 Elo) and the ladder **clamps at the ends rather than wrapping** — a 1900 player with a winning position still gets 1900.

**The dead zone is discarded, not assigned.** `drawn_cp` (60) and `decided_cp` (200) leave a gap, and `bucket_for_eval` returns `None` inside it. A +90cp endgame is neither a holdable draw nor a conversion exercise, and calling it either teaches the wrong lesson. `max_decided_cp` (900) throws out the trivially won ones at the other end.

**Maia accepts a draw only when it is not winning** (`draw_offer_verdict`, evaluated from *Maia's* side against `draw_accept_max_cp`). That is the rule the mode hangs on: a genuinely held position ends when you claim it, and a position you blundered does not — offering a draw cannot rescue what you threw away, so Maia plays on. `min_plies_before_offer` (6) stops a drawn start being claimed on move one without playing it. The verdict is one of `too_early` / `still_winning` / `held`, each with its own message.

**Positions come ~50/50 from two corpora, as a target and not a quota** (`server/endgame.py:pick_position`): `candidates_from_own_games` walks `review_moves.detail` from the account's own completed reviews, and `candidates_from_puzzles` filters the imported Lichess set by endgame themes. Whichever side can fill the gap does — a cold account has no reviewed games and gets puzzles; a DB with no puzzle import gets its own games. **Recently-played FENs are a preference, not a filter**: a hard exclusion over the last 40 sessions exhausts a small corpus and answers "No endgame positions available yet", which is what the first version did (`tests/puzzles/test_endgame_arena.py` pins the fallback).

**Two eval conventions meet here and both have to be normalized.** `review_moves.detail` stores the eval from the *side to move*, so `candidates_from_own_games` maps it to White and then to the user; `positions.best_eval` is in *pawns* from the solver's side, and the solver is who the user plays as. Getting either wrong silently inverts the bucket, which laddered the opponent the wrong way.

`is_endgame` counts pieces (`max_pieces`, both kings included) rather than material alone — a queen still on the board carries middlegame character however few pawns remain, so a queen disqualifies unless almost nothing else is left (`allow_queens_below_pieces`).

**Session state is in SQLite, not memory** (`endgame_sessions` + `endgame_results` in `server/db.py`), stored as an initial FEN plus a UCI move list that is replayed (`_rebuild`) on every request — so a takeback is just truncating the list. Takebacks are **free and unlimited** (it is a learning mode) but are counted and shown on the result, so a game full of them reads honestly; a takeback undoes both the user's move and Maia's reply, walking back to the user's turn.

Engine access goes through `app.state` like the rest of the trainer: `playout_move_fn` (Maia's reply, shared with Play Out), `analyze_fn` (Stockfish, for the draw verdict and the hint), `maia_topk_fn` (the pink "strong at your level" arrow). Routes are built by `server/endgame_api.py:build_endgame_router` under `/api/endgame` and are `current_user`-gated: `start`, `active`, `{id}/move`, `{id}/takeback`, `{id}/draw`, `{id}/resign`, `hint`, `summary`, `{id}/pgn`. Engine-free tests in `tests/core/test_endgame.py` + `tests/puzzles/test_endgame_arena.py`.

**Live analysis reuses the review's two-arrow vocabulary but not its colours.** The `#eg-analysis` toggle (persisted to `localStorage`) draws the Stockfish best move in **green** and Maia's favourite in **pink** — the review's purple means "the move the findability panel scored", and reusing it here would claim a score the arena never computes. When Maia is absent the pink arrow simply never appears, which is the correct degradation rather than an error.

**The post-game hand-off runs through the review tab's own loader.** `{id}/pgn` builds the game with a `[FEN]`/`[SetUp]` header and the arena calls `window.__volAnalyzePgn(pgn)` — the same path the Analyze button takes, so there is one PGN loader rather than two that can diverge. `frontend/vol/app.js:parsePgn` seeds its replay board from the `[FEN]` header when there is one; without that it replays a set-up position from the standard start, diverges on move one, and reports "Could not parse that PGN" for every arena game (and every other FEN-headed PGN pasted into Game Review).

Every constant in `core/constants/endgame.json` is a **PLACEHOLDER** chosen from chess reasoning, not measured — the arena's own `endgame_results` table is what would eventually fit them. Each carries a `_comment` saying so, per repo convention.


## Daily check-in (`server/daily.py`, `frontend/daily/`)

The seventh top-level tab, at `/daily`. Five timed phases in a fixed order —
**opening repertoire → puzzles → Endgame Arena → puzzles from your games → one
loss, reviewed** — plus a monthly calendar and a streak.

**It is a conductor, not a sixth trainer.** Four of the five phases hand off to
modes that already exist (`/puzzles`, `/training/endgame`, `/training/mistakes`,
`/game-review`); `server/daily.py` owns only what those modes cannot answer —
which phase you are on, how long you have been in it, and whether today counts.
Each `Phase` carries the route it hands off to, so adding a sixth is one entry
in `PHASES` and a row that `_ensure_phase_rows` backfills onto days that already
exist.

**Five minutes is a floor, not a cap** (`TARGET_SECONDS`). Nothing stops at it
and nothing past it is discarded; a phase ends when the player says it does, and
re-entering a phase already marked done keeps counting.

**The clock lives on `<body>`, not in `#daily-root`.** `frontend/daily/app.js`
appends `#daily-hud` to the document and runs its tick loop whether or not the
daily app is the active one — that is the only reason the timer survives
navigating to the tab a phase actually happens in. Consequences worth keeping:

- The HUD needs **its own `.hidden` rule** (`#daily-hud.hidden { display: none
  !important }`). A bare `.hidden { display: none }` loses to the `display:
  flex` on `#daily-hud`, the same specificity trap `style.css` documents.
- **Time is flushed, not inferred.** The client counts seconds and posts deltas
  (`/heartbeat`, every 15 s, plus `visibilitychange` and a `sendBeacon` on
  `pagehide`); the server never derives elapsed time from timestamps. So a
  closed laptop costs exactly the seconds it was closed.
- **Only a visible tab ticks**, and `daily.MAX_HEARTBEAT_SECONDS` (120) caps
  what one jump may add. Without both, "left the tab open overnight" becomes
  eight hours of practice.
- **One phase is active at a time.** `start_phase` drops any other active phase
  to pending — two running clocks would both take heartbeats and the day's
  total would be fiction.

**The day is the player's day.** Only the browser knows the timezone, so the
client sends its local `YYYY-MM-DD` and `daily.normalize_day` clamps it to ±1
day of UTC. These genuinely differ (the local date can be a day behind UTC), so
anything that touches a session — including test harnesses — must send the same
local day the page does or it will read a different session.

**A day counts when all five phases are `done`.** `skipped` is tracked but does
not count, and `reopen` undoes a verdict without discarding the time already
spent. `streaks()` counts back from today when today is complete and from
yesterday otherwise — today has not been missed until it is over.

**The review phase's game is picked once** and stored on the phase row
(`daily_phase_runs.payload`), so re-entering the phase or reloading reopens the
same game rather than rolling again. `pick_loss` prefers a loss whose review
actually scored a move — the phase promises "opened at the move it turned", and
`_worst_miss` is what supplies the ply — and prefers a game not shown recently.
Both are **preferences, not filters** (the Endgame Arena rule): a player with
three reviewed losses must not be told there is nothing available. The hand-off
is `window.__volOpenGameById(game_id, {ply})`, the same loader Insights uses.

State is in SQLite (`daily_sessions` + `daily_phase_runs` in `server/db.py`).
The rollups on the session row are denormalized on purpose: the calendar reads a
month of days at a time and re-counting five phase rows per cell is a join
nobody needs. Routes are built by `server/daily_api.py:build_daily_router` under
`/api/daily` and are `current_user`-gated: `today`, `phase/{key}/start|heartbeat
|finish|reopen`, `calendar`. Engine-free tests in
`tests/puzzles/test_daily_checkin.py`.

## Opening repertoire (`core/repertoire.py`, `server/repertoire.py`)

Phase one of the check-in, and the only part of it that is new code. Lives in
the daily app at `/daily/repertoire` (a Drill board and a "Your lines" view).

**It is mined, not authored.** There is no opening book here and no plan to ship
one: a book tells you what masters play, which is not what you need at 1400.
Every line is a path the player has actually walked at least `min_node_games`
times, built by replaying the PGNs of their own reviewed games into a move tree;
every recommendation comes from either their own results or an engine move a
stored review already found. No engine runs, and no network.

**Three verdicts, and they are the product** (`classify_move`):

- `fix` — the reviews measured this habit costing ≥ `fix_delta_w` win% a visit
  and the engine's move differs. Play that instead.
- `gap` — the player's most-played move here is under `consistent_share` of
  their games from this position. They have not decided what they play; the
  drill is to pick one. The recommendation is the best *shrunk* score among
  moves that themselves clear `min_node_games` — telling someone to settle on a
  move they played once is another experiment, not a decision.
- `keep` — their move, nothing argues with it, drill it so it is fast.

Order matters: **a measured `fix` beats a `gap`**, because being consistent
about a move that costs you is worse, not better.

**Evidence is optional and arrives as data.** `core/repertoire.py` is pure and
takes a `fen -> PositionEvidence` mapping; `server/repertoire.py:load_evidence`
assembles it from `review_moves` (`detail.fen_before`, `detail.move_uci`,
`detail.top_lines[0]`, and `delta_w` averaged per move). With no reviews every
node is `keep` or `gap` — the correct degradation, not a guess. Positions are
keyed by `fen_key` (board/side/castling/ep, **no clocks**) so a transposition
lands on one node instead of two.

**The branch caps must never hide a measured leak.** `extract_lines` keeps the
tree small (`max_branches`, `min_branch_share`), but `paths_to_fixes` marks
every node on a route to a habit the reviews already priced and follows those
too. Marking the *whole route* is the part that matters — following the costly
move itself achieves nothing when the branch three plies above it was already
pruned, which is what the first version did. Measured on a real 252-game
account: 0 fixes surfaced while 8 repeated habits costing ≥4 win% a visit sat in
the database; path-aware widening surfaces 6 of them as drill cards (e.g. "Bf4
has cost you 12 win% a game across 6 reviewed games"). Widening uses a stricter
bar than the verdict does — `min_node_games` *reviewed* visits, not merely games
played — so one bad game cannot add a branch. That account also places
`fix_delta_w`: across its 85 repeated opening habits the median cost is 0.55
win% and p90 is 3.92, so 4.0 flags the top ~9%.

**Lines are ordered by opening ROI, not frequency.** `load_roi` reads
`pro.openings.roi.rows` from the newest completed `insight_runs` — the same
ranking `insights_pro.compute_opening_roi` feeds the Insights "what to practise
next" card — so the daily drill and the dashboard can never recommend different
openings. With no insights run the lines fall back to frequency order.

**The queue is what you get wrong, in the lines that cost most** (`order_cards`):
verdict weight, then whether the card was last answered wrong / never seen /
correct-and-fresh, then ROI, then frequency. Cards are deduplicated by position,
so two lines sharing a prefix drill it once. Every attempt is written to
`repertoire_attempts` (not just the latest) because that history is what a real
spaced-repetition schedule would have to be fitted on.

Every constant in `core/constants/repertoire.json` is a **PLACEHOLDER** chosen
from chess reasoning, not measured, and each carries a `_comment` saying so —
the same convention as `endgame.json`. Routes:
`server/repertoire_api.py:build_repertoire_router` under `/api/repertoire`
(`""`, `/drill`, `/attempt`), all `current_user`-gated. Engine-free tests in
`tests/core/test_repertoire.py` + `tests/puzzles/test_daily_checkin.py`.

## Opening book: the Chessbook-style builder (`core/opening_book.py`, `server/openings_build.py`)

A second, **authored** repertoire alongside the mined one, reached from Daily → Repertoire → **Build**. Spec: the clone brief in `docs/` (reverse-engineering notes for an independent Chessbook-like builder). API is `/api/opening-book/*` — deliberately **not** `/api/openings`, which is already the legacy selected-openings preference in `server/main.py`; hanging a router off that prefix makes which handler answers a question of registration order.

**The two repertoires answer different questions and neither replaces the other.** `core/repertoire.py` *mines* what you actually played and its value is telling you your own habits; this one is what you *chose*, position by position, before the game happened. Both feed the same drill, and the "Your lines" view shows the chosen book above the mined lines.

**The three dots are the point of the screen** (`core/opening_signals.py`). Each candidate move carries three independent signals: **engine** (Stockfish's top move), **human** (the move the strongest Maia net plays — 1900, the ceiling of the shipped nets), **results** (best score in the Lichess database at the player's own rating band). A move all three light up needs no thought; the interesting rows are where they disagree — an engine move nobody wins with is a line you will not hold, a move that scores well and the engine dislikes is a practical try worth *knowing* is one.

- **Every signal is tri-state and the third state is load-bearing.** `True` lit, `False` unlit, **`None` unknown** — no engine, no Maia weights, no explorer data. `None` is never rendered as `False`: "Stockfish does not like this" and "we never asked Stockfish" are different claims, and conflating them is the one way this display can lie. It would lie on *every row* of a box without Maia. The CSS gives unknown a **dashed** ring, distinct from unlit's solid one; `is-off` and `is-unknown` looking alike is a correctness bug, not a cosmetic one.
- An explorer that answered but whose every row is under `MIN_RESULTS_GAMES` is **known-and-unlit**, not unknown — it told us nothing scores best here. `server/opening_cache.py:lookup` returns a `source` (`cache`/`network`/`unavailable`/`off`) precisely so the two can be told apart; the payload looks identical either way.
- **The explorer reports W/D/L from White regardless of whose move it is.** `results_best` flips for the mover; reading it raw hands Black the move *White* scores best with. Pinned by `test_results_best_flips_perspective_for_black`.

**Ranking follows the spec's §6.1 formula** (`core/constants/opening_book.json`, every weight a PLACEHOLDER carried over verbatim — there is no ground truth for "the right repertoire move" to fit against). Three parts of it are deliberate: popularity is `log(1+n)` **normalized within the position**, because on raw counts move one of the game outranks everything downstream forever; engine quality is a bounded transform of centipawn **loss vs the position's own best**, because near the start every sane move evaluates within a few centipawns and raw eval ranks on search noise; and the obscure penalty has an escape hatch, because a rare move the engine tops is a find rather than an obscurity. **No legal move is ever suppressed** — ranking decides order, never permission (spec §2.3, §10).

**`position_key`, not `fen_key`** (`core/repertoire.py`). chess.js writes the en-passant square after *every* double pawn push; python-chess writes it only when a capture is legal (`fen()` defaults to `en_passant="legal"`). So the browser calls the position after 1.e4 `… b KQkq e3` and the server, pushing the same move, calls it `… b KQkq -`. Keyed on either raw string that is **two nodes**: the explorer cache forks, a node created by the client is a different row from the same node created server-side, and transposition merging silently stops working. `canonical_fen` round-trips through python-chess to drop the unusable square while **preserving a genuinely capturable one** — those positions really do differ and the spec forbids merging them (§6.4). Pinned by `test_a_chess_js_fen_and_a_python_chess_fen_are_one_node`.

**Nothing 503s when a source is missing.** Engine, Maia and explorer each degrade to an unknown dot on their own, and the panel still renders every legal move — refusing to show candidates because a third-party statistics service is unreachable is the wrong trade. `lookup` catches broadly for the same reason: `_default_fetch_json` normalizes its own failures into `ExplorerError`, but an *injected* fetcher makes no such promise.

**Explorer answers are cached by position** (`explorer_cache`, no `user_id` — the answer is a property of the position and the query, like `position_cache`), keyed by corpus + `position_key` + the rating band and speeds, because the same FEN in the masters corpus and in a 1400-1600 blitz corpus are different questions. A warmed position is fully usable **with no network at all**, which is what makes the feature testable on a box that cannot reach lichess.org.

**Coverage is weighted by how likely a reply is**, from the peer corpus — covering the three replies you meet 80% of the time is a finished repertoire, covering twelve rare ones and missing the main line is not. It is `null`, never `0`, without explorer data: "you have covered nothing" and "we do not know what you face" are different and only one is the user's fault.

Tree storage: `opening_nodes` + `opening_edges` in `server/db.py`. `role` (`mine`/`theirs`) is **derived** from whose move it is, never accepted from the client — letting the caller send it invites a repertoire that drills you on your opponent's moves. Signals are **snapshotted onto the edge** when the move is chosen, for the same reason `dev_labels` snapshots the score it was shown: the evidence moves underneath. Removing an edge deliberately leaves orphaned child nodes alone, so temporarily removing one move does not discard choices made deeper in the line.

**Prep suggestions come from the Insights ROI board, and are never re-ranked** (`server/opening_prep.py` → `GET /api/opening-book/prep`). `insights_pro.compute_opening_roi` already answers "what should I work on" in win% lost per 100 games; this is only the bridge from that answer to somewhere to act on it. A second ranking that can disagree with the dashboard is worse than one that can be wrong, so `roi_score` decides the order verbatim.

The ROI row names an *opening*; a prep card has to name a *position*. So the games behind the row (`game_ids` → `games.pgn`) are replayed and their **common prefix** taken — with a super-majority (`PREFIX_SHARE` 0.6), not unanimity, because one odd game otherwise cuts the prefix to nothing and the position before *that* is where one game went strange, not where preparation ran out. The card reports `first_gap_ply`, counting **only the player's own turns**: an unanswered opponent reply is a branch not yet explored, not a hole, and counting it reports a gap at every other ply. A line whose accuracy is already above the player's own norm says so — ROI's attribution term has demoted it, and the copy explains why rather than leaving the ranking mysterious. The `why` text obeys the same no-jargon rule as the narrative tabs.

**PGN import treats variations as branches** (`server/opening_import.py` → `POST /api/opening-book/import`). A repertoire PGN *is* a tree and its branches live in the RAV parentheses — `1.e4 c5 2.Nf3 (2.Nc3 Nc6) d6` is three decisions — so the walk recurses through every variation rather than taking the mainline. Bounded three ways (`max_plies` so a full game does not file middlegame as theory, `max_moves` so a nested study cannot hang the request, `MAX_GAMES` per paste), and malformed input is **skipped and counted, never fatal**: a paste of five games where one is broken imports four and says so.

The report counts `decisions` separately from `added` for one reason: **roles are derived**, so a repertoire pasted into the wrong side still imports cleanly — every move simply lands as an opponent reply and none of it is ever drilled. That is the real wrong-colour symptom and it is otherwise completely silent, so a paste with zero decisions is flagged rather than reported as a cheerful success.

**`build_lines` walks every root, not the node at ply 0.** A root is a node nothing points *at*. Most study chapters are written from a `[FEN]`/`[SetUp]` header and so have no ply-0 node at all; anchoring on one made every such import vanish from "Your lines" while sitting correctly in the database.

**The tree view is the book as a picture** (`server/opening_tree_stats.py` + `openings_build.build_graph` → `GET /api/opening-book/graph`). Circles are positions, edges are moves, and colour is how the player actually does at that position. Reached from Daily → Repertoire → **Tree**; clicking a circle opens that position in the Build tab, so the picture is a way in rather than only a readout.

`build_graph` is separate from `build_lines` because they want opposite things. The line list duplicates a shared prefix across every line through it — right for text, wrong for a picture, where 1.e4 must be **one** circle with several children. A transposition gives a node two parents, which has no place on a layered layout, so the first parent found keeps the child and the other route is reported in `transpositions` as a hint rather than drawn as a branch.

**Colour is a comparison, not a level**, and three things make it honest:

- **Results are measured against Elo expectancy** for the opponents actually faced, not against 50%. Raw win% paints a whole repertoire red for anyone who plays up — the same mistake the ROI leak board already rejected.
- **Both baselines are leave-one-out.** Without it a line that is most of what you play sets the baseline it is judged against and scores a flat zero, exactly as `compute_opening_roi` documents for openings.
- **Accuracy is compared per ply.** Accuracy falls with depth and a first move is near-perfect for everybody, so measuring a shallow node against an all-depths mean makes the top of every tree glow green for an artefact of depth. Pinned by `test_accuracy_is_compared_at_the_same_ply`.

**Grey is not neutral.** `health` is `null` for a position never reached and renders as a dashed hollow ring; `0.5` means "as usual" and renders as a filled slate dot. Most nodes of a young book are the former, and painting them neutral would claim results that do not exist. Small samples are shrunk toward neutral by `n/(n+6)` rather than hidden, and node *radius* carries sample size — so a confident red reads louder than a tentative one even after shrinkage has muted it.

Accuracy attaches to the node the move was played **from**, so an opponent-to-move node has none and is coloured on results alone. That is correct, not a gap: you played no move there.

Layout traps already paid for: rows need to clear a full-size circle *and* the label above it (at 34px the labels landed on the circles of the row above), and each node carries a transparent hit circle wider than the drawn one — `fill: transparent` receives pointer events where `fill: none` does not, and without it the only target is a disc as small as 7px.

Frontend: `frontend/daily/app.js` (the `build` view) + `frontend/daily/styles.css`. Two traps already paid for: the `api(path, opts)` helper takes options, so a convenience wrapper that drops its second argument turns every POST into a GET and a 405; and the book pane is rebuilt on every entry to "Your lines" rather than cached, because it is edited in the Build tab next door and anything rendered once at load is stale by the time the user looks at it. Engine-free tests in `tests/core/test_opening_signals.py`, `tests/core/test_opening_book.py`, `tests/puzzles/test_opening_builder.py`.


## Duels (Guess the Elo + Guess the Eval)

**They are one top-level tab over two independent app roots.** `#elo-root` (app `"elo"`) and `#eval-root` (app `"eval"`) are still separate apps with separate routes; what they share is the single `Duels` shell tab (`data-top="duels"`) and the `#duels-subnav` that picks between them — the same pattern Training and Game Review already use. Routes are `/duels` (→ Elo), `/duels/elo` and `/duels/eval`; the pre-merge `/guess-the-elo` and `/guess-the-eval` are kept in `ROUTES` **as aliases carrying the same `top: "duels"`**, so old links and bookmarks resolve and still light the right sub-tab. Adding a path to `frontend/shell.js` is only half the job — `server/main.py:spa_routes` has to list it too or a hard load 404s.

A head-to-head guessing game (its own third app root `#elo-root`, alongside puzzles + vol, registered in `frontend/shell.js` under app `"elo"`). Two players watch the **same** game and guess its hidden rating within 2 minutes; closest wins. Logic in `server/guess_elo.py` (pure scoring `decide_winner`/`guess_points`/`bot_guess`, the `elo_games` pool + `elo_duels` records, and an **in-process matchmaking waiting room with a bot fallback** after ~6s so a duel is always available), routes in `server/guess_elo_api.py:build_guess_elo_router` (mounted under `/api/elo`, `current_user`-gated, poll-based). Games are **Maia-2 self-play at a hidden true rating** — `generate_elo_game` samples the `core.human.Maia2Policy` head; pre-generate the pool offline with `python -m scripts.generate_elo_games --per-elo 5` (needs Maia-2; `pick_random_game` filters to ≥20 plies). Frontend: `frontend/elo/app.js` (lobby → matchmaking → board replay + countdown + guess → result) + `frontend/elo/styles.css`. Engine-free tests in `tests/puzzles/test_guess_elo.py`.

## Insights & game-review persistence

Spec: [`Insights.md`](Insights.md). Seven top-level shell tabs (Home / Daily / Puzzles / Training / Game Review / Insights / Duels) with History-API routes in `frontend/shell.js`; SPA fallback routes live in `server/main.py`.

**Persistence (Phase B).** Normalized tables in `server/db.py`: `games`, `reviews`, `review_moves`, `position_cache` (shared Zobrist MultiPV + optional findability features — no `user_id`). Async review jobs: `POST/GET /api/review`, `GET /api/reviews` (`server/reviews.py` + `reviews_api.py`). Game Review opens always request **full** tier, using a completed shallow row as a placeholder while it upgrades (`frontend/vol/app.js`) — that holds for *opening a saved game* too (`openSavedGame` → `maybeUpgradeStoredReview`), not just the "Analyze PGN" path. This matters because **findability is only computed at full tier** and Insights ingests at shallow: without the upgrade every game reached from Insights showed a dead findability panel while games you reviewed yourself showed a live one. Anonymous `/analyze/*` SSE still works and is **not** persisted; durable reviews require an account. Changing findability constants recomputes scores from stored feature vectors via `server/findability_features.py` (no engine) — stamped with `constants_version` on each full review.

`review_moves` stores only the findability **score** (plus `personal`/`r_find`); the band, the C_A curve and the alternate move are rebuilt on read by `reviews_api._findability_detail` from the stored feature vector — pure math over the cached `pi_r`, ~80 ms for a 120-move game — and returned as `moves[].findability_detail`. Deriving them keeps `core/constants/findability.json` the single source of truth for the band thresholds; without it a re-opened review rendered a bare meter with no band and no sparkline.

**Insights (Phase C).** `POST/GET /api/insights`, refresh, flags (`server/insights_api.py` + `insights_run.py` + `insights_metrics.py`). Ingest is chess.com **or** Lichess (`pipeline/chesscom.py` / `pipeline/lichess.py`); handle is per-run, not a profile field. Runs are immutable snapshots (keep last 10); refresh analyzes only games not already cached at shallow tier. Metrics cover Tier 1–3, practice flags (Δw≥15, findability>60 when known → Mistakes bridge + up to 5 full-tier upgrades), missed-tactic tags, and trend vs the previous matching run.

**Narrative layer (`server/insights_narrative.py`).** Composes `metrics.narrative` from the existing catalogue — sufficiency, verdict, why-you-lose (funnel, loss shapes, openings × colour, tactics, habits, phase, moments, twin games), how-you-win, and the eval spine. `insights_metrics` imports it; it imports constants from `insights_pro`. Voice is enforced: Verdict / Why / How may not mention Δw, volatility, findability, or "expectation-adjusted" (`test_no_jargon_reaches_the_narrative_tabs`). GET attaches the story to older runs so a rebuild is not required.

**The pro layer (`server/insights_pro.py`).** `insights_metrics` owns the Tier 1–3 catalogue; `insights_pro` owns everything a player reads first and hangs off `metrics["pro"]`: headline KPIs (record, rating delta, performance rating, Elo expectancy, accuracy spread, **rating left on the board**), move-quality mix, **critical-moment** performance bucketed by volatility, timeline, opening tree, endgame conversion, resilience, blunder timing, and a ranked **leak board**. The dependency runs one way — `insights_metrics` imports `insights_pro`, never the reverse — so the shared row helpers (`user_won`, `parse_dt`, `parse_detail`, `castle_side`, …) have exactly one definition. Per-move accuracy comes from `chess_vol.game_review.move_accuracy` so Insights and the review tab can't disagree. Every leak is scored in **win% lost per game** and capped at the loss actually observed; leaks overlap by construction, so they are ranked, never summed. The coach takeaways are just the top three leaks.

**Openings are ranked by ROI, not by win rate** (`insights_pro.compute_opening_roi` → `pro.openings.roi`, and the `opening` leak). Win rate alone gets the practice question wrong twice: it promotes a disaster you meet once a season over a line you meet weekly, and it treats 50% against equal opposition as a leak when it is par. So `points_per_100_games = exposure x deficit-below-par x 100 x confidence` — exposure is `n / games`, **par** is the mean Elo expectancy of the opponents actually faced in that line shifted by how the player does in their *other* games (**leave-one-out**, or a repertoire staple that is 60% of the window sets the baseline it is judged against and scores a flat zero), and confidence is `n/(n+4)` so rare lines are shrunk rather than hidden. `roi_score` — the ranking key, a priority and not a quantity — additionally multiplies by `attribution`, which is the opening-phase loss and the in-line accuracy measured against the player's own norms: **a line you score badly in but play accurately is not fixed by studying it**, you lost those games elsewhere. `points_per_100_games` is already in the leak board's unit (win% per game), which is why the `opening` leak reports it unchanged. Dashboard card: "What to practise next" (`body-opening-roi`).

**Opening identity per source.** `server/reviews.py:opening_name` / `eco_code` normalize what each source actually sends: lichess has a real `[Opening]` header, chess.com has neither — it ships `[ECOUrl]` (parsed to a name, cutting the slug at the move continuation) and puts the *opening URL* in its API `eco` field, so the code must come from the PGN's `[ECO]`. Getting either wrong makes the Insights opening tree collapse into one "Unknown opening" row. `python -m scripts.backfill_openings` repairs already-ingested rows from their stored PGNs (no engine, no network, idempotent); follow it with `insights_metrics.recompute_run_metrics` to fold the names into saved runs.

**`metrics["game_explorer"]` is a contract, not a list view.** It is a per-game fact table (`insights_pro.build_game_facts`) carrying per-phase moves/loss/accuracy, classification counts, critical/quiet splits, scramble counts, castling, rating band and the biggest miss. The dashboard's colour/result/opponent filters **re-aggregate the whole page client-side from these rows** — `test_insights_pro.py::test_game_facts_reaggregate_to_the_server_totals` pins that summing the facts reproduces the server aggregates. Panels whose inputs only exist per move (loss taxonomy, scramble decay, session tilt, steering) carry an "All games" badge instead of silently ignoring the filter.

**Insights Cinema (`frontend/insights/cinema.{js,css}`).** Two full-screen surfaces that
front the whole tab, both children of `#insights-root` and both **empty in `index.html`** —
`cinema.js` builds their markup:

- **The Forge** (`#insights-forge`) is what a run looks like *while it computes*: a
  determinate ring, a stage line, a step rail, and a card per analyzed game. It narrates
  **real server phases**, not a fake timeline — `insights_run._set_stage` writes
  `insight_runs.stage` / `stage_detail` / `games_total` at each phase (`fetching` →
  `analyzing` → `measuring` → `practice` → `story` → `complete`) and the GET returns them.
  `progress` alone cannot tell "fetching games" from "computing metrics after the last
  game landed"; both sit at a number the client cannot interpret. Pinned by
  `test_insights_run_records_its_stage`.
- **The film** (`frontend/insights/cinema-film.js`) is a **horizontal strip, not a deck**.
  All 22 panels sit side by side in one flex track that is translated on X, and every
  transform, blur, bar and counter on them is *scrubbed* — written each frame as a
  function of `d`, the panel's signed distance from the frame centre in viewport widths.
  Wheel, trackpad, touch and drag all feed one `target`; `pos` chases it with a
  frame-rate-normalized lerp; autoplay is just a constant velocity added to that same
  `target`, which is why taking the wheel mid-cruise blends instead of cutting.
  `buildPanels()` returns `{ id, chapter, html, hasBoard }` and **every panel guards its
  own inputs** — a 5-game run plays a handful rather than 22 full of em-dashes. It ends on
  the **Atlas**: the practice set as a board gallery, a chapter-replay grid, and the
  hand-offs into the story / Deep Dive / trainers.
- **`cinema-film.js` needs its own `<script>` tag, after `cinema.js`.** The shell publishes
  `window.__insightsCinemaInternals`, which the film reads at load to install
  `window.__insightsFilm`; `cinema.js` calls it only through `film() => window.__insightsFilm
  || null`, so a missing tag is silent — play, replay and the chapter ticks all become
  no-ops and the Atlas is the only thing that renders. A merge dropped exactly that line
  once (`9405310`), and nothing failed loudly.
- **Four numbers decide the feel**, all in `FEEL` at the top of `cinema-film.js`.
  `buildWindow` is the load-bearing one: a panel must be *half* built when it is half a
  screen out, or the midpoint of every transition is two invisible panels and a black
  frame — which is exactly what a slide deck looks like. `lerp` is the smoothness,
  `cruiseSecPerScreen` the pace, `depthFade`/`depthBlur` how much a neighbour recedes.
- **A real scroller (Lenis / ScrollTrigger) was tried and rejected.** Autoplay has to
  blend with user input rather than fight it, and ScrollTrigger's play/reverse semantics
  are the slide-deck behaviour being removed. GSAP is still used for everything else:
  `gsap` + `Observer` (input normalization) + `SplitText` (headline chars) + `DrawSVGPlugin`
  (charts and gauges), vendored under `frontend/vendor/js/gsap/` — all free under GSAP's
  standard licence since 3.13. `cinema-film.js` degrades to a static, readable strip if
  any of them fail to load.
- **`measure()` reads `offsetLeft`/`offsetWidth`, never `vw * width`.** `.pnl` carries
  horizontal padding, so without `box-sizing: border-box` (set explicitly — there is no
  reset on this subtree) each panel is 128px wider than the model thinks, and the strip
  drifts a whole screen out of register by panel ten while still *looking* plausible.
- It reads only `metrics.pro`, `metrics.narrative` and `metrics.game_explorer` — the same
  contract the dashboard and post-mortem read, so the film can never disagree with the
  tables behind it. Route `/insights/:runId/cinema`; the post-mortem's `parsePath` returns
  null for it and closes itself, which is the hand-off.
- `build_game_facts` **normalizes `sparkline` to White** before sampling.
  `review_moves.win_prob` is *mover-relative*, so walking every ply straight out of the
  table alternates perspective and averages to a flat line at 0.5 — which is what the eval
  spine and the trajectory overlay were drawing. `_user_curve` flips the whole curve for a
  Black player and must not be double-corrected.
- **Colour is a verdict, so magnitude bars default to neutral.** `.bar-col i` and
  `.row-fill` are slate unless the panel classes them `is-good`/`is-warn`/`is-bad`. On
  "blunder rate by move number" or "win% given away per move", a taller *green* bar means
  a worse result — five green bars there read as five good ones.
- Three rendering traps worth keeping: a gradient `background-clip: text` on an inline
  `<em>` paints across the *whole headline's* box, so a two-word `<em>` renders half white
  and half green (solid ink + glow instead); `preserveAspectRatio="none"` on a chart SVG
  squashes every `<text>` node into unreadable condensed type; and a CSS `transition` on
  any property the frame loop also writes makes the strip judder, because the two fight.

**UI (`frontend/insights/`).** The tab is a *launcher* — run form, prior runs, and a ready card whose primary button is **Play your report** (the film), with *Why you lose*, *Study plan* and *Deep dive* alongside. That opens the narrative overlay (`#postmortem`, `postmortem.js` / `postmortem.css`): Verdict → Why you lose → How to fix it, with Deep Dive handing off to the existing seven-section dashboard. Routes: `/insights/:runId/verdict|why-you-lose|how-you-win|deep-dive`. The server owns the story (`metrics.narrative` from `server/insights_narrative.py`); the frontend only renders. Verdict / Why / How copy must not contain Δw, volatility, findability, or "expectation-adjusted". Signature visuals are custom SVG (eval spine, loss funnel, trajectory overlay, opening heat table) plus lazy Chessground boards. Two overlay traps: `#insights-root > *` sets `position: relative` at ID specificity, so `#insights-root > .pm-overlay` and `#insights-root > .insights-dashboard` need their own `position: fixed`; a `<button>` cannot contain a `<button>` (funnel chips are siblings). Old runs without `narrative` get it attached on GET (pure over the stored blob, no engine).

Every "Review"/"Open game" hand-off carries the **ply** as well as the game id — `goReview(gameId, ply)` → `window.__volOpenGameById(gameId, {ply})` → `openSavedGame` jumps straight to that move (1-based, as stored in `review_moves.ply`). A flagged miss that dumps the user at move 1 makes them hunt for it, so `insights_narrative` moments carry `ply` for exactly this.

## The study plan (`server/study_plan.py`)

Everything else in Insights answers "what happened". The plan answers "what do I
do on Tuesday". It is **pure over a computed metrics blob** — the same `pro` /
`narrative` / `game_explorer` contract the dashboard and the film read, so it can
never disagree with the tables behind it — and takes no engine, no DB and no
network. Stored at `metrics.study_plan` by `compute_tier1_metrics`, backfilled on
read by `ensure_study_plan` (the `ensure_narrative` pattern), and rebuilt for any
other budget by `GET /api/insights/{run_id}/study-plan?hours_per_week=&weeks=`,
which is why the UI's hours/weeks controls can be live.

- **Blocks are built from evidence or not at all.** Nine builders (`openings`,
  `tactics`, `mistakes`, `middlegame`, `endgames`, `calculation`, plus the habit
  blocks `blunders`, `clock`, `mental`); each returns `None` unless it can name
  the opening, the motif, the window and the number. There is no fallback copy
  telling somebody to "work on tactics": a thin window simply gets fewer blocks,
  and a run under `MIN_GAMES` gets `available: False` with a stated reason
  instead of a plan.
- **Hours are a budget, not a wish list.** `_allocate` water-fills the weekly
  budget in proportion to what each block costs the player, floored at
  `MIN_BLOCK_HOURS` and capped at `MAX_BLOCK_SHARE`, then **drops** what does not
  fit rather than shaving everything into tokens. Habit blocks cost zero study
  time — they are rules applied while playing — so a 1h/week plan still gets all
  of them.
- **Two effort models, both placeholders, both stated.** Study blocks recover
  `ceiling · h / (h + half_hours)` — saturating, so doubling the hours never
  doubles the gain. Habits are modelled on *weeks* instead and reach their
  ceiling at about a month. Nothing here is fitted; the ordering is the claim
  (memorising a line you already reach is fast at 5h, calculating better is slow
  with a low ceiling at 14h).
- **The projection is capped twice, and the second cap is the important one.**
  `pro.headline.elo_left_on_board` is the measured pool, converted through
  `insights_pro.rating_difference` (the one FIDE curve in the codebase). But on a
  bad window that pool is enormous — a seeded 24-game window with a blunder in
  most games measured **~200 points** — and no four-week plan delivers that.
  `_pace_ceiling` is a second
  bound: rating points per week a player plausibly absorbs, decaying with rating
  and scaling with committed hours. `limited_by` reports which cap bound, and the
  UI shows the larger pool separately as "on the table long-term". Without it the
  headline number was that full pool in four weeks, which would have discredited
  the whole feature. Per-block gains are the capped total split proportionally, never a sum
  of independently-converted leaks.
- **Voice follows `insights_narrative`.** No Δw, volatility, findability or
  "expectation-adjusted" in anything a player reads; `plan_strings` is the
  extractor and `test_no_jargon_reaches_the_plan` is the guard. The numbers still
  reach the UI as labelled `evidence` fields.

**Two new per-game facts feed it** (`server/game_shape.py`, added to
`build_game_facts`): `centre` (`closed`/`semi_open`/`open`, from blocked pawn
pairs on the c–f files at the first middlegame ply) and `endgame_type`
(`pawn`/`minor`/`rook`/`rook_minor`/`queen`/`heavy`, from material at the first
endgame ply). Both replay the stored PGN, both are **heuristics with the rule
written beside them**, and both are `None` — never a guessed default — when the
game never reached that phase or the PGN will not parse. They exist so the plan
can say "you score 30% in rook endings" instead of "work on endgames"; that is
the whole reason they were added.

**UI: `frontend/insights/studyplan.{js,css}`**, a third full-screen overlay
alongside the post-mortem and the cinema, reached from the ready card's **Study
plan** button and the route `/insights/:runId/study-plan`. `app.js:routeDeepLink`
now hands one deep link to whichever surface owns it (film → plan → story), each
returning falsy for a path that is not its own. The overlay's own accent is amber
rather than the story's green so a player can tell which surface they are on, and
it carries a print stylesheet because the plan is a thing people want on paper.
Same two overlay traps as the post-mortem: `#insights-root > .sp-overlay` needs
its own `position: fixed`, and it must close the other two before taking the body
scroll lock. Engine-free tests in `tests/puzzles/test_study_plan.py`.

## Human Eval, arrows and steering advice (Game Review)

Three layers added on top of volatility and findability, all reusing the MultiPV the volatility pass already produced — none costs an extra engine search.

**Human Eval (`core/human_eval.py`).** Stockfish's evaluation assumes both sides find every move, which answers "is this objectively winning" and not "am I winning". The bar re-weights the same candidate moves by a rating-conditioned human policy: `human_cp = Σ π_R(m)·eval(m) + tail·eval(worst known line)`. **The tail is the load-bearing part** — MultiPV covers only the top handful of moves, so the policy's remaining mass sits on moves we have no eval for; renormalising over the covered set would pretend the player only ever picks among the engine's top lines, biasing the bar upward exactly in the messy positions it exists for. Valuing the uncovered mass at the *worst* line we do have is optimistic but bounded. The policy is asked at **user rating + `rating_offset` (500)**, a placeholder in `core/constants/review_advice.json`. It is **one ply, not a playout**: a tactic whose *first* move is findable still reads as bad. Null-safe like findability — no model, thin coverage, or no candidates yields `null`, never `0`, and the bar hides rather than showing even.

**Steering advice (`core/vol_advice.py`).** Volatility is neutral until you know who is winning. Winning + sharp is bad for you (every extra chance is a chance for *you* to go wrong); losing + quiet is bad for you (a stable position converts their advantage for free). The second is the half players get wrong — trading into a lost endgame is not "holding". Read against win probability **after** the move from the **mover's** POV and the volatility of that state; `severity` escalates from `info` to `warn` only when the move itself swung volatility past `swing_volatility`, otherwise every quiet move in a won game nags. Needs no human model, so it survives a Maia-less box.

**Arrows are conditional now (`frontend/vol/app.js`).** The best-move arrow appears **only on an inaccuracy or worse** — pointing at the engine's move after the user already found an excellent one is noise that trains the eye to ignore the arrow. Alongside it, a pink arrow (`HUMAN_ARROW_COLOR`) shows the move the human policy actually prefers when it differs from the engine's. Each arrow carries **its own hover text** via `dataset.tip` (the old single global `arrowTipText` could not describe two arrows); the best-move arrow's tip carries the real findability score, the pink one carries the policy probability and says so, because findability is only ever computed for the engine's move. When the user *played* the human policy's favourite and was still marked down, `renderReviewNotes` says so rather than only scolding.

**Both metrics flow through two separate paths and both must be fed.** `chess_vol/server.py` handles the anonymous SSE; `server/reviews.py:analyze_and_store` handles the durable path the Game Review tab actually uses (`Done (full) · N plies · persisted`). They are stored in the per-ply `detail` blob — neither is aggregated over, so neither earned a column — and `frontend/vol/library.js:reviewToReport` maps them back onto the ply. **A completed review is cached**: `POST /api/review` returns an existing complete row without re-analysing, so changing what a review stores means old rows keep the old shape until they are re-created.

**Piece values in the review** are opt-in (`#pieceValuesToggle`, persisted to `localStorage`) and fetched per position from `/api/dev/piece-values` — about one engine search per piece, far too slow to run while arrow-keying, so requests are FEN-keyed and the newest wins.

## Contextual piece values (`core/piece_values.py`)

**A piece is worth what the position loses without it.** Remove it, re-search, and the eval drop is that piece's value *in this position* — no hand-weighted mobility/king-safety sum (that is the classical eval, which took a decade to tune and cannot be validated). `core/piece_features.py` supplies the *explanation* ("outpost", "trapped", "protected_passer") but never the number. `compute_piece_values(board, engine, ...)` takes any `core.volatility.EngineLike`, so wrapping it in `server/position_cache.py:CachingEngine` makes every probe Zobrist-cached for free.

Two identities hold exactly, and both are pinned by tests: `Σ sign·anchored_cp == eval_material_cp` (the Shapley efficiency axiom, restored by redistributing the residual proportionally to `|raw|`) and `Σ sign·premium_cp == gap_cp`. The second is the product story — "up 5 in material, eval −2" decomposes fully into per-piece premiums.

Four things were measured, not assumed, and three of them changed the design:

- **Raw ablation in centipawns is not a piece value.** Removing a queen from the opening moves the eval 773cp; removing a bishop moves it 613. The queen reads as worth *less than a bishop* because both land where the engine's eval has stopped being linear in material, and no single scale factor fixes a distortion that is not even monotone in the static table. `EvalScale` maps eval → material-equivalent *before* differencing. Fitted with **pawns as the yardstick** (remove k pawns, k=1..6): the obvious piece-at-a-time version samples only four material levels and put a rook (616) and a knight (467) close enough that inverting between them amplified noise ~3.6x. Medians, then `core.findability.pava` — the raw medians are non-monotone around 500-600cp, where the eval has so little resolution that bucket noise outweighs a whole pawn.
- **Leave-one-out is biased per piece type.** After the scale fix, minors measured ~35% above their static value and rooks ~12% below. That is substitutability, not noise: a side usually has two rooks, so removing one leaves the other doing its work, while a lone queen has no understudy. `type_scale` is a uniform factor *within* a type, so it fixes the cross-type comparison and disturbs no contextual ordering inside it. Fitted on one seed, verified on another: pawn 114, knight 310, bishop 286, rook 493, queen 897 against 100/300/300/500/900.
- **Depth 12 is not enough, and depth is load-bearing rather than a speed dial.** At depth 12 the correlation on equal-value trades swung 0.19–0.49 between samples and the best shrinkage was 0.0 ("ignore the measurement"). At depth 16 it is stable (r 0.43 and 0.53 on two seeds).
- **The measurement is real but noisy, so it ships shrunk.** On equal-value trades — where the static table predicts 0 by construction and so correlates exactly 0 — the measured values correlate 0.43–0.53 with what actually happens. But pure measurement still loses on MAE to a constant on one seed, because MAE punishes variance and a constant has none. `shrinkage: 0.5` (optima were 0.6 and 0.4) beats static on **both** seeds: MAE 86.6 vs 105.5 and 87.8 vs 93.3. It applies to `shrunk_cp` only — `anchored_cp` keeps the exact reconciliation, because explaining and predicting want opposite things from noise. **Use `shrunk_cp` to predict, `anchored_cp` to explain.**

`board.remove_piece_at()` does not clean up after itself, and each breakage needs a different answer (all four verified against python-chess 1.11.2): `BAD_CASTLING_RIGHTS` → `clean_castling_rights()`; `INVALID_EP_SQUARE` → clear it; `OPPOSITE_CHECK` (the removed piece was shielding its own king and the *opponent* is to move) → give the owner the move, tagged `turn_flipped`. Deliberately **not** repaired: the owner in check on the owner's move, which is legal and whose dreadful eval is the correct answer. `compute_piece_values` refuses an invalid input board outright — an illegal position produces plausible-looking numbers that mean nothing.

**Saturated positions are flagged, not rescaled** (`|eval| >= 800`). Past the fitted range the scale extrapolates and `raw_cp` can go wild (a passer in a +1650 position measured −29.68 pawns); `saturated` is how that is communicated rather than hidden.

Driver: `python -m chess_vol.calibrate_piece_values --mode scale|recover|oracle|trades` (`--equal-only` isolates the contextual signal by keeping only trades where the static values match). Engine-free tests in `tests/core/test_piece_values.py` + `test_piece_features.py`; real-engine oracles in `tests/core/test_piece_values_integration.py`.

## Dev tab (calibration labelling)

An **internal harness, not a product surface** — a small amber `Dev` tab parked at the far right of the shell bar (`.shell-tab--dev` gets `margin-left: auto`; `.shell-tabs` is `flex: 1 1 auto`, so the free space is there). Route `/dev`, app root `#dev-root`, app `"dev"`. It exists so the volatility and findability constants can be refit against human judgement instead of only against Lichess puzzle ratings.

It pages through **`review_moves`** — every ply of every *completed* review the account owns — draws the position, shows the volatility and findability we stored, and records a verdict on each. Five buttons per metric, keyed `1`-`5` (volatility: way lower → way higher) and `Q W E R T` (findability: way harder → way easier); `←`/`→` walk positions. Both scales run the same direction: **leftmost means "the number should be smaller"**, which is what `VOLATILITY_LABELS` / `FINDABILITY_LABELS` in `server/devlabels.py` encode as a signed `delta` for a fitter to consume.

- **The findability panel labels the *best* move, not the move played** — the same trap the review UI has. Every row carries `best_uci`/`best_san` from the stored MultiPV top line, the panel names it inline, and the board draws it in the review's purple (the played move is blue). Get this wrong and every verdict collected is about the wrong move. `test_positions_expose_the_stored_scores_and_the_best_move` pins it.
- **The two labels are independent** because findability only exists at full tier — shallow rows (what Insights ingests at) and gated positions (forced / already decided) carry volatility and a `null` findability, and the panel says so rather than showing a zero.
- **Scores are snapshotted onto the label row** (`dev_labels.volatility` / `findability` / `constants_version`). A refit changes what `review_moves.findability` says, and "about right" is only interpretable against the number it was given.
- **Ordering is server-side and stable** (`reviews.created_at, review_id, ply`). A labelling pass that reshuffles between pages is unusable, so the client addresses positions by absolute index and caches pages rather than re-sorting what it holds. The listing and its `COUNT` share one WHERE clause (`_where_clauses`) or the "position N of M" counter drifts — `test_scope_and_require_filters_agree_with_their_count` pins that.
- Clearing both labels **deletes** the row rather than leaving a tombstone, which is what keeps the `unlabeled` scope honest. Clicking the active verdict again is the toggle.
- **Piece-values mode** is a second tool behind the same tab (`.dv-modes` toggle): paste a FEN, press Measure, and get the contextual value of every piece. It is the one **engine-backed** `/api/dev/*` route (`server/piecevalues_api.py`, ~30 searches per request, ~10s at depth 14), so it goes through an injectable `ENGINE_FACTORY` like `chess_vol.server`'s and answers **503** with an actionable message when Stockfish is absent. On the board the **number is the value and the colour is the premium** over the static table — the premium is the insight, so it gets the legend. The table is sorted by `|premium|` so the piece explaining the gap is row one, and shows `Value` (reconciles to the eval) beside `Predict` (`shrunk_cp`). FastAPI reports 422 as a *list* of error objects and everything else as a string, so the client coerces both (`detailText`) — a bare `body.detail` renders "[object Object]" on the malformed-FEN path, which is the one users hit most.
- API: `GET /api/dev/positions` (`scope` = all/unlabeled/labeled, `require` = any/volatility/findability, `limit`/`offset`), `POST /api/dev/label`, `GET /api/dev/stats`, `GET /api/dev/export` (the tuning input: FEN, best move, both scores, both signed deltas). Auth-gated and scoped to the caller's own reviews like the rest of `/api/*`. Engine-free tests in `tests/puzzles/test_dev_labels.py`.

## Volatility algorithm

`chess_vol/volatility.py:compute_volatility` is pure given an engine. The math (one-ply move-choice volatility, optional recursive reply volatility via `recurse_depth`, mate-to-cp mapping, eval-aware scaling, the `decided` flag, normalization constants) is fully specified in `docs/VOL_README.md` §3 with worked examples in §3.7 and tuning constants in `chess_vol/config.py`. Two correctness traps the tests pin down: `recurse_depth=0` must stay bit-identical to Phase 1 behavior, and centipawn conversions must use the **child board's** side-to-move at every recursion level. The JSON report schema is shared between CLI and server via `chess_vol/cli_report.py` (single source of truth).

**Analysis is per-position deterministic, and reviews are parallel.** `chess_vol/engine.py:Engine.analyse` starts every search from a cleared transposition table (a fresh `game` marker makes python-chess emit `ucinewgame`). Without that, a depth-limited search depends on which positions were analysed before it, so the same position scored differently depending on walk order — reviews were reproducible only by accident. Clearing costs ~12% serially and buys two things: a position always scores the same, and a game can be walked on several engines at once. `analyze_pgn(..., engines=[...])` spreads plies over an engine pool (every ply is an independent search); `server/reviews_api.py:review_engine_count` sizes it — half the cores, capped at 6, overridable with `CHESS_REVIEW_ENGINES` (`=1` walks serially). Measured on a 16-core box at depth 18 / MultiPV 6: **~3× wall-clock** for a normal review and **2.8× for deep mode** (`recurse_depth=2`), with output identical to the serial walk (`tests/vol/test_analyze.py::test_engine_pool_matches_the_serial_walk` pins that). Deep mode additionally memoizes transpositions inside one `compute_volatility` call (`core.volatility._MemoEngine`); it sits below the `analyses` counter, which still reports the recursion's logical budget.

## Layout quick reference

| Path | What |
|------|------|
| `server/` | Trainer backend: `main.py` (combined app), `db.py`, `engine.py`, `grading.py`, `selection.py`, `stats.py`, `modes.py` + `modes_api.py`, `playout.py`, `maia.py`, `replies.py`, `evalcheck.py`; accounts: `auth.py` + `auth_api.py`, `deps.py` (shared `get_connection` + `current_user`), `vol_games_api.py` (per-user saved games); "Your Mistakes": `mistakes.py` + `mistakes_run.py` + `mistakes_api.py`; Guess the Elo Duels: `guess_elo.py` + `guess_elo_api.py`; Daily check-in: `daily.py` + `daily_api.py` and `repertoire.py` + `repertoire_api.py`; Dev calibration labelling: `devlabels.py` + `devlabels_api.py`; Opening builder: `openings_build.py` + `openings_build_api.py` + `opening_cache.py` + `opening_prep.py` (ROI → what to prep) + `opening_import.py` (PGN → book) + `opening_tree_stats.py` (how you do at each node); Insights/reviews: `reviews.py` + `reviews_api.py`, `insights_api.py` + `insights_run.py` + `insights_metrics.py` (Tier 1–3) + `insights_pro.py` (headline/leaks/game facts) + `insights_narrative.py` (story layer) + `study_plan.py` (the plan) + `game_shape.py` (centre/endgame type per game), `position_cache.py`, `findability_features.py`, `tactic_tags.py`, `game_identity.py` |
| `pipeline/` | Offline puzzle data: `import_puzzles.py` (Lichess CSV → DB, also owns the `positions` schema), `mine_quiet.py` (PGN → quiet positions via Stockfish), `seed_demo.py`, `download_data.py`, `chesscom.py` / `lichess.py` (Insights ingest), `lichess_explorer.py` (opening explorer) |
| `chess_vol/` | Vol package: `volatility.py` (re-export shim → `core.volatility`), `engine.py`, `analyze.py`, `config.py`, `cli.py`, `server.py`, `calibrate.py`, `classify.py`, `explain.py`, `game_review.py` (expected-points review + opening/key-moments), `findability_review.py` (attaches findability), `calibrate_findability.py` (Phase 3 driver: DB puzzles → full/line calibration), `calibrate_piece_values.py` (eval-scale fit + piece-value validation) |
| `core/` | Shared, FastAPI-free primitives (Game Review 2.0): `volatility.py`, `evaluation.py`, `acceptable.py`, `features.py`, `findability.py`, `human.py`, `engine.py`, `cache.py`, `calibration.py`, `piece_features.py` + `piece_values.py` (contextual piece values), `repertoire.py` (openings mined from the player's own games; also `canonical_fen`/`position_key`), `opening_signals.py` + `opening_book.py` (the builder's three dots and candidate ranking), `endgame.py`, `constants/findability.json` + `constants/piece_values.json` + `constants/repertoire.json` + `constants/opening_book.json` + `constants/endgame.json` |
| `frontend/` | Single page: `index.html` + `shell.js`/`shell.css` (tab shell), `auth.js` (login/signup overlay gate), `app.js` (puzzles), `home/` (landing page — see below), `vol/` (vol UI; `vol/library.js` merges `/api/reviews` + `/api/vol/games`), `insights/` (launcher + Deep Dive dashboard; `postmortem.js`/`postmortem.css` are the narrative overlay; `cinema.js`/`cinema.css` are the generation Forge + the auto-playing film; `studyplan.js`/`studyplan.css` are the study plan), `daily/` (the check-in: calendar, phase list, floating clock HUD, repertoire drill), `elo/`, `eval/`, `dev/` (internal calibration labelling), `vendor/` (vol's vendored chessground bundle) |
| `tests/puzzles/`, `tests/vol/`, `tests/core/` | The three suites; `tests/vol/conftest.py` holds `FakeEngine` and fixtures; `tests/core/` covers the shared primitives + findability (engine-free, plus `@integration` real-engine capture tests) |
| `data/` | Runtime only (gitignored): `trainer.db`, Stockfish/lc0 binaries, Maia weights, raw downloads |

`positions` table schema is created by `pipeline/import_puzzles.py:ensure_positions_schema` (imported by `db.py`), while the app-side tables (`users`, `sessions`, `attempts`, `playouts`, `playout_sessions`, `vol_games`, `dev_labels`, `daily_sessions` + `daily_phase_runs`, `repertoire_attempts`, mode + mistakes + Insights tables) are in `server/db.py:APP_SCHEMA`. Columns added after a table's first creation go in `server/db.py:_migrate_add_columns` (idempotent `ALTER TABLE`), since the shipped `data/trainer.db` predates them — that's how `email`/`password_hash`/`password_salt`/`chesscom_username` / `insight_runs.source` reach the existing DB.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHESS_TRAINER_DB` | `data/trainer.db` | Trainer SQLite path |
| `STOCKFISH_PATH` | auto-detect | Stockfish for vol analysis |
| `CHESS_TRAINER_LC0` | `lc0` (or `data/lc0.exe`) | lc0 binary for Maia playouts |
| `CHESS_TRAINER_MAIA_WEIGHTS_DIR` | `data/maia_weights` | Maia weight files |
| `CHESS_TRAINER_MAIA_NODES` | `800` | lc0 node budget |
| `CHESS_REVIEW_ENGINES` | `min(6, cores/2)` | Stockfish processes one review walks a game on (`1` = serial) |

## Known gaps (inherited from source repos)

- Sound files ship in `frontend/sounds/` (`Move`/`Capture`/`Check`/`GenericNotify`, `.mp3` + `.ogg`); both the trainer (`frontend/app.js`) and the vol tab (`frontend/vol/audio.js`) load from `/static/sounds/`. They are Lichess's **`sfx`** theme (`lichess-org/lila:public/sound/sfx/`) — the crisp wooden set, the closest freely-licensed match to chess.com's. Not the `standard` theme: its `Check.mp3` is a symlink to `Silence.mp3`, which is why ChessMax originally shipped a synthesized check cue. Chess.com's own audio is proprietary and is deliberately not vendored.
- Stockfish / lc0 binaries and Maia weights are user-provided; nothing in the repo downloads them automatically.
