# Persistence, Insights & Navigation — Implementation Spec

---

## 0. Scope and build order

Three changes, in this order. The order matters — each one constrains the next.


| Phase | Deliverable               | Why this order                                                  |
| ----- | ------------------------- | --------------------------------------------------------------- |
| A     | Tab restructure to 5 tabs | Pure routing. Doing it later means moving the same files twice. |
| B     | Game review persistence   | Insights aggregates over this data. Schema must exist first.    |
| C     | Insights page             | Reads from B's schema.                                          |


**The key coupling:** the Insights page is aggregation over stored review data. If reviews are persisted as opaque JSON blobs, every insight becomes a full table scan plus reparse. Phase B's schema decides what Phase C can do.

---

## Phase A — Navigation restructure

### Target structure

Five top-level tabs:

1. **Puzzles** — primary/default tab
2. **Training** — container for the smaller modes
3. **Game Review**
4. **Insights**
5. **Guess the Elo**

Everything currently living as its own top-level tab that isn't in that list moves under **Training**: Guess the Evaluation, Defense, and the other small modes.

### Requirements

- Training sub-modes must be **real nested routes** (`/training/guess-eval`, `/training/defense`), not dropdown component state. Reasons: deep links survive, browser back works, and Insights needs to link directly into a specific training mode ("you miss deflections → practice deflections").
- Default route is `/puzzles`.
- Preserve existing URLs with redirects if any are already shared.
- No logic changes in this phase. Routing and layout only.

---

## Phase B — Game review persistence

### Goal

A user reviews a game, closes the tab, returns a week later, and the review is there instantly with no re-analysis.

### B.1 Two cache layers

These are separate and both are needed.

**Position cache** — keyed by Zobrist hash, **shared across all users**. Engine and Maia output for a position does not depend on who is looking at it. Common openings hit constantly; this is where most of the compute savings live.

**Review cache** — keyed by game, per user. Assembles cached positions into a review with user-specific fields (personal findability at their rating, time-adjusted scores).

### B.2 Game identity

- chess.com games: use the chess.com game ID / URL
- lichess games: lichess game ID
- pasted PGN: SHA-256 of the normalized SAN move list (strip comments, clocks, annotations)

Two users reviewing the same game must resolve to the same `game_id` so they share position cache entries.

### B.3 Store features, not just scores

**This is the most important decision in Phase B.**

The findability constants get refit during calibration, and probably again later. If only final scores are persisted, every stored review is dead the moment constants change. If the underlying feature vectors are persisted, recomputation is pure arithmetic with zero engine work.

Every cached position row stores: `d_star` per move, `forc`, `narr`, `q`, `delta_w`, `pi_r` across all seven rating bands, plus the raw MultiPV list and PVs.

Stamp every row with `engine_version`, `maia_version`, `constants_version`, `nodes`. On read, if `constants_version` is stale, recompute scores from cached features and write back. Only a change to `engine_version`, `maia_version`, or `nodes` requires re-running the engine.

### B.4 Schema

Normalized for the fields Insights aggregates over; JSON blob for bulky detail nothing aggregates over.

