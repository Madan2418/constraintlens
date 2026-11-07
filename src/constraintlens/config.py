"""Environment-backed configuration (ARCHITECTURE.md §18).

Every value comes from a ``CL_``-prefixed environment variable, with ``.env`` for
local use. ``Settings.snapshot()`` returns the effective configuration with
secrets redacted, for recording alongside evaluation runs (§18, §21.3).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FailMode = Literal["open", "closed"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Service
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Storage / transport
    db_url: str = "sqlite:///./data/constraintlens.db"
    redis_url: str = "redis://localhost:6379/0"

    # Checker thresholds (starting points; tuned on the benchmark val split, §14.4)
    theta_low: float = Field(default=0.35, ge=0.0, le=1.0)
    theta_high: float = Field(default=0.80, ge=0.0, le=1.0)
    delta_min: float = Field(default=0.15, ge=0.0, le=1.0)
    dwell_threshold_s: int = Field(default=300, gt=0)
    default_max_age_s: int = Field(default=3600, gt=0)

    # Tiers
    enable_tier_1c: bool = True
    enable_tier_2: bool = True
    nli_model: str = ""
    embed_model: str = ""

    # Judge (provider-agnostic)
    judge_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    judge_model: str = ""
    judge_api_key: SecretStr | None = None
    judge_timeout_s: float = Field(default=10.0, gt=0)
    judge_cache: bool = True
    judge_prompt_version: str = "v1"

    # Extraction (ADR 0003): off | fallback | always
    extraction_llm_mode: Literal["off", "fallback", "always"] = "fallback"

    # Failure policy and enforcement
    fail_high_risk: FailMode = "closed"
    fail_low_risk: FailMode = "open"
    max_regrounds: int = Field(default=2, ge=0)

    # Privacy
    store_full_text: bool = False
    redact_patterns: str = "email,card,phone"

    @model_validator(mode="after")
    def _band_ordered(self) -> Settings:
        if self.theta_low > self.theta_high:
            raise ValueError("CL_THETA_LOW must not exceed CL_THETA_HIGH")
        return self

    @property
    def redact_pattern_list(self) -> list[str]:
        return [p.strip() for p in self.redact_patterns.split(",") if p.strip()]

    def snapshot(self) -> dict[str, Any]:
        """Effective configuration with secrets redacted."""
        data = self.model_dump(mode="json")
        if self.judge_api_key is not None:
            data["judge_api_key"] = "***"
        return data
