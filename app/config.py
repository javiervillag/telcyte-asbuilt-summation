from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="anthropic/claude-sonnet-4",
        alias="OPENROUTER_MODEL",
    )
    openrouter_model_candidates: str = Field(
        default=(
            "anthropic/claude-sonnet-4,"
            "openai/gpt-4.1"
        ),
        alias="OPENROUTER_MODEL_CANDIDATES",
    )
    openrouter_http_referer: Optional[str] = Field(default=None, alias="OPENROUTER_HTTP_REFERER")
    openrouter_app_title: str = Field(default="Telcyte As-Built Summation", alias="OPENROUTER_APP_TITLE")
    openrouter_timeout_seconds: float = Field(default=90.0, alias="OPENROUTER_TIMEOUT_SECONDS")
    openrouter_max_tokens: int = Field(default=1800, alias="OPENROUTER_MAX_TOKENS")
    max_upload_bytes: int = Field(default=35 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    include_page_images: bool = Field(default=False, alias="INCLUDE_PAGE_IMAGES")
    include_materials: bool = Field(default=False, alias="INCLUDE_MATERIALS")
    allow_llm_inferred_totals: bool = Field(default=False, alias="ALLOW_LLM_INFERRED_TOTALS")
    rate_card_codes: str = Field(default="", alias="RATE_CARD_CODES")
    rate_card_paths: str = Field(default="", alias="RATE_CARD_PATHS")

    @property
    def candidate_models(self) -> list[str]:
        return [m.strip() for m in self.openrouter_model_candidates.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
