# Chess-Volatility-Bar

# Chess Volatility Bar — Implementation Guide

## 1. Project Overview

Build a **Chess Volatility Bar** — a companion visualization to Stockfish's evaluation bar that measures how *volatile* (sharp/risky) a position is, not just its static evaluation.

**Core insight:** A position evaluated as "0.0" can mean two very different things:
- **Low-vol draw:** most reasonable moves maintain equality. Safe position.
- **High-vol draw:** only the single best move draws; every other move loses instantly. Razor-sharp.

The standard eval bar cannot distinguish these. The Volatility Bar can.

A position is "volatile" for two distinct reasons, and the algorithm measures both:
- **Move-choice volatility:** how punishing is it if *you* pick the wrong move right now?
- **Reply volatility:** after your best move, how punishing is the resulting position — for you or your opponent — down the line? A position where every ply has one correct move is genuinely sharp, even if each individual ply looks like it has an obvious best move.

### Deliverables (build in this order)
1. **Phase 1 — Core engine (one-ply):** Python library. FEN in, volatility score out. Measures move-choice vol only.
2. **Phase 1.5 — Recursive volatility:** extend the core function with an optional `recurse_depth` parameter that adds reply volatility.
3. **Phase 2 — CLI tool:** Paste PGN, get move-by-move volatility. Flag `--deep` toggles recursive mode.
4. **Phase 3 — Local web app:** Upload PGN, see eval + volatility chart. Supports both shallow and deep modes.
5. **Phase 4 — Browser extension:** Live overlay on chess.com. **One-ply only** (deep mode is too slow for live use).

Each phase must pass its tests before the next begins.

---

## 2. Technical Stack

- **Language:** Python 3.11+
- **Engine:** Stockfish (latest stable), accessed via `python-chess` (`chess.engine.SimpleEngine.popen_uci`).
- **Core libs:** `python-chess`, `numpy`, `pytest`.
- **Phase 3:** FastAPI backend + vanilla JS / Chart.js frontend (one-file backend).
- **Phase 4:** Manifest V3 Chrome extension. Content script reads FEN from chess.com and POSTs it to the local FastAPI server. No chess analysis inside the extension.

Stockfish is installed separately. Auto-detect at common paths (`/usr/local/bin/stockfish`, `/opt/homebrew/bin/stockfish`, `shutil.which("stockfish")`) with override via env var `STOCKFISH_PATH`. Raise a clear `EngineNotFoundError` with install instructions if missing.

---

## 3. The Volatility Algorithm

### 3.1 One-Ply Volatility (Phase 1)

#### Inputs
- FEN.
- `depth` (default 18), `multipv` (default 6 = best + 5 alternatives).
- Side to move derived from FEN.

#### Procedure
1. Launch Stockfish with `MultiPV = N` (default 6).
2. Analyze to the configured depth. N lines returned.
3. Convert every eval to **centipawns from the side-to-move POV**, including mate translation (§3.3).
4. Let `E = [e_1, e_2, ..., e_N]` sorted descending; `e_1` is the best move.
5. Compute drops, apply the drop cap, compute the weighted sum, apply eval-aware scaling, normalize.

#### Formula

```
drop_i        = e_1 - e_i                            (capped at DROP_CAP = 2000 cp)
weights       = [1/(i-1) for i in 2..N]              → [1.0, 0.5, 0.333, 0.25, 0.2] for N=6
weighted_sum  = sum(w_i * drop_i) / sum(w_i)         (centipawns)

scale_eval    = min(|e_1|, |e_2|, EVAL_SCALE_MAX)
scale         = 1 / (1 + (max(0, scale_eval - EVAL_SCALE_GRACE) / EVAL_SCALE_WIDTH)²)

V_raw         = scale * weighted_sum

V             = 100 * (1 - exp(-V_raw / K))
```

Sum of weights for N=6: 2.283.

**Defaults (all named constants in `config.py`):**
- `K = 150` — normalization scaling constant (centipawns)
- `DROP_CAP = 2000` — max value for any single drop_i
- `EVAL_SCALE_GRACE = 200` — cp below which no eval-based dampening applies
- `EVAL_SCALE_WIDTH = 300` — controls how fast dampening kicks in above the grace zone
- `EVAL_SCALE_MAX = 2000` — cap on scale_eval so mate scores don't collapse V to zero

