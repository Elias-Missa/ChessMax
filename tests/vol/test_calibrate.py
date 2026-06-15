"""Unit tests for :mod:`chess_vol.calibrate`.

These tests are engine-free: every :class:`CachedAnalysis` is constructed
in-memory from a known list of evals. The point is to lock down the pure
math — recompute, loss functions, JSON round-trip, and optimiser convergence
on synthetic data — independently of Stockfish.
"""

from __future__ import annotations

import math

import chess
import pytest

from chess_vol.calibrate import (
    DEFAULT_TARGETS,
    CachedAnalysis,
    Constants,
    CorpusEntry,
    RawScore,
    analyses_from_json,
    analyses_to_json,
    blended_loss,
    build_report,
    corpus_from_json,
    corpus_to_json,
    distributional_loss,
    expert_loss,
    recompute_v,
    tune_constants,
)
from chess_vol.config import K_SHALLOW, MATE_BASE, MATE_STEP
from chess_vol.volatility import compute_volatility, mate_to_cp

from .conftest import FakeEngine, evals_to_infos

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _analysis(evals: list[int | str], legal: int | None = None) -> CachedAnalysis:
    """Build a CachedAnalysis from the same `int | "Mk"` shorthand the
    existing test suite uses for `evals_to_infos`."""
    lines: list[RawScore] = []
    for e in evals:
        if isinstance(e, str):
            sign = -1 if e.startswith("-") else 1
            digits = e.lstrip("-M")
            lines.append(RawScore(mate=sign * int(digits)))
        else:
            lines.append(RawScore(cp=e))
    return CachedAnalysis(
        fen="8/8/8/8/8/8/8/4K2k w - - 0 1",
        lines=lines,
        legal_count=legal if legal is not None else max(2, len(evals)),
    )


# --------------------------------------------------------------------------- #
# RawScore validation                                                         #
# --------------------------------------------------------------------------- #


class TestRawScore:
    def test_xor_required(self) -> None:
        with pytest.raises(ValueError):
            RawScore()
        with pytest.raises(ValueError):
            RawScore(cp=0, mate=1)

    def test_cp_only(self) -> None:
        r = RawScore(cp=42)
        assert r.cp == 42 and r.mate is None

    def test_mate_only(self) -> None:
        r = RawScore(mate=-3)
        assert r.cp is None and r.mate == -3


# --------------------------------------------------------------------------- #
# Constants ⇄ vector                                                           #
# --------------------------------------------------------------------------- #


class TestConstantsVector:
    def test_round_trip(self) -> None:
        c = Constants(
            k_shallow=123.0,
            eval_scale_grace=10.0,
            eval_scale_width=400.0,
            eval_scale_max=2500.0,
            mate_base=2100.0,
            mate_step=42.0,
            decided_best_cp=900.0,
            decided_alt_cp=350.0,
        )
        c2 = Constants.from_vector(c.as_vector())
        assert c == c2

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError):
            Constants.from_vector([1.0, 2.0, 3.0])


# --------------------------------------------------------------------------- #
# recompute_v matches compute_volatility under default constants               #
# --------------------------------------------------------------------------- #


class TestRecomputeMatchesEngine:
    """The whole calibration pipeline rests on this: re-running V from a
    cached analysis under the *current* config should produce the same number
    as running compute_volatility live.
    """

    @pytest.mark.parametrize(
        "evals",
        [
            [80, 30, 10, -5, -20, -40],
            [350, 50, 20, 0, -15, -30],
            [0, -250, -400, -600, -900, -1200],
            [+450, +420, +400, +380, +350, +300],
            [+5, 0, -5, -10, -15, -25],
        ],
    )
    def test_matches_engine_for_cp_evals(self, evals: list[int]) -> None:
        engine = FakeEngine(scripts=[evals_to_infos(evals)])
        # Use a "real" board so legal_count > 1 and the math fires.
        board = chess.Board()
        live = compute_volatility(board, engine)
        cached = recompute_v(_analysis(evals, legal=20), Constants())
        assert live.score is not None
        assert cached is not None
        assert cached == pytest.approx(live.score, abs=1e-6)

    def test_matches_engine_for_mate_evals(self) -> None:
        engine = FakeEngine(scripts=[evals_to_infos(["M1", 200, 0, -200, -400, -600])])
        board = chess.Board()
        live = compute_volatility(board, engine)
        cached = recompute_v(
            _analysis(["M1", 200, 0, -200, -400, -600], legal=20), Constants()
        )
        assert live.score is not None
        assert cached is not None
        assert cached == pytest.approx(live.score, abs=1e-6)


# --------------------------------------------------------------------------- #
# recompute_v: undefined cases                                                 #
# --------------------------------------------------------------------------- #


