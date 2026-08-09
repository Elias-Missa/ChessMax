# The Insights report — what it computes and what it shows

This is a description of the Insights feature **as currently built**, not a plan.
Every metric, threshold and panel listed here exists in the code today.

- Spec of record for the original phases: [`Insights.md`](../Insights.md)
- Backend: `server/insights_run.py` (orchestration), `server/insights_metrics.py`
  (Tier 1–3 catalogue), `server/insights_pro.py` (headline / leaks / game facts)
- Frontend: `frontend/insights/app.js`, `frontend/insights/styles.css`, and the
  `#insights-root` block in `frontend/index.html`

---

## 1. How a report is produced

1. You enter a **handle**, a **window** (7 or 30 days) and a **time control**
   (bullet / blitz / rapid / daily), and pick **Chess.com** or **Lichess**.
2. Games are pulled from that source's public API, capped at the most recent
   **300** in the window (the UI says so when the cap is hit).
3. Each game gets a **shallow-tier** review: Stockfish MultiPV at low fixed
   nodes, no Maia. Games already reviewed are reused, so a refresh mostly costs
   nothing.
4. Metrics are computed over the stored moves and saved as an immutable run.
   The last **10** runs per handle are kept.
5. Up to **5** flagged games are upgraded to **full tier** (findability) in the
   background after the run completes.

A run is a snapshot. Re-running creates a new one, which is what makes the
**Progress** card possible.

### Depth tiers

| | Shallow | Full |
|---|---|---|
| Applies to | every game in a run | games you open in Game Review, plus up to 5 flagged per run |
| Produces | win%, Δw, classification, volatility, phase, clock/time | everything above **plus findability** |

Anything marked **[F]** below needs full tier and states its sample size in the
UI. `findability` is `null` when it was never computed and `0` when the position
is engine-only — the two never get conflated.

### Units

- **Δw** ("win% lost") — expected-points loss for one move, on a 0–100 scale.
- **win_prob** — expected points before a move, 0–1.
- **Accuracy** — CAPS2-style, from `chess_vol.game_review.move_accuracy`, so the
  Insights number and the Game Review number always agree.
- **Volatility** — the one-ply move-choice volatility score, 0–100
  (see [`VOL_README.md`](VOL_README.md)).

---

## 2. The UI

The Insights tab is a **launcher**: the run form, prior runs, and a "Report
ready" card summarising the latest run (W–D–L, accuracy, performance rating,
rating left on the board, top leak).

**Open Insights** opens the report as a full-screen overlay: sticky header,
section rail on the left, scrollable content, `Esc` to exit.

### Filters

Three filters live in the report header — **Colour** (all/white/black),
**Result** (all/wins/draws/losses) and **Opponent** (all/lower/similar/higher,
banded at ±100 rating points).

They re-aggregate the dashboard **client-side** from `metrics.game_explorer`,
the per-game fact table. Any panel whose inputs only exist per move cannot be
re-derived at game granularity; those carry an **"All games"** badge rather than
silently ignoring the filter. The badge column below says which is which.

---

## 3. Section by section

### Overview

**KPI strip** — seven tiles, all filter-aware:

| KPI | What it is |
|---|---|
| Games | Games in the selection, with the W–D–L record underneath |
| Score | Points per game, and the gap versus what the rating differences predicted |
| Performance rating | Average opponent rating + FIDE `dp(score)`, clamped to ±800 |
| Rating change | First → last rating in the window |
| Accuracy | Mean, with the standard deviation and a consistency label |
| Blunders / 100 | Blunders per 100 of your moves |
| Rating on the table | Estimated rating recoverable — see below |

| Card | Shows | Filters |
|---|---|---|
| **Your biggest leaks** | Up to 6 ranked leaks, each with a quantified impact, a severity, an impact bar and a "Fix this →" jump to the evidence | All games |
| **What's working** | Up to 5 detected strengths | All games |
| **Rating left on the board** | Elo estimate, actual vs potential score, and the model stated in plain words | filter-aware |
| **Timeline** | Accuracy per game with the rating overlaid on a second axis | filter-aware |
| **Progress** | Deltas against your previous run with the same handle/window/time control | All games |

### Move quality

| Card | Shows | Filters |
|---|---|---|
| **Move quality mix** | Every user move classified — brilliant, great, best, excellent, good, book, inaccuracy, mistake, miss, blunder — as a proportional bar plus counts and percentages | filter-aware |
| **Where the quality goes** | Accuracy per phase as bars, with Δw per move in the tooltip and beneath | filter-aware |
| **Error rates** | Blunders / mistakes / inaccuracies per 100 moves, moves per blunder, share of blunder-free games, total win% surrendered | filter-aware |
| **How your games slip** | Loss taxonomy doughnut | All games |
| **Blunder rate by move number** | Blunder rate and Δw/move across move windows 1-10, 11-20, 21-30, 31-40, 41+; the header states the average move your first serious error lands on | All games |

