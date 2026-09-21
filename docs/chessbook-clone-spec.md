<!-- Extracted from the uploaded .docx (chessbook_opening_repertoire_clone_spec.docx),
     prepared 20 September 2026. Source of truth for the opening builder;
     see CLAUDE.md § 'Opening book: the Chessbook-style builder'. -->

# Chessbook-Style Opening Repertoire AppProduct reverse-engineering notes and Claude Code implementation brief
Prepared 20 September 2026 | Public-source and public-demo review
Purpose. This document gives Claude Code a precise, implementation-oriented target for an independent opening-repertoire trainer inspired by the publicly observable behavior of Chessbook. It describes product behavior, data structures, algorithms, screens, API boundaries, and acceptance tests. It does not reproduce Chessbook source code, private APIs, trademarks, artwork, or proprietary content.

## 1. Executive summary
The product is a personal opening book rather than a generic chess game. A player chooses the side and opening moves they want to play; the app turns those choices into an opening tree. At each position, it recommends candidate moves using master-game frequency, engine evaluation, and results for players near the user’s rating. The player drills the tree with spaced repetition, reviews coverage gaps, studies model games and middlegame plans, and can audit online games for deviations.
Product pillar
What the clone must do
Repertoire builder
Create a White/Black move tree from legal positions; branch at any opponent reply; annotate lines.
Evidence-led choice
Show popularity, engine score, peer results, opening name, and short move explanation.
Targeted training
Ask the learner to recall the repertoire move at a position; schedule weak nodes for review.
Coverage and gaps
Measure likely response space covered and prioritize missing branches.
Context after the opening
Provide model games and practical middlegame plans tied to resulting positions.
Game feedback
Import Lichess/Chess.com games and mark the first deviation from the saved repertoire.

## 2. Verified public behavior
Observed on chessbook.com’s public site and anonymous “Get started” walkthrough on 20 September 2026, then cross-checked against public iOS/Android listings. ‘Observed’ describes user-visible behavior; implementation choices later are recommendations.

### 2.1 Marketing and plan surface
- Landing page headline: “Your personal opening book.” The promise is a bulletproof repertoire built from likely lines.
- Public claims: custom repertoire, spaced repetition, automatic gaps, avoiding obscure moves, transpositions, online-game mistake finding, model games, middlegame plans, and a fast interface.
- Website starter copy showed a free move cap of 200; mobile listings currently describe free users up to 400 moves and Pro as unlimited. Make limits configurable, not hard-coded.
- The iOS listing showed monthly and annual Pro purchases; defer billing from the first vertical slice.

### 2.2 Anonymous onboarding walkthrough
Step
Observed UI/state
Implementation implication
1
Intro: create a repertoire; walkthrough covers adding first moves and practicing with spaced repetition.
Offer a no-account demo stored locally; make it skippable.
2
Ask current rating with ranges 0–1000 through 2500+.
Persist a rating band and use it to select opponent-response priors.
3
Ask platform: Chess.com, Lichess, FIDE, or USCF.
Persist rating source and normalize to an internal band.
4
Ask first White move. Rows show move, opening name, master %, engine eval, and peer results.
Render a position-aware candidate list; clicking adds a move to the tree.
5
After a move, ask what to play against it; show legal replies with evidence and descriptions.
Every node is a reusable decision point; branches are opponent replies followed by the user response.
6
“Show more” reveals candidates; “Something else…” permits an unlisted move.
Rank candidates but never block arbitrary legal moves.
7
“I’ll finish later, save my progress” is offered.
Support resumable local drafts and later account linking.
Important detail: the builder is not a flat list of openings. It is a position-by-position tree. The board and move list update as moves are selected, and the UI labels the current opening/variation.

### 2.3 Evidence shown for move selection
Field
Meaning in clone
Notes
Potential moves
Ranked legal candidates for current position.
Include Show more and legal-move fallback.
Masters %
Frequency in a master-game corpus.
Display sample size and corpus date.
Eval
Engine score, converted to a human-friendly label.
Show raw centipawns in details.
Peer results
Results for the selected rating band/platform.
Show W/D/L or score and sample size.
Opening / variation
Name from an ECO/opening taxonomy.
Names are metadata, not node identity.
Description
Short practical reason for a move when available.
Allow editable user notes.

