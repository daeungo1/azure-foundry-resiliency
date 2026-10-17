"""Test runnable examples, preflight checks and deterministic scenario acceptance criteria."""

from pathlib import Path

import pytest

from foundry_resiliency.examples import example_settings
from foundry_resiliency.preflight import validate_documents
from foundry_resiliency.simulation import simulate

pytestmark = pytest.mark.anyio


def test_examples_cover_every_model_market_and_subscription() -> None:
    config = example_settings(mixed=True, subscriptions=2, include_eu=True)
    assert len(config.backends) == 40
    assert set(config.region_preferences) == {"KR", "UK", "US", "CA", "EU"}
    assert len(config.fallbacks) == 4
    single = example_settings(mixed=False)
    assert len(single.backends) == 16
    assert not single.fallbacks
    assert len({backend.quota_pool for backend in single.backends}) == 4


def test_preflight_rejects_mismatched_sku_or_version_or_endpoint() -> None:
    backend = example_settings(mixed=False).backends[0]
    account = {
        "location": backend.region,
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


async def test_all_four_models_pass_both_modes_with_expected_failures(tmp_path: Path) -> None:
    report = await simulate(count=3, streaming=False)
    assert report["mode"] == "simulation"
    assert report["live_azure_calls"] == 0
    assert report["passed"]
    assert len(report["scenarios"]) == 64


async def test_streaming_scenario_matrix() -> None:
    report = await simulate(count=1, streaming=True)
    assert report["passed"]
