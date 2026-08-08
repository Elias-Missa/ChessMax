# Game Review 2.0 — Implementation Spec

**Target repo:** `Chess-Volatility-Bar`
**Stack:** Python / FastAPI / SQLite, Chessground frontend
**Status:** Phase 0 done · Phase 1 pre-existing (expected-points review) · Phase 2 done (findability pipeline + wiring + JSON + review UI panel) · Phase 3 driver built + run on 4.29M-puzzle DB — metric does NOT validate (|r|≈0.20 vs 0.7 bar; see notes) · Phase 4 mechanism done, mapping unfit (disabled by default) · Phase 5 backend + endpoint done; interactive play-session UI remaining · chess.com parity: opening name + key moments added to the review summary (estimated Elo/accuracy were already present)

### Implementation notes (what's built)

- **Phase 0** — `core/` package created with no FastAPI imports; `volatility.py` **moved** into `core/` (`chess_vol/volatility.py` is now a re-export shim). `core.acceptable` is the single tau site; the review consumes it. The trainer's cp-based `server/evalcheck.py` is a *different* rule and is left unmigrated on purpose (that's the Puzzles 2.0 semantic redesign, not a mechanical move).
- **Phase 2** — `core.findability.score_position` implements the full curve pipeline (calc-reweight → PAVA → `R_find = C_A^-1(0.5)` → score/band, personal score, alternate move). Constants live in `core/constants/findability.json` (`FindabilityConstants.load()`). Wired opt-in and null-safe via `chess_vol/findability_review.py` and the `chess_vol.server.POLICY_FACTORY` seam; `findability` rides the per-ply JSON (`null` when gated, `0` = engine-only). `core.human.MaiaPolicy` is a **documented approximation** over the repo's per-rating nets pending a rating-conditioned Maia-3/2 policy head.
- **Phase 3** — `core.calibration` provides puzzle parsing (with the FEN-convention trap pinned by a test), filtering, stratified sampling, and metrics (Pearson r, Brier, intercept fit). **Driver built + run:** `chess_vol/calibrate_findability.py` reads the 4.29M Lichess puzzles already in `data/trainer.db` (solver-position form — do NOT re-run `solver_position`), runs the full model (Stockfish MultiPV + Maia policy-head over the grid → `R_find`), and correlates with puzzle rating. **Result (n=315): the metric does not validate — |r|≈0.20, censoring-free (`mean_C_A`, `top_C_A`) too, vs the 0.7 bar / §4.3 redesign line.** Cause: the shipped club-level Maia per-rating nets don't assign high probability to tactical best-moves and barely discriminate across nets; puzzle rating also reflects whole-line difficulty, not move-1 policy. Constants must NOT be "fit"/enabled as trustworthy. Levers: intended Maia-2/3 rating-conditioned **policy head** (most promising), multi-move line scoring, or keep findability experimental. Also: `core.human.MaiaPolicy` softmaxes lc0's **value** head (r≈0.04, noise) — the policy head (`VerboseMoveStats` `P:`) is the correct readout and should replace it regardless.
- **Phase 4** — `core.acceptable.tau_for` is the single site and now implements `tau = f(volatility)` (linear between `tau_min`/`tau_max`), routed from `score_position(volatility=...)`. Disabled by default (`tau_volatility.enabled=false` in the JSON) so Phases 0-3 stay bit-identical; the mapping constants are placeholders until fit in Phase 3.
- **Phase 5** — `POST /api/vol/play/maia-move` (`server/main.py`, unauthenticated, stateless) plays one Maia reply from an arbitrary FEN via the `playout_move_fn` seam. The review shows a read-only findability panel (`ChessReviewUI.renderFindability`); the interactive "play a whole game from here" client loop is the remaining frontend work.
- Tests: `tests/core/` (engine-free) plus `@integration` real-engine capture tests, the Phase 5 endpoint, and the SSE findability seam; full suite green (446).

