"""Chess.com-style expected-points move review and game summaries."""

from __future__ import annotations

import io
import math
import statistics
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import chess
import chess.pgn

if TYPE_CHECKING:
    from chess_vol.analyze import PlyResult


ReviewLabel = Literal[
    "brilliant",
    "great",
    "best",
    "excellent",
    "good",
    "book",
    "inaccuracy",
    "mistake",
    "miss",
    "blunder",
]

REVIEW_SYMBOLS: dict[ReviewLabel, str] = {
    "brilliant": "!!",
    "great": "!",
    "best": "★",
    "excellent": "👍",
    "good": "✓",
    "book": "📖",
    "inaccuracy": "?!",
    "mistake": "?",
    "miss": "❌",
    "blunder": "??",
}

REVIEW_COLORS: dict[ReviewLabel, str] = {
    "brilliant": "#1BACA6",
    "great": "#5C8BB0",
    "best": "#7DB249",
    "excellent": "#96BC4B",
    "good": "#A4BA65",
    "book": "#A88865",
    "inaccuracy": "#E3AF35",
    "mistake": "#CA6830",
    "miss": "#FF7769",
    "blunder": "#B33430",
}

_OPENING_LINES = (
    "e2e4 e7e5 g1f3 b8c6 f1b5",
    "e2e4 c7c5 g1f3 d7d6 d2d4 c5d4",
    "e2e4 e7e6 d2d4 d7d5",
    "e2e4 c7c6 d2d4 d7d5",
    "d2d4 d7d5 c2c4 e7e6",
    "d2d4 g8f6 c2c4 g7g6",
    "d2d4 g8f6 c2c4 e7e6",
    "d2d4 d7d5 g1f3 g8f6 c1f4",
    "c2c4 e7e5 b1c3 g8f6",
    "g1f3 d7d5 d2d4 g8f6",
)


def _opening_prefixes() -> set[tuple[str, ...]]:
    prefixes: set[tuple[str, ...]] = set()
    for line in _OPENING_LINES:
        moves = tuple(line.split())
        prefixes.update(moves[:idx] for idx in range(1, len(moves) + 1))
    return prefixes


_BOOK_PREFIXES = _opening_prefixes()


