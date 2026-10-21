"""Wall-clock SDK SSE comparison with controlled delays, never Azure inference."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from typing import TypedDict

import httpx
from openai import AsyncOpenAI

from .config import TARGET_MODELS, Backend, Settings
from .examples import example_settings
from .metrics import percentile
from .router import Request, Router, RoutingExhausted
from .simulation import HTTPAttempt, body
from .transport import BackendError, SDKTransport

logger = logging.getLogger(__name__)
COMPARISON_SCENARIOS = (
    "healthy",
    "slow_primary",
    "subscription_429",
    "subscription_503",
    "same_model_429",
)
CALIBRATION_COUNT = 3
WARMUP_TIMEOUT_SECONDS = 120


class Sample(TypedDict):
    success: bool
    output_verified: bool
    first_token_ms: float | None
    elapsed_ms: float
    selected_model: str | None
    selected_region: str | None
    selected_subscription: str | None
    degraded: bool
    failure_reason: str | None
    attempts: list[HTTPAttempt]


class Metrics(TypedDict):
    requests: int
    successes: int
    failures: int
    success_rate: float
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    ttft_max_ms: float | None
    http_attempts: int
    degraded_responses: int


class Arm(TypedDict):
    metrics: Metrics
    samples: list[Sample]


class Calibration(TypedDict):
    region: str
    first_token_ms: list[float]
    ttft_p95_ms: float


class ComparisonRow(TypedDict):
    requested_model: str
    scenario: str
    region_preferences: list[str]
    calibration: list[Calibration]
    before: Arm
    after: Arm
    passed: bool


class SummaryRow(TypedDict):
    scenario: str
    before: Metrics
    after: Metrics


class ComparisonReport(TypedDict):
    generated_at: str
    mode: str
    timing_mode: str
    clock_implementation: str
    clock_resolution_seconds: float
    live_azure_calls: int
    cold_sdk_initialization_included: bool
    sdk_version: str
    models: dict[str, str]
    policy: dict[str, float | int]
    controlled_delays_seconds: dict[str, float]
    warmup_requests: int
    warmup_timeout_seconds: int
    calibration_requests: int
    methodology: list[str]
    passed: bool
    scenarios: list[ComparisonRow]
    summary: list[SummaryRow]


@dataclass
class ControlledConditions:
    model: str
    scenario: str
    fast_delay: float
    slow_delay: float
    faults: bool = False
    trace: list[HTTPAttempt] = field(default_factory=list)


class DelayedSSE(httpx.AsyncByteStream):
    def __init__(self, model: str, delay_seconds: float) -> None:
        self.model = model
        self.delay_seconds = delay_seconds

    async def __aiter__(self) -> AsyncIterator[bytes]:
        data = body(self.model)
        yield f"data: {json.dumps({'type': 'response.created', 'response': data})}\n\n".encode()
        # OS sleep avoids early wakeups from coarse Windows event-loop timer ticks.
        await asyncio.to_thread(time.sleep, self.delay_seconds)
        delta = {
            "type": "response.output_text.delta",
            "delta": "OK",
            "sequence_number": 1,
            "item_id": "msg_simulation",
            "output_index": 0,
            "content_index": 0,
            "logprobs": [],
        }
        yield f"data: {json.dumps(delta)}\n\n".encode()
        yield f"data: {json.dumps({'type': 'response.completed', 'response': data})}\n\n".encode()


@asynccontextmanager
async def controlled_transport(
    config: Settings, conditions: ControlledConditions
) -> AsyncIterator[SDKTransport]:
    clients: dict[str, AsyncOpenAI] = {}

    def handle(backend: Backend, request: httpx.Request) -> httpx.Response:
        if not json.loads(request.content).get("stream"):
            raise ValueError("Controlled TTFT comparisons require streaming")
        status = 200
        if conditions.faults and backend.model == conditions.model:
            if conditions.scenario == "same_model_429" or (
                conditions.scenario == "subscription_429"
                and backend.subscription == "Subscription01"
            ):
                status = 429
            elif (
                conditions.scenario == "subscription_503"
                and backend.subscription == "Subscription01"
            ):
                status = 503
        conditions.trace.append(
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
        delay = (
            conditions.slow_delay
            if conditions.scenario == "slow_primary" and backend.region == "Region01"
            else conditions.fast_delay
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=DelayedSSE(backend.model, delay),
        )

    def mock(backend: Backend) -> httpx.MockTransport:
        return httpx.MockTransport(lambda request: handle(backend, request))

    try:
        for backend in config.backends:
            client = AsyncOpenAI(
                api_key="controlled-comparison-not-a-secret",
                base_url=backend.base_url,
                max_retries=0,
                http_client=httpx.AsyncClient(transport=mock(backend), trust_env=False),
            )
            clients[backend.id] = client
            _ = client.responses
        yield SDKTransport(clients)
    finally:
        for client in clients.values():
            await client.close()


async def _sample(
    router: Router, request: Request, trace: list[HTTPAttempt], backends: dict[str, Backend]
) -> Sample:
    start = time.perf_counter()
    trace_start = len(trace)
    sample = Sample(
        success=False,
        output_verified=False,
        first_token_ms=None,
        elapsed_ms=0,
        selected_model=None,
        selected_region=None,
        selected_subscription=None,
        degraded=False,
        failure_reason=None,
        attempts=[],
    )
    try:
        async with router.stream(request) as result:
            if result.reply.chunks is None or result.first_token_ms is None:
                raise RuntimeError("Expected first-text timing and SDK SSE chunks")
            sample["first_token_ms"] = result.first_token_ms
            text = "".join([chunk async for chunk in result.reply.chunks])
            if text != "OK":
                raise RuntimeError("Controlled SSE output did not match the expected text")
            sample.update(
                {
                    "success": True,
                    "output_verified": True,
                    "selected_model": result.selected_model,
                    "selected_region": result.resource_region,
                    "selected_subscription": backends[result.backend_id].subscription,
                    "degraded": result.degraded,
                }
            )
    except RoutingExhausted as error:
        sample["failure_reason"] = error.reason
    except BackendError as error:
        sample["failure_reason"] = f"http_{error.status}:{error.code}"
        logger.warning("comparison_stream_failed status=%s code=%s", error.status, error.code)
    sample["elapsed_ms"] = (time.perf_counter() - start) * 1000
    sample["attempts"] = trace[trace_start:]
    return sample


def _metrics(samples: list[Sample]) -> Metrics:
    ttfts = [
        sample["first_token_ms"]
        for sample in samples
        if sample["success"] and sample["first_token_ms"] is not None
    ]
    successes = sum(sample["success"] for sample in samples)
    return Metrics(
        requests=len(samples),
        successes=successes,
        failures=len(samples) - successes,
        success_rate=successes / len(samples),
        ttft_p50_ms=percentile(ttfts, 0.5) if ttfts else None,
        ttft_p95_ms=percentile(ttfts, 0.95) if ttfts else None,
        ttft_max_ms=max(ttfts) if ttfts else None,
        http_attempts=sum(len(sample["attempts"]) for sample in samples),
        degraded_responses=sum(sample["degraded"] for sample in samples),
    )


async def _calibrate(
    config: Settings,
    transport: SDKTransport,
    model: str,
    trace: list[HTTPAttempt],
    backends: dict[str, Backend],
) -> list[Calibration]:
    measurements: list[Calibration] = []
    for backend in config.backends:
        if backend.model != model or backend.subscription != "Subscription01":
            continue
        single = config.model_copy(
            update={"backends": [backend], "fallbacks": {}, "max_attempts": 1}
        )
        router = Router(single, transport, now=time.perf_counter)
        request = Request(
            model=model,
            input="Reply with OK only.",
            estimated_input_tokens=8,
            max_output_tokens=16,
            stream=True,
        )
        async with asyncio.timeout(WARMUP_TIMEOUT_SECONDS):
            reply = await transport.send(backend, request, WARMUP_TIMEOUT_SECONDS)
            try:
                if reply.chunks is None:
                    raise RuntimeError("Warmup did not return SDK SSE chunks")
                text = "".join([chunk async for chunk in reply.chunks])
                if text != "OK":
                    raise RuntimeError("Warmup SSE output did not match the expected text")
            finally:
                if reply.close is not None:
                    await reply.close()
        timings: list[float] = []
        for _ in range(CALIBRATION_COUNT):
            sample = await _sample(router, request, trace, backends)
            if not sample["success"] or sample["first_token_ms"] is None:
                raise RuntimeError(
                    f"Calibration failed for {backend.id}: {sample['failure_reason']}"
                )
            timings.append(sample["first_token_ms"])
        measurements.append(
            Calibration(
                region=backend.region,
                first_token_ms=timings,
                ttft_p95_ms=percentile(timings, 0.95),
            )
        )
    return sorted(measurements, key=lambda item: item["ttft_p95_ms"])


async def _compare_case(
    config: Settings,
    transport: SDKTransport,
    conditions: ControlledConditions,
    model: str,
    scenario: str,
    count: int,
) -> ComparisonRow:
    conditions.model = model
    conditions.scenario = scenario
    conditions.faults = False
    conditions.trace.clear()
    trace = conditions.trace
    backends = {backend.id: backend for backend in config.backends}
    primary = next(
        backend
        for backend in config.backends
        if backend.model == model
        and backend.region == "Region01"
        and backend.subscription == "Subscription01"
    )
    before_config = config.model_copy(
        update={"backends": [primary], "fallbacks": {}, "max_attempts": 1}
    )
    calibration = await _calibrate(config, transport, model, trace, backends)
    preferred = [item["region"] for item in calibration]
    conditions.faults = True
    before_samples: list[Sample] = []
    after_samples: list[Sample] = []
    after_router = Router(config, transport, now=time.perf_counter)
    for index in range(count):
        arms = ("before", "after") if index % 2 == 0 else ("after", "before")
        for arm in arms:
            request = Request(
                model=model,
                input="Reply with OK only.",
                estimated_input_tokens=8,
                max_output_tokens=16,
                preferred_regions=preferred if arm == "after" else ["Region01"],
                allow_model_fallback=arm == "after",
            )
            router = (
                after_router
                if arm == "after"
                else Router(before_config, transport, now=time.perf_counter)
            )
            sample = await _sample(router, request, trace, backends)
            (after_samples if arm == "after" else before_samples).append(sample)
    before = _metrics(before_samples)
    after = _metrics(after_samples)
    expected_before = count if scenario in {"healthy", "slow_primary"} else 0
    return ComparisonRow(
        requested_model=model,
        scenario=scenario,
        region_preferences=preferred,
        calibration=calibration,
        before=Arm(metrics=before, samples=before_samples),
        after=Arm(metrics=after, samples=after_samples),
        passed=(
            before["successes"] == expected_before
            and after["successes"] == count
            and after["degraded_responses"] == (count if scenario == "same_model_429" else 0)
        ),
    )


async def compare_streaming(
    count: int = 20, *, fast_delay_seconds: float = 0.03, slow_delay_seconds: float = 0.25
) -> ComparisonReport:
    if not 1 <= count <= 100:
        raise ValueError("Comparison count must be between 1 and 100")
    if not (
        math.isfinite(fast_delay_seconds)
        and math.isfinite(slow_delay_seconds)
        and 0 < fast_delay_seconds < slow_delay_seconds <= 1
    ):
        raise ValueError("Controlled delays require 0 < fast < slow <= 1 second")
    config = example_settings(mixed=True, subscriptions=2, region_count=4).model_copy(
        update={
            "backoff_base_seconds": 60,
            "backoff_cap_seconds": 60,
            "circuit_break_seconds": 60,
        }
    )
    rows: list[ComparisonRow] = []
    conditions = ControlledConditions(
        model=next(iter(TARGET_MODELS)),
        scenario="healthy",
        fast_delay=fast_delay_seconds,
        slow_delay=slow_delay_seconds,
    )
    async with controlled_transport(config, conditions) as transport:
        for model in TARGET_MODELS:
            for scenario in COMPARISON_SCENARIOS:
                rows.append(
                    await _compare_case(config, transport, conditions, model, scenario, count)
                )
    summary = [
        SummaryRow(
            scenario=scenario,
            before=_metrics(
                [
                    sample
                    for row in rows
                    if row["scenario"] == scenario
                    for sample in row["before"]["samples"]
                ]
            ),
            after=_metrics(
                [
                    sample
                    for row in rows
                    if row["scenario"] == scenario
                    for sample in row["after"]["samples"]
                ]
            ),
        )
        for scenario in COMPARISON_SCENARIOS
    ]
    return ComparisonReport(
        generated_at=datetime.now(UTC).isoformat(),
        mode="controlled_streaming_comparison",
        timing_mode="real_monotonic_clock",
        clock_implementation=time.get_clock_info("perf_counter").implementation,
        clock_resolution_seconds=time.get_clock_info("perf_counter").resolution,
        live_azure_calls=0,
        cold_sdk_initialization_included=False,
        sdk_version=version("openai"),
        models=dict(TARGET_MODELS),
        policy={
            "deadline_seconds": config.deadline_seconds,
            "first_token_timeout_seconds": config.first_token_timeout_seconds,
            "stream_timeout_seconds": config.stream_timeout_seconds,
            "before_max_attempts": 1,
            "after_max_attempts": config.max_attempts,
        },
        controlled_delays_seconds={
            "normal": fast_delay_seconds,
            "slow_primary": slow_delay_seconds,
        },
        warmup_requests=len(rows) * 4,
        warmup_timeout_seconds=WARMUP_TIMEOUT_SECONDS,
        calibration_requests=len(rows) * 4 * CALIBRATION_COUNT,
        methodology=[
            "Actual OpenAI SDK and delayed mock SSE; no sockets or Azure inference.",
            "Same prompt, model versions, clients and timeouts; warm SDK resources before timing.",
            "Before: fixed Subscription01/Region01 endpoint, one attempt and no model fallback.",
            "After: calibrated region order, shared router state and approved fixture fallback.",
            "Alternate before/after execution order; calibration and warmup excluded from results.",
            "Readiness warmup is capped at 120s; measured request deadlines are unchanged.",
            "Success requires OK text and stream completion; no-token failures have null TTFT.",
            "TTFT includes attempts and waits; percentiles pool raw successful request samples.",
            "Local timing and injected delays are not Azure latency or production SLO evidence.",
            "Mock clients disable ambient proxy routing; only the supplied MockTransport is used.",
            "Calibration uses Subscription01 endpoints; it is not cross-subscription ranking.",
            "Fallback order is a fixture, not a model quality or latency ranking.",
        ],
        passed=all(row["passed"] for row in rows),
        scenarios=rows,
        summary=summary,
    )
