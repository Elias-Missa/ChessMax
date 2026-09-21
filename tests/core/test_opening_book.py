"""Candidate ranking (core/opening_book.py). Pure — no engine, no network."""

from __future__ import annotations

import chess
import pytest

from core.opening_book import (
    Candidate,
    OpeningBookConstants,
    build_candidates,
    engine_quality,
    rank_candidates,
    shrunk_score,
)
from core.opening_signals import MoveSignals

START = chess.Board().fen()
CONSTANTS = OpeningBookConstants.load()


def explorer(*specs: tuple[str, str, int, int, int, int | None]) -> dict:
    return {
        "moves": [
            {
                "uci": uci,
                "san": san,
                "white": w,
                "draws": d,
                "black": b,
                "average_rating": rating,
            }
            for uci, san, w, d, b, rating in specs
        ]
    }


def engine(*specs: tuple[str, int]) -> list[dict]:
    return [{"move": uci, "eval": cp, "pv": []} for uci, cp in specs]


# ── the pieces of the formula ─────────────────────────────────────────────── #


def test_shrinkage_pulls_a_tiny_sample_toward_even() -> None:
    lucky = shrunk_score(2, 0, 0, prior_games=12.0)
    solid = shrunk_score(110, 0, 90, prior_games=12.0)
    assert lucky is not None and solid is not None
    assert lucky < 0.65, "2-for-2 must not read as a 100% move"
    assert solid > 0.5
    assert shrunk_score(0, 0, 0, prior_games=12.0) is None


def test_engine_quality_falls_off_with_centipawn_loss() -> None:
    assert engine_quality(0, scale_cp=60.0) == pytest.approx(1.0)
    assert engine_quality(60, scale_cp=60.0) == pytest.approx(0.3679, abs=1e-3)
    assert engine_quality(None, scale_cp=60.0) is None
    # Monotone, and bounded below by zero rather than going negative.
    assert engine_quality(600, scale_cp=60.0) < engine_quality(120, scale_cp=60.0)
    assert engine_quality(600, scale_cp=60.0) > 0


# ── assembly ──────────────────────────────────────────────────────────────── #


def test_candidates_are_the_union_of_every_source() -> None:
    """A move only the engine names, and a move only the explorer names, both
    belong on the list — the disagreement is the point."""

    cands = build_candidates(
        START,
        masters=explorer(("e2e4", "e4", 10, 10, 10, 2500)),
        peers=explorer(("d2d4", "d4", 10, 10, 10, 1500)),
        engine_top_moves=engine(("g1f3", 20)),
        constants=CONSTANTS,
    )
    assert {c.uci for c in cands} == {"e2e4", "d2d4", "g1f3"}


def test_illegal_moves_from_a_stale_cache_are_dropped() -> None:
    """A cache row fetched for a different position must not inject a move that
    cannot be played here."""

    cands = build_candidates(
        START,
        peers=explorer(("e7e5", "e5", 50, 0, 50, 1500)),  # Black's move, White to play
        constants=CONSTANTS,
    )
    assert cands == []


def test_peer_counts_are_read_from_the_movers_side() -> None:
    board = chess.Board()
    board.push_san("e4")
    cands = build_candidates(
        board.fen(),
        peers=explorer(("e7e5", "e5", 300, 40, 660, 1500)),
        constants=CONSTANTS,
    )
    (c,) = cands
    assert c.peer_wins == 660, "Black to move: 'black' column is the win column"
    assert c.peer_losses == 300
    assert c.peer_score is not None and c.peer_score > 0.5


def test_loss_cp_is_measured_against_the_positions_own_best() -> None:
    cands = {
        c.uci: c
        for c in build_candidates(
            START,
            engine_top_moves=engine(("e2e4", 30), ("d2d4", 28), ("a2a3", -20)),
            constants=CONSTANTS,
        )
    }
    assert cands["e2e4"].loss_cp == 0
    assert cands["d2d4"].loss_cp == 2
    assert cands["a2a3"].loss_cp == 50


# ── ranking ───────────────────────────────────────────────────────────────── #


def test_popularity_is_a_share_of_this_position_not_a_raw_count() -> None:
    """Normalizing within the position is what keeps scores comparable across
    plies; on raw log(1+n) the first move of the game outranks everything."""

    busy = rank_candidates(
        build_candidates(
            START,
            masters=explorer(
                ("e2e4", "e4", 500_000, 0, 0, 2500),
                ("d2d4", "d4", 250_000, 0, 0, 2500),
            ),
            constants=CONSTANTS,
        ),
        constants=CONSTANTS,
    )
    quiet = rank_candidates(
        build_candidates(
            START,
            masters=explorer(
                ("e2e4", "e4", 50, 0, 0, 2500), ("d2d4", "d4", 25, 0, 0, 2500)
            ),
            constants=CONSTANTS,
        ),
        constants=CONSTANTS,
    )
    assert busy[0].uci == quiet[0].uci == "e2e4"
    assert busy[0].rank_score == pytest.approx(quiet[0].rank_score, abs=1e-9)