# Curated ECO book for opening-name detection. Each entry is
# ``(uci_sequence, eco, name)``; the longest sequence that is a prefix of the
# played moves wins. This is intentionally small and recognizable rather than a
# full ECO database — PGN ``[Opening]`` headers are preferred when present.
_OPENING_BOOK: tuple[tuple[str, str, str], ...] = (
    # 1.e4 e5
    ("e2e4 e7e5 g1f3 b8c6 f1b5 a7a6", "C68", "Ruy López: Morphy Defense"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5", "C60", "Ruy López"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5", "C50", "Italian Game: Giuoco Piano"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 g8f6", "C55", "Italian Game: Two Knights Defense"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4", "C50", "Italian Game"),
    ("e2e4 e7e5 g1f3 b8c6 d2d4", "C44", "Scotch Game"),
    ("e2e4 e7e5 g1f3 g8f6", "C42", "Petrov's Defense"),
    ("e2e4 e7e5 g1f3 d7d6", "C41", "Philidor Defense"),
    ("e2e4 e7e5 b1c3", "C25", "Vienna Game"),
    ("e2e4 e7e5 f2f4", "C30", "King's Gambit"),
    ("e2e4 e7e5 f1c4", "C23", "Bishop's Opening"),
    ("e2e4 e7e5", "C20", "King's Pawn Game"),
    # 1.e4 c5 (Sicilian)
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6", "B90", "Sicilian: Najdorf"),
    ("e2e4 c7c5 g1f3 b8c6 d2d4 c5d4 f3d4 g7g6", "B34", "Sicilian: Accelerated Dragon"),
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 g7g6", "B70", "Sicilian: Dragon"),
    ("e2e4 c7c5 g1f3 e7e6", "B40", "Sicilian: French Variation"),
    ("e2e4 c7c5 g1f3 b8c6", "B30", "Sicilian: Old Sicilian"),
    ("e2e4 c7c5 g1f3 d7d6", "B50", "Sicilian Defense"),
    ("e2e4 c7c5 b1c3", "B23", "Sicilian: Closed"),
    ("e2e4 c7c5", "B20", "Sicilian Defense"),
    # 1.e4 e6 / c6 / d5 / d6 / g6 / Nf6
    ("e2e4 e7e6 d2d4 d7d5 b1c3", "C10", "French Defense: Paulsen"),
    ("e2e4 e7e6 d2d4 d7d5 e4e5", "C02", "French Defense: Advance"),
    ("e2e4 e7e6 d2d4 d7d5 e4d5", "C01", "French Defense: Exchange"),
    ("e2e4 e7e6", "C00", "French Defense"),
    ("e2e4 c7c6 d2d4 d7d5 e4e5", "B12", "Caro-Kann: Advance"),
    ("e2e4 c7c6 d2d4 d7d5 b1c3", "B15", "Caro-Kann Defense"),
    ("e2e4 c7c6 d2d4 d7d5 e4d5", "B13", "Caro-Kann: Exchange"),
    ("e2e4 c7c6", "B10", "Caro-Kann Defense"),
    ("e2e4 d7d5", "B01", "Scandinavian Defense"),
    ("e2e4 d7d6", "B07", "Pirc Defense"),
    ("e2e4 g7g6", "B06", "Modern Defense"),
    ("e2e4 g8f6", "B02", "Alekhine's Defense"),
    ("e2e4 b7b6", "B00", "Owen's Defense"),
    ("e2e4", "B00", "King's Pawn Opening"),
    # 1.d4
    ("d2d4 d7d5 c2c4 e7e6", "D30", "Queen's Gambit Declined"),
    ("d2d4 d7d5 c2c4 c7c6", "D10", "Slav Defense"),
    ("d2d4 d7d5 c2c4 d5c4", "D20", "Queen's Gambit Accepted"),
    ("d2d4 d7d5 c2c4", "D06", "Queen's Gambit"),
    ("d2d4 g8f6 c2c4 g7g6 b1c3 f8g7 e2e4", "E70", "King's Indian Defense"),
    ("d2d4 g8f6 c2c4 g7g6 b1c3 d7d5", "D80", "Grünfeld Defense"),
    ("d2d4 g8f6 c2c4 g7g6", "E60", "King's Indian Defense"),
    ("d2d4 g8f6 c2c4 e7e6 b1c3 f8b4", "E20", "Nimzo-Indian Defense"),
    ("d2d4 g8f6 c2c4 e7e6 g1f3 b7b6", "E12", "Queen's Indian Defense"),
    ("d2d4 g8f6 c2c4 e7e6", "E00", "Indian Game"),
    ("d2d4 g8f6 c2c4 c7c5", "A15", "Benoni Defense"),
    ("d2d4 g8f6 c1g5", "A45", "Trompowsky Attack"),
    ("d2d4 g8f6 g1f3", "A46", "Indian Game"),
    ("d2d4 g8f6", "A45", "Indian Game"),
    ("d2d4 d7d5 c1f4", "D00", "London System"),
    ("d2d4 d7d5 g1f3 g8f6 c1f4", "D02", "London System"),
    ("d2d4 f7f5", "A80", "Dutch Defense"),
    ("d2d4 d7d5", "D00", "Queen's Pawn Game"),
    ("d2d4", "A40", "Queen's Pawn Opening"),
    # Flank
    ("c2c4 e7e5", "A20", "English: Reversed Sicilian"),
    ("c2c4 g8f6", "A15", "English: Anglo-Indian"),
    ("c2c4", "A10", "English Opening"),
    ("g1f3 d7d5 d2d4 g8f6", "D02", "Queen's Pawn Game"),
    ("g1f3", "A04", "Réti Opening"),
    ("b2b3", "A01", "Nimzo-Larsen Attack"),
    ("g2g3", "A00", "King's Fianchetto Opening"),
    ("f2f4", "A02", "Bird's Opening"),
)


def _opening_index() -> dict[tuple[str, ...], tuple[str, str]]:
    return {tuple(line.split()): (eco, name) for line, eco, name in _OPENING_BOOK}


_OPENING_INDEX = _opening_index()
_OPENING_MAX_PLIES = max((len(key) for key in _OPENING_INDEX), default=0)


