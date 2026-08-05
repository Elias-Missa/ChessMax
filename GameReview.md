clone this repo and use it as a base

[https://github.com/H0NEYP0T-466/ChessReviewEngine.git](https://github.com/H0NEYP0T-466/ChessReviewEngine.git)



# Chess Game Review Clone: Coding Specification and Developer Guide

## 1. Project Overview & Architecture

Your task is to build a local web application that precisely clones the modern [chess.com](http://chess.com) Game Review feature.

- **Backend Environment:** Python using the `python-chess` library and a local Stockfish executable via `chess.engine.SimpleEngine`.
- **Frontend Environment:** HTML/CSS/JS (React or Vanilla) implementing a responsive two-panel desktop layout (collapsing to a single column on mobile).
- **Core Logic Paradigm:** Do not evaluate human decisions using raw centipawns. The entire application logic is built on translating engine evaluations into "Expected Points" (Win Probability) to grade moves and calculate accuracy.

## 2. Mathematical Models

### Win Probability (Expected Points) Conversion

Convert the raw centipawn ($cp$) evaluation from Stockfish into a Win Probability percentage ($Win\%$) using the following logistic regression formula calibrated to human play:

$Win\% = 50 + 50 \times \left( \frac{2}{1 + e^{-0.00368208 \times cp}} - 1 \right)$

*Note: You must algorithmically clamp forced mate scores to extreme values (e.g., $+10000$ or $-10000$ centipawns) before passing them through this formula so the function resolves to $1.00$ or $0.00$.*

Calculate the Expected Points Delta ($\Delta EP$) caused by the human player's move:

$\Delta EP = Win\%_{before} - Win\%_{after}$

### CAPS2 Move & Game Accuracy Formula

Calculate individual move accuracy using an exponential decay function based on the Win% drop:

$Accuracy\% = 103.1668 \times e^{-0.04354 \times (\Delta EP)} - 3.1669$

**Aggregating Game Accuracy:**

Do not simply average the move accuracies. To calculate the final game accuracy:

1. Divide the game into sliding windows (e.g., chunks of 5-10 moves).
2. Compute the standard deviation of the $Win\%$ within each window to determine volatility weights.
3. Calculate the volatility-weighted mean of all move accuracies.
4. Calculate the harmonic mean of all move accuracies (to heavily penalize blunders).
5. The final accuracy score is the average of the volatility-weighted mean and the harmonic mean.

## 3. Move Classification Algorithmic Logic

Categorize standard moves strictly based on the $\Delta EP$ thresholds below:


|                    |                  |                  |                                         |
| ------------------ | ---------------- | ---------------- | --------------------------------------- |
| **Classification** | **Min ΔEP Loss** | **Max ΔEP Loss** | **Algorithmic Definition**              |
| Best               | $0.00$           | $0.00$           | Matches engine's top PV.                |
| Excellent          | $0.00$           | $0.02$           | Maintains near-optimal pressure.        |
| Good               | $0.02$           | $0.05$           | Solid move, minor probability loss.     |
| Inaccuracy         | $0.05$           | $0.10$           | Suboptimal strategic move.              |
| Mistake            | $0.10$           | $0.20$           | Poor decision altering game trajectory. |
| Blunder            | $0.20$           | $1.00$           | Devastating error or hanging material.  |


### Special Classification Heuristics

- **Brilliant (!!):** The move must be categorized as "Best" or "Excellent". The backend must simulate the sequence to verify a piece was sacrificed. The player's $Win\%$ must remain above $0.50$ after the sacrifice, and they must not have been completely dominating before the move. Sacrifice generosity scales inversely with Elo.
- **Great (!):** Requires two conditions: (1) The move swings the game from losing to equal, or equal to winning. (2) It is the absolute *only* non-losing move in the position (determined by checking the gap between the first and second engine lines).
- **Miss (x):** Triggered when the opponent's immediate previous move was a Mistake or Blunder, and the current player fails to capture the advantage, resulting in an equal or worse position.
- **Book:** Matches a local opening database (ECO table or DAG). Bypasses the Stockfish evaluation pipeline entirely.

## 4. Backend Implementation Specifications (Python)

Implement the asynchronous Engine loop using `python-chess`. You must set the UCI parameter `MultiPV` to at least 3. Without knowing the evaluation of the 2nd and 3rd best moves, you cannot mathematically verify a "Great" move or calculate game complexity for Elo estimation.

Here is the conceptual structure for the engine call:

Python

```
import chess
import chess.engine

async def evaluate_position(fen_string):
    # Initialize engine and set MultiPV for Great Move detection
    engine = chess.engine.SimpleEngine.popen_uci("path/to/stockfish")
    board = chess.Board(fen_string)
    
    # Run analysis at fixed depth (e.g., 18 or 20)
    info = await engine.analyse(
        board, 
        chess.engine.Limit(depth=18), 
        multipv=3
    )
    
    # Extract centipawns/mate scores for all PV lines here
    # Apply logistic regression math here
    
    engine.quit()
    return structured_json_response

```

*Optional Next-Gen Feature:* Integrate the Maia3 neural network engine alongside Stockfish to predict human moves. Run `maia3-uci` and pass parameters like `--elo 1500` to evaluate if a human blunder was statistically likely for their rating band.

## 5. Frontend UI/UX and Visual Specifications

**Layout Structure:**

- **Left Panel:** Contains the interactive Chessboard and the Evaluation Bar. The Evaluation bar fill height MUST be animated smoothly via CSS transitions and tied strictly to the $Win\%$ variable (0.00 to 1.00), not the raw centipawn score.
- **Right Panel:** Displays the player header (with Estimated Performance Elo), two SVG Accuracy Donut Charts (White and Black), a dynamically generated Coach textual feedback card, and a scrollable Move List Matrix.

**Design System (Colors & Icons):** Implement the following exact hex codes and typography for the move badges in the Move List, and map the RGBA alpha variants to highlight the origin and destination squares on the board:


|                         |            |              |                                  |
| ----------------------- | ---------- | ------------ | -------------------------------- |
| **Move Classification** | **Symbol** | **Hex Code** | **Background Highlight (Alpha)** |
| Brilliant               | !!         | `#1BACA6`    | `rgba(27, 172, 166, 0.4)`        |
| Great                   | !          | `#5C8BB0`    | `rgba(92, 139, 176, 0.4)`        |
| Best                    | ★          | `#7DB249`    | `rgba(125, 178, 73, 0.4)`        |
| Excellent               | 👍         | `#96BC4B`    | `rgba(150, 188, 75, 0.4)`        |
| Good                    | ✓          | `#A4BA65`    | `rgba(164, 186, 101, 0.4)`       |
| Book                    | 📖         | `#A88865`    | `rgba(168, 136, 101, 0.4)`       |
| Inaccuracy              | ?!         | `#E3AF35`    | `rgba(227, 175, 53, 0.4)`        |
| Mistake                 | ?          | `#CA6830`    | `rgba(202, 104, 48, 0.4)`        |
| Miss                    | ❌          | `#FF7769`    | `rgba(255, 119, 105, 0.4)`       |
| Blunder                 | ??         | `#B33430`    | `rgba(179, 52, 48, 0.4)`         |


