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
        "coach": coach,
    }


__all__ = [
    "MoveReview",
    "ReviewLabel",
    "attach_move_reviews",
    "build_game_review_summary",
    "expected_points",
    "move_accuracy",
    "win_probability",
]