def detect_opening(
    results: list[PlyResult], headers: object | None = None
) -> dict[str, object] | None:
    """Identify the game's opening (spec §0: chess.com-parity).

    Prefers the PGN ``[Opening]``/``[ECO]`` headers when the game carries them
    (lichess/chess.com exports do); otherwise falls back to a longest-prefix
    match against the curated built-in book. Returns ``None`` when nothing
    matches (e.g. an irregular first move outside the book).
    """

    if headers is not None:
        get = getattr(headers, "get", None)
        if callable(get):
            name = str(get("Opening", "") or "").strip()
            eco = str(get("ECO", "") or "").strip()
            variation = str(get("Variation", "") or "").strip()
            if name and name != "?":
                if variation and variation != "?" and variation.lower() not in name.lower():
                    name = f"{name}: {variation}"
                return {"name": name, "eco": eco or None, "source": "headers"}

    played = [result.move_uci for result in results if result.move_uci]
    upper = min(len(played), _OPENING_MAX_PLIES)
    for length in range(upper, 0, -1):
        entry = _OPENING_INDEX.get(tuple(played[:length]))
        if entry is not None:
            eco, name = entry
            return {"name": name, "eco": eco, "source": "book"}
    return None


def compute_key_moments(
    results: list[PlyResult], *, limit: int = 4, min_swing: float = 0.10
) -> list[dict[str, object]]:
    """Pick the game's turning points (spec §0: chess.com-parity "key moments").

    A moment scores on how much expected value the move shed, with a bonus when
    it flipped who was winning (crossed the 0.5 line). Returns up to ``limit``
    moments sorted by impact, each carrying the 0-based ply ``index`` the review
    UI uses to navigate. Empty when the game had no move above ``min_swing``.
    """

    scored: list[tuple[float, dict[str, object]]] = []
    for idx, result in enumerate(results):
        review = result.review
        if review is None:
            continue
        swing = review.expected_points_loss
        before = review.expected_points_before
        after = review.expected_points_after
        # A genuine lead change straddles 0.5 on both sides by a margin — a
        # 0.50→0.49 nudge from an equal position is not a turning point.
        lead_change = (before - 0.5) * (after - 0.5) < 0 and (
            min(abs(before - 0.5), abs(after - 0.5)) >= 0.05
        )
        score = swing + (0.15 if lead_change else 0.0)
        if score < min_swing:
            continue
        side = "white" if chess.Board(result.fen_before).turn == chess.WHITE else "black"
        scored.append(
            (
                score,
                {
                    "ply": result.ply,
                    "index": idx,
                    "san": result.san,
                    "side": side,
                    "classification": review.classification,
                    "swing_pct": round(swing * 100, 1),
                    "lead_change": lead_change,
                    "reason": review.coach,
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1]["ply"]))
    return [moment for _, moment in scored[:limit]]


@dataclass(frozen=True)
class MoveReview:
    classification: ReviewLabel
    symbol: str
    color: str
    expected_points_before: float
    expected_points_after: float
    expected_points_loss: float
    accuracy: float
    best_move_uci: str | None
    best_move_san: str | None
    best_line_san: tuple[str, ...]
    eval_after_cp_white: int
    coach: str


def win_probability(cp: int | float) -> float:
    """Convert a White- or player-POV centipawn score to Win%."""

    bounded = max(-10_000.0, min(10_000.0, float(cp)))
    return 50.0 + 50.0 * (
        2.0 / (1.0 + math.exp(-0.00368208 * bounded)) - 1.0
    )


def expected_points(cp: int | float) -> float:
    """Expected score in ``[0, 1]`` (the spec's Win% divided by 100)."""

    return win_probability(cp) / 100.0


def move_accuracy(expected_points_loss: float) -> float:
    """CAPS2-style move accuracy from an expected-points loss."""

    loss_points = max(0.0, expected_points_loss) * 100.0
    value = 103.1668 * math.exp(-0.04354 * loss_points) - 3.1669
    return max(0.0, min(100.0, value))


def _played_eval(prev: PlyResult, next_ply: PlyResult | None) -> int:
    if next_ply is not None and next_ply.volatility.reason is None:
        return -int(next_ply.eval_cp)
    for line in prev.volatility.top_lines:
        if line.uci == prev.move_uci:
            return int(line.eval_cp)
    return int(prev.eval_cp)


