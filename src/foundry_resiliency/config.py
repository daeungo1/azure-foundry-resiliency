"""Validated configuration for existing, explicitly named model deployments."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TARGET_MODELS = {
    "gpt-6.1-sol": "2026-09-29",
    "gpt-6-luna": "2026-09-22",
    "gpt-5.6-terra": "2026-07-09",
    "gpt-5.6-luna": "2026-07-09",
}


class Validated(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Backend(Validated):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    model: str = Field(min_length=1)
    version: str = Field(min_length=1)
    deployment: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    region: str = Field(min_length=1)
    subscription: str = Field(default="Subscription01", min_length=1)
    azure_location: str | None = Field(default=None, min_length=1)
    base_url: str
    deployment_type: Literal["GlobalStandard"] = "GlobalStandard"
    priority: int = Field(default=0, ge=0)
    max_in_flight: int = Field(default=4, ge=1)
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    resource_id: str | None = None
    api_key_env: str | None = None
    quota_pool: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != "/openai/v1/"
        ):
            raise ValueError(
                "base_url must be an HTTPS /openai/v1/ URL without credentials or query"
            )
        return value


class Settings(Validated):
    backends: list[Backend] = Field(min_length=1)
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)
    region_preferences: dict[str, list[str]] = Field(default_factory=dict)
    deadline_seconds: float = Field(default=8, gt=0)
    attempt_timeout_seconds: float = Field(default=3, gt=0)
    first_token_timeout_seconds: float = Field(default=2, gt=0)
    stream_timeout_seconds: float = Field(default=60, gt=0)
    max_attempts: int = Field(default=4, ge=1, le=20)
    circuit_failure_threshold: int = Field(default=2, ge=1)
    circuit_break_seconds: float = Field(default=10, gt=0)
    backoff_base_seconds: float = Field(default=0.25, gt=0)
    backoff_cap_seconds: float = Field(default=4, gt=0)

    @model_validator(mode="after")
    def validate_pools(self) -> Self:
        if len({b.id for b in self.backends}) != len(self.backends):
            raise ValueError("backend IDs must be unique")
        versions: dict[str, str] = {}
        for backend in self.backends:
            if backend.model in versions and versions[backend.model] != backend.version:
                raise ValueError("same-model pools must pin one version per model")
            versions[backend.model] = backend.version
        for model, fallbacks in self.fallbacks.items():
            if (
                model not in versions
                or model in fallbacks
                or len(set(fallbacks)) != len(fallbacks)
                or any(candidate not in versions for candidate in fallbacks)
            ):
                raise ValueError("fallback lists must reference distinct configured models")
        return self


def load_settings(path: Path) -> Settings:
    return Settings.model_validate_json(path.read_text(encoding="utf-8"))
