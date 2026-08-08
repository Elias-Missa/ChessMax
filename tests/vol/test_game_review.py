from __future__ import annotations

from types import SimpleNamespace

import chess
import pytest

from chess_vol.game_review import (
    compute_key_moments,
    detect_opening,
    expected_points,
    move_accuracy,
)


def test_expected_points_is_logistic_and_clamps_mates() -> None:
    assert expected_points(0) == pytest.approx(0.5)
    assert expected_points(10_000) > 0.999
    assert expected_points(-10_000) < 0.001
    assert expected_points(50_000) == expected_points(10_000)


def test_caps2_accuracy_penalizes_expected_point_loss() -> None:
    perfect = move_accuracy(0)
    small_loss = move_accuracy(0.05)
    blunder = move_accuracy(0.30)

    assert perfect == pytest.approx(99.9999)
    assert perfect > small_loss > blunder
    assert 0 <= blunder <= 100


# ── Opening detection + key moments (chess.com parity) ──────────────────────

_FEN_W = chess.STARTING_FEN
_FEN_B = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def _ply(ply, san, move_uci, fen_before, *, review=None):
    return SimpleNamespace(
        ply=ply, san=san, move_uci=move_uci, fen_before=fen_before, review=review
    )


def _review(loss, before, after, classification="mistake", coach="c"):
    return SimpleNamespace(
        expected_points_loss=loss,
        expected_points_before=before,
        expected_points_after=after,
        classification=classification,
        coach=coach,
    )


def test_detect_opening_prefers_pgn_headers() -> None:
    results = [_ply(1, "e4", "e2e4", _FEN_W)]
    headers = {"Opening": "Sicilian Defense", "ECO": "B20", "Variation": "Najdorf"}
    opening = detect_opening(results, headers)
    assert opening is not None
    assert opening["source"] == "headers"
    assert opening["eco"] == "B20"
    assert "Sicilian Defense" in opening["name"]
    assert "Najdorf" in opening["name"]


def test_detect_opening_falls_back_to_book_longest_prefix() -> None:
    ucis = "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6".split()
    results = [_ply(i + 1, "", uci, _FEN_W) for i, uci in enumerate(ucis)]
    opening = detect_opening(results, None)
    assert opening is not None
    assert opening["source"] == "book"
    assert opening["eco"] == "C68"  # Morphy Defense, the longest matching prefix
    assert "Ruy" in opening["name"]


def test_detect_opening_none_for_irregular_first_move() -> None:
    results = [_ply(1, "a3", "a2a3", _FEN_W)]
    assert detect_opening(results, None) is None


def test_compute_key_moments_ranks_by_impact_and_limits() -> None:
    results = [
        _ply(1, "e4", "e2e4", _FEN_W, review=_review(0.02, 0.5, 0.48, "excellent")),
        _ply(2, "e5", "e7e5", _FEN_B, review=_review(0.25, 0.5, 0.25, "blunder")),
        _ply(3, "Nf3", "g1f3", _FEN_W, review=_review(0.12, 0.62, 0.40, "mistake")),
        _ply(4, "Nc6", "b8c6", _FEN_B, review=_review(0.11, 0.4, 0.29, "mistake")),
    ]
    moments = compute_key_moments(results, limit=2)

    assert len(moments) == 2
    # ply 3 shed 0.12 AND flipped the lead (+0.15 bonus = 0.27) > ply 2's 0.25.
    assert [m["ply"] for m in moments] == [3, 2]
    assert moments[0]["index"] == 2  # 0-based, for jumpToPly
    assert moments[0]["lead_change"] is True
    assert moments[0]["side"] == "white"
    assert moments[1]["side"] == "black"
    assert moments[1]["swing_pct"] == 25.0


def test_compute_key_moments_empty_when_calm() -> None:
    results = [
        _ply(1, "e4", "e2e4", _FEN_W, review=_review(0.01, 0.5, 0.49, "excellent")),
        _ply(2, "e5", "e7e5", _FEN_B, review=_review(0.0, 0.5, 0.5, "best")),
    ]
    assert compute_key_moments(results) == []


def test_compute_key_moments_skips_unreviewed_plies() -> None:
    assert compute_key_moments([_ply(1, "e4", "e2e4", _FEN_W, review=None)]) == []
