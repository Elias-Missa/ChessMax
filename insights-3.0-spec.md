# Insights 3.0 — Evidence, Rigour & New Measurement

**Target repo:** `Chess-Volatility-Bar`
**Builds on:** the shipped Insights feature (`server/insights_run.py`, `insights_metrics.py`, `insights_pro.py`, `frontend/insights/`)
**Companion docs:** `INSIGHTS_REPORT.md` (what exists today), `game-review-2.0-spec.md` (findability pipeline)

This document is additive. Nothing here removes an existing card. It changes how existing numbers are *computed and presented*, and adds new measurements on top.

---

## 0. Build order

Dependencies matter more than usual here — Phase 1 is a prerequisite for four later phases.

| Phase | Deliverable | Depends on | Why this position |
|---|---|---|---|
| 1 | **Evidence layer** | — | Unlocks phases 6, 7, 9, 10. Build first. |
| 2 | **Expectation adjustment** | — | Correctness fix to existing cards. |
| 3 | **Shrinkage + intervals** | 2 | Kills phantom leaks. |
| 4 | **Reference corpus** | 2 | Offline job. Highest leverage per unit of work. |
| 5 | **Cheap new measurements** | — | Opponent error harvesting, blunder splits. Independent. |
| 6 | **Recurrence tracking** | 1 | Strongest leak signal available. |
| 7 | **Counterfactual simulator** | 2 | Interactive version of "rating left on the board". |
| 8 | **Latent skill model (IRT)** | 4 | Needs findability coverage + reference data. |
| 9 | **Shapley leak attribution** | 3 | Makes leaks additive. |
| 10 | **Structure & geometry analysis** | 1 | New position classifiers. |
| 11 | **Psychological layer** | 5 | Extends existing time/tilt cards. |
| 12 | **Presentation & exports** | 1, 4 | Narrative diagnosis, coach memo, PGN. |

---

## Phase 1 — The evidence layer

### 1.1 The core change

**Every metric function must return a result object carrying its provenance, not a bare number.**

Today metrics collapse `review_moves` into a value and discard which moves produced it. Nothing downstream can show a user *why* a claim is true. This is the single highest-value change in the document, and four other phases depend on it.

```python
@dataclass
class Evidence:
    game_id: str
    ply: int
    delta_w: float
    findability: int | None
    volatility: float
    time_spent: float | None
    win_prob_before: float
    caption: str            # generated, see 1.4

@dataclass
class MetricResult:
    value: float
    n: int                          # support size — how many moves/games back this
    ci: tuple[float, float] | None  # populated in Phase 3
    exemplars: list[Evidence]       # top 5, see 1.3
    counter_exemplars: list[Evidence]  # up to 3, see 1.5
    query: dict                     # predicate that regenerates the full support set
```

`query` is a serialized filter predicate (phase, volatility range, clock range, classification, etc.) so the full support set can be regenerated on demand without storing hundreds of plies per metric.

### 1.2 Storage

Do **not** inflate `insight_runs.metrics` with full support sets. Store exemplars in their own table:

```sql
CREATE TABLE metric_evidence (
  run_id      TEXT NOT NULL REFERENCES insight_runs(run_id),
  metric_key  TEXT NOT NULL,   -- dotted path into the metrics blob
  kind        TEXT NOT NULL,   -- 'exemplar' | 'counter'
  rank        INTEGER NOT NULL,
  game_id     TEXT NOT NULL REFERENCES games(game_id),
  ply         INTEGER NOT NULL,
  score       REAL,            -- exemplar_score, see 1.3
  caption     TEXT,
  PRIMARY KEY (run_id, metric_key, kind, rank)
);

CREATE INDEX idx_metric_evidence_lookup ON metric_evidence(run_id, metric_key);
```

`metric_key` is the dotted path into the existing metrics payload — `pro.leaks.scramble`, `critical_moments.criticality_gap`, `tier2.castling.queenside`. This is the contract that lets the frontend attach evidence chips **generically**: one component resolves any metric key, so adding a new metric never requires new evidence UI.

### 1.3 Exemplar selection

The obvious pick — highest Δw — surfaces the most dramatic example, which is usually the *least* representative one. Score candidates on all three axes:

```
impact     = clamp(delta_w / 25, 0, 1)
typicality = 1 / (1 + mahalanobis_distance(move, centroid(support_set)))
recency    = exp(-days_ago / 14)

exemplar_score = impact * typicality * sqrt(recency)
```