---

## 0. What we're building and why

A chess.com-style game review, plus three things chess.com doesn't have:

1. **Findability score** — for every move, how humanly possible was the engine's best move to find?
2. **Best findable move** — when findability is low, show the strongest move a human could realistically have played instead.
3. **Volatility bar + graph** — existing feature, wired into the review timeline.

The problem being solved: engine review shows you a move no human would ever find, which is frustrating and pedagogically useless. Findability tells you whether the miss was reasonable, and shows you a move you could actually have played.

### Build order


| Phase | Deliverable                                             | Blocking?                       |
| ----- | ------------------------------------------------------- | ------------------------------- |
| 0     | Extract shared `core/` package                          | Yes — do first                  |
| 1     | Game review tab (classification, eval graph, move list) | No                              |
| 2     | Findability scoring pipeline                            | Needs 0                         |
| 3     | Calibration harness (Lichess puzzles)                   | Needs 2                         |
| 4     | Volatility integration (`tau = f(volatility)`)          | Needs 2, 3                      |
| 5     | "Play from this position vs Maia"                       | Independent — cheap, do anytime |


---



## 1. Phase 0 — `core/` extraction

Puzzles 2.0 and Game Review will both need the same primitives. If they're duplicated they will drift, and Phase 4 (`tau = f(volatility)`) requires them unified.

Create `core/` as an internal package with **no FastAPI or route imports**:

```
core/
  engine.py        # Stockfish wrapper, MultiPV, iterative-deepening PV capture
  evaluation.py    # cp <-> WDL <-> win% conversion, delta_w
  volatility.py    # MOVED from existing location, not copied
  acceptable.py    # the acceptable-move set A
  human.py         # Maia policy wrapper (rating-conditioned)
  features.py      # per-move feature extraction for findability
  findability.py   # the score itself
  cache.py         # Zobrist-keyed SQLite cache
```

**Critical:** Puzzles 2.0's "any move that doesn't tank the eval" rule and the review's acceptable set are the same concept. Both must call `core.acceptable.acceptable_set()`. Refactor Puzzles 2.0 to use it as part of this phase — do not leave a second implementation.

### `core/evaluation.py`

Never compare centipawns directly. 50cp at eval 0.0 is decisive; 50cp at +7 is noise. Use Stockfish's `UCI_ShowWDL`:

```python
def win_prob(wdl: tuple[int, int, int]) -> float:
    """WDL in per-mille from side-to-move POV -> expected score in [0, 1]."""
    w, d, l = wdl
    return (w + 0.5 * d) / 1000.0

def delta_w(best_wdl, move_wdl) -> float:
    """Win-percentage points lost. Always >= 0."""
    return 100.0 * (win_prob(best_wdl) - win_prob(move_wdl))
```



### `core/acceptable.py`

```python
def acceptable_set(move_evals: dict[Move, WDL], tau: float) -> set[Move]:
    """Moves whose win% loss vs the best move is <= tau."""
```

`tau` is a constant (2.5) in Phases 0–3, and becomes `f(volatility)` in Phase 4. Route every call through one function so Phase 4 is a single-site change.

---



## 2. Phase 1 — Game review tab

Standard review: eval graph, move classification, accuracy, move list with navigation.

**Do not vendor an existing JS/TS clone.** Reference implementations (`SelimWaly/game-review`, `wdeloo/Brilliant-Chess`, `vietan0/chess-game-review`) are Node/TypeScript and won't integrate with FastAPI. Port the classification *thresholds* — that logic is a few hundred lines of centipawn-delta rules. Check licenses before copying anything; AGPL would constrain later commercial use.

Classification thresholds in win% loss, not centipawns:


| Label      | Condition                 |
| ---------- | ------------------------- |
| Best       | played == engine top move |
| Excellent  | delta_w <= 2              |
| Good       | delta_w <= 5              |
| Inaccuracy | delta_w <= 10             |
| Mistake    | delta_w <= 20             |
| Blunder    | delta_w > 20              |
| Book       | in opening book           |
| Great      | see below                 |
| Brilliant  | see below                 |


