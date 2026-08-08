"""Tests for stable game identity helpers."""

from __future__ import annotations

from server import game_identity

PGN = """
[Event "Test"]
[White "Alice"]
[Black "Bob"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *
"""


def test_chesscom_from_url() -> None:
    assert game_identity.chesscom_game_id(
        "https://www.chess.com/game/live/123456789"
    ) == "chesscom:123456789"


def test_chesscom_from_meta_uuid() -> None:
    assert game_identity.chesscom_game_id({"game_id": "aabbccdd-eeff-0011"}) == (
        "chesscom:aabbccdd-eeff-0011"
    )


def test_pgn_hash_stable() -> None:
    a = game_identity.pgn_san_hash(PGN)
    b = game_identity.pgn_san_hash(PGN.replace("1. e4 e5", "1. e4 {[%clk 0:03:00]} e5"))
    assert a == b
    assert a.startswith("pgn:")


def test_result_from_chesscom() -> None:
    assert (
        game_identity.result_from_chesscom(
            {"white_result": "win", "black_result": "checkmated"}
        )
        == "1-0"
    )
    assert (
        game_identity.result_from_chesscom(
            {"white_result": "resigned", "black_result": "win"}
        )
        == "0-1"
    )