class TestRecomputeUndefined:
    def test_terminal_returns_none(self) -> None:
        a = CachedAnalysis(fen="x", lines=[], legal_count=0, is_terminal=True)
        assert recompute_v(a, Constants()) is None

    def test_only_legal_move_returns_none(self) -> None:
        a = _analysis([0], legal=1)
        assert recompute_v(a, Constants()) is None

    def test_single_line_returns_none(self) -> None:
        # legal_count > 1 but only one MultiPV line — degenerate input.
        a = _analysis([0], legal=20)
        assert recompute_v(a, Constants()) is None


# --------------------------------------------------------------------------- #
# Mate translation under candidate constants                                  #
# --------------------------------------------------------------------------- #


class TestMateUnderConstants:
    def test_default_constants_match_mate_to_cp(self) -> None:
        for n in (1, 3, 6, 10, 20, 30):
            v = recompute_v(
                _analysis([f"M{n}", -100, -200, -300, -400, -500], legal=20),
                Constants(),
            )
            assert v is not None and v > 0  # only validates the call doesn't error

    def test_changing_mate_step_shifts_v(self) -> None:
        """Larger MATE_STEP → mate-in-N is worth less, drop from M-N to next-best
        is smaller, V should drop. (Not strictly monotonic in all corners but
        true for the simple case where best is M3 and alt2 is winning cp.)"""
        evals = ["M3", 200, 0, -200, -400, -600]
        a = _analysis(evals, legal=20)
        v_step50 = recompute_v(a, Constants(mate_step=50))
        v_step150 = recompute_v(a, Constants(mate_step=150))
        assert v_step50 is not None and v_step150 is not None
        assert v_step50 > v_step150

    def test_default_mate_base_matches_library(self) -> None:
        """If a caller passes Constants() it should match mate_to_cp() exactly."""
        c = Constants()
        assert c.mate_base == MATE_BASE
        assert c.mate_step == MATE_STEP
        # Spot-check via compute_volatility - covered in TestRecomputeMatchesEngine
        # but also verify the helper directly:
        from chess_vol.calibrate import _mate_to_cp_with

        for n in (1, 5, 10, 20):
            assert _mate_to_cp_with(n, c.mate_base, c.mate_step) == mate_to_cp(n)
            assert _mate_to_cp_with(-n, c.mate_base, c.mate_step) == mate_to_cp(-n)


# --------------------------------------------------------------------------- #
# Loss functions                                                              #
# --------------------------------------------------------------------------- #


class TestExpertLoss:
    def test_zero_when_v_matches_label(self) -> None:
        # Pick evals → V, label = V → MSE = 0.
        evals = [80, 30, 10, -5, -20, -40]
        v = recompute_v(_analysis(evals, legal=20), Constants())
        assert v is not None
        corpus = [CorpusEntry(id="x", fen="dummy", label=v)]
        analyses = {"x": _analysis(evals, legal=20)}
        assert expert_loss(corpus, analyses, Constants()) == pytest.approx(0.0)

    def test_positive_when_v_misses_label(self) -> None:
        evals = [80, 30, 10, -5, -20, -40]
        corpus = [CorpusEntry(id="x", fen="dummy", label=99.0)]
        analyses = {"x": _analysis(evals, legal=20)}
        assert expert_loss(corpus, analyses, Constants()) > 0.0

    def test_unlabelled_entries_skipped(self) -> None:
        corpus = [CorpusEntry(id="x", fen="dummy")]
        analyses = {"x": _analysis([80, 30], legal=20)}
        # No label → no error term → loss = 0 (caller checks emptiness)
        assert expert_loss(corpus, analyses, Constants()) == 0.0

    def test_terminal_entries_skipped(self) -> None:
        # Labelled entry but V is undefined — should be skipped silently.
        corpus = [CorpusEntry(id="x", fen="dummy", label=50.0)]
        analyses = {"x": CachedAnalysis(fen="x", lines=[], legal_count=0, is_terminal=True)}
        assert expert_loss(corpus, analyses, Constants()) == 0.0


