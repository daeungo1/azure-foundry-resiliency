"""Virtual subscriptions exercise the real router without Azure accounts or network calls."""

import json
from pathlib import Path

import pytest

from foundry_resiliency.cli import main
from foundry_resiliency.config import TARGET_MODELS
from foundry_resiliency.examples import example_settings
from foundry_resiliency.router import Request, Router
from foundry_resiliency.simulation import mock_transport, simulate

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("regions", [1, 2, 4, 8, 16])
def test_two_subscription_topology_has_independent_model_pools(regions: int) -> None:
    config = example_settings(mixed=True, subscriptions=2, region_count=regions)
    assert len(config.backends) == 2 * regions * len(TARGET_MODELS)
    assert {backend.subscription for backend in config.backends} == {
        "Subscription01",
        "Subscription02",
    }
    assert set(config.region_preferences) == {
        f"Region{number:02d}" for number in range(1, regions + 1)
    }
    assert len({backend.quota_pool for backend in config.backends}) == 8
    assert config.max_attempts == min(20, len(config.backends))
    for model in TARGET_MODELS:
        for subscription in ("Subscription01", "Subscription02"):
            candidates = [
                backend
                for backend in config.backends
                if backend.model == model and backend.subscription == subscription
            ]
            assert len(candidates) == regions
            assert len({backend.quota_pool for backend in candidates}) == 1


@pytest.mark.parametrize("regions", [0, 17])
def test_region_count_is_bounded(regions: int) -> None:
    with pytest.raises(ValueError, match="region"):
        example_settings(mixed=False, subscriptions=2, region_count=regions)


def test_expansion_flag_and_explicit_count_cannot_conflict() -> None:
    with pytest.raises(ValueError, match="expansion"):
        example_settings(mixed=False, region_count=2, include_expansion=True)


@pytest.mark.parametrize(
    "scenario",
    [
        "subscription_request_headroom",
        "subscription_token_headroom",
    ],
)
async def test_exhausted_pool_does_not_block_other_subscription(scenario: str) -> None:
    model = next(iter(TARGET_MODELS))
    config = example_settings(mixed=False, subscriptions=2, region_count=2)
    trace = []
    async with mock_transport(config, scenario, model, trace) as (transport, _counts):
        client = Router(config, transport)
        req = Request(
            model=model,
            input="Reply with OK only.",
            estimated_input_tokens=8,
            max_output_tokens=16,
            preferred_regions=config.region_preferences["Region01"],
        )
        first = await client.route(req)
        second = await client.route(req)
        by_id = {backend.id: backend for backend in config.backends}
        assert by_id[first.backend_id].subscription == "Subscription01"
        assert by_id[second.backend_id].subscription == "Subscription02"
        assert first.selected_model == second.selected_model == model
        assert not first.degraded and not second.degraded
        assert len(second.attempts) == 1
        a = [
            backend
            for backend in config.backends
            if backend.model == model and backend.subscription == "Subscription01"
        ]
        b = [
            backend
            for backend in config.backends
            if backend.model == model and backend.subscription == "Subscription02"
        ]
        assert client.budgets[a[0].id] is client.budgets[a[1].id]
        assert client.budgets[a[0].id] is not client.budgets[b[0].id]
        index = 0 if scenario == "subscription_request_headroom" else 1
        assert client.budgets[a[0].id][index].remaining == (0 if index == 0 else 23)


@pytest.mark.parametrize("regions,streaming", [(1, False), (2, True), (4, False), (8, True)])
async def test_virtual_subscription_matrix(regions: int, streaming: bool) -> None:
    report = await simulate(count=3, streaming=streaming, subscriptions=2, regions=regions)
    assert report["passed"] and report["live_azure_calls"] == 0
    assert len(report["scenarios"]) == 112
    assert report["topology"]["backend_count"] == 2 * regions * 4
    assert report["topology"]["quota_pool_count"] == 8
    for row in report["scenarios"]:
        assert row["http_attempts"] == sum(len(sample["attempts"]) for sample in row["samples"])
        assert row["max_attempts_per_request"] <= 20
        assert row["requests"] == 3
        for sample in row["samples"]:
            assert all(
                step["subscription"] in {"Subscription01", "Subscription02"}
                for step in sample["attempts"]
            )
        if row["scenario"] in {"subscription_429", "subscription_503"}:
            assert row["successes"] == 3
            assert row["degraded_responses"] == 0
            assert all(
                sample["selected_subscription"] == "Subscription02"
                and sample["selected_model"] == row["requested_model"]
                for sample in row["samples"]
            )
            expected_status = 429 if row["scenario"] == "subscription_429" else 503
            assert [step["status"] for step in row["samples"][0]["attempts"]] == [
                expected_status,
                200,
            ]
        if row["scenario"].endswith("_headroom"):
            assert row["successes"] == 3
            assert row["samples"][0]["selected_subscription"] == "Subscription01"
            assert all(
                sample["selected_subscription"] == "Subscription02"
                and not sample["degraded"]
                and len(sample["attempts"]) == 1
                for sample in row["samples"][1:]
            )
        if row["scenario"] == "correlated_429":
            assert row["successes"] == 0 and row["failures"] == 3
        if (
            regions == 8
            and row["scenario"] == "fallback_model_429"
            and row["routing_mode"] == "mixed"
        ):
            assert row["samples"][0]["failure_reason"] == "attempt_budget"
            assert len(row["samples"][0]["attempts"]) == 20
            assert row["successes"] == 2


def test_example_cli_supports_two_subscriptions_and_n_regions(tmp_path: Path) -> None:
    path = tmp_path / "two.json"
    assert (
        main(
            [
                "example",
                "--mode",
                "same",
                "--subscriptions",
                "2",
                "--regions",
                "3",
                "--output",
                str(path),
            ]
        )
        == 0
    )
    report = json.loads(path.read_text())
    assert len(report["backends"]) == 24
    assert report["max_attempts"] == 6


@pytest.mark.parametrize("regions", ["0", "17"])
def test_simulation_cli_rejects_invalid_region_count(tmp_path: Path, regions: str) -> None:
    path = tmp_path / "invalid.json"
    assert (
        main(["simulate", "--subscriptions", "2", "--regions", regions, "--output", str(path)]) == 2
    )
    assert not path.exists()


def test_simulation_cli_emits_topology_without_azure(tmp_path: Path) -> None:
    path = tmp_path / "simulation.json"
    assert (
        main(
            [
                "simulate",
                "--subscriptions",
                "2",
                "--regions",
                "2",
                "--count",
                "1",
                "--output",
                str(path),
            ]
        )
        == 0
    )
    report = json.loads(path.read_text())
    assert report["passed"] and report["live_azure_calls"] == 0
    assert report["topology"]["subscriptions"] == ["Subscription01", "Subscription02"]