#### Why eval-aware scaling

Without scaling, "winning +400 with five good alternatives" and "balanced 0.0 with five good alternatives" produce similar V scores. In practice they feel very different — in a winning position, picking the 2nd-best winning move is not volatile behavior. The scale factor dampens V when the second-best move is already meaningfully winning, and leaves V unchanged when alternatives are balanced or worse.

Using `min(|e_1|, |e_2|)` rather than `|e_1|` alone is deliberate: we only dampen when *both* the best and backup lines are already decided. If your best move is +400 but your second-best is -200, that's a critical decision and V should not be dampened. `e_2` is the honest "backup plan" eval.

#### What the scaling produces

| scale_eval | scale |
|---|---|
| ≤ 200 | 1.00 |
| 300 | 0.90 |
| 500 | 0.50 |
| 800 | 0.20 |
| 1000 | 0.12 |
| 1500 | 0.047 |
| 2000 (cap) | 0.028 |

This correctly encodes the rule: *going from +200 to −200 (through zero) is more volatile than going from +1000 to +600 (still crushing).*

### 3.2 Recursive Volatility (Phase 1.5)

The one-ply formula only captures *move-choice* volatility. To capture *reply* volatility — the danger living in the positions your candidate moves lead to — recurse into the top-k candidate moves and combine.

#### Algorithm

```
compute_volatility_raw(pos, depth, multipv, recurse_depth, recurse_k,
                       recurse_alpha, child_depth):

    V_local = scaled_weighted_sum(pos, depth, multipv)   # §3.1 through V_raw (before normalization)

    if recurse_depth == 0 or pos is terminal:
        return V_local

    top_k_moves = top_k_candidates_from_analysis(pos, k=recurse_k)

    child_Vs = []
    for move in top_k_moves:
        child_pos = pos.after(move)
        child_V = compute_volatility_raw(
            child_pos,
            depth=child_depth,
            multipv=multipv,
            recurse_depth=recurse_depth - 1,
            recurse_k=recurse_k,
            recurse_alpha=recurse_alpha,
            child_depth=child_depth,
        )
        child_Vs.append(child_V)

    return V_local + recurse_alpha * mean(child_Vs)
```

Normalize **once** at the top level:

```
V = 100 * (1 - exp(-V_total_raw / K))
```

#### Default parameters
- `recurse_depth = 0` → pure one-ply (Phase 1 behavior, the default).
- `recurse_depth = 2` → deep mode. Looks at your move + opponent's reply.
- `recurse_k = 3` → recurse into your top 3 candidate moves, not all 6.
- `recurse_alpha = 0.5` → each recursion level contributes half as much as the one above it.
- `child_depth = 12` → recursed calls use shallower search. Children are volatility *sensors*, not truth oracles.
- `K` is tuned separately for shallow and deep modes (see §7).

#### Critical implementation note: side-to-move flips

When you recurse into the child position (after your move), side to move is now the **opponent**. All centipawn conversions at that node must be from *their* perspective. Derive `turn` from the child board at every recursion level. Add a test that verifies this (§6).

#### Cost

- One-ply: one MultiPV-6 analysis per position. ~1–3 s at depth 18.
- Recursive (`recurse_depth=2, recurse_k=3`): 1 + 3 + 9 = 13 analyses per root position. With `child_depth=12` most are cheap — ~3–6 s per root position. A 40-move game: ~4–8 minutes. Usable offline. **Not** usable live.

This is why the extension (Phase 4) is one-ply only.

### 3.3 Mate Handling

Uniform clamping of all mate scores to ±10000 collapses important distinctions (mate-in-1 vs. mate-in-10; mate-available vs. lose-material). Use a distance-aware mapping.

```
MATE_BASE   = 2000
MATE_STEP   = 50
MATE_MAX_N  = 20

mate_to_cp(n):
    # n > 0 → we mate in n;  n < 0 → opponent mates us in |n|
    sign = +1 if n > 0 else -1
    return sign * (MATE_BASE - MATE_STEP * min(|n|, MATE_MAX_N))
```

Produces:

| Mate in N | cp value |
|---|---|
| M1 | 1950 |
| M2 | 1900 |
| M3 | 1850 |
| M6 | 1700 |
| M10 | 1500 |
| M20+ | 1000 |

All mates are worth more than any normal eval (which rarely exceeds ±1500 cp), preserving the "mate beats material" ordering. But shorter mates outrank longer ones, and a mate-in-20 is closer in value to "+10 pawns" than to "+100 pawns" — reflecting the reality that long mates are harder for humans to find and more easily lost.

### 3.4 Decided-Position Flag

A position is "decided" only if both the best *and* second-best lines agree it's won for the same side, and both are meaningfully winning.

```
decided = (|e_1| > 800) AND (sign(e_2) == sign(e_1)) AND (|e_2| > 400)
```

This correctly dims the UI bar for "trivially winning with multiple paths" (e.g., [M1, M3, +400]) but keeps it undimmed for "winning only if you find the one move" (e.g., [M3, -500, -700]) — the latter is knife's-edge, not decided.

### 3.5 Edge Cases (handle explicitly)

- Fewer legal moves than N → set `MultiPV = K_legal` where K_legal = legal move count. If K_legal = 1, local V is **undefined** — treat as 0 during recursion (no move-choice risk from this node), but at the *root* return `None` with reason `"only_move"`. UI renders "—", not 0.
- Checkmate / stalemate → local V is 0 during recursion; at root return `None` with reason `"checkmate"` / `"stalemate"`.
- Already-decided positions (per §3.4) → still compute V, but set `decided=True` so the UI dims the bar.

### 3.6 Color Mapping (used in later phases; define as constants)

- `V < 25` → gray/green (low vol)
- `25 ≤ V < 60` → yellow
- `V ≥ 60` → red (high vol)

Thresholds shared across shallow and deep modes — normalization is designed so the same V means roughly the same thing regardless of recursion depth.

### 3.7 Worked Examples

With all constants at their defaults:

| Scenario | Evals (cp) | best_eval | scale | V_raw | V | Decided? |
|---|---|---|---|---|---|---|
| Quiet opening | [+5, 0, −5, −10, −15, −25] | +5 | 1.00 | 11 | 7 | No |
| Normal middlegame | [+80, +30, +10, −5, −20, −40] | +80 | 1.00 | 71 | 38 | No |
| Only-move crisis | [0, −250, −400, −600, −900, −1200] | 0 | 1.00 | 488 | 96 | No |
| Sharp tactic | [+350, +50, +20, 0, −15, −30] | +350 | 0.83 | 271 | 84 | No |
| Winning, many paths | [+450, +420, +400, +380, +350, +300] | +450 | 0.26 | 15 | 10 | No |
| Dead-drawn endgame | [0, 0, 0, −5, −5, −10] | 0 | 1.00 | 2 | 1 | No |
| Mate available | [M1, +200, 0, −200, −400, −600] | 1950 | 1.00 (e_2=200 low) | ~2000 | ~100 | Check |

For the last row, `scale_eval = min(1950, 200) = 200`, so scale = 1.0 → V saturates. The `decided` check: `|e_1|=1950 > 800` ✓, `sign(e_2)=+` same sign ✓, but `|e_2|=200 < 400` ✗ → **not decided**, bar stays bright red. Correct: your mate is there but all alternatives lose the advantage — genuinely sharp decision.

Contrast with `[M1, M3, +400, +200, 0, −100]`: `scale_eval = min(1950, 1850) = 1850`, scale ≈ 0.033, V drops to ~15. And decided check: `|e_1|=1950>800` ✓, same sign ✓, `|e_2|=1850>400` ✓ → **decided, dimmed**. Correct: you'll win with either of your top two moves, bar shouldn't scream.

---

## 4. Project Structure