### 2.4 Public app-listing signals
- iOS explicitly names transpositions, Lichess/Chess.com review, model games, spaced repetition, automatic gaps, and avoiding obscure moves.
- Android describes master stats, engine evaluation, rating-level results, middlegame plans, model games, and a free move cap.
- Public reviews mention a built-in engine, GM move frequency, variation names after several moves, similar games/tactics, and common-variation practice. Treat reviews as validation signals, not authoritative internals.

## 3. Product requirements for the independent clone

### 3.1 MVP scope
- Anonymous/local onboarding: rating band, platform, preferred side(s), initial move.
- Interactive board with legal move input and repertoire tree sidebar.
- Candidate panel with popularity, engine eval, peer results, opening name, description, and Show more.
- Create, edit, delete, rename, and annotate repertoire branches.
- Training mode: recall a repertoire move; grade Again / Hard / Good / Easy; schedule next review.
- Dashboard: due cards, coverage by root opening, weakest branches, recent accuracy.
- PGN import and first-deviation report for local games; add online connectors after MVP.

### 3.2 Post-MVP scope
- Transposition-aware node merging with path context.
- Model-game explorer filtered to games passing through a node.
- Middlegame plan cards: pawn breaks, piece placements, thematic tactics, and “how to play from here.”
- Cloud sync, account linking, push reminders, subscription limits, mobile packaging.

## 4. UX and screen specification
Screen
Layout and behavior
Acceptance criteria
Onboarding
Centered wizard, progress indicator, rating/platform selectors, skip/resume.
New user reaches a playable tree in under 2 minutes without signup.
Repertoire dashboard
Left: roots/coverage; center: board/tree; right: due cards/gaps.
Counts update immediately after edits and training.
Builder
Board, SAN line, candidate table, move details, add branch, notes.
Selecting a legal move creates one node and refreshes candidates for the new FEN.
Training
Board with hidden expected move, hints, answer feedback, next-card scheduling.
Wrong move shows expected move and explanation; illegal moves are rejected.
Coverage
Opening cards with score, due count, depth, missing high-probability replies.
Clicking a gap opens the exact position and candidate replies.
Game review
PGN list, move-by-move board, first-deviation marker, add-to-repertoire.
First deviation is deterministic and shows saved line vs played move.
Model games
Filter by opening node/player/year/result; guess-the-move mode.
Reveal/pause/continue never mutates repertoire.

## 5. Domain model and storage
Entity
Key fields
UserProfile
id, rating_band, rating_source, timezone, preferences, created_at
Repertoire
id, user_id, name, orientation, move_limit, created_at, updated_at
PositionNode
id, repertoire_id, fen, zobrist_key, ply, opening_code/name, parent_id, transposition_group_id
RepertoireEdge
id, node_id, child_node_id, san, uci, role, priority, note, source
EvidenceSnapshot
position_key, move_uci, corpus_type, master_count, peer_count, W/D/L, eval_cp, engine_depth, fetched_at
ReviewCard
user_id, node_id, due_at, stability, difficulty, reps, lapses, last_grade, last_reviewed_at
Game
user_id, source, external_id, pgn, played_at, result, opponent_rating
Deviation
game_id, ply, node_id, expected_moves, played_move, severity, created_at
ModelGame
id, pgn, metadata, indexed_positions
Store FEN as canonical position identity, not only SAN path. Use a transposition table keyed by a normalized position hash; retain each user path so the UI can explain how a position was reached.

## 6. Ranking, coverage, and training logic

### 6.1 Candidate ranking
Recommended deterministic score (tune with fixtures; do not present as Chessbook internals):
> rank_score = 0.45·log(1 + master_count) + 0.25·peer_score + 0.20·engine_quality + 0.10·recency_bonus − obscure_penalty
- master_count: frequency in a licensed/open corpus.
- peer_score: normalized score for the user band; shrink toward 0.5 when sample size is small.
- engine_quality: bounded transform of centipawn loss relative to best move.
- obscure_penalty: only when a move is rare and not materially better.

### 6.2 Coverage
For position p, let R(p) be likely opponent replies from the selected rating/corpus. Coverage(p) = weighted mass of replies for which the repertoire contains a response, capped at 100%. Aggregate as a weighted mean across reachable positions. Show both the percentage and top uncovered replies.