Great and Brilliant use the standard heuristic definitions, same as chess.com and the open-source clones. Findability is a **separate, additive metric** — it does not replace or gate these labels. A move can be Brilliant and also have a findability score of 12; those are two different facts about it and both get displayed.

**Brilliant (!!)** — all of:

- move is in the acceptable set (`delta_w <= tau`)
- move sacrifices material: static exchange evaluation on the destination square is negative, or a piece is left hanging
- the sacrifice is not trivially recovered (not a forced recapture sequence that nets even)
- position was not already winning before the move (`win_prob < 0.97`) and is not lost after
- more than one legal move

**Great (!)** — all of:

- move is the engine top move
- it is substantially the only good move: `delta_w(second_best) > 15`
- not a forced recapture, more than one legal move
- Brilliant did not already apply

---



## 3. Phase 2 — Findability



### 3.1 The core object

Findability is not a number, it is a **curve** over rating. Everything else is a functional of that curve.

For a position, evaluate the rating-conditioned human model at `r ∈ {800, 1100, 1400, 1700, 2000, 2300, 2600}`, producing two curves:

- `C_star(r)` = probability a player rated r plays the engine move `m*`
- `C_A(r)` = probability a player rated r plays **any** move in the acceptable set A

`C_star` drives the alternate-move recommendation. `C_A` **is the headline score** — the user's complaint is about whether a *good enough* decision was findable, not about engine move-matching.

### 3.2 Human model

Use **Maia-3** (CSSLab; recommended over Maia-2 for new projects) or **Maia-2** as a fallback. Both are rating-conditioned single models.

Do **not** use the nine original per-rating Maia nets, and do **not** implement the "find the Maia level where the recommendation flips" approach. The Maia-2 paper explicitly documents that independent per-rating models are incoherent — they predict a player handles a position correctly at one level, blunders at the next, and recovers at the next. The flip point is therefore often not well-defined. The full policy distribution from a rating-conditioned model is strictly more information and is better behaved.

```python
def policy(fen: str, rating: int, moves: list[Move]) -> dict[Move, float]:
    """P(move | position, rating). Returns probs for the requested moves only."""
```



### 3.3 Pipeline

**Step 1 — Gate.** Skip scoring entirely if:

- only one legal move (findability = 100, label "Forced")
- position is in the opening book
- both `win_prob(m*)` and `win_prob(second_best)` are > 0.97 or < 0.03 — no real decision exists

**Step 2 — Engine pass.** Stockfish `MultiPV=8`, **fixed node count, not fixed depth** (depth is not reproducible across hardware; nodes are). Capture the root PV at *every* iterative-deepening iteration — this gives `d_star(m)` for free with no extra searches.

**Step 3 — Win%.** `delta_w(m)` for every move in the MultiPV set.

**Step 4 — Acceptable set.** `A = acceptable_set(evals, tau)`, tau = 2.5.

**Step 5 — Calculation reweighting.**

Maia is pure pattern recognition with no search. It will call a forced 6-move mate "unfindable" when a human would simply calculate it. This is the single largest false-positive source and must be corrected.

Humans find moves two ways — recognition and calculation — so reweight the Maia prior by search-visibility:

```
dep(m)  = clamp(1 - (d_star(m) - 1) / 14, 0, 1)
forc(m) = fraction of first 4 PV plies that are checks, captures,
          promotions, or forced recaptures
narr(m) = 1 / (1 + mean count of opponent replies within tau,
          over the first 3 opponent nodes of the PV)
q(m)    = 1 if a QUIET move at PV ply >= 3 is uniquely winning at its
          node (delta_w to second best there > 3*tau), else 0

calc(m) = (0.45*dep + 0.35*forc + 0.20*narr) * (1 - 0.6*q)
```

