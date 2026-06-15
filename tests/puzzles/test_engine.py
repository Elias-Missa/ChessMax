import pytest

from server import engine


def analysis_with_evals(*evals: int) -> dict[str, list[dict[str, int | str]]]:
    return {
        "top_moves": [
            {"move": f"e2e{idx + 1}", "eval": eval_cp}
            for idx, eval_cp in enumerate(evals)
        ]
    }


def test_quiet_position_accepts_balanced_broad_position() -> None:
    analysis = analysis_with_evals(-20, 0, 35, 149, -150)

    assert engine.is_quiet_position(analysis) is True


def test_quiet_position_rejects_top_three_outside_range() -> None:
    analysis = analysis_with_evals(101, 0, 35, 40, 50)

    assert engine.is_quiet_position(analysis) is False


def test_quiet_position_rejects_without_five_reasonable_moves() -> None:
    analysis = analysis_with_evals(0, 15, -25, 151, -151)

    assert engine.is_quiet_position(analysis) is False


@pytest.mark.parametrize(
    ("eval_loss", "expected_grade"),
    [
        (-5, "best"),
        (10, "best"),
        (30, "good"),
        (100, "acceptable"),
        (150, "inaccuracy"),
        (300, "mistake"),
        (301, "blunder"),
    ],
)
def test_grade_from_eval_loss(eval_loss: float, expected_grade: str) -> None:
    assert engine.grade_from_eval_loss(eval_loss) == expected_grade


def test_grade_move_flips_post_move_eval_to_user_pov(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_analyze(
        fen: str,
        depth: int = 20,
        multipv: int = 5,
        engine_command: str = engine.DEFAULT_ENGINE_COMMAND,
    ) -> dict[str, list[dict[str, int | str]]]:
        assert " b " in fen
        assert depth == 18
        assert multipv == 1
        assert engine_command == engine.DEFAULT_ENGINE_COMMAND
        return {"top_moves": [{"move": "e7e5", "eval": -35}]}

    monkeypatch.setattr(engine, "analyze", fake_analyze)

    result = engine.grade_move(
        chess_start_fen(),
        "e2e4",
        best_eval=60,
    )

    assert result == {"eval_loss": 25.0, "grade": "good"}


def test_grade_move_rejects_illegal_moves() -> None:
    with pytest.raises(ValueError, match="Illegal move"):
        engine.grade_move(chess_start_fen(), "e2e5", best_eval=0)


def test_analyze_rejects_invalid_multipv() -> None:
    with pytest.raises(ValueError, match="multipv"):
        engine.analyze(chess_start_fen(), multipv=0)


def chess_start_fen() -> str:
    return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
