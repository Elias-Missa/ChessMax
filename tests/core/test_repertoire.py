"""The opening repertoire builder (``core/repertoire.py``). Pure — no DB, no engine."""

from __future__ import annotations

import chess

from core.repertoire import (
    FIX,
    GAP,
    KEEP,
    DrillCard,
    GameLine,
    PositionEvidence,
    RepertoireConstants,
    build_tree,
    cards_from_lines,
    classify_move,
    extract_lines,
    fen_key,
    order_cards,
)

CONSTANTS = RepertoireConstants.load()


def uci(sans: list[str]) -> list[str]:
    board = chess.Board()
    out = []
    for san in sans:
        move = board.parse_san(san)
        out.append(move.uci())
        board.push(move)
    return out


def game(
    gid: str,
    sans: list[str],
    color: str = "white",
    points: float | None = 0.5,
    opening: str | None = "Test Opening",
) -> GameLine:
    return GameLine(
        game_id=gid,
        user_color=color,
        moves=uci(sans),
        points=points,
        opening=opening,
        eco="A00",
    )


LONDON = ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]
QG = ["d4", "d5", "c4", "e6", "Nc3", "Nf6"]


# --------------------------------------------------------------------------- #
# The tree                                                                     #
# --------------------------------------------------------------------------- #


def test_the_tree_counts_games_and_ignores_the_other_colour() -> None:
    lines = [game(f"g{i}", LONDON) for i in range(4)]
    lines.append(game("black", LONDON, color="black"))

    root = build_tree(lines, color="white", constants=CONSTANTS)

    assert root.games == 4
    assert root.children["d2d4"].games == 4


def test_an_illegal_move_truncates_the_game_rather_than_dropping_it() -> None:
    """A PGN that goes wrong on move 20 still says what was played on move 3."""

    broken = GameLine("g1", "white", uci(LONDON)[:2] + ["a1a8"] + uci(LONDON)[3:])
    root = build_tree([broken], color="white", constants=CONSTANTS)

    assert root.games == 1
    node = root.children["d2d4"].children[uci(LONDON)[1]]
    assert node.games == 1
    assert node.children == {}


def test_a_game_result_reaches_every_node_it_passes_through() -> None:
    lines = [
        game("w1", LONDON, points=1.0),
        game("w2", LONDON, points=0.0),
        game("w3", LONDON, points=None),
    ]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    node = root.children["d2d4"]

    assert node.games == 3
    assert node.decided == 2  # the unfinished game shapes the tree, not the score
    assert node.score_pct == 0.5


# --------------------------------------------------------------------------- #
# Extraction                                                                   #
# --------------------------------------------------------------------------- #


def test_a_branch_below_min_node_games_is_not_part_of_the_repertoire() -> None:
    lines = [game(f"g{i}", LONDON) for i in range(5)]
    lines.append(game("odd", QG))  # one game, below min_node_games

    root = build_tree(lines, color="white", constants=CONSTANTS)
    extracted = extract_lines(root, color="white", constants=CONSTANTS)

    assert len(extracted) == 1
    assert [m.san for m in extracted[0].moves] == LONDON


def test_max_branches_caps_the_fan_out() -> None:
    """Three real replies, two followed — the tree is for practice, not theory."""

    a = ["d4", "d5", "Bf4", "Nf6"]
    b = ["d4", "d5", "c4", "Nf6"]
    c = ["d4", "d5", "Nf3", "Nf6"]
    lines = (
        [game(f"a{i}", a) for i in range(4)]
        + [game(f"b{i}", b) for i in range(4)]
        + [game(f"c{i}", c) for i in range(4)]
    )
    root = build_tree(lines, color="white", constants=CONSTANTS)
    extracted = extract_lines(root, color="white", constants=CONSTANTS)

    assert len(extracted) == CONSTANTS.max_branches


