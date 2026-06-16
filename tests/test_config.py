from app.config import Settings


def test_candidate_models_are_trimmed() -> None:
    settings = Settings(OPENROUTER_MODEL_CANDIDATES=" a ,b,, c ")
    assert settings.candidate_models == ["a", "b", "c"]


def test_strict_review_badges_defaults_off() -> None:
    assert Settings().strict_review_badges is False


def test_cable_footage_flags_default_safe() -> None:
    settings = Settings()
    assert settings.include_cable_footage is False
    assert settings.auto_stamp_cable_footage is False
    assert settings.cable_path_code == "Comp-15"
    assert settings.coax_rounding_increment == 10
