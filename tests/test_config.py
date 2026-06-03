from app.config import Settings


def test_candidate_models_are_trimmed() -> None:
    settings = Settings(OPENROUTER_MODEL_CANDIDATES=" a ,b,, c ")
    assert settings.candidate_models == ["a", "b", "c"]


def test_openrouter_max_tokens_defaults_below_current_credit_limit() -> None:
    settings = Settings()
    assert settings.openrouter_max_tokens == 1800