def test_a_measured_leak_in_a_side_line_is_still_drilled() -> None:
    """The branch caps keep the tree small; they must not hide a known leak.

    Measured against a real 252-game account: without this, every one of the
    eight habits the reviews had already priced sat unreachable behind
    ``max_branches`` while the evidence for them was in the database.
    """

    main = ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]
    second = ["d4", "d5", "c4", "Nf6", "Nc3", "e6"]
    side = ["d4", "Nf6", "Bf4", "g6", "e3", "Bg7"]
    lines = (
        [game(f"m{i}", main) for i in range(10)]
        + [game(f"s{i}", second) for i in range(8)]
        + [game(f"x{i}", side) for i in range(4)]  # third-most-played: capped out
    )
    root = build_tree(lines, color="white", constants=CONSTANTS)

    # Without evidence the side line is pruned by max_branches.
    plain = extract_lines(root, color="white", constants=CONSTANTS)
    assert not any("g6" in [m.san for m in line.moves] for line in plain)

    # The leak is three plies below the pruned branch, so marking only the
    # costly move would not be enough — the whole route has to be reopened.
    board = chess.Board()
    for san in side[:4]:
        board.push_san(san)
    evidence = {
        fen_key(board.fen()): PositionEvidence(
            best_uci="c2c4",
            best_san="c4",
            loss_by_uci={"e2e3": CONSTANTS.fix_delta_w + 6.0},
            n_by_uci={"e2e3": 4},
        )
    }
    widened = extract_lines(
        root, color="white", evidence=evidence, constants=CONSTANTS
    )

    reached = [line for line in widened if "g6" in [m.san for m in line.moves]]
    assert reached, "the route to the measured leak was still pruned"
    leak = next(m for m in reached[0].moves if m.san == "e3")
    assert leak.verdict == FIX
    assert leak.recommended_san == "c4"


def test_widening_needs_repeated_reviewed_evidence_not_one_bad_game() -> None:
    """A single reviewed blunder is a mistake, not a habit — and not a branch."""

    main = ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]
    second = ["d4", "d5", "c4", "Nf6", "Nc3", "e6"]
    side = ["d4", "Nf6", "Bf4", "g6", "e3", "Bg7"]
    lines = (
        [game(f"m{i}", main) for i in range(10)]
        + [game(f"s{i}", second) for i in range(8)]
        + [game(f"x{i}", side) for i in range(4)]
    )
    root = build_tree(lines, color="white", constants=CONSTANTS)
    board = chess.Board()
    for san in side[:4]:
        board.push_san(san)
    evidence = {
        fen_key(board.fen()): PositionEvidence(
            best_uci="c2c4",
            best_san="c4",
            loss_by_uci={"e2e3": 40.0},
            n_by_uci={"e2e3": 1},  # one reviewed ply
        )
    }

    widened = extract_lines(
        root, color="white", evidence=evidence, constants=CONSTANTS
    )

    assert not any("g6" in [m.san for m in line.moves] for line in widened)


def test_extraction_stops_at_max_plies() -> None:
    long_game = [
        "d4", "d5", "Bf4", "Nf6", "e3", "e6", "Nf3", "Be7",
        "Bd3", "O-O", "Nbd2", "c5", "c3", "Nc6", "Ne5", "Qc7",
        "O-O", "b6",
    ]
    lines = [game(f"g{i}", long_game) for i in range(4)]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    extracted = extract_lines(root, color="white", constants=CONSTANTS)

    assert len(extracted[0].moves) == CONSTANTS.max_plies


def test_only_the_users_own_moves_carry_a_verdict() -> None:
    lines = [game(f"g{i}", LONDON) for i in range(4)]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    line = extract_lines(root, color="white", constants=CONSTANTS)[0]

    for move in line.moves:
        assert (move.verdict is not None) == move.user_to_move


# --------------------------------------------------------------------------- #
# Verdicts                                                                     #
# --------------------------------------------------------------------------- #