### 6.3 Spaced repetition
- Attach a review card to a user decision node, not an entire line.
- Use FSRS or a documented SM-2 variant with Again/Hard/Good/Easy grades.
- Select due cards first, then low stability, then high importance (frequency × depth × recent game exposure).
- On a miss, show expected/played move and evidence, then schedule a short-delay re-test.

### 6.4 Transpositions
- Parse moves with a chess rules library and compute FEN after each ply.
- Look up normalized position hash; link matches into one transposition group.
- Train in path context but aggregate mastery across equivalent positions.
- Never merge positions differing in side to move, castling, en-passant, or variant.

## 7. Technical architecture
Layer
Recommendation
Web UI
Next.js + TypeScript + Tailwind; responsive desktop/tablet layout; PWA shell.
Board
Chessground-compatible renderer; chess.js for legal moves, SAN, FEN, PGN.
API
Next.js route handlers or FastAPI; typed OpenAPI contract.
Database
PostgreSQL; JSONB evidence snapshots; indexes on user_id, repertoire_id, FEN hash, due_at.
Engine
Stockfish worker; cache FEN + engine version + depth/time.
Jobs
Queue for corpus ingestion, engine analysis, game sync, model-game indexing.
Auth/sync
Anonymous device ID first; optional account linking later.
Observability
Events: move_selected, branch_created, review_graded, import_completed, deviation_found.

## 8. API contract sketch
Method
Endpoint
Purpose
POST
/api/repertoires
Create repertoire with orientation and calibration.
GET
/api/repertoires/:id/tree
Return nodes/edges, coverage, and due summaries.
GET
/api/positions/:fen/candidates
Return ranked legal moves and evidence.
POST
/api/repertoires/:id/edges
Add/update a selected repertoire branch.
POST
/api/reviews/session
Create prioritized training session.
POST
/api/reviews/:card/grade
Grade card and return next_due_at/feedback.
POST
/api/games/import
Parse PGN(s) and compute first deviations.
GET
/api/positions/:fen/model-games
Return model games and plan cards.

## 9. Build sequence for Claude Code
- Scaffold web app, schema, chess rules service, board/tree shell, and original branding.
- Implement anonymous onboarding and local persistence with a licensed fixture corpus.
- Implement builder loop: FEN → candidates → click move → edge → new FEN.
- Add evidence cards and opening taxonomy; show sample sizes and freshness.
- Add coverage/gap computation with weighted-reply unit tests.
- Add review cards, FSRS/SM-2 scheduling, session UI, grading, dashboard.
- Add PGN import/deviation review, then model-game explorer and plan notes.
- Only after product fit: cloud sync, online connectors, billing, mobile, reminders.

## 10. Acceptance tests
Test
Expected result
Legal move integrity
Every candidate/user move is legal; SAN/UCI round-trip.
Branching
Two opponent replies create two child paths without overwriting.
Transposition
Equivalent FEN reached by two move orders resolves to one group.
Evidence fallback
Missing data shows insufficient sample and still permits legal moves.
Coverage
Adding a response increases coverage by exact weighted mass, never above 100%.
Review scheduling
Grades produce deterministic next dates under a frozen clock and algorithm version.
First deviation
PGN reports first ply where played move is absent from saved repertoire.
Offline
Loaded training cards can be reviewed offline and sync without duplicate grades.
Privacy
No external account data is sent until the user explicitly connects a provider.

## 11. Risks, boundaries, and design decisions
- Do not copy Chessbook’s name, logo, screenshots, CSS, verbatim copy, proprietary datasets, or private endpoints. Use an original identity and licensed/open chess data.
- “Exact” means exact user-visible behavior, not private implementation. Keep a source note for observed behavior and label inferred algorithms as recommendations.
- Engine scores and statistics are volatile: cache with version/date and show uncertainty for small samples.
- Move explanations can be wrong: store source/confidence, allow edits, and do not imply engine evaluation is a human explanation.
- Online integrations require OAuth, provider terms, rate limits, token encryption, deletion, and explicit consent; defer until local PGN review works.

## 12. Source notes
Public sources consulted (accessed 20 September 2026):
- https://chessbook.com/
- https://apps.apple.com/us/app/chessbook-master-openings/id6466343415
- https://play.google.com/store/apps/details?id=com.chessbook.android
- https://www.chessable.com/discussion/thread/1107907/chessbook-chessable/
Observed walkthrough notes: anonymous public website flow; selected a rating band and Lichess only to inspect the public builder. No account was created, no private data was entered, and no external account was connected.

