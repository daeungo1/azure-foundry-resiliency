"""Configuration must not silently change deployment type, versions or fallback scope."""

import pytest
from pydantic import ValidationError

from foundry_resiliency.config import Backend, Settings


def backend(**changes: object) -> Backend:
    values: dict[str, object] = {
        "id": "a",
        "model": "gpt-6.1-sol",
        "version": "2026-09-29",
        "deployment": "sol",
        "region": "eastus2",
        "base_url": "https://example.openai.azure.com/openai/v1/",
    }
    return Backend.model_validate(values | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"deployment_type": "ProvisionedManaged"},
        {"base_url": "http://example.openai.azure.com/openai/v1/"},
        {"base_url": "https://secret@example.openai.azure.com/openai/v1/"},
        {"base_url": "https://example.openai.azure.com/openai/v1/?key=secret"},
        {"base_url": "https://example.openai.azure.com/"},
        {"max_in_flight": 0},
        {"deployment": "../other"},
    ],
)
def test_reject_unsafe_backend(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        backend(**changes)


def test_duplicate_ids_and_different_versions_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        Settings(backends=[backend(), backend()])
    with pytest.raises(ValidationError, match="version"):
        Settings(backends=[backend(), backend(id="b", version="different")])


def test_unknown_fallback_rejected() -> None:
    with pytest.raises(ValidationError, match="fallback"):
        Settings(backends=[backend()], fallbacks={"gpt-6.1-sol": ["not-deployed"]})


def test_nonfinite_deadlines_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(backends=[backend()], deadline_seconds=float("inf"))