def _standard_label(loss: float, played_best: bool) -> ReviewLabel:
    if played_best and loss <= 0.0005:
        return "best"
    if loss <= 0.02:
        return "excellent"
    if loss <= 0.05:
        return "good"
    if loss <= 0.10:
        return "inaccuracy"
    if loss <= 0.20:
        return "mistake"
    return "blunder"


def _is_great(prev: PlyResult, before: float, after: float) -> bool:
    lines = prev.volatility.top_lines
    if not lines or prev.move_uci != lines[0].uci or len(lines) < 2:
        return False
    second_ep = expected_points(lines[1].eval_cp)
    only_non_losing = after - second_ep >= 0.12 and second_ep < 0.45
    swing = (before < 0.40 <= after) or (0.40 <= before <= 0.60 and after > 0.65)
    return only_non_losing and swing


def _material(board: chess.Board, color: chess.Color) -> int:
    values = {
        chess.PAWN: 1,
        chess.KNIGHT: 3,
        chess.BISHOP: 3,
        chess.ROOK: 5,
        chess.QUEEN: 9,
    }
    return sum(len(board.pieces(kind, color)) * value for kind, value in values.items())


def _is_brilliant(
    prev: PlyResult,
    before: float,
    after: float,
    *,
    player_elo: int,
) -> bool:
    if prev.move_uci != (prev.volatility.top_lines[0].uci if prev.volatility.top_lines else None):
        return False
    if after <= 0.50 or before >= 0.90:
        return False
    try:
        board = chess.Board(prev.fen_before)
        mover = board.turn
        balance_before = _material(board, mover) - _material(board, not mover)
        move = chess.Move.from_uci(prev.move_uci)
        board.push(move)
        line = prev.volatility.top_lines[0]
        if len(line.pv_san) < 2:
            return False
        reply = board.parse_san(line.pv_san[1])
        board.push(reply)
        balance_after = _material(board, mover) - _material(board, not mover)
    except (ValueError, IndexError):
        return False
    generosity = 1 if player_elo < 1400 else 2 if player_elo < 2000 else 3
    return balance_before - balance_after >= generosity


def _coach(label: ReviewLabel, best_san: str | None, loss: float) -> str:
    if label == "book":
        return "This follows established opening theory."
    if label == "brilliant":
        return "A sound sacrifice that preserves the advantage."
    if label == "great":
        return "You found the only move that changes the course of the game."
    if label == "best":
        return "Best move. You matched the engine's first choice."
    if label == "excellent":
        return "Excellent move; it keeps virtually all of the position's value."
    if label == "good":
        return "A solid move with only a small concession."
    suggestion = f" {best_san} was stronger." if best_san else ""
    if label == "inaccuracy":
        return f"This gave up {loss * 100:.1f} expected points.{suggestion}"
    if label == "mistake":
        return f"This materially changed the position.{suggestion}"
    if label == "miss":
        return f"You missed the chance to punish the previous error.{suggestion}"
    return f"This lost a large share of the position's winning chances.{suggestion}"


def _parse_elo(value: object, default: int = 1500) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def attach_move_reviews(results: list[PlyResult], pgn: str) -> None:
    """Attach expected-points reviews to analyzed plies in place."""

    game = chess.pgn.read_game(io.StringIO(pgn))
    headers = game.headers if game is not None else {}
    ratings = {
        chess.WHITE: _parse_elo(headers.get("WhiteElo")),
        chess.BLACK: _parse_elo(headers.get("BlackElo")),
    }
    played_prefix: list[str] = []

    for idx, prev in enumerate(results):
        next_ply = results[idx + 1] if idx + 1 < len(results) else None
        mover = chess.Board(prev.fen_before).turn
        played_prefix.append(prev.move_uci)
        is_book = tuple(played_prefix) in _BOOK_PREFIXES
        played_eval = _played_eval(prev, next_ply)
        before = expected_points(prev.eval_cp)
        after = expected_points(played_eval)
        loss = max(0.0, before - after)
        lines = prev.volatility.top_lines
        best = lines[0] if lines else None
        played_best = bool(best and prev.move_uci == best.uci)
        label = "book" if is_book else _standard_label(loss, played_best)
        if not is_book and label in {"best", "excellent"}:
            if _is_brilliant(prev, before, after, player_elo=ratings[mover]):
                label = "brilliant"
            elif _is_great(prev, before, after):
                label = "great"
        eval_after_white = played_eval if mover == chess.WHITE else -played_eval
        prev.review = MoveReview(
            classification=label,
            symbol=REVIEW_SYMBOLS[label],
            color=REVIEW_COLORS[label],
            expected_points_before=before,
            expected_points_after=after,
            expected_points_loss=loss,
            accuracy=100.0 if is_book else move_accuracy(loss),
            best_move_uci=best.uci if best else None,
            best_move_san=best.san if best else None,
            best_line_san=tuple(best.pv_san) if best else (),
            eval_after_cp_white=eval_after_white,
            coach=_coach(label, best.san if best else None, loss),
        )

    for idx in range(1, len(results)):
        previous = results[idx - 1].review
        current = results[idx].review
        if previous is None or current is None:
            continue
        previous_error = previous.classification in {"mistake", "blunder"}
        failed_to_convert = current.expected_points_before >= 0.60 and current.expected_points_after <= 0.55
        if previous_error and failed_to_convert and current.classification not in {"book", "blunder"}:
            results[idx].review = replace(
                current,
                classification="miss",
                symbol=REVIEW_SYMBOLS["miss"],
                color=REVIEW_COLORS["miss"],
                coach=_coach("miss", current.best_move_san, current.expected_points_loss),
            )