class TestDistributionalLoss:
    def test_zero_when_distribution_matches_target(self) -> None:
        # Build a corpus whose Vs land entirely in the "low" bucket and a
        # category target that's 100% low — KL with smoothing is small.
        corpus = [
            CorpusEntry(id=f"q{i}", fen="x", category="quiet") for i in range(20)
        ]
        # All-equal evals → V ≈ 0 → all in low bucket.
        analyses = {f"q{i}": _analysis([0, 0, 0, 0, 0, 0], legal=20) for i in range(20)}
        # Synthetic target: 100% low (matches what we'll observe).
        from chess_vol.calibrate import CategoryTarget

        targets = {"quiet": CategoryTarget("quiet", low=1.0, medium=0.0, high=0.0)}
        loss = distributional_loss(corpus, analyses, Constants(), targets=targets)
        # Smoothing means it's not exactly 0 but it should be tiny.
        assert loss < 0.01

    def test_positive_when_distribution_diverges(self) -> None:
        # Same all-low corpus but target says all should be high → big loss.
        corpus = [
            CorpusEntry(id=f"q{i}", fen="x", category="quiet") for i in range(20)
        ]
        analyses = {f"q{i}": _analysis([0, 0, 0, 0, 0, 0], legal=20) for i in range(20)}
        from chess_vol.calibrate import CategoryTarget

        targets = {"quiet": CategoryTarget("quiet", low=0.0, medium=0.0, high=1.0)}
        loss = distributional_loss(corpus, analyses, Constants(), targets=targets)
        assert loss > 0.5

    def test_unknown_category_ignored(self) -> None:
        corpus = [CorpusEntry(id="x", fen="x", category="not_in_targets")]
        analyses = {"x": _analysis([0, 0, 0, 0, 0, 0], legal=20)}
        # No matched category → loss 0 (no signal).
        assert distributional_loss(corpus, analyses, Constants()) == 0.0


class TestBlendedLoss:
    def test_grows_when_labels_misalign(self) -> None:
        """Blended = expert + distributional. Label corruption should swamp
        the distributional term, so a corrupted-label loss must be much
        larger than the well-labelled one."""
        analyses = {
            "a": _analysis([0, 0, 0, 0, 0, 0], legal=20),
            "b": _analysis([0, 0, 0, 0, 0, 0], legal=20),
        }
        good_corpus = [
            CorpusEntry(id="a", fen="x", label=0.0, category="quiet"),
            CorpusEntry(id="b", fen="x", label=0.0, category="quiet"),
        ]
        bad_corpus = [
            CorpusEntry(id="a", fen="x", label=99.0, category="quiet"),
            CorpusEntry(id="b", fen="x", label=99.0, category="quiet"),
        ]
        good_loss = blended_loss(good_corpus, analyses, Constants())
        bad_loss = blended_loss(bad_corpus, analyses, Constants())
        assert bad_loss > good_loss * 100  # expert MSE dominates ≈ 99² ≈ 9800

    def test_zero_when_no_corpus(self) -> None:
        # Empty corpus → both components 0.
        assert blended_loss([], {}, Constants()) == 0.0


# --------------------------------------------------------------------------- #
# Optimiser                                                                    #
# --------------------------------------------------------------------------- #


pytest.importorskip("scipy")


class TestTuneConstantsConvergesToKnownOptimum:
    """Synthetic recovery test: we pick a known K, compute V at it, label
    each position with that V, then tune from a different K. A good optimiser
    should walk back to the original K (or very close)."""

    def test_recovers_k_shallow(self) -> None:
        # Diverse synthetic evals so the optimiser has signal to chew on.
        eval_sets = [
            [80, 30, 10, -5, -20, -40],
            [200, 100, 50, 0, -50, -100],
            [+5, 0, -5, -10, -15, -25],
            [350, 50, 20, 0, -15, -30],
            [0, -250, -400, -600, -900, -1200],
            [+450, +420, +400, +380, +350, +300],
            [120, 80, 40, 0, -40, -80],
        ]
        # Compute "true" Vs at K_target, label corpus with them, then tune.
        target = Constants(k_shallow=100.0)
        corpus: list[CorpusEntry] = []
        analyses: dict[str, CachedAnalysis] = {}
        for i, evals in enumerate(eval_sets):
            a = _analysis(evals, legal=20)
            v = recompute_v(a, target)
            assert v is not None
            corpus.append(CorpusEntry(id=str(i), fen="x", label=v))
            analyses[str(i)] = a

        # Start from defaults (K_shallow=150) — far from target=100.
        result = tune_constants(corpus, analyses, mode="expert", max_iter=400)
        assert result.converged
        # Other constants are unconstrained by labels-only loss, so we only
        # assert the *one* the labels uniquely identify: K_SHALLOW.
        assert result.constants.k_shallow == pytest.approx(100.0, abs=1.0)
        assert result.loss < 1e-3

    def test_initial_constants_respected(self) -> None:
        # Trivial corpus where any constants give loss=0 → optimiser shouldn't
        # move from `initial`.
        analyses: dict[str, CachedAnalysis] = {}
        corpus: list[CorpusEntry] = []  # no labels, no categories → loss=0
        custom = Constants(k_shallow=200.0)
        result = tune_constants(corpus, analyses, mode="expert", initial=custom)
        # With zero gradient, L-BFGS-B exits at x0.
        assert result.constants.k_shallow == pytest.approx(200.0, abs=0.001)


# --------------------------------------------------------------------------- #
# build_report                                                                 #
# --------------------------------------------------------------------------- #


