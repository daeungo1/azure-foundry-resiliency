"""SDK-over-mock-HTTP chaos matrix; no sockets, Azure credentials or real model calls."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from typing import TypedDict

import httpx
from openai import AsyncOpenAI
from openai.types.responses import Response

from .config import TARGET_MODELS, Backend, Settings
from .examples import example_settings
from .router import Request, Router, RoutingExhausted
from .transport import BackendError, SDKTransport

SCENARIOS = (
    "healthy",
    "region_429",
    "same_model_429",
    "same_model_503",
    "fallback_model_429",
    "correlated_429",
    "primary_503",
    "primary_timeout",
    "auth_401",
    "headroom",
)
SUBSCRIPTION_SCENARIOS = (
    "subscription_429",
    "subscription_503",
    "subscription_request_headroom",
    "subscription_token_headroom",
)


class HTTPAttempt(TypedDict):
    backend_id: str
    model: str
    region: str
    subscription: str
    status: int


class RequestSample(TypedDict):
    success: bool
    selected_model: str | None
    selected_region: str | None
    selected_subscription: str | None
    degraded: bool
    output_verified: bool
    failure_reason: str | None
    attempts: list[HTTPAttempt]


class ScenarioResult(TypedDict):
    requested_model: str
    routing_mode: str
    scenario: str
    streaming: bool
    requests: int
    successes: int
    failures: int
    http_attempts: int
    max_attempts_per_request: int
    degraded_responses: int
    model_invariant: bool
    first_token_ms: list[float]
    elapsed_ms: float
    passed: bool
    samples: list[RequestSample]


class Topology(TypedDict):
    subscriptions: list[str]
    regions: list[str]
    backend_count: int
    quota_pool_count: int
    quota_scope_assumption: str
    capacity_isolation_verified: bool


class SimulationReport(TypedDict):
    generated_at: str
    mode: str
    live_azure_calls: int
    sdk_version: str
    timing_mode: str
    policy: dict[str, float | int]
    topology: Topology
    passed: bool
    scenarios: list[ScenarioResult]


class SimulationClock:
    """Advance policy time only for simulated waits, not SDK startup or host scheduling."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds
        await asyncio.sleep(0)


def body(model: str) -> dict[str, object]:
    return {
        "id": "resp_simulation",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
        "parallel_tool_calls": False,
        "tool_choice": "none",
        "tools": [],
        "output": [
            {
                "type": "message",
                "id": "msg_simulation",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}],
            }
        ],
    }


@asynccontextmanager
async def mock_transport(
    settings: Settings, scenario: str, model: str, trace: list[HTTPAttempt]
) -> AsyncIterator[tuple[SDKTransport, Counter[str]]]:
    if scenario not in (*SCENARIOS, *SUBSCRIPTION_SCENARIOS):
        raise ValueError(f"Unknown simulation scenario: {scenario}")
    counts: Counter[str] = Counter()
    clients = {}
    primary = next(
        backend.id
        for backend in settings.backends
        if backend.model == model and backend.region == "Region01"
    )
    next_model = next(candidate for candidate in TARGET_MODELS if candidate != model)

    def handler(backend: Backend, req: httpx.Request) -> httpx.Response:
        counts[backend.id] += 1
        is_primary = backend.id == primary
        if scenario == "primary_timeout" and is_primary:
            trace.append(
                HTTPAttempt(
                    backend_id=backend.id,
                    model=backend.model,
                    region=backend.region,
                    subscription=backend.subscription,
                    status=0,
                )
            )
            raise httpx.ReadTimeout("injected timeout", request=req)
        status = 200
        if (
            (scenario == "region_429" and is_primary)
            or (scenario == "same_model_429" and backend.model == model)
            or (scenario == "fallback_model_429" and backend.model in {model, next_model})
            or scenario == "correlated_429"
            or (scenario == "subscription_429" and backend.subscription == "Subscription01")
        ):
            status = 429
        elif (
            (scenario == "primary_503" and is_primary)
            or (scenario == "same_model_503" and backend.model == model)
            or (scenario == "subscription_503" and backend.subscription == "Subscription01")
        ):
            status = 503
        elif scenario == "auth_401":
            status = 401
        trace.append(
            HTTPAttempt(
                backend_id=backend.id,
                model=backend.model,
                region=backend.region,
                subscription=backend.subscription,
                status=status,
            )
        )
        if status != 200:
            return httpx.Response(
                status, headers={"Retry-After": "60"}, json={"error": {"code": "injected"}}
            )
        headers = {}
        if scenario == "headroom" and is_primary:
            headers = {
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "60",
            }
        if backend.subscription == "Subscription01" and backend.model == model:
            if scenario == "subscription_request_headroom":
                headers = {
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": "60",
                }
            elif scenario == "subscription_token_headroom":
                headers = {
                    "x-ratelimit-remaining-tokens": "23",
                    "x-ratelimit-reset-tokens": "60",
                }
        data = body(backend.model)
        if json.loads(req.content).get("stream"):
            events = [
                {"type": "response.created", "response": data},
                {
                    "type": "response.output_text.delta",
                    "delta": "OK",
                    "sequence_number": 1,
                    "item_id": "msg_simulation",
                    "output_index": 0,
                    "content_index": 0,
                    "logprobs": [],
                },
                {"type": "response.completed", "response": data},
            ]
            payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(
                200,
                headers=headers | {"content-type": "text/event-stream"},
                content=payload.encode(),
            )
        return httpx.Response(200, headers=headers, json=data)

    def make_handler(backend: Backend) -> httpx.MockTransport:
        return httpx.MockTransport(lambda req: handler(backend, req))

    try:
        for backend in settings.backends:
            clients[backend.id] = AsyncOpenAI(
                api_key="simulation-not-a-secret",
                base_url=backend.base_url,
                max_retries=0,
                http_client=httpx.AsyncClient(transport=make_handler(backend)),
            )
        yield SDKTransport(clients), counts
    finally:
        for client in clients.values():
            await client.close()