def test_a_consistent_unmeasured_habit_is_keep() -> None:
    lines = [game(f"g{i}", LONDON) for i in range(4)]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    line = extract_lines(root, color="white", constants=CONSTANTS)[0]

    assert line.moves[0].verdict == KEEP
    assert line.moves[0].recommended_uci == line.moves[0].uci


def test_review_evidence_turns_a_costly_habit_into_a_fix() -> None:
    lines = [game(f"g{i}", LONDON) for i in range(4)]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    start = chess.Board().fen()
    evidence = {
        fen_key(start): PositionEvidence(
            best_uci="e2e4",
            best_san="e4",
            loss_by_uci={"d2d4": CONSTANTS.fix_delta_w + 2.0},
            n_by_uci={"d2d4": 4},
        )
    }

    line = extract_lines(
        root, color="white", evidence=evidence, constants=CONSTANTS
    )[0]

    assert line.moves[0].verdict == FIX
    assert line.moves[0].recommended_uci == "e2e4"
    assert line.moves[0].recommended_san == "e4"
    assert "cost you" in line.moves[0].reason


def test_a_loss_below_the_threshold_is_not_a_fix() -> None:
    lines = [game(f"g{i}", LONDON) for i in range(4)]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    evidence = {
        fen_key(chess.Board().fen()): PositionEvidence(
            best_uci="e2e4",
            best_san="e4",
            loss_by_uci={"d2d4": CONSTANTS.fix_delta_w - 0.5},
            n_by_uci={"d2d4": 4},
        )
    }

    line = extract_lines(
        root, color="white", evidence=evidence, constants=CONSTANTS
    )[0]

    assert line.moves[0].verdict == KEEP


def test_inconsistency_is_a_gap_and_the_best_scoring_move_is_recommended() -> None:
    """Two replies to 1.d4 d5, neither dominant — pick one, and pick the good one."""

    good = ["d4", "d5", "Bf4", "Nf6"]
    bad = ["d4", "d5", "c4", "Nf6"]
    lines = [game(f"g{i}", good, points=1.0) for i in range(5)] + [
        game(f"b{i}", bad, points=0.0) for i in range(5)
    ]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    after_d5 = root.children["d2d4"].children["d7d5"]
    child = after_d5.children["c1f4"]

    verdict, recommended, reason, _ = classify_move(
        after_d5, child, None, CONSTANTS
    )

    assert verdict == GAP
    assert recommended == "c1f4"          # the 100% line, not the 0% one
    assert "different moves here" in reason


def test_a_fix_beats_a_gap_because_consistency_is_not_the_problem() -> None:
    good = ["d4", "d5", "Bf4", "Nf6"]
    bad = ["d4", "d5", "c4", "Nf6"]
    lines = [game(f"g{i}", good, points=1.0) for i in range(5)] + [
        game(f"b{i}", bad, points=0.0) for i in range(5)
    ]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    after_d5 = root.children["d2d4"].children["d7d5"]
    record = PositionEvidence(
        best_uci="g1f3",
        best_san="Nf3",
        loss_by_uci={"c1f4": 9.0},
        n_by_uci={"c1f4": 5},
    )

    verdict, recommended, _, loss = classify_move(
        after_d5, after_d5.children["c1f4"], record, CONSTANTS
    )

    assert verdict == FIX
    assert recommended == "g1f3"
    assert loss == 9.0


def test_a_one_off_experiment_is_never_the_recommendation_at_a_gap() -> None:
    """A move played once is not a decision, however well it went."""

    proven = ["d4", "d5", "Bf4", "Nf6"]
    other = ["d4", "d5", "c4", "Nf6"]
    fluke = ["d4", "d5", "Nf3", "Nf6"]
    lines = (
        [game(f"p{i}", proven, points=1.0 if i < 7 else 0.0) for i in range(10)]
        + [game(f"o{i}", other, points=0.0) for i in range(5)]
        + [game("f0", fluke, points=1.0)]  # 1-for-1, and the best raw score
    )
    root = build_tree(lines, color="white", constants=CONSTANTS)
    after_d5 = root.children["d2d4"].children["d7d5"]

    verdict, recommended, _, _ = classify_move(
        after_d5, after_d5.children["c2c4"], None, CONSTANTS
    )

    assert verdict == GAP
    assert recommended == "c1f4"