Take the top 5 by `exemplar_score`, but enforce **at most 2 from any one game** so a single disastrous game doesn't fill the evidence panel.

The centroid is computed in the feature space relevant to that metric (Δw, volatility, findability, time_spent, phase one-hot, ply).

### 1.4 Captions

Generate from the row, not from a template bank. Format:

```
Move {move_number}{color_suffix} vs {opponent} ({opponent_rating}), {date}
— you played {san_played} with {clock}s left. {san_best} was {assessment}.
Findability {findability}.
```

Omit clauses whose data is missing (no clock data, no findability). `assessment` derives from the Δw band: "winning", "much better", "the accurate move".

### 1.5 Counter-examples

Every leak card must also surface up to 3 moves where the user handled that same situation **correctly** — same predicate, but `delta_w <= CRITICAL_HANDLED_DELTA_W`. Two reasons: the report currently reads as a prosecutor, and the ratio (5 failures against 40 successes) communicates severity more honestly than any percentage.

### 1.6 Frontend

- Evidence chips render **inline in the claim sentence**, not as a footnote or a separate panel.
- Clicking opens a board inspector: position **before** the move, red arrow for the move played, green arrow for the best move, caption below, and a jump into full Game Review at that ply.
- Animate the **2 plies of lead-in** before the position rather than showing it cold. The move before is usually what makes a blunder comprehensible.
- One generic `EvidenceChip(metric_key)` component. No per-metric UI code.

### 1.7 Two things this unlocks for free

**Evidence count doubles as sample size.** A leak backed by 3 moves visibly looks weaker than one backed by 60, with no statistical vocabulary required. This is the layperson-readable version of Phase 3.

**A dispute loop.** Put a "this doesn't match my experience" control on each leak, next to its evidence. A user disagreeing *while looking at the evidence* is a far cleaner calibration signal than raw outcome data, and it directly addresses the "leak thresholds are reasoned, not calibrated" limitation.

```sql
CREATE TABLE leak_disputes (
  user_id    TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  metric_key TEXT NOT NULL,
  agreed     BOOLEAN NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Phase 2 — Expectation adjustment (correctness fix)

**Most existing rates are confounded by opponent strength.** "Score by opening" is partly just who you happened to face. This affects the opening repertoire card, castling, endgame conversion, opponent-relative performance, and every leak whose impact is computed from a raw score.

The Score KPI already does this correctly. Make it universal.

```python
def expected_score(own_rating: int, opp_rating: int) -> float:
    return 1.0 / (1.0 + 10 ** ((opp_rating - own_rating) / 400.0))

def performance_gap(games) -> float:
    """Points above/below what the rating differences predicted."""
    return sum(g.points for g in games) - sum(
        expected_score(g.own_rating, g.opp_rating) for g in games
    )