`q` is the "no human finds this" detector — a non-forcing move buried deep in a line.

```
pi_tilde_r(m)  proportional to  pi_r(m) * exp(beta * calc(m))
```

with `beta = 1.2`. Renormalize over the MultiPV set with the remaining probability mass lumped into a tail bucket.

**All nine constants above are placeholders.** They get fit in Phase 3. Define them in one `FindabilityConstants` dataclass loaded from a JSON file so refitting never requires a code change.

**Step 6 — Curve.** Build `C_star(r)` and `C_A(r)` over the seven rating points. Run **PAVA isotonic regression** on each to enforce monotonicity — real players improve monotonically, and this guarantees we don't inherit local zigzag from the model.

**Step 7 — Scores.**

*Overall score (rating-free, time-free).* Invert the curve rather than averaging it:

```
R_find = C_A^-1(0.5)      # monotone interpolation between sampled points
findability = 100 * clamp(1 - (R_find - 600) / 2000, 0, 1)
```

Rationale: a population-weighted average compresses everything difficult into 0–10 and all sharp moves look identical. Rating-inversion is linear in a quantity chess players already have intuition for, and the label writes itself: *"Findability 34 · around 1900 strength."*

If `C_A` never crosses 0.5 within [600, 2600], clamp to 0 and label "Engine-only". Do not extrapolate beyond the model's trained range.

*Personal score.* `C_star(R_user)` and `C_A(R_user)` directly, times the time modifier:

```
t_mod = clamp(log(t_spent / t_expected + 1), 0.5, 1.3)
```

where `t_expected` scales with `|A|` and PV depth. Only applies when the PGN has clock data.

**Step 8 — Alternate move.** Recommend a different move iff:

```
C_star(R_user) < 0.15
AND exists m in A with pi_tilde(m) >= max(0.20, 3 * C_star(R_user))
```

Recommend `argmax pi_tilde(m)` over A.

### 3.4 Display bands


| Score  | Label         |
| ------ | ------------- |
| 90–100 | Obvious       |
| 70–89  | Natural       |
| 45–69  | Needs thought |
| 20–44  | Hard          |
| 0–19   | Engine-only   |




### 3.5 Cost and caching

MultiPV 8 at fixed nodes across ~70 positions is the bottleneck; Maia forward passes are cheap. Cache keyed on Zobrist hash in SQLite. Cache the **feature vector**, not just the score — Phase 3 refits constants repeatedly and must not re-run the engine each time.

Cached per position: `d_star` per move, `forc`, `narr`, `q`, `delta_w`, and `pi_r` for all seven rating bands.

---



## 4. Phase 3 — Calibration

Without this, the score is a plausible-looking number with no evidence behind it. A confidently wrong findability score destroys user trust permanently — if it reads 35 on a move every club player would call obvious, nobody believes any of it again.

### 4.1 Ground truth

The **Lichess puzzle database** (`lichess_db_puzzle.csv.zst`) is a direct empirical measurement of the thing we're computing. Each puzzle carries a Glicko rating derived from how often players at each rating actually solve it — that *is* `R_find`.

**FEN convention trap:** the FEN column is the position *before* the opponent's setup move. The first entry in `Moves` is the opponent's move; the solver moves second. Getting this backwards silently poisons the entire dataset. Assert this in a test.

**Filtering:**

- `NbPlays > 1000` (low rating deviation)
- drop `mateIn1`
- solution length <= 3 plies
- stratified sample across rating bands so the 1500 mass doesn't dominate

**Known bias:** puzzle ratings overstate findability because the solver knows a win exists. In a real game you don't. Model this as an intercept in the `R_find -> puzzle_rating` regression. Do not assume identity.

### 4.2 The quiet-position problem

Lichess puzzles are selected for having one clear forcing win, so quiet positions are essentially absent. Fitting on puzzles alone will overfit `forc` and `dep` — precisely the terms that only matter in the tactical regime.