### Critical moments

The differentiated section: performance on the moves that actually decide games.

| Card | Shows | Filters |
|---|---|---|
| **Do you rise to the moment?** | Moves, accuracy, Δw/move, "handled cleanly" rate, blunder rate and average think time for Critical (V ≥ 60) / Tense (35–60) / Quiet (V ≤ 35), plus the **criticality gap** | All games (the gap is also recomputed under the filter) |
| **Fixable loss** **[F]** | Total Δw against the share that was realistically findable, with the full-tier sample size | filter-aware |
| **How human are your misses?** **[F]** | Share of positions where the right move was one a human policy at your rating would plausibly pick | All games |
| **Volatility steering** | Mean volatility when you play the engine's move versus when you leave it — whether your deviations sharpen or quieten the game | All games |
| **Missed tactics** **[F]** | Missed motifs (fork, pin, skewer, deflection, discovered attack, back rank, overloaded defender, zwischenzug) with the share that were findable | All games |
| **Sharpness profile** | Win rate bucketed by your games' mean volatility | All games |

A callout at the top of the section surfaces the criticality note or the
inverted-time-budget note when either fires.

### Openings & endgames

| Card | Shows | Filters |
|---|---|---|
| **Opening repertoire** | Per opening × colour: ECO, games, score, accuracy, opening Δw/move, the average ply you leave book, blunders/100. Toggle: both / as white / as black | All games |
| **Repertoire depth** | Average ply you leave book, and mean Δw over the following five moves — separates a bad opening choice from not knowing the resulting plans | All games |
| **Castling** | Points per game by kingside / queenside / never, and same-side vs opposite-side | filter-aware |
| **Endgames** | Reach rate, endgame Δw/move, and score split by whether you entered winning (>0.6), level, or worse (<0.4) | All games |
| **Conversion & resilience** | Score from winning positions (peak win% > 70) and from losing ones (trough < 30), with points dropped and points rescued | All games |
| **Missed wins** | Games where you reached >85% win probability and did not win, each linking into Game Review | All games |

### Time & mind

| Card | Shows | Filters |
|---|---|---|
| **Scramble decay** | Δw/move and blunder rate by clock remaining: >60s, 30–60s, 10–30s, <10s | All games |
| **Tilt & fatigue** | Win rate by game index within a session (sessions split on a >2h gap) | All games |
| **Recovery** | Win rate in the game immediately after a defeat versus overall, with the tilt signal | All games |
| **Time of day** | Games played and mean accuracy by hour | filter-aware |
| **How long to play** | Win rate by session length (1–2, 3–5, 6+ games) | All games |
| **Opponent-relative performance** | Win rate by rating band, cross-tabbed with Δw/move per phase | All games |

### Games

Every analyzed game, sortable on any column and searchable by opponent, opening
or ECO: date, side, opponent and rating band, opening, result, accuracy, total
Δw, blunders, a win% sparkline from your point of view, your worst move of the
game, and a **Review** button that opens it in Game Review.

### Practice set

The Insights → Puzzles loop. Positions from your own games where the miss was
both **costly** (Δw ≥ 15) and **findable** (findability > 60 when known). Each
card opens an inspector with the board, the move played, the best move, Δw,
findability and volatility, and buttons into the trainer or the full review.
These positions are also written into the Mistakes trainer as puzzles.

---

## 4. The two headline calculations

### Rating left on the board

Recoverable win% converted into rating points. Per game, the recoverable pool is
the **findability-weighted loss** where full-tier data exists and the loss from
**outright blunders** otherwise; `basis` reports `findability`, `blunders` or
`mixed`. Recovery is capped by the result actually dropped — a game already won
has nothing to recover — then the lifted score fraction is converted through the
FIDE score-to-difference curve.

This is an estimate, and the model is printed on the card rather than hidden.

### The leak board

Every detectable leak is scored in the **same unit — win% lost per game** — so a
phase problem, a clock problem and an opening problem can be ranked on one axis.

| Leak | Fires when | Impact estimate |
|---|---|---|
| Weakest phase | worst phase leaks more per move than your best phase | excess × that phase's moves per game |
| Critical-move collapse | ≥8 critical moves and a gap >4 accuracy points | critical Δw/move × critical moves per game |
| Time scrambles | ≥10 moves under 10s and ≥10 with time | excess over the unhurried rate × scramble moves per game |
| Inverted time budget | you spend less time on critical moves than quiet ones | half the critical-move exposure |
| Winning positions slip | ≥3 winning positions scoring under 85% | half the points dropped, per game |
| A losing opening | ≥3 games in one opening scoring under 40% | shortfall against 50% × its share of your games |
| Tilt | ≥4 post-loss games with a >10 point lower win rate | the drop × post-loss share, halved |
| A repeated motif | ≥3 misses of one tactic | total cost of those misses per game |
| A bad stretch of moves | one move-number window blunders more than the rest | excess over the baseline × that window's moves per game |

