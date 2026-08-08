"""Engine-free tests for Guess the Eval Duels (server.guess_eval)."""

from __future__ import annotations

import random
import pytest

from server import guess_eval
from server.db import connect


# --------------------------------------------------------------------------- #
# Pure scoring                                                                  #
# --------------------------------------------------------------------------- #


def test_decide_winner_closest_wins() -> None:
    # true_eval_cp = 150 (+1.50)
    # player_a = 100 (+1.00, diff 50), player_b = 300 (+3.00, diff 150)
    assert guess_eval.decide_winner(150, 100, 300) == "a"
    assert guess_eval.decide_winner(150, -100, 200) == "b"
    assert guess_eval.decide_winner(150, 100, 200) == "draw"  # equal distance 50cp


def test_decide_winner_missing_guesses() -> None:
    assert guess_eval.decide_winner(150, None, 500) == "b"
    assert guess_eval.decide_winner(150, 200, None) == "a"
    assert guess_eval.decide_winner(150, None, None) == "draw"


def test_guess_points_bullseye_and_decay() -> None:
    assert guess_eval.guess_points(150, 150) == 100
    assert guess_eval.guess_points(150, None) == 0
    near = guess_eval.guess_points(150, 200)
    far = guess_eval.guess_points(150, 600)
    assert 100 > near > far >= 0


def test_bot_guess_is_in_range_and_seeded() -> None:
    rng = random.Random(1)
    guesses = [guess_eval.bot_guess(150, rng) for _ in range(50)]
    assert all(guess_eval.EVAL_MIN <= g <= guess_eval.EVAL_MAX for g in guesses)
    assert all(g % 10 == 0 for g in guesses)
    assert guess_eval.bot_guess(150, random.Random(7)) == guess_eval.bot_guess(150, random.Random(7))


def test_clamp_eval_guess() -> None:
    assert guess_eval.clamp_eval_guess(-2000) == guess_eval.EVAL_MIN
    assert guess_eval.clamp_eval_guess(5000) == guess_eval.EVAL_MAX
    assert guess_eval.clamp_eval_guess(150) == 150


# --------------------------------------------------------------------------- #
# Duel flow + matchmaking (in-memory DB)                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def db():
    conn = connect(":memory:")
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'alice')")
    conn.execute("INSERT INTO users (id, username) VALUES (2, 'bob')")
    conn.commit()
    guess_eval.ensure_eval_positions_pool(conn)
    yield conn
    conn.close()
    guess_eval._WAITING.clear()


def test_pool_auto_seeding(db) -> None:
    assert guess_eval.pool_size(db) >= 10


def test_bot_duel_resolves_on_guess(db) -> None:
    guess_eval._WAITING.clear()
    now = 1000.0
    guess_eval._WAITING[1] = guess_eval._Waiter(now - guess_eval.BOT_WAIT_SECONDS - 1)
    status, duel = guess_eval.find_or_create_match(db, 1, now=now, rng=random.Random(0))
    assert status == "matched" and duel is not None
    assert bool(duel["is_bot"]) is True

    true_eval = int(duel["true_eval_cp"])
    resolved = guess_eval.submit_guess(db, duel, "a", true_eval, now + 5)
    assert resolved["status"] == "done"
    public = guess_eval.duel_public(db, resolved, 1)
    assert public["true_eval_cp"] == true_eval
    assert public["your_guess"] == true_eval
    assert public["outcome"] in {"win", "loss", "draw"}
    assert public["outcome"] != "loss"


def test_two_humans_get_paired_into_one_duel(db) -> None:
    guess_eval._WAITING.clear()
    now = 2000.0
    s1, d1 = guess_eval.find_or_create_match(db, 1, now=now, rng=random.Random(0))
    assert s1 == "searching" and d1 is None
    s2, d2 = guess_eval.find_or_create_match(db, 2, now=now + 1, rng=random.Random(0))
    assert s2 == "matched" and d2 is not None
    assert not bool(d2["is_bot"])
    s3, d3 = guess_eval.find_or_create_match(db, 1, now=now + 2, rng=random.Random(0))
    assert s3 == "matched" and int(d3["id"]) == int(d2["id"])
    assert {guess_eval.side_of(d3, 1), guess_eval.side_of(d3, 2)} == {"a", "b"}


def test_duel_resolves_on_deadline(db) -> None:
    guess_eval._WAITING.clear()
    now = 3000.0
    guess_eval._WAITING[1] = guess_eval._Waiter(now - guess_eval.BOT_WAIT_SECONDS - 1)
    _, duel = guess_eval.find_or_create_match(db, 1, now=now, rng=random.Random(0))
    after = int(duel["deadline_ts"]) + 1
    resolved = guess_eval.maybe_resolve(db, guess_eval.get_duel(db, int(duel["id"])), after)
    assert resolved["status"] == "done"
    assert resolved["winner"] == "b"
