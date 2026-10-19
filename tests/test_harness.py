"""Test runnable examples, preflight checks and deterministic scenario acceptance criteria."""

from pathlib import Path

import pytest

from foundry_resiliency.config import TARGET_MODELS
from foundry_resiliency.examples import example_settings
from foundry_resiliency.preflight import validate_documents
from foundry_resiliency.simulation import simulate

pytestmark = pytest.mark.anyio


def test_examples_cover_every_model_market_and_subscription() -> None:
    config = example_settings(mixed=True, subscriptions=2, include_expansion=True)
    assert len(config.backends) == 40
    assert set(config.region_preferences) == {
        "Region01",
        "Region02",
        "Region03",
        "Region04",
        "Region05",
    }
    assert all(backend.azure_location is None for backend in config.backends)
    assert len(config.fallbacks) == 4
    single = example_settings(mixed=False)
    assert len(single.backends) == 16
    assert not single.fallbacks
    assert len({backend.quota_pool for backend in single.backends}) == 4


def test_preflight_rejects_mismatched_sku_or_version_or_endpoint() -> None:
    backend = (
        example_settings(mixed=False)
        .backends[0]
        .model_copy(update={"region": "Region01", "azure_location": "private-location-1"})
    )
    account = {
        "location": backend.azure_location,
        "properties": {"customSubDomainName": backend.base_url.split("//")[1].split(".")[0]},
    }
    deployment = {
        "sku": {"name": "GlobalStandard"},
        "properties": {
            "model": {"name": backend.model, "version": backend.version},
            "provisioningState": "Succeeded",
        },
    }
    validate_documents(backend, account, deployment)
    deployment["sku"]["name"] = "ProvisionedManaged"
    with pytest.raises(ValueError, match="GlobalStandard"):
        validate_documents(backend, account, deployment)
    deployment["sku"]["name"] = "GlobalStandard"
    deployment["properties"]["model"]["version"] = "wrong"
    with pytest.raises(ValueError, match="version"):
        validate_documents(backend, account, deployment)


def test_preflight_requires_real_location_separate_from_alias() -> None:
    backend = example_settings(mixed=False).backends[0]
    with pytest.raises(ValueError, match="azure_location"):
        validate_documents(backend, {}, {})


def test_preflight_keeps_validating_location_when_region_is_an_alias() -> None:
    backend = (
        example_settings(mixed=False)
        .backends[0]
        .model_copy(update={"region": "Region01", "azure_location": "private-location-1"})
    )
    account = {
        "location": "different-private-location",
        "properties": {"customSubDomainName": backend.base_url.split("//")[1].split(".")[0]},
    }
    deployment = {
        "sku": {"name": "GlobalStandard"},
        "properties": {
            "model": {"name": backend.model, "version": backend.version},
            "provisioningState": "Succeeded",
        },
    }
    with pytest.raises(ValueError, match="region mismatch"):
        validate_documents(backend, account, deployment)


async def test_all_four_models_pass_both_modes_with_expected_failures(tmp_path: Path) -> None:
    report = await simulate(count=3, streaming=False)
    assert report["mode"] == "simulation"
    assert report["live_azure_calls"] == 0
    assert report["passed"]
    assert len(report["scenarios"]) == 80
    for model in TARGET_MODELS:
        rows = {
            row["scenario"]: row
            for row in report["scenarios"]
            if row["requested_model"] == model and row["routing_mode"] == "mixed"
        }
        for scenario, status in (("same_model_429", 429), ("same_model_503", 503)):
            row = rows[scenario]
            assert row["successes"] == row["requests"] == row["degraded_responses"] == 3
            first, *rest = row["samples"]
            assert [item["status"] for item in first["attempts"]] == [status] * 4 + [200]
            assert first["selected_model"] != model
            assert first["success"] and first["output_verified"]
            assert all(item["model"] == model for item in first["attempts"][:4])
            assert first["attempts"][-1]["region"] == "Region01"
            assert all(len(sample["attempts"]) == 1 for sample in rest)
        cascade = rows["fallback_model_429"]["samples"][0]
        assert [attempt["status"] for attempt in cascade["attempts"]] == [429] * 8 + [200]
        assert cascade["selected_model"] not in {
            model,
            rows["same_model_429"]["samples"][0]["selected_model"],
        }
    for row in report["scenarios"]:
        assert row["http_attempts"] == sum(len(sample["attempts"]) for sample in row["samples"])
        assert len(row["samples"]) == row["requests"]
        assert all(sample["output_verified"] for sample in row["samples"] if sample["success"])
        if row["scenario"] == "correlated_429":
            assert not any(sample["success"] for sample in row["samples"])
        if row["scenario"] == "auth_401":
            assert all(
                [step["status"] for step in sample["attempts"]] == [401]
                for sample in row["samples"]
            )


async def test_streaming_scenario_matrix() -> None:
    report = await simulate(count=1, streaming=True)
    assert report["passed"]


async def test_simulation_clock_separates_policy_time_from_host_delay() -> None:
    import asyncio

    from foundry_resiliency.simulation import SimulationClock

    clock = SimulationClock()
    await asyncio.sleep(0.01)
    assert clock.now() == 0
    await clock.sleep(7)
    assert clock.now() == 7


async def test_simulation_reports_virtual_policy_time_explicitly() -> None:
    report = await simulate(count=1, streaming=True)
    assert report["passed"]
    assert report["timing_mode"] == "virtual_policy_clock"
    assert all(value == 0 for row in report["scenarios"] for value in row["first_token_ms"])