```
chess-volatility-bar/
├── README.md
├── pyproject.toml
├── .env.example                 # STOCKFISH_PATH=...
├── src/chess_vol/
│   ├── __init__.py
│   ├── engine.py                # Stockfish wrapper (context manager)
│   ├── volatility.py            # Core algorithm (§3), both shallow and deep
│   ├── analyze.py               # PGN → [(move, eval, vol), ...]
│   ├── config.py                # All constants: K, thresholds, mate mapping, scaling
│   ├── cli.py                   # Phase 2
│   └── server.py                # Phase 3 FastAPI
├── web/                         # Phase 3 frontend
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── extension/                   # Phase 4
│   ├── manifest.json
│   ├── content.js
│   ├── background.js
│   └── bar.css
└── tests/
    ├── test_volatility.py
    ├── test_volatility_recursive.py
    ├── test_mate_handling.py
    ├── test_engine.py
    ├── test_analyze.py
    └── fixtures/
        ├── sharp_position.fen
        ├── quiet_position.fen
        ├── only_move.fen
        ├── forced_sequence.fen     # one-ply V low, deep V high
        ├── mate_in_3.fen
        ├── multiple_mates.fen      # decided, should dim
        └── sample_game.pgn
```

---

## 5. Phase 1 — Core Library (one-ply)

### 5.1 `engine.py`

Context-managed Stockfish wrapper:
```python
class Engine:
    def __init__(self, path: str | None = None): ...
    def __enter__(self) -> "Engine": ...
    def __exit__(self, *args): ...
    def analyse(self, board: chess.Board, depth: int, multipv: int) -> list[InfoDict]: ...
```

Path resolution order: explicit arg → `STOCKFISH_PATH` env → `shutil.which` → known install paths.

### 5.2 `volatility.py`

Public function (Phase 1 shape — designed for Phase 1.5 extension):

```python
@dataclass
class VolatilityResult:
    score: float | None          # 0-100, or None if undefined
    raw_cp: float | None         # unnormalized weighted total (centipawns)
    local_raw_cp: float | None   # root's own contribution (for UI split)
    best_eval_cp: int            # side-to-move POV, mate-translated
    alt_evals_cp: list[int]
    scale: float                 # eval-aware scale factor applied
    decided: bool
    reason: str | None           # "only_move", "checkmate", "stalemate", or None
    recurse_depth_used: int      # 0 in Phase 1

def compute_volatility(
    board: chess.Board,
    engine: Engine,
    depth: int = 18,
    multipv: int = 6,
    # Phase 1.5 additions (default to one-ply):
    recurse_depth: int = 0,
    recurse_k: int = 3,
    recurse_alpha: float = 0.5,
    child_depth: int = 12,
    weights: Callable[[int], list[float]] | None = None,
    scale_fn: Callable[[int, int], float] | None = None,   # swappable eval-scaling
) -> VolatilityResult: ...
```

Pure logic given the engine. No file I/O, no printing. Set up the signature in Phase 1 even though recurse_* params are no-ops until Phase 1.5. Both `weights` and `scale_fn` are swappable so Phase 2 tuning can A/B-test.

### 5.3 `analyze.py`

PGN string → list of per-ply results: ply, SAN, FEN, eval, volatility. Support `max_plies`, a progress callback, and pass-through of all volatility params.

### 5.4 Tests (must pass before Phase 1.5)

1. **`test_engine.py`** — Engine starts, analyzes startpos, returns N lines. Auto-detection works.
2. **`test_volatility.py`** — All the scenarios in §3.7 (plus endpoints for V≈0 and V≈100). Mock the engine to feed known eval arrays; verify each step of the formula matches hand calculation. Cover: starting position, only-move puzzle, K+P vs K endgame, 1-legal-move, checkmate, stalemate.
3. **`test_mate_handling.py`** — M1 through M20 map to correct cp values; opponent-mate scores are negative; extremely long mates clamp at MATE_MAX_N. Verify decided flag for the two contrasting cases in §3.7.
4. **`test_analyze.py`** — Sample PGN parses, one result per ply.

Mark integration tests `@pytest.mark.integration`, skip if Stockfish unavailable, run in CI when available.

**Do not start Phase 1.5 until all Phase 1 tests pass.**

---

## 6. Phase 1.5 — Recursive Volatility

Extend `compute_volatility` to honor `recurse_depth > 0` as specified in §3.2. Keep all Phase 1 behavior identical when `recurse_depth=0` — critical for backwards compatibility and the extension.

### Implementation notes