class TestBuildReport:
    def test_counts_and_buckets(self) -> None:
        corpus = [
            CorpusEntry(id="quiet1", fen="x", label=5.0, category="quiet"),
            CorpusEntry(id="quiet2", fen="x", label=8.0, category="quiet"),
            CorpusEntry(id="sharp1", fen="x", label=80.0, category="sharp"),
            CorpusEntry(id="terminal", fen="x", label=None, category=None),
        ]
        analyses = {
            "quiet1": _analysis([0, 0, 0, 0, 0, 0], legal=20),
            "quiet2": _analysis([5, 0, -5, -10, -15, -25], legal=20),
            "sharp1": _analysis([0, -250, -400, -600, -900, -1200], legal=20),
            "terminal": CachedAnalysis(fen="x", lines=[], legal_count=0, is_terminal=True),
        }
        report = build_report(corpus, analyses)
        assert report.n_total == 4
        assert report.n_undefined == 1
        assert {c.name for c in report.categories} == {"quiet", "sharp"}
        # Quiet positions should be solidly in the low bucket
        quiet = next(c for c in report.categories if c.name == "quiet")
        assert quiet.low_share == 1.0
        # Sharp position should land high
        sharp = next(c for c in report.categories if c.name == "sharp")
        assert sharp.high_share == 1.0
        # Expert MSE only over labelled, defined entries (3 of them)
        assert report.expert_mse is not None
        assert report.expert_mse >= 0.0

    def test_targets_reflected_in_categories(self) -> None:
        corpus = [CorpusEntry(id="m", fen="x", category="middlegame")]
        analyses = {"m": _analysis([80, 30, 10, -5, -20, -40], legal=20)}
        report = build_report(corpus, analyses)
        cat = report.categories[0]
        assert cat.target == DEFAULT_TARGETS["middlegame"]

    def test_no_categories_no_kl(self) -> None:
        corpus = [CorpusEntry(id="x", fen="x", label=10.0)]
        analyses = {"x": _analysis([80, 30, 10, -5, -20, -40], legal=20)}
        report = build_report(corpus, analyses)
        assert report.distributional_kl is None
        assert report.expert_mse is not None


# --------------------------------------------------------------------------- #
# JSON round-trip                                                             #
# --------------------------------------------------------------------------- #


class TestJsonRoundTrip:
    def test_corpus_round_trip(self) -> None:
        corpus = [
            CorpusEntry(id="a", fen="rnbq", label=10.0, category="quiet"),
            CorpusEntry(id="b", fen="ppp"),  # no label, no category
        ]
        round_tripped = corpus_from_json(corpus_to_json(corpus))
        assert round_tripped == corpus

    def test_analyses_round_trip(self) -> None:
        analyses = {
            "a": _analysis([80, 30, 10, -5, -20, -40], legal=20),
            "b": _analysis(["M3", 200, 0, -200, -400, -600], legal=20),
            "c": CachedAnalysis(fen="x", lines=[], legal_count=0, is_terminal=True),
        }
        round_tripped = analyses_from_json(analyses_to_json(analyses))
        assert round_tripped == analyses


# --------------------------------------------------------------------------- #
# Sanity: starter corpus is loadable and shaped sanely                         #
# --------------------------------------------------------------------------- #


class TestStarterCorpus:
    def test_starter_corpus_loads_and_validates(self) -> None:
        from pathlib import Path

        path = Path(__file__).parent / "fixtures" / "calibration_corpus.json"
        import json

        corpus = corpus_from_json(json.loads(path.read_text(encoding="utf-8")))
        assert len(corpus) >= 15
        # Every entry must have an id and FEN; the FEN must parse.
        for e in corpus:
            assert e.id and e.fen
            try:
                chess.Board(e.fen)
            except ValueError as exc:  # pragma: no cover — diagnostic only
                pytest.fail(f"corpus entry {e.id} has invalid FEN: {exc}")

    def test_starter_corpus_covers_all_default_categories(self) -> None:
        from pathlib import Path

        path = Path(__file__).parent / "fixtures" / "calibration_corpus.json"
        import json

        corpus = corpus_from_json(json.loads(path.read_text(encoding="utf-8")))
        cats = {e.category for e in corpus if e.category}
        # Should at least touch the four default-target categories.
        assert {"quiet", "middlegame", "sharp", "tactical"}.issubset(cats)


# --------------------------------------------------------------------------- #
# Sanity: K_SHALLOW default still 150 (regression — explainer math depends on it) #
# --------------------------------------------------------------------------- #


def test_k_shallow_unchanged() -> None:
    """If you tune K_SHALLOW, also update the explainer's _score_from_raw and
    the worked examples in README §3.7. This test exists to make that linkage
    visible — flip the expected value when you intentionally change it."""
    assert K_SHALLOW == 150.0
    assert math.isclose(Constants().k_shallow, 150.0)
