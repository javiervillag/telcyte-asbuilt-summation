from app.config import Settings


def test_candidate_models_are_trimmed() -> None:
    settings = Settings(OPENROUTER_MODEL_CANDIDATES=" a ,b,, c ")
    assert settings.candidate_models == ["a", "b", "c"]
