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
    assert settings.fallback_path_code_list == ["UG-54", "UG-55", "UG-56", "UG-57", "DP-11"]
    assert settings.coax_rounding_increment == 10


def test_fallback_path_codes_are_trimmed() -> None:
    settings = Settings(CABLE_FALLBACK_PATH_CODES=" UG-56, , DP-11 ")
    assert settings.fallback_path_code_list == ["UG-56", "DP-11"]