# --------------------------------------------------------------------------- #
# Cards and ordering                                                           #
# --------------------------------------------------------------------------- #


def test_two_lines_sharing_a_prefix_produce_one_card_per_position() -> None:
    a = ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]
    b = ["d4", "d5", "Bf4", "c5", "e3", "Nc6"]
    lines = [game(f"a{i}", a) for i in range(4)] + [game(f"b{i}", b) for i in range(4)]
    root = build_tree(lines, color="white", constants=CONSTANTS)
    extracted = extract_lines(root, color="white", constants=CONSTANTS)
    cards = cards_from_lines(extracted, constants=CONSTANTS)

    assert len(extracted) == 2
    ids = [c.card_id for c in cards]
    assert len(ids) == len(set(ids))
    # 1.d4 and 2.Bf4 are shared; only the third white move differs.
    assert len(cards) == 4


def test_the_queue_drills_fixes_first_then_gaps_then_habits() -> None:
    def card(name: str, verdict: str) -> DrillCard:
        return DrillCard(
            card_id=name,
            color="white",
            fen=chess.Board().fen(),
            ply=0,
            expected_uci="d2d4",
            expected_san="d4",
            played_uci="d2d4",
            played_san="d4",
            verdict=verdict,
            reason="",
            opening=None,
            eco=None,
            games=3,
            score_pct=0.5,
            roi_score=0.0,
            line_san=[],
            alternatives=[],
        )

    ordered = order_cards(
        [card("k", KEEP), card("g", GAP), card("f", FIX)], constants=CONSTANTS
    )

    assert [c.card_id for c in ordered] == ["f", "g", "k"]


def test_a_card_answered_correctly_today_sinks_below_one_never_seen() -> None:
    def card(name: str) -> DrillCard:
        return DrillCard(
            card_id=name,
            color="white",
            fen=chess.Board().fen(),
            ply=0,
            expected_uci="d2d4",
            expected_san="d4",
            played_uci="d2d4",
            played_san="d4",
            verdict=KEEP,
            reason="",
            opening=None,
            eco=None,
            games=3,
            score_pct=0.5,
            roi_score=0.0,
            line_san=[],
            alternatives=[],
        )

    history = {
        "fresh": {"correct": True, "days_ago": 0.2},
        "failed": {"correct": False, "days_ago": 0.2},
        "stale": {"correct": True, "days_ago": 30.0},
    }
    ordered = order_cards(
        [card("fresh"), card("new"), card("failed"), card("stale")],
        history,
        constants=CONSTANTS,
    )

    assert [c.card_id for c in ordered] == ["failed", "new", "stale", "fresh"]


def test_roi_breaks_ties_between_equally_urgent_cards() -> None:
    def card(name: str, roi: float) -> DrillCard:
        return DrillCard(
            card_id=name,
            color="white",
            fen=chess.Board().fen(),
            ply=0,
            expected_uci="d2d4",
            expected_san="d4",
            played_uci="d2d4",
            played_san="d4",
            verdict=KEEP,
            reason="",
            opening=None,
            eco=None,
            games=3,
            score_pct=0.5,
            roi_score=roi,
            line_san=[],
            alternatives=[],
        )

    ordered = order_cards([card("low", 0.2), card("high", 9.0)], constants=CONSTANTS)

    assert [c.card_id for c in ordered] == ["high", "low"]


# --------------------------------------------------------------------------- #
# Identity                                                                     #
# --------------------------------------------------------------------------- #


def test_fen_key_ignores_the_move_counters() -> None:
    """A transposition carries different clocks and must still be one node."""

    a = "rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2"
    b = "rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 4 12"

    assert fen_key(a) == fen_key(b)
