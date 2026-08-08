"""Engine-free tests for Guess the Elo Duels (server.guess_elo)."""

from __future__ import annotations

import random

import chess
import pytest

from server import guess_elo
from server.db import connect


# --------------------------------------------------------------------------- #
# Pure scoring                                                                  #
# --------------------------------------------------------------------------- #


def test_decide_winner_closest_wins() -> None:
    assert guess_elo.decide_winner(1500, 1450, 1700) == "a"
    assert guess_elo.decide_winner(1500, 1200, 1550) == "b"
    assert guess_elo.decide_winner(1500, 1400, 1600) == "draw"  # equal distance


def test_decide_winner_missing_guesses() -> None:
    assert guess_elo.decide_winner(1500, None, 1900) == "b"
    assert guess_elo.decide_winner(1500, 1600, None) == "a"
    assert guess_elo.decide_winner(1500, None, None) == "draw"


def test_guess_points_bullseye_and_decay() -> None:
    assert guess_elo.guess_points(1500, 1500) == 100
    assert guess_elo.guess_points(1500, None) == 0
    near = guess_elo.guess_points(1500, 1550)
    far = guess_elo.guess_points(1500, 1900)
    assert 100 > near > far >= 0


def test_bot_guess_is_in_range_and_seeded() -> None:
    rng = random.Random(1)
    guesses = [guess_elo.bot_guess(1500, rng) for _ in range(50)]
    assert all(guess_elo.GUESS_MIN <= g <= guess_elo.GUESS_MAX for g in guesses)
    assert all(g % 50 == 0 for g in guesses)
    # Deterministic given the seed.
    assert guess_elo.bot_guess(1500, random.Random(7)) == guess_elo.bot_guess(1500, random.Random(7))


def test_clamp_guess() -> None:
    assert guess_elo.clamp_guess(50) == guess_elo.GUESS_MIN
    assert guess_elo.clamp_guess(9999) == guess_elo.GUESS_MAX
    assert guess_elo.clamp_guess(1500) == 1500


# --------------------------------------------------------------------------- #
# Game generation (fake uniform policy — no Maia needed)                         #
# --------------------------------------------------------------------------- #


def _uniform_policy(fen: str, elo: int, moves: list[chess.Move]) -> dict[chess.Move, float]:
    if not moves:
        return {}
    p = 1.0 / len(moves)
    return {m: p for m in moves}


def test_generate_elo_game_produces_valid_pgn() -> None:
    rng = random.Random(3)
    pgn, plies = guess_elo.generate_elo_game(_uniform_policy, 1500, rng=rng, max_plies=20)
    assert plies > 0
    moves = guess_elo.game_moves_san(pgn)
    assert len(moves) == plies
    # The PGN must be replayable.
    import io
    import chess.pgn

    game = chess.pgn.read_game(io.StringIO(pgn))
    assert game is not None
    board = game.board()
    for mv in game.mainline_moves():
        assert mv in board.legal_moves
        board.push(mv)


# --------------------------------------------------------------------------- #
# Duel flow + matchmaking (in-memory DB)                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def db():
    conn = connect(":memory:")
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'alice')")
    conn.execute("INSERT INTO users (id, username) VALUES (2, 'bob')")
    conn.commit()
    # One game in the pool.
    guess_elo.store_game(conn, 1700, _tiny_pgn(), 4)
    yield conn
    conn.close()
    guess_elo._WAITING.clear()


def _tiny_pgn() -> str:
    return '[Event "t"]\n\n1. e4 e5 2. Nf3 Nc6 *'


def test_bot_duel_resolves_on_guess(db) -> None:
    guess_elo._WAITING.clear()
    now = 1000.0
    # Force the bot path: user has waited past the threshold.
    guess_elo._WAITING[1] = guess_elo._Waiter(now - guess_elo.BOT_WAIT_SECONDS - 1)
    status, duel = guess_elo.find_or_create_match(db, 1, now=now, rng=random.Random(0))
    assert status == "matched" and duel is not None
    assert bool(duel["is_bot"]) is True

    resolved = guess_elo.submit_guess(db, duel, "a", 1700, now + 5)
    assert resolved["status"] == "done"
    public = guess_elo.duel_public(db, resolved, 1)
    assert public["true_elo"] == 1700
    assert public["your_guess"] == 1700
    assert public["outcome"] in {"win", "loss", "draw"}
    # A perfect guess cannot lose.
    assert public["outcome"] != "loss"


def test_two_humans_get_paired_into_one_duel(db) -> None:
    guess_elo._WAITING.clear()
    now = 2000.0
    # Alice searches first — no opponent yet, goes to the waiting room.
    s1, d1 = guess_elo.find_or_create_match(db, 1, now=now, rng=random.Random(0))
    assert s1 == "searching" and d1 is None
    # Bob searches — pairs with Alice.
    s2, d2 = guess_elo.find_or_create_match(db, 2, now=now + 1, rng=random.Random(0))
    assert s2 == "matched" and d2 is not None
    assert not bool(d2["is_bot"])
    # Alice polls again and finds the same active duel.
    s3, d3 = guess_elo.find_or_create_match(db, 1, now=now + 2, rng=random.Random(0))
    assert s3 == "matched" and int(d3["id"]) == int(d2["id"])
    assert {guess_elo.side_of(d3, 1), guess_elo.side_of(d3, 2)} == {"a", "b"}


def test_duel_resolves_on_deadline(db) -> None:
    guess_elo._WAITING.clear()
    now = 3000.0
    guess_elo._WAITING[1] = guess_elo._Waiter(now - guess_elo.BOT_WAIT_SECONDS - 1)
    _, duel = guess_elo.find_or_create_match(db, 1, now=now, rng=random.Random(0))
    # Nobody submits; polling past the deadline resolves it.
    after = int(duel["deadline_ts"]) + 1
    resolved = guess_elo.maybe_resolve(db, guess_elo.get_duel(db, int(duel["id"])), after)
    assert resolved["status"] == "done"
    # Bot guessed, human didn't -> bot (b) wins.
    assert resolved["winner"] == "b"