```

**Rule:** no card reports a raw win rate or score without also reporting the gap against expectation. Where space is tight, report the gap *instead of* the raw rate — it is the more informative number.

Apply to: opening repertoire, castling, sharpness profile, session length, time of day, tilt/recovery, endgame entry splits, conversion & resilience.

### Recency weighting

A 30-day aggregate treats day 1 and day 30 equally. You are estimating *current* ability. Apply exponential decay in all aggregates:

```
w_i = exp(-days_ago_i / HALF_LIFE)     # HALF_LIFE = 14 days
```

Expose the half-life in the UI as a note, not a setting.

---

## Phase 3 — Shrinkage and intervals

### 3.1 The problem

Seven days of blitz can be 40 games. Points-per-game by castling side on 11 queenside games is noise, and the leak board will rank it. Users chase phantom leaks, don't improve, and stop trusting the tool.

### 3.2 Partial pooling

Shrink every bucket estimate toward the player's own global mean, in proportion to sample size. This is the principled replacement for hand-tuned minimum-sample thresholds.

```
k        = sigma2_within / sigma2_between      # fit per metric family across buckets
shrunk_i = (n_i / (n_i + k)) * observed_i + (k / (n_i + k)) * global_mean
```

`sigma2_between` is the variance of bucket means, `sigma2_within` the pooled within-bucket variance. Fit once per metric family (openings, phases, clock bands, castling) per run.

A 6-game opening barely moves off baseline. A 60-game opening speaks for itself. Phantom leaks disappear without a single threshold.

### 3.3 Intervals

- Proportions (win rates, blunder-free share, punish rate): **Wilson score interval**, not normal approximation — samples are small.
- Means (Δw/move, accuracy): bootstrap percentile interval, 1000 resamples.

Store as `MetricResult.ci`. Display as a range or an error bar, and gate leak eligibility on the interval excluding the null rather than on a raw threshold.

### 3.4 Sample-size guidance

The inverse is genuinely useful and nobody ships it:

> "You need roughly 35 more rapid games before your endgame numbers mean anything."

Compute from the width of the current interval and the target width. Show on any card whose `n` is below the display floor.

**Display floor:** a metric with `n < 8` renders greyed with its count, and is ineligible for the leak board entirely.

---

## Phase 4 — Reference corpus and percentiles

### 4.1 What to build

An **offline batch job** that runs the shallow-tier pipeline over Lichess database games, stratified by rating band (100-point buckets, 800–2400) and time control. Target ~2000 games per cell.

Store the resulting metric distributions:

```sql
CREATE TABLE reference_distribution (
  metric_key   TEXT NOT NULL,
  rating_band  INTEGER NOT NULL,   -- lower bound, e.g. 1500
  time_class   TEXT NOT NULL,
  n_games      INTEGER NOT NULL,
  p10 REAL, p25 REAL, p50 REAL, p75 REAL, p90 REAL,
  mean REAL, sd REAL,
  PRIMARY KEY (metric_key, rating_band, time_class)
);
```

This is a one-time cost that converts **every existing card** into a percentile. Highest leverage per unit of work in this document.

### 4.2 Percentiles everywhere

"Your endgame Δw/move is 0.9" means nothing. "Your endgame Δw/move is 0.9; the median 1500 is 0.62; you're in the 18th percentile" is a diagnosis. Every metric card gains a percentile badge against the user's own rating band and time control.

### 4.3 The rating-implied profile

The headline feature this unlocks. Compare the user's full metric vector against the typical vector for their band, and convert each metric to a **rating-equivalent** by finding which band's median matches their value.

> "You play tactics like a 1780 and endgames like a 1390. Your rating is 1620 — the endgames are what's holding it down."

Render as a radar or a horizontal bar chart with the user's actual rating drawn as a reference line. This is the screenshot-worthy output of the entire product.

### 4.4 Do not

Do not recompute the corpus per run. It is static, versioned, and shipped with the app. Stamp `reference_version` on each insight run.

---

## Phase 5 — Cheap new measurements

Independent of everything else, low effort, high value. Build these while Phase 4 batches.

### 5.1 Opponent error harvesting

**The largest blind spot in the current product.** You measure the user's errors exhaustively and never measure whether they *punish* the opponent's. Roughly half of rating is capitalizing on mistakes.

The data already exists in `review_moves` — you're only querying `is_user_move = 1`.

```
opponent_errors  = count of opponent moves with delta_w > MISTAKE_DELTA_W
punished         = of those, the share where the user's very next move
                   had delta_w <= CRITICAL_HANDLED_DELTA_W and captured
                   at least half the swing
punish_rate      = punished / opponent_errors
```

Report against the reference corpus percentile. Split by phase and by whether the error was a tactic or a positional slip.

### 5.2 Offensive vs. defensive blindness

"Missed tactics" measures tactics the user failed to *play*. Entirely separate skill: tactics they failed to *see coming*.

Split every blunder by whether the refutation was **the opponent's resource the user missed** (defensive blindness) versus **the user's own opportunity missed** (offensive blindness). Determined from whether the punishing move in the PV belongs to the opponent's next move or the user's forgone alternative.

Most players are heavily lopsided, and the training prescriptions are completely different.

### 5.3 Fast blunders vs. slow blunders

Nearly free, and it doubles the diagnostic value of the existing blunder card.

- A blunder after a 40s think is a **flawed evaluation** — they looked and judged wrong.
- A blunder after 2s is **impulse** — they didn't look.

Same Δw, opposite fixes. Split `blunders` by `time_spent` against the game's median move time, and report both counts with separate evidence sets.

### 5.4 Trade quality

Δw restricted to captures and exchange initiations. Bad trading is one of the most common and least diagnosed weaknesses below 1800, and it is a one-line filter over data you already store.

---

## Phase 6 — Recurrence tracking

**The strongest leak signal available, and it requires Phase 1.**

A mistake that happens once is noise. The same mistake six times is a leak. Recurrence is more persuasive to a user than any aggregate, and it gives an honest filter: a "leak" that never recurs probably isn't one.

### 6.1 Error signatures

```python
def error_signature(move) -> str:
    return hash_of((
        move.motif_tag,          # fork, pin, deflection, ...
        move.piece_moved,
        move.piece_lost,
        move.phase,
        move.geometry_class,     # see Phase 10.2
        move.opponent_piece_type,
    ))