- **Engine reuse:** pass the same `Engine` instance through recursion. Never open a new Stockfish per call.
- **Candidate extraction:** top-k candidate moves come from the *same* MultiPV analysis used to compute `V_local`. No extra engine call needed just to list them.
- **Budget control:** total analyses = `1 + k + k² + ... + k^recurse_depth`. Log this at the start of a deep call. Consider a `--max-analyses-per-position` safety cap.
- **Side-to-move flip:** see §3.2. Primary bug risk.

### Additional tests (`test_volatility_recursive.py`)

1. **Determinism:** `recurse_depth=0` returns identical results to Phase 1 (regression guard).
2. **Composition:** mock engine to return controlled evals at parent and children; hand-calculate expected `V_raw` with `recurse_alpha=0.5`; verify.
3. **Side-to-move correctness:** fixture where the child position is winning for the original side-to-move (losing for the new side-to-move). Verify child drops are computed from child's POV. Non-negotiable.
4. **`forced_sequence.fen`:** one-ply V low, deep V high (current move obvious, resulting position sharp). Assert `deep_V > shallow_V + 20`.
5. **Budget:** with `recurse_depth=2, k=3`, count engine calls. Assert exactly 13.

**Do not start Phase 2 until all Phase 1.5 tests pass.**

---

## 7. Phase 2 — CLI

```
chess-vol analyze game.pgn --depth 18 --multipv 6 --output report.json
chess-vol analyze game.pgn --deep                  # shorthand for recurse_depth=2
chess-vol analyze game.pgn --recurse-depth 2 --recurse-k 3 --child-depth 12
chess-vol fen "rnbqkbnr/..." --depth 20 --deep
```

Per-move output: SAN, eval, ASCII bar (`████░░░░░░ 42`), and in deep mode a second bar showing local-vs-reply split (e.g., `local 18  reply +24  → total 42`). Color with `rich` if installed.

### Install & run

```
pip install -e .                 # installs the `chess-vol` entry point
pip install -e .[cli]            # adds rich for colorful output (optional)
export STOCKFISH_PATH=/path/to/stockfish   # or let auto-detection handle it

chess-vol analyze tests/fixtures/sample_game.pgn --max-plies 10
chess-vol analyze tests/fixtures/sample_game.pgn --deep --output report.json
chess-vol fen "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
```

Useful flags (shared between `analyze` and `fen`):
- `--depth N` (default 18) — root Stockfish search depth.
- `--multipv N` (default 6) — number of lines analysed per position.
- `--deep` — shorthand for `--recurse-depth 2` (Phase 1.5 reply volatility).
- `--recurse-depth N --recurse-k K --child-depth D` — fine-grained recursion.
- `--output path.json` — write a machine-readable report (schema below).
- `--no-color` — plain ASCII output, even in a color-capable terminal.
- `--quiet` — suppress per-ply progress stream (still writes `--output`).
- `--max-plies N` (analyze only) — truncate after N plies, handy for smoke tests.

### JSON report schema

```jsonc
{
  "mode": "shallow" | "deep",
  "params": {
    "depth": 18, "multipv": 6,
    "recurse_depth": 0, "recurse_k": 3, "recurse_alpha": 0.5,
    "child_depth": 12, "max_plies": null
  },
  "plies": [                         // `analyze` only; `fen` reports use "volatility" + "fen" at the top level.
    {
      "ply": 1, "san": "e4",
      "fen_before": "...", "fen_after": "...",
      "eval_cp": 34,
      "volatility": {
        "score": 12.7, "raw_cp": 20.4, "local_raw_cp": 20.4,
        "best_eval_cp": 34, "alt_evals_cp": [10, -5, -20, -40, -60],
        "scale": 1.0, "decided": false, "reason": null,
        "recurse_depth_used": 0, "analyses": 1,
        "color": "low"
      }
    }
  ]
}
```

The `color` field maps the score through §3.6 thresholds (`low`/`medium`/`high`) and is
`null` whenever `score` is `null` (only-move, checkmate, stalemate).

### Calibration (Phase 2.5)

The eight tunable constants — `K_SHALLOW`, `EVAL_SCALE_GRACE`, `EVAL_SCALE_WIDTH`, `EVAL_SCALE_MAX`, `MATE_BASE`, `MATE_STEP`, `DECIDED_BEST_CP`, `DECIDED_ALT_CP` — were chosen to satisfy the worked examples in §3.7 by hand. To trust them globally, calibrate them against a labelled corpus.