```sql
CREATE TABLE games (
  game_id        TEXT PRIMARY KEY,      -- see B.2
  source         TEXT NOT NULL,         -- 'chesscom' | 'lichess' | 'pgn'
  pgn            TEXT NOT NULL,
  white_name     TEXT,
  black_name     TEXT,
  white_rating   INTEGER,
  black_rating   INTEGER,
  result         TEXT,                  -- '1-0' | '0-1' | '1/2-1/2'
  time_class     TEXT,                  -- 'bullet' | 'blitz' | 'rapid' | 'daily'
  time_control   TEXT,                  -- raw, e.g. '600+5'
  eco            TEXT,
  opening_name   TEXT,
  played_at      TIMESTAMP,
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE reviews (
  review_id          TEXT PRIMARY KEY,
  user_id            TEXT NOT NULL,
  game_id            TEXT NOT NULL REFERENCES games(game_id),
  user_color         TEXT NOT NULL,     -- 'white' | 'black'
  user_rating        INTEGER,           -- rating used for personal findability
  depth_tier         TEXT NOT NULL,     -- 'shallow' | 'full'  (see C.3)
  status             TEXT NOT NULL,     -- 'pending' | 'complete' | 'error'
  progress           REAL DEFAULT 0,
  engine_version     TEXT,
  maia_version       TEXT,
  constants_version  TEXT,
  nodes              INTEGER,
  accuracy           REAL,
  fixable_loss       REAL,              -- see C.4
  total_loss         REAL,
  loss_type          TEXT,              -- see C.4 loss taxonomy
  created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, game_id, depth_tier)
);

CREATE TABLE review_moves (
  review_id      TEXT NOT NULL REFERENCES reviews(review_id),
  ply            INTEGER NOT NULL,
  san            TEXT NOT NULL,
  is_user_move   BOOLEAN NOT NULL,
  phase          TEXT NOT NULL,         -- 'opening' | 'middlegame' | 'endgame'
  is_book        BOOLEAN DEFAULT 0,
  classification TEXT,                  -- best/excellent/good/inaccuracy/mistake/blunder/great/brilliant
  win_prob       REAL,
  delta_w        REAL,
  volatility     REAL,
  findability    INTEGER,               -- 0-100, NULL when gated out
  findability_personal REAL,            -- NULL when no user_rating
  r_find         INTEGER,
  time_spent     REAL,                  -- seconds, NULL if no clock data
  clock_remaining REAL,
  tactic_tags    TEXT,                  -- JSON array, see C.4
  detail         TEXT,                  -- JSON: PV, curve points, MultiPV list, alternate move
  PRIMARY KEY (review_id, ply)
);

CREATE INDEX idx_review_moves_agg
  ON review_moves(review_id, is_user_move, phase, classification);

CREATE TABLE position_cache (
  zobrist            TEXT PRIMARY KEY,
  fen                TEXT NOT NULL,
  engine_version     TEXT NOT NULL,
  maia_version       TEXT NOT NULL,
  nodes              INTEGER NOT NULL,
  features           TEXT NOT NULL,     -- JSON: full feature vector, see B.3
  created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

```

`findability` is `NULL` when the Step 1 gate skips the position. `0` means "engine-only". These must never be conflated.

### B.5 Async behaviour

- `POST /api/review` returns immediately with a `review_id`
- Client polls `GET /api/review/{review_id}` for `status` and `progress`
- **Resumable**: if the user closes the tab mid-analysis, the job continues server-side and completes. Returning to that game shows the finished review.
- Re-requesting a review that already exists with a matching version stamp returns the cached one immediately.

### B.6 Review list

`GET /api/reviews?limit=&offset=` returns the user's saved reviews, newest first, with enough summary for a list view: opponent, result, date, accuracy, `fixable_loss`, `loss_type`, thumbnail eval sparkline.

---

## Phase C — Insights

### C.1 Flow

1. User enters a chess.com username (**entered each time — not stored on the account**)
2. Picks a window: **last 7 days** or **last 30 days**
3. Picks a time control: bullet / blitz / rapid / daily
4. App pulls matching games, analyzes them, computes insights, **saves the run**
5. User can return anytime to view the saved run, or hit **Refresh** to generate a new one

Because the username is a parameter rather than a profile field, a single account can hold insight runs for multiple chess.com handles. Store the handle on the run record.

### C.2 Ingestion

Chess.com's public API needs no auth:

```
GET https://api.chess.com/pub/player/{username}/games/{YYYY}/{MM}

```

- Monthly archives. A 7- or 30-day window spans one or two archives.
- A descriptive `User-Agent` header is required or requests get blocked.
- Request archives serially, not in parallel — the API rate-limits aggressive clients.
- Filter on the `time_class` field for the time control selection.
- Each game arrives with full PGN including clock annotations, both ratings, result, and time control.
- Handle gracefully: username not found, private account, zero games in window.