```

### 6.2 Persistence across runs

Signatures must survive run immutability — recurrence is a cross-run, cross-window property.

```sql
CREATE TABLE error_signatures (
  user_id      TEXT NOT NULL,
  signature    TEXT NOT NULL,
  game_id      TEXT NOT NULL,
  ply          INTEGER NOT NULL,
  delta_w      REAL,
  played_at    TIMESTAMP,
  PRIMARY KEY (user_id, game_id, ply)
);

CREATE INDEX idx_error_sig ON error_signatures(user_id, signature, played_at);
```

### 6.3 Output

> "You've hung a knight to this same fork pattern **6 times since March 3**" — with all six boards.

Any signature with count ≥ 3 in the window becomes leak-eligible with impact = total Δw of its instances per game. Signature recurrence should also **boost** existing leaks: a leak whose supporting moves share a signature is more actionable than one whose don't.

### 6.4 Practice-set efficacy

Close the Insights → Puzzles loop. After a user solves a practice set for signature X, measure Δw on signature X in games played afterwards.

```sql
CREATE TABLE practice_efficacy (
  user_id        TEXT NOT NULL,
  signature      TEXT NOT NULL,
  solved_at      TIMESTAMP NOT NULL,
  delta_w_before REAL,      -- rate per 100 moves, 30 days prior
  delta_w_after  REAL,      -- rate per 100 moves, 30 days following
  PRIMARY KEY (user_id, signature, solved_at)
);
```

If the rate drops, you can prove the tool works — a claim no competitor can make. If it doesn't, that is more valuable information than any metric on the page.

---

## Phase 7 — Counterfactual simulator

"Rating left on the board" is a static estimate. Make it interactive.

Sliders / toggles:
- "Never move under 5 seconds"
- "Eliminate cliff blunders"
- "Cut endgame Δw by 20%"
- "Punish opponent errors at the median rate for my band"

For each configuration, re-simulate expected score across the user's **actual games** by removing the affected Δw from the relevant positions, recomputing the resulting win probabilities, and converting the lifted score fraction through the FIDE curve (reuse the existing "rating left on the board" conversion).

Show the rating delta live. State the model on-card, as the existing implementation already does.

This makes the leak board's implications concrete, and people will spend real time with it.

---

## Phase 8 — Latent skill model (IRT)

The most mathematically interesting addition, and it fits the existing data unusually well.

### 8.1 Why it fits

Item Response Theory needs an item difficulty and a binary response. You already have both:

- **Item difficulty** = `R_find` (the calibrated rating at which a position becomes findable). Already on the rating scale.
- **Response** = did the user play an acceptable move.

Because `R_find` is already expressed in rating points, **no rescaling is needed** — θ comes out on the same scale as chess ratings, which is exactly what you want to display.

### 8.2 Model

2PL, fit per skill category:

```
P(correct | theta, b, a) = 1 / (1 + exp(-a * (theta - b) / 400))
```

where `b = R_find` for the position and `a` is a per-category discrimination parameter fit across the corpus.

Categories: tactics, endgame technique, defense, calculation (long forcing lines), positional judgement (quiet positions). Assign each position to a category from its existing tags and phase.

### 8.3 Output

Per-category ability with standard errors:

> θ_tactics = 1890 ± 45 · θ_endgame = 1640 ± 70 · θ_defense = 1710 ± 55

Strictly better than accuracy averages, because it correctly handles the fact that different players face different difficulty distributions — a player who only faced easy positions and got them right should not outrank one who faced hard positions and got most right.

### 8.4 Shared with Puzzles 2.0

The same model gives a principled adaptive selector: serve items with `b` near θ, where Fisher information is maximized. Both products then run on one model, and puzzle results feed θ back.

### 8.5 Constraint

Requires full-tier findability coverage. With only 5 upgraded games per run, θ will have wide error bars initially. Gate the card behind a minimum item count (~60 scored positions) and show the standard errors prominently. Consider raising the background upgrade budget for users who open Insights repeatedly.

---

## Phase 9 — Shapley leak attribution

### 9.1 The problem

Leaks currently overlap by construction — one move can be critical, in the endgame, *and* played in a scramble — so they are ranked, never summed. That is the correct call today, but it caps the product: users want to know where their lost win% actually went, and a ranked list can't answer that.

### 9.2 Approach

Fit a model over the user's move table:

```
delta_w ~ phase + volatility_bucket + clock_bucket + opponent_band
          + move_number_bucket + findability_bucket + is_capture