def test_an_obscure_move_is_penalised_but_still_returned() -> None:
    """The spec is explicit that no legal move may be blocked (§2.3, §10) —
    ranking decides order, never permission."""

    ranked = rank_candidates(
        build_candidates(
            START,
            masters=explorer(
                ("e2e4", "e4", 10_000, 0, 0, 2500), ("a2a4", "a4", 3, 0, 0, 2200)
            ),
            peers=explorer(
                ("e2e4", "e4", 900, 100, 800, 1550), ("a2a4", "a4", 4, 0, 6, 1400)
            ),
            engine_top_moves=engine(("e2e4", 30), ("a2a4", -25)),
            constants=CONSTANTS,
        ),
        constants=CONSTANTS,
    )
    obscure = next(c for c in ranked if c.uci == "a2a4")
    assert obscure.obscure is True
    assert ranked[-1].uci == "a2a4", "penalised to the bottom, not removed"
    assert len(ranked) == 2


def test_low_sample_tracks_the_bigger_of_the_two_corpora() -> None:
    """`low_sample` is a display flag ('insufficient sample'), separate from
    `obscure` (a ranking penalty). A move can be rare enough to penalise while
    still having enough games to quote a number for."""

    cands = {
        c.uci: c
        for c in build_candidates(
            START,
            masters=explorer(("e2e4", "e4", 3, 0, 0, 2500), ("d2d4", "d4", 1, 0, 0, 2500)),
            peers=explorer(("e2e4", "e4", 40, 0, 40, 1500)),
            constants=CONSTANTS,
        )
    }
    assert cands["e2e4"].low_sample is False, "80 peer games is quotable"
    assert cands["d2d4"].low_sample is True, "1 master game, no peer games"


def test_a_rare_move_the_engine_tops_escapes_the_obscure_penalty() -> None:
    """A novelty the engine says is best is a find, not an obscurity."""

    ranked = {
        c.uci: c
        for c in rank_candidates(
            build_candidates(
                START,
                masters=explorer(("e2e4", "e4", 10_000, 0, 0, 2500)),
                peers=explorer(("e2e4", "e4", 900, 100, 800, 1550)),
                engine_top_moves=engine(("g1f3", 40), ("e2e4", 30)),
                constants=CONSTANTS,
            ),
            constants=CONSTANTS,
        )
    }
    assert ranked["g1f3"].obscure is False


def test_a_move_already_in_the_repertoire_is_never_called_obscure() -> None:
    ranked = {
        c.uci: c
        for c in rank_candidates(
            build_candidates(
                START,
                masters=explorer(
                    ("e2e4", "e4", 10_000, 0, 0, 2500), ("b2b3", "b3", 2, 0, 0, 2300)
                ),
                engine_top_moves=engine(("e2e4", 30), ("b2b3", 5)),
                repertoire_ucis=["b2b3"],
                constants=CONSTANTS,
            ),
            constants=CONSTANTS,
        )
    }
    assert ranked["b2b3"].obscure is False
    assert ranked["b2b3"].in_repertoire is True


def test_ranking_is_deterministic() -> None:
    args = dict(
        masters=explorer(
            ("e2e4", "e4", 100, 0, 0, 2500), ("d2d4", "d4", 100, 0, 0, 2500)
        ),
        peers=explorer(
            ("e2e4", "e4", 50, 0, 50, 1500), ("d2d4", "d4", 50, 0, 50, 1500)
        ),
        constants=CONSTANTS,
    )
    first = [c.uci for c in rank_candidates(build_candidates(START, **args), constants=CONSTANTS)]
    second = [c.uci for c in rank_candidates(build_candidates(START, **args), constants=CONSTANTS)]
    assert first == second


def test_missing_sources_fall_back_to_neutral_rather_than_zero() -> None:
    """With no explorer and no engine a move scores the neutral 0.5 on both
    terms — it is not ranked as if it had lost every game."""

    (only,) = rank_candidates(
        build_candidates(START, engine_top_moves=engine(("e2e4", 30)), constants=CONSTANTS),
        constants=CONSTANTS,
    )
    assert only.peer_score is None
    assert only.rank_score > 0


def test_signals_ride_along_into_the_payload() -> None:
    cands = build_candidates(
        START,
        peers=explorer(("e2e4", "e4", 10, 10, 10, 1500)),
        signals={"e2e4": MoveSignals(True, None, False)},
        constants=CONSTANTS,
    )
    assert cands[0].as_dict()["signals"] == {
        "engine": True,
        "human": None,
        "results": False,
    }


def test_empty_position_ranks_to_an_empty_list() -> None:
    assert rank_candidates([], constants=CONSTANTS) == []
    assert isinstance(Candidate(uci="e2e4", san="e4").as_dict(), dict)
