"""Engine-free tests for the Maia-2 policy backend (core.human.Maia2Policy).

These never load the model — they exercise the pure Elo-clamp, the ``available``
guard, and the ``best_available_policy`` selector, so they run fast and stay
deterministic regardless of whether the ``maia2`` package/weights are present.
The real inference path is integration-only (needs the downloaded weights).
"""

from __future__ import annotations

from core.human import Maia2Policy, Maia3Policy, best_available_policy


def test_maia2_clamps_elo_to_trained_range() -> None:
    policy = Maia2Policy(elo_min=1100, elo_max=2000)
    assert policy._clamp(800) == 1100  # below range -> floor
    assert policy._clamp(2600) == 2000  # above range -> ceil
    assert policy._clamp(1500) == 1500  # inside -> unchanged


def test_maia2_available_is_boolean() -> None:
    # True where the maia2 package is installed, False otherwise — never raises.
    assert isinstance(Maia2Policy().available, bool)


def test_best_available_policy_returns_maia_or_none() -> None:
    # Preference order is Maia-3 > Maia-2 > None; any of the three is acceptable
    # depending on what's installed.
    policy = best_available_policy()
    assert policy is None or isinstance(policy, (Maia2Policy, Maia3Policy))


def test_maia3_clamps_elo_to_trained_range() -> None:
    # Maia-3 reaches master strength: the trained range is ~600-2600, unlike
    # Maia-2's 1100-2000 cap.
    policy = Maia3Policy(elo_min=600, elo_max=2600)
    assert policy._clamp(400) == 600  # below range -> floor
    assert policy._clamp(3000) == 2600  # above range -> ceil
    assert policy._clamp(2300) == 2300  # inside -> unchanged


def test_maia3_available_is_boolean() -> None:
    assert isinstance(Maia3Policy().available, bool)


def test_maia3_call_returns_empty_when_load_fails(monkeypatch) -> None:
    # Missing/broken backend must degrade to {} (findability -> null), never raise.
    policy = Maia3Policy()

    def _boom() -> None:
        raise RuntimeError("no maia3")

    monkeypatch.setattr(policy, "_load", _boom)
    import chess

    board = chess.Board()
    try:
        result = policy(board.fen(), 1500, list(board.legal_moves))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"__call__ must not raise, got {exc!r}") from exc
    assert result == {}


def test_maia2_call_returns_empty_without_model_when_unavailable(monkeypatch) -> None:
    # If the backend can't load, __call__ must degrade to {} (findability -> null),
    # never raise. Simulate an unavailable model load.
    policy = Maia2Policy()

    def _boom() -> None:
        raise RuntimeError("no maia2")

    monkeypatch.setattr(policy, "_load", _boom)
    # _model is None, _load raises -> __call__ should swallow and return {}.
    import chess

    board = chess.Board()
    try:
        result = policy(board.fen(), 1500, list(board.legal_moves))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"__call__ must not raise, got {exc!r}") from exc
    assert result == {}
