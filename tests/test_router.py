"""Deterministic tests exercise real router state, without Azure credentials or sleeps."""

import asyncio
from collections import defaultdict

import pytest

from foundry_resiliency.config import Backend, Settings
from foundry_resiliency.router import Request, Router, RoutingExhausted
from foundry_resiliency.transport import BackendError, Reply

PRIMARY = "gpt-6.1-sol"
FALLBACK = "gpt-6-luna"
pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds
        await asyncio.sleep(0)


class Script:
    def __init__(self, outcomes: dict[str, list[Reply | BackendError]]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.counts: defaultdict[str, int] = defaultdict(int)

    async def send(self, backend: Backend, request: Request, timeout_seconds: float) -> Reply:
        self.calls.append(backend.id)
        index = self.counts[backend.id]
        self.counts[backend.id] += 1
        choices = self.outcomes.get(backend.id, [Reply({"id": "ok"}, {})])
        outcome = choices[min(index, len(choices) - 1)]
        if isinstance(outcome, BackendError):
            raise outcome
        return outcome


def settings(**changes: object) -> Settings:
    backends = [
        Backend(
            id=name,
            model=model,
            version="2026-09-29" if model == PRIMARY else "2026-09-22",
            deployment=name,
            region=region,
            priority=priority,
            max_in_flight=1,
            base_url=f"https://{name}.openai.azure.com/openai/v1/",
        )
        for name, model, region, priority in [
            ("a", PRIMARY, "eastus2", 0),
            ("b", PRIMARY, "swedencentral", 1),
            ("c", FALLBACK, "eastus2", 0),
        ]
    ]
    return Settings.model_validate(
        {
            "backends": backends,
            "fallbacks": {PRIMARY: [FALLBACK]},
            "deadline_seconds": 10,
            "attempt_timeout_seconds": 1,
            "max_attempts": 4,
            "circuit_failure_threshold": 1,
            "circuit_break_seconds": 2,
            **changes,
        }
    )


def router(script: Script, config: Settings | None = None) -> tuple[Router, Clock]:
    clock = Clock()
    return (
        Router(
            config or settings(),
            script,
            now=clock.now,
            sleep=clock.sleep,
            wall_time=lambda: 0,
            random_value=lambda: 1,
        ),
        clock,
    )


def request(**changes: object) -> Request:
    return Request.model_validate(
        {
            "model": PRIMARY,
            "input": "test",
            "estimated_input_tokens": 10,
            "max_output_tokens": 20,
            **changes,
        }
    )


async def test_same_model_region_failover_precedes_model_change() -> None:
    script = Script({"a": [BackendError(429, {"retry-after-ms": "3600000"})]})
    client, clock = router(script)
    result = await client.route(request(allow_model_fallback=True))
    assert script.calls == ["a", "b"]
    assert result.selected_model == PRIMARY
    assert not result.degraded
    assert clock.value == 0


async def test_mixed_requires_request_opt_in() -> None:
    script = Script({name: [BackendError(503, {})] for name in ("a", "b")})
    client, _ = router(script, settings(max_attempts=2))
    with pytest.raises(RoutingExhausted):
        await client.route(request())
    assert "c" not in script.calls


async def test_explicit_mixed_after_both_same_model_regions_fail() -> None:
    script = Script({name: [BackendError(429, {"Retry-After": "60"})] for name in ("a", "b")})
    client, _ = router(script)
    result = await client.route(request(allow_model_fallback=True))
    assert script.calls == ["a", "b", "c"]
    assert result.degraded
    assert result.selected_model == FALLBACK
    assert [item.status for item in result.attempts] == [429, 429, 200]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
async def test_terminal_errors_never_fallback(status: int) -> None:
    script = Script({"a": [BackendError(status, {})]})
    client, _ = router(script)
    with pytest.raises(BackendError) as error:
        await client.route(request(allow_model_fallback=True))
    assert error.value.status == status
    assert script.calls == ["a"]


async def test_all_global_pools_constrained_is_bounded_failure_not_success() -> None:
    script = Script(
        {name: [BackendError(429, {"Retry-After": "3600"})] for name in ("a", "b", "c")}
    )
    client, clock = router(script)
    with pytest.raises(RoutingExhausted) as error:
        await client.route(request(allow_model_fallback=True))
    assert error.value.reason == "deadline"
    assert len(error.value.attempts) == 3
    assert error.value.retry_after_seconds >= 3600
    assert clock.value == 0


async def test_attempt_budget_is_total_not_per_backend() -> None:
    script = Script({name: [BackendError(503, {})] for name in ("a", "b")})
    client, _ = router(script, settings(max_attempts=3, circuit_break_seconds=0.1))
    with pytest.raises(RoutingExhausted) as error:
        await client.route(request())
    assert len(script.calls) == 3
    assert len(error.value.attempts) == 3
    assert error.value.reason == "attempt_budget"


async def test_header_exhaustion_skips_backend_until_reset() -> None:
    script = Script(
        {
            "a": [
                Reply(
                    {},
                    {
                        "x-ratelimit-remaining-requests": "0",
                        "x-ratelimit-reset-requests": "2s",
                    },
                )
            ]
        }
    )
    client, clock = router(script)
    await client.route(request())
    await client.route(request())
    assert script.calls == ["a", "b"]
    clock.value = 3
    await client.route(request())
    assert script.calls[-1] == "a"


async def test_token_headroom_reserves_input_plus_output() -> None:
    script = Script(
        {
            "a": [
                Reply(
                    {},
                    {
                        "x-ratelimit-remaining-tokens": "29",
                        "x-ratelimit-reset-tokens": "1m",
                    },
                )
            ]
        }
    )
    client, _ = router(script)
    await client.route(request())
    await client.route(request())
    assert script.calls == ["a", "b"]


async def test_out_of_order_header_cannot_increase_remaining_budget() -> None:
    script = Script(
        {
            "a": [
                Reply(
                    {}, {"x-ratelimit-remaining-requests": "2", "x-ratelimit-reset-requests": "60"}
                ),
                Reply(
                    {},
                    {"x-ratelimit-remaining-requests": "999", "x-ratelimit-reset-requests": "60"},
                ),
            ]
        }
    )
    client, _ = router(script)
    for _ in range(4):
        await client.route(request())
    assert script.calls == ["a", "a", "a", "b"]


async def test_half_open_success_restores_primary() -> None:
    script = Script({"a": [BackendError(503, {}), Reply({}, {})]})
    client, clock = router(script)
    assert (await client.route(request())).backend_id == "b"
    clock.value = 3
    assert (await client.route(request())).backend_id == "a"
    assert (await client.route(request())).backend_id == "a"


async def test_timeout_failover() -> None:
    class Slow(Script):
        async def send(self, backend: Backend, req: Request, timeout_seconds: float) -> Reply:
            if backend.id == "a":
                await asyncio.sleep(1)
            return await super().send(backend, req, timeout_seconds)

    client, _ = router(Slow({}), settings(attempt_timeout_seconds=0.01))
    result = await client.route(request())
    assert result.backend_id == "b"
    assert result.attempts[0].status == 0


async def test_bulkhead_and_cancel_release_capacity() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class Held(Script):
        async def send(self, backend: Backend, req: Request, timeout_seconds: float) -> Reply:
            if backend.id == "a":
                started.set()
                await release.wait()
            return await super().send(backend, req, timeout_seconds)

    client, _ = router(Held({}))
    task = asyncio.create_task(client.route(request()))
    await started.wait()
    assert (await client.route(request())).backend_id == "b"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert (await client.route(request())).backend_id == "a"


async def test_unknown_model_and_unsupported_capability_fail_before_network() -> None:
    script = Script({})
    client, _ = router(script)
    with pytest.raises(ValueError, match="model"):
        await client.route(request(model="unconfigured"))
    with pytest.raises(ValueError, match="capabilit"):
        await client.route(request(required_capabilities=["tools"]))
    assert not script.calls


async def test_user_region_preference_precedes_static_priority() -> None:
    script = Script({})
    client, _ = router(script)
    result = await client.route(request(preferred_regions=["swedencentral", "eastus2"]))
    assert result.backend_id == "b"


async def test_no_model_change_without_configured_allowlist() -> None:
    script = Script({name: [BackendError(503, {})] for name in ("a", "b")})
    client, _ = router(script, settings(fallbacks={}, max_attempts=2))
    with pytest.raises(RoutingExhausted):
        await client.route(request(allow_model_fallback=True))
    assert "c" not in script.calls


async def test_subscription_quota_pool_headroom_is_shared_across_regions() -> None:
    config = settings()
    backends = [
        backend.model_copy(update={"quota_pool": "sub-a-sol"})
        if backend.model == PRIMARY
        else backend
        for backend in config.backends
    ]
    script = Script(
        {
            "a": [
                Reply(
                    {},
                    {
                        "x-ratelimit-remaining-requests": "0",
                        "x-ratelimit-reset-requests": "60",
                    },
                )
            ]
        }
    )
    client, _ = router(script, config.model_copy(update={"backends": backends}))
    await client.route(request())
    result = await client.route(request(allow_model_fallback=True))
    assert result.backend_id == "c"
    assert script.calls == ["a", "c"]


async def test_total_deadline_is_checked_after_synchronous_transport_work() -> None:
    clock = Clock()

    class Blocking(Script):
        async def send(self, backend: Backend, req: Request, timeout_seconds: float) -> Reply:
            clock.value = 20
            return Reply({}, {})

    client = Router(settings(), Blocking({}), now=clock.now, sleep=clock.sleep)
    with pytest.raises(RoutingExhausted, match="deadline"):
        await client.route(request())
    assert client.states["a"].in_flight == 0