Each estimate is `excess rate × exposure`, which is unbounded above, so all are
**capped at the mean win% you actually lost per game**. Leaks overlap by
construction — one move can be critical, in the endgame, *and* played in a
scramble — so they are **ranked, never summed**.

The three highest leaks are also the AI coach takeaways.

### Strengths

The counterweight, up to five of: steady under sharpness, converts winning
positions, hard to put away, an opening that works, outperforming your rating,
a strong phase, mostly blunder-free games.

---

## 5. Thresholds

All defined once, in `server/insights_pro.py` unless noted.

| Constant | Value | Used for |
|---|---|---|
| `BLUNDER_DELTA_W` | 25 | blunder counting |
| `MISTAKE_DELTA_W` | 10 | mistake counting |
| `INACCURACY_DELTA_W` | 5 | inaccuracy counting |
| `CRITICAL_VOLATILITY` | 60 | critical-move bucket |
| `QUIET_VOLATILITY` | 35 | quiet-move bucket |
| `CRITICAL_HANDLED_DELTA_W` | 5 | "handled cleanly" |
| `WINNING_WIN_PROB` | 0.70 | conversion |
| `LOSING_WIN_PROB` | 0.30 | comeback |
| `DECISIVE_WIN_PROB` | 0.85 | missed wins |
| `SCRAMBLE_CLOCK_SECONDS` | 10 | time scramble |
| `MOVE_NUMBER_BUCKETS` | 1-10, 11-20, 21-30, 31-40, 41+ | blunder timing |
| `PRACTICE_DELTA_W` | 15 | practice flag cost (`insights_metrics.py`) |
| `PRACTICE_FINDABILITY_MIN` | 60 | practice flag findability (`insights_metrics.py`) |
| `SESSION_GAP` | 2 hours | session splitting (`insights_metrics.py`) |
| Rating band | ±100 | opponent bands |
| Endgame entry | >0.6 / <0.4 | endgame conversion |
| Volatility profile | <35 / 35–60 / >60 | sharpness profile |
| Game length | <40 / 40–80 / >80 plies | length buckets |

### Loss taxonomy

One label per game, in this precedence order (`server/reviews.py:classify_loss_type`):

| Label | Condition |
|---|---|
| Converted then lost | reached win% > 80 and finished below 40 |
| Cliff | a single user move losing > 25 win% from a non-losing position |
| Scramble | more than half the total loss came from the bottom decile of clock |
| Never in it | the first eight user moves inside ply 30 are all below 35% |
| Bleed | everything else |

---

## 6. Payload shape

`insight_runs.metrics` is one JSON blob. Top-level keys:

```
total_loss              fixable_loss           fixable_sample_size
loss_taxonomy           time_vs_criticality    volatility_profile
volatility_steering     games                  tier2
tier3                   practice_flags         missed_tactics
time_scramble_decay     maia_naturalness       game_explorer
ai_coach_takeaways      pro                    trend
```

`pro` holds `headline`, `move_quality`, `critical_moments`, `timeline`,
`openings`, `endgame`, `resilience`, `blunder_timing`, `leaks`, `strengths`.

**`game_explorer` is a contract, not a list view.** It is one rich row per game
(colour, result, points, ratings and band, accuracy, per-phase moves/loss/accuracy,
classification counts, critical and quiet splits, scramble counts, castling,
opening, deviation ply, first error, biggest miss, sparkline). The filters
re-aggregate the dashboard from these rows, and
`tests/puzzles/test_insights_pro.py::test_game_facts_reaggregate_to_the_server_totals`
pins that summing them reproduces the server's own aggregates. A new metric that
should respond to filters has to add its inputs here, not only to the aggregate.

---

## 7. Known limits

- **Findability coverage is thin on a fresh run.** Only full-tier games have it,
  so `fixable_loss`, the Maia card and the findability side of missed tactics run
  on a handful of games until upgrades land. Every one of those panels prints its
  sample size.
- **The filters cannot reach move-level panels.** Those are badged "All games".
- **Leak thresholds are reasoned, not calibrated.** They were chosen to be
  defensible, not fitted against outcomes. Calibrating them the way findability
  was calibrated in Phase 3 is the obvious next step.
- **Opening names depend on the source.** Chess.com ships `ECOUrl` rather than a
  name; `server/reviews.py:opening_name` parses it. Games ingested before that
  existed need `python -m scripts.backfill_openings`.
- **Anonymous use is not supported here.** Runs are per-account, since they are
  stored per user.