def _side_accuracy(reviews: list[MoveReview]) -> float | None:
    if not reviews:
        return None
    accuracies = [review.accuracy for review in reviews]
    weights: list[float] = []
    for idx in range(len(reviews)):
        window = reviews[max(0, idx - 3) : min(len(reviews), idx + 5)]
        volatility = statistics.pstdev(r.expected_points_before for r in window)
        weights.append(max(0.01, volatility))
    weighted = sum(a * w for a, w in zip(accuracies, weights, strict=True)) / sum(weights)
    harmonic = len(accuracies) / sum(1.0 / max(0.01, value) for value in accuracies)
    return (weighted + harmonic) / 2.0


def _estimated_elo(accuracy: float | None, base_elo: int) -> int | None:
    if accuracy is None:
        return None
    return int(round(max(400, min(3200, base_elo + (accuracy - 75.0) * 12.0)) / 10) * 10)


def build_game_review_summary(results: list[PlyResult], pgn: str) -> dict[str, object]:
    game = chess.pgn.read_game(io.StringIO(pgn))
    headers = game.headers if game is not None else {}
    names = {
        "white": headers.get("White", "White"),
        "black": headers.get("Black", "Black"),
    }
    ratings = {
        "white": _parse_elo(headers.get("WhiteElo")),
        "black": _parse_elo(headers.get("BlackElo")),
    }
    sides: dict[str, list[MoveReview]] = {"white": [], "black": []}
    counts: dict[str, dict[str, int]] = {"white": {}, "black": {}}
    for result in results:
        if result.review is None:
            continue
        side = "white" if chess.Board(result.fen_before).turn == chess.WHITE else "black"
        sides[side].append(result.review)
        label = result.review.classification
        counts[side][label] = counts[side].get(label, 0) + 1

    white_accuracy = _side_accuracy(sides["white"])
    black_accuracy = _side_accuracy(sides["black"])
    all_reviews = sides["white"] + sides["black"]
    worst = max(all_reviews, key=lambda item: item.expected_points_loss, default=None)
    if worst is None:
        coach = "Analyze a game to receive coach feedback."
    elif worst.classification in {"blunder", "mistake", "miss"}:
        coach = (
            f"The key lesson is tactical awareness: the largest swing cost "
            f"{worst.expected_points_loss * 100:.1f} expected points. {worst.coach}"
        )
    else:
        coach = "A well-played game. Most moves preserved the position's expected value."
    return {
        "players": names,
        "ratings": ratings,
        "accuracy": {"white": white_accuracy, "black": black_accuracy},
        "estimated_elo": {
            "white": _estimated_elo(white_accuracy, ratings["white"]),
            "black": _estimated_elo(black_accuracy, ratings["black"]),
        },
        "classification_counts": counts,
        "opening": detect_opening(results, headers),
        "key_moments": compute_key_moments(results),
        "coach": coach,
    }


__all__ = [
    "MoveReview",
    "ReviewLabel",
    "attach_move_reviews",
    "build_game_review_summary",
    "compute_key_moments",
    "detect_opening",
    "expected_points",
    "move_accuracy",
    "win_probability",
]