### C.3 Tiered analysis — required, not optional

30 days of blitz for an active player can be 500+ games. Running the full findability pipeline (MultiPV 8 + seven Maia evaluations per position) across that is not viable.

**Shallow tier** — used for all games in an insights run:

- Stockfish MultiPV 3, low fixed nodes
- No Maia, no findability, no calc features
- Produces: `win_prob`, `delta_w`, classification, volatility, phase, time data
- Sufficient for the large majority of insights

**Full tier** — findability pipeline, run only when:

- the user opens that specific game in Game Review, or
- the game is flagged as a highlight by the insights run (see C.5)

Both tiers write to `reviews` with `depth_tier` set. A game can hold both rows. Any insight requiring findability must query `depth_tier = 'full'` and state its sample size in the UI.

**Cap:** most recent 300 games in window. Show the cap in the UI when hit.

### C.4 Metric catalogue

Metrics marked **[F]** require the full tier.

#### Tier 1 — the differentiated ones. Build these first.

**Fixable loss [F]** — sum of `delta_w` over user moves where `findability > 60`. This is realistic improvement headroom: points actually recoverable by a human at their level. Display alongside total loss: *"You dropped 340 win% points; 90 were realistically fixable."* Standard accuracy penalizes players for missing moves no human finds. This does not. Nothing else on the market computes this.

**Loss taxonomy** — classify every loss by eval trajectory shape:


| Type                | Condition                                                                    |
| ------------------- | ---------------------------------------------------------------------------- |
| Cliff               | a single user move with `delta_w > 25` from a non-losing position            |
| Bleed               | no single drop > 15, but cumulative `delta_w` in the top quartile            |
| Never in it         | `win_prob < 0.35` by move 15                                                 |
| Converted then lost | reached `win_prob > 0.8` and lost                                            |
| Scramble            | >50% of total `delta_w` occurred with `clock_remaining` in the bottom decile |


Precedence: Converted-then-lost > Cliff > Scramble > Never-in-it > Bleed. One stacked bar chart answers "do I blunder or get slowly outplayed."

**Time allocation vs. criticality** — correlate `time_spent` against `volatility` and (where available) findability. Surfaces things like *"you average 38s on forced recaptures and 6s on your highest-volatility moves."* Uniquely possible here because both signals exist per move.

**Volatility profile** — win rate bucketed by mean game volatility. Then go further: **volatility steering** — compare the volatility of positions the user *chose* against alternatives that were available. Distinguishes "avoids sharp positions they're good at" from "seeks sharp positions that are losing them games."

**Missed tactic taxonomy × findability [F]** — tag missed tactics by pattern from the PV shape: fork, pin, skewer, deflection, discovered attack, back rank, overloaded defender, zwischenzug. Cross with findability: *"you miss 60% of deflections, and most were findability > 70."*

#### Tier 2 — chess.com parity, done better

**Phase attribution by eval swing** — sum `delta_w` per phase normalized by move count in that phase. Win rate by phase is confounded by position quality entering the phase; attribution is not.

**Repertoire depth** — where the user leaves book, and mean `delta_w` over the 5 moves following. Separates "bad opening choice" from "fine opening, doesn't know the resulting plans."

**Conversion vs. comeback** — win rate from positions with `win_prob > 0.7` (conversion) and from `win_prob < 0.3` (comeback). The measurable version of aggressive-vs-defensive.

**Castling analysis** — win rate by castling side, by opposite-side vs same-side castling, and by never castling.

**Opponent-relative performance** — split by opponent rating band, cross-tabbed with phase attribution. Common pattern: crushes lower-rated tactically, collapses against higher-rated in endgames.

**Missed wins** — count of positions reaching `win_prob > 0.85` in games not won, with the specific move where it slipped.