The `chess_vol.calibrate` module + `scripts/calibrate.py` CLI separate the **slow** (Stockfish on a corpus) and **fast** (re-tuning constants against cached engine output) parts so you can iterate on the math without re-running Stockfish.

#### Workflow

```
# 1. Curate a labelled corpus. Starter set ships at:
#    tests/fixtures/calibration_corpus.json
#
# Each entry is {id, fen, label?, category?}. `label` is your 0-100 sharpness
# rating; `category` is one of "quiet", "middlegame", "sharp", "tactical",
# "endgame" (or any name you add to DEFAULT_TARGETS).

# 2. Run Stockfish once on the corpus. Slow — minutes for 20 positions, hours
#    for hundreds. Cached as JSON so you only do it once per (depth, multipv).
python scripts/calibrate.py dump tests/fixtures/calibration_corpus.json \
    --out cache/analyses.json --depth 18 --multipv 6

# 3. See where the *current* constants land:
python scripts/calibrate.py report tests/fixtures/calibration_corpus.json \
    --analyses cache/analyses.json
# → mean V per category, bucket distributions vs targets, expert MSE vs labels.

# 4. Tune. Three modes:
#    - expert         : minimise MSE between V and your hand labels
#    - distributional : minimise KL between observed bucket distribution and
#                       the per-category targets in chess_vol.calibrate.DEFAULT_TARGETS
#    - blended (default): both, weighted to balance their scales
python scripts/calibrate.py tune tests/fixtures/calibration_corpus.json \
    --analyses cache/analyses.json --mode blended --out tuned.json
# → prints a ready-to-paste replacement for chess_vol/config.py.

# 5. Re-report under the tuned constants and compare:
python scripts/calibrate.py report tests/fixtures/calibration_corpus.json \
    --analyses cache/analyses.json --constants tuned.json
```

#### Three calibration modes — when to use each

| Mode | Needs | What it optimises |
|---|---|---|
| **`expert`** | Hand-labelled positions (`label` set) | V matches your gut-feel sharpness rating per position |
| **`distributional`** | Categorised positions (`category` set) | Bucket shares per category match `DEFAULT_TARGETS` |
| **`blended`** (default) | Either or both | Weighted sum — robust when only some entries are labelled |

The blended mode is the recommended starting point. Expert-mode tuning is sharper but only constrains the constants where you have labels; distributional mode constrains the *shape* of the V distribution but not specific positions. Together they cover both.

#### Why the slow/fast split matters

The optimiser may evaluate the loss hundreds of times. Each evaluation is just a re-computation of V from the cached MultiPV results — no engine call. A 200-position corpus tunes in seconds. If we re-ran Stockfish in each iteration, the same tune would take days.

This works because **mate translation, eval-aware scaling, and normalisation are all post-hoc**: the engine output (cp / mate-in-N from STM POV) doesn't depend on the constants. Only the projection from that output to the 0-100 V score does. So we cache once, tune freely.

#### Recommended corpus shape

The starter set has 20 positions across 4 categories — enough to verify the pipeline works, not enough to trust the result globally. For real calibration, target **150-300 positions**:

- **40-60 quiet** — opening positions, dead-drawn endgames, simple K+P endings. Should sit ≥ 90% in the low (green) bucket.
- **40-60 middlegame** — master games sampled at random plies. Should distribute roughly 55% low, 35% medium, 10% high (the `middlegame` target).
- **40-60 sharp** — tactical middlegames, sacrificial attacks (Tal, Shirov, Topalov style). Should distribute roughly 20% low, 45% medium, 35% high.
- **20-40 tactical** — curated mate-in-N puzzles, only-move studies, swindle positions. Should land predominantly high.
- **10-20 endgame** — instructive endings (Lucena, Philidor, opposite-coloured bishops). Mixed target depending on character.

Label each by hand on a 0-100 scale. Disagreement between human raters is normal; you're not aiming for one true value, you're aiming for "the algorithm broadly agrees with human intuition."

#### Tuning order matters

The constants are coupled. If you must tune them in stages (rather than all-at-once via the optimiser), the order is:

1. **`MATE_STEP`** first — calibrate against the mate set so M1 vs M20 spread feels right.
2. **`EVAL_SCALE_WIDTH`** next — against the contrast between "winning with multiple paths" and "knife's-edge".
3. **`K_SHALLOW`** last — to land the global distribution shape.
4. **`DECIDED_BEST_CP` / `DECIDED_ALT_CP`** are mostly UX (they only affect bar dimming), tune by eye.

The optimiser does this jointly, which is fine for the blended loss. Stage tuning is for when you suspect the optimiser found a local minimum and want to seed it differently.

#### Deep mode

The current pipeline calibrates `K_SHALLOW` only. Deep mode (`recurse_depth=2`) re-uses the same shape but with `K_DEEP` and `recurse_alpha`. To calibrate those, extend `dump` to recursively cache the top-k child positions too — left as future work since it expands the cache by ~13× per position. Until then, `K_DEEP = K_SHALLOW` is a reasonable default.

---

## 8. Phase 3 — Local Web App

FastAPI single-file server:
- `POST /analyze/fen` → `{fen, depth, multipv, recurse_depth?, recurse_k?, child_depth?, deep?}` → `FenReportJson` (same schema as `chess-vol fen --output`).
- `POST /analyze/pgn` → `{pgn, deep?: bool, ...}` → Server-Sent Events stream. Event sequence: `start` (mode + params), one `ply` per ply (full `PlyJson`), `done` (plies + engine-call count), or `error` on failure. Critical for deep mode where analysis takes minutes.
- `GET /` → static frontend (served from `web/`).
- `GET /healthz` → simple liveness check (`{"status": "ok"}`).

### Frontend

Vanilla JS + `chessboard.js` + `chess.js` + `Chart.js` — all assets vendored locally under `web/vendor/` (no CDN, no internet required at runtime, no build step). One page, shared board:

- **Position editor (default tab):** drag-and-drop board with spare pieces; `dropOffBoard: 'trash'` for removal. FEN input is two-way bound to the board + side-to-move / castling / en-passant / halfmove / fullmove fields. "Clear board", "Starting position", "Flip board", "Analyze position" buttons. Auto-analyze toggle (debounced ~450 ms) re-runs as you edit.
- **Game (PGN) tab:** paste or upload a `.pgn`, "Load game" to populate a clickable move list, "Analyze game" to stream per-ply results into a Chart.js line chart (V + white-POV eval series). Click any chart point or move-list entry to scrub the board/eval-bar/vol-bar to that ply.
- **Standard eval bar + volatility bar side-by-side** next to the board in both tabs. Eval bar is bipolar white/black, mapped via `tanh(cp/400)`; mate scores pin to full. Volatility bar is unipolar 0–100 colored per §3.6, with a dashed split line in deep mode marking the `local` vs `reply` boundary (read from `local_raw_cp / raw_cp`). Bar dims when `decided=true`; renders "—" when `score=null`.
- **Deep-analysis checkbox** at the top — shared by both tabs, sends `deep=true` on every request.

CORS: allow `localhost` / `127.0.0.1` (any port) and `https://www.chess.com` (extension hits this server).

### Run it

```
pip install -e .[web]
export STOCKFISH_PATH=/path/to/stockfish   # or let auto-detection handle it
chess-vol serve                            # http://127.0.0.1:8000/
chess-vol serve --host 0.0.0.0 --port 9000 --reload
```

`chess-vol serve` is a thin wrapper over `uvicorn chess_vol.server:app`. The
frontend at `/` uses the same origin for its API calls, so no CORS configuration
is needed for local use.

---

## 9. Phase 4 — Chess.com Extension (one-ply only)

Manifest V3 Chrome extension. **No analysis in the extension** — extract FEN, POST to local server with `recurse_depth=0`, render returned V as a vertical bar next to chess.com's eval bar.

Deep mode is explicitly not supported. Latency makes it unusable live. Users wanting deep analysis use the web app or CLI.

**Content script responsibilities:**
1. Detect a chess.com analysis URL.
2. Locate the board + eval bar DOM. Chess.com changes its DOM periodically — use `MutationObserver` and isolate selectors into one function at the top of `content.js`.
3. Extract FEN: read the move list and reconstruct FEN with a bundled `chess.js`. Do not scrape board pixels.
4. Debounce 200 ms after each move.
5. Cache by FEN — never re-request the same position.
6. Inject `<div class="cvb-bar">` with a gradient fill reflecting V, plus a tooltip showing numeric V and raw cp drop.