```

Gradient-boosted trees or a GLM, whichever validates better. Then compute **Shapley values** per feature over the fitted model.

Attribution becomes additive by construction, and overlapping causes are split proportionally to marginal contribution:

> "Of 340 win% lost: 94 attributable to time pressure independent of phase and criticality, 71 to endgame technique, 48 to critical-moment collapse..."

### 9.3 Presentation

The leak board becomes a **budget**, not a leaderboard — a stacked bar summing to total loss. Keep the ranked view as a secondary tab; the ordering is still what most users act on.

Retain the existing cap logic as a sanity check: Shapley values should sum to observed total loss. Assert this in tests.

---

## Phase 10 — Structure and geometry

New position classifiers. Each needs a labeller in `core/`, then metrics fall out.

### 10.1 Pawn structure families

The phase split is time-based; **structure** is what determines which skills a position demands, and it is how coaches actually diagnose. Classify middlegame positions into: IQP, Carlsbad, hanging pawns, closed centre, open centre, symmetrical, opposite-side castling race, French chain, Benoni chain, King's Indian chain.

Report Δw/move and expectation-adjusted score per family, with percentiles. This is a blind spot in every existing tool.

### 10.2 Geometric blind spots

Humans systematically miss backward moves, long-diagonal moves, and retreats. This is directly measurable and the result is clean and actionable.

Classify each *best move that was missed* by geometry:
- direction (forward / backward / lateral)
- distance (adjacent / medium / long)
- piece type
- whether it moves toward or away from the enemy king

**Critically: control for difficulty.** Compare miss rates *within* findability deciles, otherwise you are just measuring that backward moves tend to be harder.

> "Controlling for difficulty, you miss backward knight moves 2.8× more often than forward ones."

### 10.3 Piece-specific error attribution

Same machinery: which piece does the user hang, whose tactics do they miss. Knight-blindness is real, commonly discussed, and has never been quantified for an individual player.

### 10.4 Endgame types and tablebase ground truth

"Endgame" is too coarse — rook endings are roughly half of all endgames and nearly everyone is weak in them. Split by material signature: rook, opposite-coloured bishops, same-coloured bishops, knight, queen, pawn, rook+minor.

At **≤7 pieces**, use **tablebases** rather than the engine. This gives literal perfect play as ground truth, producing a "technique score" that is uniquely trustworthy — not an engine approximation, but optimal play. Report DTZ-optimal move rate.

### 10.5 Unsupervised blunder clustering

The motif taxonomy is hand-labelled. Cluster blunders in raw feature space (piece placement, attack geometry, material, phase) and surface recurring personal signatures no named motif covers.

> "38% of your blunders share this signature: an enemy piece attacking from more than three squares away along a diagonal."

Pair with **position-similarity montage**: show six visually similar boards side by side. The pattern is obvious before the user reads a word, and it is more persuasive than any statistic.

---

## Phase 11 — Psychological layer

Extends the existing time/tilt cards. All of these are behavioural, all measurable from stored data.

### 11.1 Impulsivity rate

Fraction of moves played under ~3 seconds **when not in a scramble**, and the blunder rate on those versus matched-volatility slow moves. Probably the most common fixable leak in online chess, and it is invisible in every existing tool.

### 11.2 Metacognition score

The continuous version of the existing inverted-time-budget leak: correlation between time spent and actual position difficulty (volatility, findability). Display as a scatter with a fitted line.

"Do you know which positions are hard?" is a real, trainable skill.

### 11.3 Risk conditioned on game state

Volatility steering is currently unconditional. Condition it on `win_prob`:

- Do they sharpen when losing (correct) or when winning (usually fatal)?
- Do they go **passive** when winning — measure Δw and time spent as a function of `win_prob > 0.7`?

This is the *mechanism* behind the existing "converted then lost" taxonomy label, which currently only names the symptom.

### 11.4 Stubbornness

After a plan the user initiated is refuted, do they persist? Detect as repeated moves of the same piece into the same refuted structure within a window of plies. Sunk-cost behaviour at the board — never measured anywhere, as far as I know.

### 11.5 Tilt significance testing

Run the existing tilt/recovery numbers through a proper test — post-loss win rate versus baseline with a binomial test, or a Markov chain against an independence null.

Being able to say *"your post-loss drop is not statistically distinguishable from chance"* builds more trust than confirming every folk belief. Report it either way.

### 11.6 Move-time distribution shape

Beyond the mean: fit the distribution. Bimodal = impulsive-plus-deliberate mix, which correlates with the fast/slow blunder split in 5.3. Report the shape as a fingerprint alongside the histogram.

---

## Phase 12 — Presentation and exports

### 12.1 Narrative diagnosis

The report is card-dense — right for exploration, wrong for the first thirty seconds. The most valuable output is not a dashboard but a **diagnosis**.

Promote the existing leaks + AI coach takeaways + practice set into a single opening screen:

1. Three sentences naming the one thing costing the most rating
2. Its evidence (Phase 1), inline
3. The prescription — a "Practice this" button into the trainer

Dashboard becomes the "show your work" underneath.

### 12.2 Self-assessment first

Before revealing the report, ask three questions:
- Which phase do you think is your weakest?
- Do you blunder more when winning or when losing?
- Do you spend enough time on critical moves?

Then reveal. The **gap** between self-perception and data is itself an insight, and it is the stickiest thing on this list — people remember being wrong about themselves far longer than they remember a number.

Store the answers; the accuracy of self-assessment is a metric in its own right and can be tracked across runs.

### 12.3 Greatest hits

Strengths are currently categories. Add specific moments: moves the user **found** that had low findability. Positive reinforcement, and it is the shareable artifact — nobody posts their blunder rate, everybody posts a brilliancy.

### 12.4 Coach memo export

A one-page markdown/PDF a human coach can read in two minutes, with evidence positions embedded as diagrams. Coaches don't want a dashboard. Every export also travels, which is distribution.

### 12.5 Annotated PGN download

Export with NAGs plus comments carrying Δw, findability, and volatility per move. Opens in any GUI. Nearly free given what is already stored, and portability builds trust rather than lock-in.

---

## Anti-goals

- **Do not sum leaks until Phase 9 exists.** The current ranked-never-summed rule is correct and must stay until Shapley attribution replaces it.
- **Do not display any metric with `n < 8`** outside a greyed state, and never let one onto the leak board.
- **Do not recompute the reference corpus per run.** It is static and versioned.
- **Do not store full support sets in the metrics blob.** Exemplars in `metric_evidence`, predicate in `query`.
- **Do not add a metric to the aggregate only.** Anything that should respond to filters must add its inputs to `game_explorer`, per the existing contract.
- **Do not report raw win rates** once Phase 2 lands. Expectation-adjusted or nothing.

---

## Acceptance criteria

**Phase 1:** Every leak and every Tier 1 card renders inline evidence chips. Clicking any chip opens the correct ply with both arrows. Exemplars never draw more than 2 moves from one game. Counter-examples present on every leak.

**Phase 2:** No card in the product displays a raw win rate or raw score without its expectation gap. Recency weighting active with the half-life documented on-page.

**Phase 3:** Every `MetricResult` carries `n` and `ci`. Buckets with small `n` visibly shrink toward the global mean. A metric with `n < 8` cannot appear on the leak board. Sample-size guidance renders where applicable.

**Phase 4:** Every metric card shows a percentile against the user's rating band and time control. Rating-implied profile renders. `reference_version` stamped on runs.

**Phase 5:** Punish rate computed and percentile-ranked. Blunders split by fast/slow. Offensive vs. defensive blindness split renders with separate evidence sets.

**Phase 6:** Signatures persist across runs. Any signature with count ≥ 3 becomes leak-eligible with all instances as evidence. Practice efficacy recorded for every solved set.

**Phase 8:** Per-category θ with standard errors, gated behind a minimum item count. Puzzles 2.0 selector queries θ.

**Phase 9:** Shapley values sum to observed total loss (asserted in tests). Leak board renders as a budget with the ranked view retained as a secondary tab.