**Standard set** — win rate by color, by opening (ECO), accuracy over time, results by time of day, game length distribution.

#### Tier 3 — behavioural

**Tilt** — performance by game index within a session (sessions split by >2h gap), and performance in the game immediately following a loss.

**Time-of-day and session-length effects** on accuracy and fixable loss.

### C.5 The Insights → Puzzles loop

**This is the feature that makes the product cohere rather than being a collection of tools.**

An insights run flags its most instructive positions — user moves with high `delta_w` and `findability > 60`, meaning the miss was both costly and learnable. Those positions generate a personalized Puzzles 2.0 set drawn from the user's own games.

- Filter to `findability > 60` so the set is actually solvable, not engine-move trivia
- Reuse the existing Puzzles 2.0 format, including the "is there even a tactic here?" decision step
- Every Tier 1 insight gets a "Practice this" button that deep-links into the relevant training mode (this is why Phase A requires real nested routes)

Flagged positions also queue for full-tier analysis, so findability is available when the user opens them.

### C.6 Storage and refresh

```sql
CREATE TABLE insight_runs (
  run_id          TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,
  chesscom_handle TEXT NOT NULL,
  window_days     INTEGER NOT NULL,     -- 7 | 30
  time_class      TEXT NOT NULL,
  games_analyzed  INTEGER,
  games_capped    BOOLEAN DEFAULT 0,
  status          TEXT NOT NULL,
  progress        REAL DEFAULT 0,
  metrics         TEXT,                 -- JSON: computed metric payload
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE insight_run_games (
  run_id   TEXT NOT NULL REFERENCES insight_runs(run_id),
  game_id  TEXT NOT NULL REFERENCES games(game_id),
  PRIMARY KEY (run_id, game_id)
);

```

**Runs are immutable snapshots.** Refresh creates a new run rather than mutating the old one. Keep the last 10 runs per user.

Two payoffs from immutability:

- **Incremental refresh** — a new 30-day run overlaps heavily with the previous one. Only analyze games not already in `games`. Most of a refresh should be free.
- **Trend across runs** — comparing consecutive runs gives progress over time for free. *"Your fixable loss in endgames dropped 22% versus last month."* Worth surfacing once a user has 2+ runs.

### C.7 API

```
POST /api/insights
  body: { chesscom_handle: str, window_days: 7 | 30, time_class: str }
  -> { run_id: str }

GET /api/insights/{run_id}
  -> { status, progress, games_analyzed, games_capped, metrics: {...} }

GET /api/insights?handle=&window_days=&time_class=
  -> list of prior runs, newest first

POST /api/insights/{run_id}/refresh
  -> { run_id: str }   # new run, incremental

```

---

## Open decisions

1. **Does a shallow-tier review satisfy a Game Review request?** Recommendation: no — opening a game in Game Review always triggers full tier, with the shallow data shown immediately as a placeholder while it upgrades.
2. **Rating source for personal findability.** Chess.com rating from the game itself is the obvious choice, but it varies by time control. Recommendation: use the user's rating in that specific game.
3. **Retention on** `position_cache`**.** It grows without bound. Recommendation: no eviction initially, revisit at 10GB.
4. **Anonymous users.** Do reviews and insights require an account? If reviews are stored per user, they must. Confirm whether anonymous review is still allowed and simply not persisted.

---

## Acceptance criteria

**Phase A:** Five top-level tabs. Training sub-modes reachable at their own URLs. Back button and deep links work. No behaviour changes.

**Phase B:** Reviewing a game twice runs the engine once. Closing the tab mid-analysis does not lose the job. Changing `constants_version` updates stored scores without re-running the engine. Two users reviewing the same game share position cache rows.

**Phase C:** Entering a handle with a window and time control produces a saved run. Returning shows it without recomputation. Refresh only analyzes games not already cached. Every findability-dependent metric displays its full-tier sample size.