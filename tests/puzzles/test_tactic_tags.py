"""Heuristic tactic tags + missed_tactics metric."""

from __future__ import annotations

import json

import chess

from server.insights_metrics import _compute_missed_tactics
from server.tactic_tags import tag_tactics


def test_fork_tag_on_knight_fork() -> None:
    # Ne6-c7 forks king on e8 and rook on a8
    board = chess.Board("r3k3/8/4N3/8/8/8/8/4K3 w - - 0 1")
    tags = tag_tactics(board, "e6c7")
    assert "fork" in tags


def test_pin_tag() -> None:
    # Ra1-e1 pins Ne7 to the king on the e-file
    before = chess.Board("4k3/4n3/8/8/8/8/8/R6K w - - 0 1")
    tags = tag_tactics(before, "a1e1")
    assert "pin" in tags


def test_no_tags_for_quiet_developing_move() -> None:
    board = chess.Board()
    tags = tag_tactics(board, "g1f3")
    assert tags == []


def test_missed_tactics_crosses_findability() -> None:
    moves_by_review = {
        "r1": [
            {
                "is_user_move": 1,
                "tactic_tags": json.dumps(["fork", "pin"]),
                "findability": 75,
                "delta_w": 20.0,
            },
            {
                "is_user_move": 1,
                "tactic_tags": json.dumps(["fork"]),
                "findability": 40,
                "delta_w": 12.0,
            },
            {
                "is_user_move": 0,
                "tactic_tags": json.dumps(["skewer"]),
                "findability": 90,
                "delta_w": 5.0,
            },
        ]
    }
    out = _compute_missed_tactics(moves_by_review)
    tags = {row["tag"]: row for row in out["tags"]}
    assert tags["fork"]["n"] == 2
    assert tags["fork"]["high_findability_n"] == 1
    assert tags["pin"]["n"] == 1
    assert "skewer" not in tags