**Options page:** server URL, depth, multipv. No recursion controls — force one-ply.

**Fallback:** if server unreachable, show "●" with tooltip pointing to README for starting the local server.

---

## 10. Non-Negotiable Requirements

- No hardcoded Stockfish path — auto-detect with env override.
- Engine always closed (context manager / `try/finally`). A leaked Stockfish process is a bug.
- Engine instances reused across recursive calls — never open a fresh process per recursion level.
- Type hints everywhere; `mypy --strict` on `src/chess_vol/` must pass.
- `ruff` + `black` configured in `pyproject.toml`.
- README with: install (incl. Stockfish), the §3.7 worked-examples table, and a plain-English "how the algorithm works" section covering both move-choice and reply volatility.
- Sensible defaults. `chess-vol analyze game.pgn` zero-flag produces a useful one-ply result. `--deep` is always opt-in.

---

## 11. Known Risks / Open Questions

Surface these to the user; don't silently decide.

1. **Eight interacting constants.** `K`, `EVAL_SCALE_GRACE`, `EVAL_SCALE_WIDTH`, `EVAL_SCALE_MAX`, `MATE_BASE`, `MATE_STEP`, `MATE_MAX_N`, decided thresholds. Hand-worked examples in §3.7 validate them locally but not globally. Phase 2 calibration against a real corpus is mandatory before locking defaults.
2. **Mate calibration specifically.** `MATE_STEP=50` means M1 vs M20 differ by 950 cp — roughly the value of a rook. That feels right (a mate-in-20 is "clearly winning" rather than "crushing") but should be validated against actual mate positions in Phase 2.
3. **Depth vs speed.** Depth 18 × multipv 6 ≈ 1–3 s per position → ~1–2 min for a 40-move game shallow. Deep mode (`recurse_depth=2, k=3, child_depth=12`) ~3–6 s per position → ~4–8 min per game. Document loudly.
4. **Side-to-move flip in recursion.** Primary Phase 1.5 bug risk. Test (§6 test 3) non-negotiable.
5. **Chess.com TOS and DOM.** Extension reads a page the user's already viewing and sends FEN to their own local server — standard for analysis extensions, but chess.com can break the scraper at any time.
6. **MultiPV cost.** MultiPV=6 at depth 18 is slower than single-line. If live use is sluggish, fall back to depth 14 for the extension.
7. **Recursion params unproven.** `recurse_k=3, alpha=0.5, child_depth=12` are educated starting points. All swappable (including weight and scale functions) for post-launch A/B testing.
8. **Budget explosion.** `recurse_depth=3` means 40 analyses per position — ~30 min per game. Gate behind `--experimental`.
9. **Scaling philosophy.** Dampening V in winning positions serves UX ("help me notice when I need to concentrate") but not every user will agree it's "correct." `scale_fn` is swappable so users who want raw unscaled volatility can have it.

---

## 12. Milestone Checklist

- [ ] **Phase 1:** `compute_volatility` works one-ply, all unit + integration tests pass.
- [ ] **Phase 1.5:** `recurse_depth > 0` works; composition + side-to-move + regression tests pass.
- [ ] **Phase 2:** CLI runs end-to-end on a PGN in both modes; `K_shallow`, `K_deep`, and all other constants tuned against a real corpus.
- [x] **Phase 2.5:** Calibration scaffolding — `chess_vol.calibrate` module + `scripts/calibrate.py` + starter labelled corpus + tests. Run `dump → tune → report` to lock in tuned constants.
- [x] **Phase 3:** FastAPI server (`chess-vol serve`) + drag-and-drop board editor; PGN paste/upload streams per-ply volatility via SSE; eval bar + volatility bar rendered side-by-side in both tabs.
- [x] **Explain panel:** `chess_vol.explain` produces a deterministic, structured "why this volatility?" explanation — patterns, components, summary — surfaced inline in the side panel under each tab and reachable by clicking the bar.
- [ ] **Phase 4:** Extension shows a live one-ply bar on chess.com, backed by local server.

Build sequentially. Do not start a phase until the previous is done and its tests pass.
