from __future__ import annotations

from core.acceptable import TAU_DEFAULT, acceptable_set, tau_for


def test_tau_for_is_constant_in_phases_0_to_3() -> None:
    assert tau_for() == TAU_DEFAULT == 2.5
    assert tau_for(0.0) == 2.5
    assert tau_for(99.0) == 2.5


class TestTauForPhase4Mapping:
    def test_disabled_ignores_volatility(self) -> None:
        assert tau_for(80.0, base=2.5, enabled=False, tau_min=1.0, tau_max=6.0) == 2.5

    def test_none_volatility_returns_base(self) -> None:
        assert tau_for(None, base=2.5, enabled=True, tau_min=1.0, tau_max=6.0) == 2.5

    def test_endpoints(self) -> None:
        assert tau_for(0.0, enabled=True, tau_min=1.0, tau_max=6.0) == 1.0
        assert tau_for(100.0, enabled=True, tau_min=1.0, tau_max=6.0) == 6.0

    def test_midpoint_and_monotone(self) -> None:
        assert tau_for(50.0, enabled=True, tau_min=1.0, tau_max=6.0) == 3.5
        assert tau_for(20.0, enabled=True, tau_min=1.0, tau_max=6.0) < tau_for(
            80.0, enabled=True, tau_min=1.0, tau_max=6.0
        )

    def test_clamps_out_of_range_volatility(self) -> None:
        assert tau_for(150.0, enabled=True, tau_min=1.0, tau_max=6.0) == 6.0


def test_best_move_always_acceptable() -> None:
    evals = {"best": 0.60, "mid": 0.30, "bad": 0.10}
    a = acceptable_set(evals, tau=2.5)
    assert "best" in a


def test_within_tau_included_outside_excluded() -> None:
    # best=0.60; 0.585 loses 1.5 pts (<=2.5 -> in); 0.55 loses 5 pts (out).
    evals = {"best": 0.60, "close": 0.585, "far": 0.55}
    a = acceptable_set(evals, tau=2.5)
    assert a == {"best", "close"}


def test_tau_defaults_to_tau_for_when_none() -> None:
    evals = {"best": 0.60, "close": 0.585, "far": 0.55}
    assert acceptable_set(evals) == {"best", "close"}


def test_accepts_wdl_triples() -> None:
    evals = {
        "best": (1000, 0, 0),
        "draw": (0, 1000, 0),
    }
    # best win% 100, draw 50 -> loss 50 -> excluded.
    assert acceptable_set(evals, tau=2.5) == {"best"}


def test_empty_is_empty() -> None:
    assert acceptable_set({}) == set()
