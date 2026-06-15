# Chess Trainer — Project Spec

A self-hosted chess training web app for a small group of friends (rated ~1200–2000).

## Core idea

Standard puzzle training has a fatal flaw: the user *knows* a tactic exists, so they stop deciding whether to calculate and just hunt for the win. This app fixes that by mixing tactical puzzles with **quiet positions** — positions where no tactic exists and the correct answer is to play any reasonable move. The user has to genuinely decide: is there something here, or do I just play solid?

The UI must show these identically. No tell, ever.

Optionally, after any puzzle, the user can play the position out against **Maia** (human-like neural net engine) and try to convert.

---

## Scope

- Local web app, runs on one Windows machine.
- **Single user.** No accounts, no login. The server auto-provisions one default user row to hold rating + attempt history. *(Superseded: email/password accounts were added later — each user has isolated data, and the first account claims this default row. See README.md / CLAUDE.md. The rest of this spec still describes per-user behavior; the data was already `user_id`-scoped.)*
- Two openings supported at launch: **London** (White) and **Caro-Kann** (Black).
- No LLM features, no clock, no mobile UI, no anti-cheat.

---

## Tech stack

- **Python 3.11+** backend
- **python-chess** for board logic and UCI engine communication
- **Stockfish** (latest) for analysis and grading
- **lc0 + Maia weights** for human-like play-out
- **SQLite** for storage
- **FastAPI** for the server
- **chessground** (Lichess's board library) for the frontend
- Vanilla HTML/JS frontend, no framework needed

---

## Position pool: three streams

### Stream 1: General tactical puzzles (Lichess puzzle DB)

Direct CSV import, **no Stockfish analysis needed** — trust the existing ratings, themes, and solutions.

- Source: https://database.lichess.org/#puzzles
- CSV columns: `PuzzleId, FEN, Moves, Rating, RatingDeviation, Popularity, NbPlays, Themes, GameUrl, OpeningFamily, OpeningVariation`
- Filter on import:
  - Keep all puzzles with rating between 1000 and 2400 (matches user band with margin)
  - Keep all themes
- Tag in DB: `source='lichess_puzzle'`, `classification='tactical'`, `opening_tag` = `OpeningFamily` (may be NULL — only set for puzzles before move 20)

### Stream 2: Opening-flavored tactical puzzles

These are **a subset of Stream 1**, not separate data. The puzzles where `OpeningFamily` matches London or Caro-Kann are flagged via the `opening_tag` column. The selection algorithm uses this tag to bias toward selected openings.

**Opening flavor is tactical-only.** Quiet positions (Stream 3) are not tagged by opening — opening practice comes entirely from this stream.

### Stream 3: Quiet positions (pipeline-generated)

This is the only stream that requires the offline Stockfish pipeline. Quiet positions are **opening-agnostic** — no ECO filter, no tagging.

**Source data:** one recent month of Lichess games database (https://database.lichess.org/, the PGN dump). Stream-parse with python-chess.

**Filtering games:**
- Both players rated 1500–2200
- Game length ≥ 25 moves
- Any opening
- Stop streaming once you have enough quiet positions (target ~5000)

**Sampling positions per game:** at most **one quiet position per game**. Algorithm:

```
for each game:
    pick a random move-range window from [(12,15), (18,22), (25,30)]
    candidates = []
    for each ply where the just-completed full-move number is in window:
        fen = position after that ply  # yields BOTH colors-to-move
        analysis = stockfish.analyze(fen, depth=12, multipv=5)
        if is_quiet_position(analysis):
            candidates.append((fen, analysis))
    if candidates:
        pick one at random
        insert into positions table
    # if nothing qualified, move on — do NOT try other windows in this game
```

**Both colors to move.** Each window N yields up to 2 positions per move number (after White's ply, after Black's ply). This keeps the mined pool balanced — half the puzzles are Black-to-move.

**Never sample before move 12.** Earlier positions are still in opening book; they pass the quiet criterion trivially but they're not real decision-making positions.

**One position per game.** Multiple positions from the same game share too much structure to be useful as independent puzzles.

**Random window per game.** Rotating across the three windows ensures even coverage of early/mid/late middlegame without correlating positions within games.

Skip positions where:
- Only one legal move (forced)
- Material already lopsided (existing eval beyond ±400 centipawns)

**Quiet position criterion (both rules must hold):**
- All top 3 moves evaluate within −100 to +100 centipawns
- At least 5 legal moves evaluate within −150 to +150 centipawns (ensures real breadth of choice, not a forced sequence)

**Estimated rating for quiet positions:** start everyone at 1500 and let the rating system adjust over time. No heuristic difficulty estimate needed for MVP.

---

## Pool composition target

- ~4M general tactical puzzles (Stream 1 — direct import, essentially free)
- ~5000 quiet positions (Stream 3, opening-agnostic)

Lichess opening-tagged tactical puzzles naturally yield tens of thousands for London (~18k) and Caro-Kann (~48k) within Stream 1 — no extra work needed for the opening-tactical stream.

---

## Puzzle selection algorithm

When the user requests a puzzle:

**Base mix (no openings selected):**
- 50% tactical (from Stream 1, no opening tag, rating ±200 of user)
- 50% quiet (from Stream 3)

**With openings selected (London and/or Caro-Kann):**
- 40% tactical general (Stream 1, no opening tag)
- 20% tactical opening-tagged (Stream 1 filtered to selected openings)
- 40% quiet (Stream 3 — opening-agnostic)

This gives roughly **60/40 tactical/quiet** overall when openings are active.

**Fallback chain:** if the rolled bucket is empty (no candidates after the recent-attempt exclusion), fall through to the next bucket. Order: `tactical_general → quiet → tactical_opening`.

**Within each bucket:** exclude positions the user attempted in their last 50 attempts. Random selection from candidates.

**Critical:** the API response is identical regardless of classification. Same fields, same shape. The user never knows what they're getting until after they move.

---

## Grading

Tactical and quiet puzzles use **different** grading. They share the same response shape (so the "no tell" rule still holds *before* the user moves), but the back-end logic diverges.

### Tactical puzzles — step-driven solution match (no Stockfish)

Each Lichess puzzle's `solution_moves` is a UCI sequence alternating user-move / opponent-forced-response. The client tracks a `step` index (0, then 2, then 4, …) and submits one user move per `/attempt` POST. The server replays `solution_moves[0..step]` on the puzzle FEN to reach the current position, then validates the user move against `solution_moves[step]` and returns one of four statuses:

| status | when | side effects |
|---|---|---|
| `continue` | user move matches AND there's still another user move to play after the opponent's forced reply | none — no rating delta, no `attempts` row |
| `solved` | user move matches AND no further user moves remain | rating +Δ, one `attempts` row |
| `failed` | user move ≠ `solution_moves[step]` | rating −Δ, one `attempts` row |
| `graded` | quiet only | rating ±Δ, one `attempts` row |

On `continue` / `solved` the server also returns `opponent_move` (SAN) + `opponent_move_uci` if the puzzle's next ply is an opponent response, so the client can auto-play it with animation + sound (~500ms delay after the user move lands). On `failed` the server returns the full intended `solution_line` (SAN) from the puzzle start, so the user sees what they missed.

Stockfish is **never** invoked for tactical attempts.

### Quiet positions — Stockfish eval loss, top 3 lines

1. Apply move to position, get resulting FEN.
2. Original-position analysis at depth 18, **multipv 3** — surfaces the top 3 candidates with eval and PV (returned as `top_lines` for display).
3. Resulting-position analysis at depth 18, multipv 1 → eval **from opponent's POV**, flip sign to user's POV.
4. `eval_loss = best_eval - user_eval_after` (where `best_eval` is the original-position multipv-1 eval).
5. Grade:

| eval_loss (cp) | grade        | rating delta direction |
|----------------|--------------|------------------------|
| ≤ 10           | best         | full positive          |
| ≤ 30           | good         | most positive          |
| ≤ 100          | acceptable   | small positive         |
| ≤ 150          | inaccuracy   | small negative         |
| ≤ 300          | mistake      | medium negative        |
| > 300          | blunder      | large negative         |

Pass threshold is 100cp. Within that, tiered rewards. Multiple moves stay inside 100cp loss for a quiet position, which is correct — many reasonable moves should pass.

**Mate puzzles** are tactical (`mateInN`) and follow the step-driven flow: the user has to find the mate sequence move by move. There's no special-case "accept any mate-in-≤N" because the Lichess `solution_moves` already encodes the intended line.

**Rating math:** simple Elo with K=20 for the user, K=10 for the position. Each position has its own rating (start tactical positions from their Lichess `Rating`; start quiet positions at 1500). After each attempt:
- `expected = 1 / (1 + 10^((position_rating - user_rating) / 400))`
- `actual = 1 if eval_loss ≤ 100 else 0`
- `user_rating += K_user * (actual - expected)`
- `position_rating += K_position * (expected - actual)`

---

## Maia play-out

After any puzzle that didn't end in mate:

1. Show "Play it out" button
2. User picks Maia rating (default: closest of 1100/1300/1500/1700/1900 to user's puzzle rating)
3. Play continues from the position resulting after the user's move (whether they passed or failed — they can still practice)
4. User vs. Maia, standard play
5. Game ends on mate, resignation by eval threshold (eval has been ≥ 5 against the losing side for 3 moves), or user clicks "End"
6. Record result, PGN, and Maia rating in `playouts` table

**Implementation:** Maia is an lc0 network. Run lc0 with `--weights=maia-XXXX.pb.gz`. python-chess talks to lc0 via UCI exactly like Stockfish.

---

## Database schema

```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    fen TEXT NOT NULL,
    side_to_move TEXT NOT NULL,             -- 'w' or 'b'
    source TEXT NOT NULL,                   -- 'lichess_puzzle' | 'pipeline_quiet'
    classification TEXT NOT NULL,           -- 'tactical' | 'quiet'
    opening_tag TEXT,                       -- 'london' | 'caro-kann' | NULL
    best_move TEXT NOT NULL,                -- UCI
    best_eval REAL NOT NULL,                -- centipawns, user POV
    solution_moves TEXT,                    -- full solution for tactical (UCI, space-separated); NULL for quiet
    themes TEXT,                            -- comma-separated, from Lichess for tactical; NULL for quiet
    rating INTEGER NOT NULL,                -- position rating
    rating_deviation INTEGER,               -- from Lichess for tactical; NULL for quiet
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_positions_classification ON positions(classification);
CREATE INDEX idx_positions_opening ON positions(opening_tag);
CREATE INDEX idx_positions_themes ON positions(themes);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    rating INTEGER DEFAULT 1500,
    selected_openings TEXT,                 -- JSON: ["london", "caro-kann"]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE attempts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    user_move TEXT NOT NULL,
    eval_loss REAL NOT NULL,
    grade TEXT NOT NULL,
    user_rating_before INTEGER NOT NULL,
    user_rating_after INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX idx_attempts_user ON attempts(user_id);

CREATE TABLE playouts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    maia_rating INTEGER NOT NULL,
    result TEXT NOT NULL,                   -- 'win' | 'loss' | 'draw'
    pgn TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## API

There are no `user_id` parameters in requests. *(Updated: the server resolves the authenticated user from the `chessmax_session` cookie via `server/deps.py:current_user`; routes are gated by login. Originally this resolved a single auto-provisioned user.)*

```
GET  /api/user                           -> { rating, selected_openings }

GET  /api/openings                       -> { selected: [...], available: ["london", "caro-kann"] }
PUT  /api/openings                       { openings: ["london"] } -> { selected: [...] }

GET  /api/puzzle/next                    -> { position_id, fen, side_to_move }
                                            (NEVER includes classification)

POST /api/puzzle/{id}/attempt            { move, step }
                                          -> { status: "continue" | "solved" | "failed" | "graded",
                                               position_classification,
                                               user_rating_after,
                                               solved,
                                               -- continue / solved (when there's an opp ply):
                                               opponent_move, opponent_move_uci,
                                               -- continue:
                                               next_step,
                                               -- terminal (solved | failed | graded):
                                               grade, eval_loss,
                                               best_move, solution_line, can_play_out,
                                               -- graded (quiet) only:
                                               top_lines: [{move_san, eval_cp, pv_san}, ...] }

POST /api/playout/start                  { position_id, maia_rating }
                                          -> { playout_id, fen }
POST /api/playout/{id}/move              { move }
                                          -> { maia_move, fen, status }
POST /api/playout/{id}/end               -> { final_pgn, result }

GET  /api/stats                          -> stats breakdown (see below)
```

---

## Stats view

Per user, show:

- Current rating + rating chart over time
- Overall accuracy
- **Quiet-position accuracy** (dedicated stat, prominently displayed — this is the unique skill)
- Tactical accuracy
- Accuracy by theme (fork, pin, mateInN, etc. — from the `themes` field of attempted tactical puzzles)
- Accuracy by opening (for opening-tagged positions attempted)

---

## Frontend

Single-page app, three views accessible from a top nav:

1. **Train** (default) — chessground board, side-to-move indicator, move input. After a move: feedback panel with eval loss, best move, solution line, "Play it out" button if applicable, "Next" button.
2. **Play out** — board against Maia, no eval bar (would telegraph the position).
3. **Stats** — rating chart and breakdowns.

Header has a settings dropdown for selecting openings.

**Visual design:** clean, dark theme, Lichess-inspired. Use chessground for the board, identical interaction to Lichess puzzle training. Critically: every puzzle looks exactly the same. No badges, banners, or hints that differentiate tactical from quiet positions before the move is played.

**Lichess parity components (built on lila's open-source assets):**
- Sounds — `Move.mp3`, `Capture.mp3`, `Check.mp3`, `GenericNotify.mp3` from lichess1.org's standard set, served from `frontend/sounds/`.
- Arrows + circles — chessground's built-in `drawable` (right-click drag for arrows, right-click for circles).
- Openings settings — header checkboxes for London / Caro-Kann, persisted via `/api/openings`.

---

## Project structure

```
chess-trainer/
├── README.md
├── requirements.txt
├── config.yaml                  # paths to engines, weights, DB
├── data/
│   ├── trainer.db
│   ├── stockfish.exe
│   ├── lc0.exe
│   ├── maia_weights/            # maia-1100.pb.gz, etc.
│   └── raw/                     # downloaded Lichess data
├── pipeline/
│   ├── import_puzzles.py        # Stream 1: CSV -> DB
│   ├── mine_quiet.py            # Stream 3: games -> quiet positions -> DB
│   └── run_all.py
├── server/
│   ├── main.py                  # FastAPI app
│   ├── engine.py                # Stockfish wrapper
│   ├── maia.py                  # lc0 wrapper
│   ├── grading.py
│   ├── selection.py             # puzzle selection algorithm
│   ├── stats.py
│   └── db.py
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── sounds/                  # Move.mp3, Capture.mp3, Check.mp3, GenericNotify.mp3 (lichess1.org standard set)
└── tests/
```

---

## Build order

Do these strictly in sequence. Each milestone is independently testable.

### M1: Stockfish wrapper and quiet-position classifier
`server/engine.py` with functions:
- `analyze(fen, depth=20, multipv=5) -> { top_moves: [{move, eval}, ...] }`
- `is_quiet_position(analysis) -> bool` (top 3 within ±100cp AND ≥5 legal moves within ±150cp)
- `grade_move(fen, user_move, best_eval) -> { eval_loss, grade }`

Test from CLI with a handful of FENs you find by hand. Verify quiet detection on real quiet positions and rejection of tactical ones.

### M2: Pipeline — import Lichess puzzles (Stream 1)
`pipeline/import_puzzles.py`. Download the puzzle CSV, stream-parse, bulk insert into `positions`. ~half-gig file, runs in a few minutes. Use the `OpeningFamily` column to tag London / Caro-Kann puzzles.

Verify: query the DB for puzzles tagged Caro-Kann and confirm you get a reasonable number.

### M3: Pipeline — mine quiet positions (Stream 3)

`pipeline/mine_quiet.py`. Opening-agnostic. Process games from one month's PGN dump until target count reached.

**Sampling rules (critical):**
- Never sample positions before move 12 (still in opening book).
- For each game, pick **one random move-range window** from `(12,15)`, `(18,22)`, `(25,30)`.
- Yield positions after each ply within the window — both colors-to-move, so the pool isn't White-biased.
- If any candidate passes the quiet criterion, pick **exactly one** at random and insert.
- If none qualified, skip the game entirely — do not try other windows.
- **One quiet position per game maximum.**

**Stockfish settings for mining:** depth 12, multipv 5, threads 8 (or whatever the host has). Depth 12 is plenty for the coarse quiet predicate (top-3 within ±100cp).

**Manual review step:** before generating the full pool, run on a small sample and **eyeball 20–30 generated quiet positions**. Do they feel quiet? Are there any with obvious tactics that slipped through? If even a few are bad, tighten the criterion before running on more games.

### M4: Minimal API + frontend
- FastAPI server with the puzzle and attempt endpoints (single-user, no login).
- One-page frontend with chessground. First puzzle loads on page load; user plays moves, sees feedback.
- No openings selection yet, no stats, no play-out. Just the core loop.

This is where you confirm the end-to-end experience works and feels right.

### M5: Openings selection + Lichess polish
- Settings UI: header checkboxes to toggle London / Caro-Kann, persisted via `/api/openings`.
- Update selection algorithm to apply the 40/20/40 opening-weighted mix.
- Add Lichess sound effects (`Move`, `Capture`, `Check`, `GenericNotify`) and chessground's `drawable` for right-click arrows + circles.

### M6: Maia play-out
- Install lc0, download Maia weights.
- `server/maia.py` wrapper.
- Play-out API + frontend view.

### M7: Stats
- Compute breakdowns from `attempts` joined with `positions`.
- Stats view with rating chart, quiet accuracy stat prominent.

---

## Windows setup notes

- Install Python 3.11+ from python.org
- Stockfish: download from stockfishchess.org, drop the `.exe` in `data/`
- lc0: download from lczero.org, install, note path
- Maia weights: download `maia-1100.pb.gz` through `maia-1900.pb.gz` from https://github.com/CSSLab/maia-chess/releases, put in `data/maia_weights/`
- `pip install python-chess fastapi uvicorn pydantic aiosqlite pyyaml`
- Run: `uvicorn server.main:app --host 0.0.0.0 --port 8000`
- Friends connect to `http://<your-LAN-IP>:8000`

---

## Out of scope

- LLM explanations (maybe later)
- Clock / time pressure
- Mobile UI
- Real auth
- Spaced repetition
- Endgame-specific position source
- Cross-game duplicate detection
- Additional openings (trivial to add later — extend the ECO filter list and re-run M3)