**Puzzles 2.0 already solves this.** Its quiet-position miner produces the missing regime, and its format — decide whether a tactic exists, and if not play a move that doesn't tank the eval — removes the "you know a win exists" bias that inflates Lichess puzzle ratings. That makes it closer to real-game conditions than Lichess puzzles are.

For the calibration holdout specifically, mine a **second** quiet set sampled uniformly at random from real games. The existing miner's selection criterion will bias the fit toward positions matching that criterion.

### 4.3 Procedure

1. Extract and cache features for ~50k puzzles + the quiet holdout
2. Fit the nine constants with Optuna or Nelder-Mead, minimizing error on `predicted R_find` vs `published puzzle rating`
3. Report Pearson r on a held-out split, and Brier score for the `C_A` probability calibration
4. Reliability diagram per rating band

**Run this baseline first:** how well does raw `pi_1500(m*)` *alone* predict puzzle rating, with no calc reweighting at all? If the full pipeline doesn't clearly beat that number, the reweighting isn't earning its complexity and should be cut. This is an hour of work and it tells you whether the design is right before any UI exists.

**Success bar:** r > 0.7 on the tactical set means the metric is defensible. r ~ 0.3 means stop and redesign.

**Leakage:** never condition on the move actually played when scoring. The review is post-hoc and it would be trivially easy to leak this.

---



## 5. Phase 4 — Volatility integration

`tau` should not be constant. In a sharp tactical position 2.5 win-% is a rounding error; in a quiet maneuvering position it's the whole game. The volatility bar already measures exactly this.

Replace the constant with `tau = f(volatility)` in `core/acceptable.py`, fit the mapping in the Phase 3 harness, and re-run calibration to confirm it improves r.

Also parked for later: **volatility x (1 - findability)** as a composite "practical danger" metric — how much eval was at stake, weighted by how likely the player was to miss the path. Nothing else on the market has this. Not in scope for this spec.

---



## 6. Phase 5 — Play from this position vs Maia

Independent of everything above and cheap to build. Add a button on any review position that starts a game from that FEN against Maia at the user's rating. Highest value-per-line-of-code in the whole project — build it whenever it's convenient.

---



## 7. API contract

```
POST /api/review
  body: { pgn: str, user_rating: int | null }
  -> { review_id: str }          # async, poll for status

GET /api/review/{review_id}
  -> {
       status: "pending" | "complete" | "error",
       progress: float,
       moves: [ {
         ply: int,
         san: str,
         classification: str,
         win_prob: float,
         delta_w: float,
         volatility: float,
         findability: {
           score: int,              # 0-100, rating-free
           r_find: int | null,      # null if engine-only
           band: str,
           personal: float | null,  # null if no user_rating
           curve: [[int, float]]    # (rating, C_A) pairs, for the graph
         } | null,                  # null when gated out
         alternate: { san: str, delta_w: float, pi: float } | null
       } ]
     }
```

Findability is `null`, not `0`, when the gate in Step 1 skips the position. Zero means "engine-only" and must not be confused with "not scored".

---



## 8. Acceptance criteria

**Phase 0:** Puzzles 2.0 and review both import `core.acceptable`. Zero duplicated volatility code. `core/` has no FastAPI imports.

**Phase 1:** A PGN produces a full review with eval graph and the full classification set including Great and Brilliant. Findability is not required for any label to work — Phase 1 must be complete and shippable on its own.

**Phase 2:** Every non-gated move has a findability score. Same position scores identically across runs (fixed nodes, not depth). Forced moves score 100. Curves are monotone after PAVA.

**Phase 3:** Pearson r > 0.7 on held-out Lichess puzzles. Full pipeline beats the `pi_1500` baseline. Constants live in JSON, not code.

**Phase 4:** `tau = f(volatility)` improves calibration r versus constant tau, measured on the same holdout.