async def _scenario(
    base_config: Settings, model: str, mixed: bool, scenario: str, count: int, streaming: bool
) -> ScenarioResult:
    config = base_config.model_copy(
        update={
            "deadline_seconds": 15,
            "attempt_timeout_seconds": 10,
            "first_token_timeout_seconds": 10,
            "backoff_base_seconds": 60,
            "backoff_cap_seconds": 60,
            "circuit_break_seconds": 60,
        }
    )
    success = failures = degraded = max_attempts = 0
    invariant = True
    first_tokens: list[float] = []
    trace: list[HTTPAttempt] = []
    samples: list[RequestSample] = []
    backends = {backend.id: backend for backend in config.backends}
    model_endpoints = sum(backend.model == model for backend in config.backends)
    subscriptions = {backend.subscription for backend in config.backends}
    start = time.perf_counter()
    async with mock_transport(config, scenario, model, trace) as (transport, counts):
        clock = SimulationClock()
        client = Router(
            config,
            transport,
            now=clock.now,
            sleep=clock.sleep,
            random_value=lambda: 1,
        )
        for _ in range(count):
            trace_start = len(trace)
            sample = RequestSample(
                success=False,
                selected_model=None,
                selected_region=None,
                selected_subscription=None,
                degraded=False,
                output_verified=False,
                failure_reason=None,
                attempts=[],
            )
            req = Request(
                model=model,
                input="Reply with OK only.",
                estimated_input_tokens=8,
                max_output_tokens=16,
                allow_model_fallback=mixed,
                preferred_regions=config.region_preferences["Region01"],
            )
            try:
                if streaming:
                    async with client.stream(req) as result:
                        if result.reply.chunks is None:
                            raise RuntimeError("SDK streaming adapter did not return chunks")
                        text = "".join([chunk async for chunk in result.reply.chunks])
                        output_verified = text == "OK"
                else:
                    result = await client.route(req)
                    response = Response.model_validate(result.reply.payload)
                    output_verified = response.output_text == "OK" and (
                        response.model == result.selected_model
                    )
                sample.update(
                    {
                        "success": True,
                        "selected_model": result.selected_model,
                        "selected_region": result.resource_region,
                        "selected_subscription": backends[result.backend_id].subscription,
                        "degraded": result.degraded,
                        "output_verified": output_verified,
                    }
                )
                success += 1
                degraded += int(result.degraded)
                allowed = [model, *config.fallbacks.get(model, [])] if mixed else [model]
                invariant &= result.selected_model in allowed
                invariant &= result.selected_version == TARGET_MODELS[result.selected_model]
                max_attempts = max(max_attempts, len(result.attempts))
                if result.first_token_ms is not None:
                    first_tokens.append(result.first_token_ms)
            except RoutingExhausted as error:
                sample["failure_reason"] = error.reason
                failures += 1
                max_attempts = max(max_attempts, len(error.attempts))
            except BackendError as error:
                if error.status != 401:
                    raise
                failures += 1
                sample["failure_reason"] = f"http_{error.status}"
                max_attempts = max(max_attempts, 1)
            sample["attempts"] = trace[trace_start:]
            samples.append(sample)
        attempts = counts.total()
    expect_failure = (
        scenario in {"correlated_429", "auth_401"}
        or (scenario in {"same_model_429", "same_model_503", "fallback_model_429"} and not mixed)
        or (
            model_endpoints == 1
            and not mixed
            and scenario in {"region_429", "primary_503", "primary_timeout"}
        )
    )
    # Headroom is shared by the conservative subscription-level pool, not per region.
    expected_successes = (
        1
        if scenario == "headroom" and not mixed and len(subscriptions) == 1
        else (0 if expect_failure else count)
    )
    if mixed and scenario in {"same_model_429", "same_model_503", "fallback_model_429"}:
        blocked = model_endpoints * (2 if scenario == "fallback_model_429" else 1)
        expected_successes = max(0, count - blocked // config.max_attempts)
    passed = (
        success == expected_successes
        and failures == count - expected_successes
        and invariant
        and max_attempts <= config.max_attempts
        and (
            scenario not in {"same_model_429", "same_model_503", "fallback_model_429"}
            or not mixed
            or degraded == expected_successes
        )
        and (scenario != "auth_401" or attempts == count)
        and all(sample["output_verified"] for sample in samples if sample["success"])
        and attempts == sum(len(sample["attempts"]) for sample in samples)
    )
    if samples:
        first_trace = [attempt["status"] for attempt in samples[0]["attempts"]]
        if scenario in {"same_model_429", "same_model_503", "fallback_model_429"}:
            status = 503 if scenario == "same_model_503" else 429
            failed_endpoints = model_endpoints * (
                2 if mixed and scenario == "fallback_model_429" else 1
            )
            initial_attempts = min(failed_endpoints, config.max_attempts)
            first_succeeds = mixed and failed_endpoints < config.max_attempts
            passed &= first_trace == [status] * initial_attempts + ([200] if first_succeeds else [])
            if mixed:
                failed_models = [model]
                if scenario == "fallback_model_429":
                    failed_models.append(config.fallbacks[model][0])
                passed &= all(
                    sample["selected_model"] not in failed_models
                    and sample["degraded"]
                    and len(sample["attempts"])
                    == (
                        failed_endpoints % config.max_attempts + 1
                        if index == failed_endpoints // config.max_attempts
                        else 1
                    )
                    for index, sample in enumerate(samples)
                    if sample["success"]
                )
        if scenario in SUBSCRIPTION_SCENARIOS:
            passed &= success == count and degraded == 0
            for index, sample in enumerate(samples):
                expected_subscription = (
                    "Subscription01"
                    if index == 0 and scenario.endswith("_headroom")
                    else "Subscription02"
                )
                passed &= (
                    sample["selected_subscription"] == expected_subscription
                    and sample["selected_model"] == model
                )
            if scenario in {"subscription_429", "subscription_503"}:
                passed &= first_trace == [429 if scenario == "subscription_429" else 503, 200]
            else:
                passed &= all(len(sample["attempts"]) == 1 for sample in samples)
    return ScenarioResult(
        requested_model=model,
        routing_mode="mixed" if mixed else "same",
        scenario=scenario,
        streaming=streaming,
        requests=count,
        successes=success,
        failures=failures,
        http_attempts=attempts,
        max_attempts_per_request=max_attempts,
        degraded_responses=degraded,
        model_invariant=invariant,
        first_token_ms=first_tokens,
        elapsed_ms=(time.perf_counter() - start) * 1000,
        passed=passed,
        samples=samples,
    )


async def simulate(
    *, count: int = 3, streaming: bool = False, subscriptions: int = 1, regions: int = 4
) -> SimulationReport:
    if not 1 <= count <= 100:
        raise ValueError("Simulation count must be between 1 and 100")
    configs = {
        mixed: example_settings(mixed=mixed, subscriptions=subscriptions, region_count=regions)
        for mixed in (False, True)
    }
    scenarios = (*SCENARIOS, *SUBSCRIPTION_SCENARIOS) if subscriptions == 2 else SCENARIOS
    rows = [
        await _scenario(configs[mixed], model, mixed, scenario, count, streaming)
        for model in TARGET_MODELS
        for mixed in (False, True)
        for scenario in scenarios
    ]
    return SimulationReport(
        generated_at=datetime.now(UTC).isoformat(),
        mode="simulation",
        live_azure_calls=0,
        sdk_version=version("openai"),
        timing_mode="virtual_policy_clock",
        policy={
            "same_model_max_attempts": configs[False].max_attempts,
            "mixed_model_max_attempts": configs[True].max_attempts,
            "deadline_seconds": 15,
            "attempt_timeout_seconds": 10,
            "first_token_timeout_seconds": 10,
            "injected_retry_after_seconds": 60,
            "local_backoff_seconds": 60,
        },
        topology=Topology(
            subscriptions=sorted({backend.subscription for backend in configs[False].backends}),
            regions=sorted({backend.region for backend in configs[False].backends}),
            backend_count=len(configs[False].backends),
            quota_pool_count=len({backend.quota_pool for backend in configs[False].backends}),
            quota_scope_assumption="Separate subscription/model/version Global Standard pools",
            capacity_isolation_verified=False,
        ),
        passed=all(row["passed"] for row in rows),
        scenarios=rows,
    )
