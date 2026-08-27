"""Steering advice (core/vol_advice.py). Pure — no engine, no policy."""

from __future__ import annotations

from core.vol_advice import (
    KEEP_IT_SIMPLE,
    MAKE_IT_MESSY,
    VolAdviceConstants,
    vol_advice,
)

C = VolAdviceConstants()


def test_winning_and_sharp_asks_you_to_simplify() -> None:
    advice = vol_advice(win_prob=0.9, volatility=70.0, constants=C)
    assert advice is not None
    assert advice.kind == KEEP_IT_SIMPLE
    assert "winning" in advice.detail.lower()


def test_losing_and_quiet_asks_you_to_complicate() -> None:
    """The counter-intuitive half: trading into a lost endgame is not holding."""

    advice = vol_advice(win_prob=0.1, volatility=10.0, constants=C)
    assert advice is not None
    assert advice.kind == MAKE_IT_MESSY
    assert "keep pieces on" in advice.detail.lower()


def test_winning_and_quiet_is_exactly_what_you_want() -> None:
    assert vol_advice(win_prob=0.9, volatility=10.0, constants=C) is None


def test_losing_and_sharp_is_exactly_what_you_want() -> None:
    assert vol_advice(win_prob=0.1, volatility=80.0, constants=C) is None


def test_a_balanced_position_never_fires() -> None:
    for volatility in (0.0, 30.0, 60.0, 100.0):
        assert vol_advice(win_prob=0.5, volatility=volatility, constants=C) is None


# --------------------------------------------------------------------------- #
# Severity: did the move cause it, or is the position simply like that?        #
# --------------------------------------------------------------------------- #


def test_causing_the_swing_escalates_to_a_warning() -> None:
    caused = vol_advice(
        win_prob=0.9, volatility=70.0, volatility_before=40.0, constants=C
    )
    assert caused is not None
    assert caused.severity == "warn"
    assert caused.volatility_delta == 30.0


def test_inheriting_a_sharp_position_is_only_an_observation() -> None:
    inherited = vol_advice(
        win_prob=0.9, volatility=70.0, volatility_before=68.0, constants=C
    )
    assert inherited is not None
    assert inherited.severity == "info"


def test_calming_a_position_you_needed_messy_is_a_warning() -> None:
    """Trading a piece when losing: volatility falls, and that is the mistake."""

    advice = vol_advice(
        win_prob=0.12, volatility=8.0, volatility_before=45.0, constants=C
    )
    assert advice is not None
    assert advice.kind == MAKE_IT_MESSY
    assert advice.severity == "warn"
    assert advice.volatility_delta == -37.0


def test_severity_is_info_when_the_prior_volatility_is_unknown() -> None:
    advice = vol_advice(win_prob=0.9, volatility=70.0, constants=C)
    assert advice is not None
    assert advice.severity == "info"
    assert advice.volatility_delta is None


# --------------------------------------------------------------------------- #
# Null-safety and boundaries                                                   #
# --------------------------------------------------------------------------- #


def test_missing_inputs_yield_none() -> None:
    assert vol_advice(win_prob=None, volatility=70.0, constants=C) is None
    assert vol_advice(win_prob=0.9, volatility=None, constants=C) is None


def test_thresholds_are_inclusive_at_the_boundary() -> None:
    assert vol_advice(
        win_prob=C.winning_win_prob, volatility=C.high_volatility, constants=C
    ) is not None
    assert vol_advice(
        win_prob=C.losing_win_prob, volatility=C.low_volatility, constants=C
    ) is not None


def test_shipped_constants_load_and_are_ordered() -> None:
    loaded = VolAdviceConstants.load()
    assert loaded.losing_win_prob < loaded.winning_win_prob
    assert loaded.low_volatility < loaded.high_volatility


def test_as_dict_is_json_shaped() -> None:
    advice = vol_advice(win_prob=0.9, volatility=70.0, volatility_before=40.0)
    assert advice is not None
    payload = advice.as_dict()
    assert set(payload) == {
        "kind", "severity", "headline", "detail",
        "win_prob", "volatility", "volatility_delta",
    }
