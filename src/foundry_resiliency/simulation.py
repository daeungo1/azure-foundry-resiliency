"""SDK-over-mock-HTTP chaos matrix; no sockets, Azure credentials or real model calls."""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import TypedDict

import httpx
from openai import AsyncOpenAI

from .config import TARGET_MODELS, Backend, Settings
from .examples import example_settings
from .router import Request, Router, RoutingExhausted
from .transport import BackendError, SDKTransport

SCENARIOS = (
    "healthy",
    "region_429",
    "same_model_429",
    "correlated_429",
    "primary_503",
    "primary_timeout",
    "auth_401",
    "headroom",
)


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


class SimulationReport(TypedDict):
    mode: str
    live_azure_calls: int
    sdk_version: str
    passed: bool
    scenarios: list[ScenarioResult]


def body(model: str) -> dict[str, object]:
    return {
        "id": "resp_simulation",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
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
    settings: Settings, scenario: str, model: str
) -> AsyncIterator[tuple[SDKTransport, Counter[str]]]:
    counts: Counter[str] = Counter()
    clients = {}
    primary = next(
        backend.id
        for backend in settings.backends
        if backend.model == model and backend.region == "koreacentral"
    )

    def handler(backend: Backend, req: httpx.Request) -> httpx.Response:
        counts[backend.id] += 1
        is_primary = backend.id == primary
        if scenario == "primary_timeout" and is_primary:
            raise httpx.ReadTimeout("injected timeout", request=req)
        status = 200
        if (
            (scenario == "region_429" and is_primary)
            or (scenario == "same_model_429" and backend.model == model)
            or scenario == "correlated_429"
        ):
            status = 429
        elif scenario == "primary_503" and is_primary:
            status = 503
        elif scenario == "auth_401":
            status = 401
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
    model: str, mixed: bool, scenario: str, count: int, streaming: bool
) -> ScenarioResult:
    config = example_settings(mixed=mixed)
    # There are four regional endpoints before mixed-model fallback in this global example.
    config = config.model_copy(
        update={
            "max_attempts": 8,
            "deadline_seconds": 15,
            "attempt_timeout_seconds": 10,
            "first_token_timeout_seconds": 10,
        }
    )
    success = failures = degraded = max_attempts = 0
    invariant = True
    first_tokens: list[float] = []
    start = time.perf_counter()
    async with mock_transport(config, scenario, model) as (transport, counts):
        client = Router(config, transport)
        for _ in range(count):
            req = Request(
                model=model,
                input="Reply with OK only.",
                estimated_input_tokens=8,
                max_output_tokens=16,
                allow_model_fallback=mixed,
                preferred_regions=config.region_preferences["KR"],
            )
            try:
                if streaming:
                    async with client.stream(req) as result:
                        if result.reply.chunks is None:
                            raise RuntimeError("SDK streaming adapter did not return chunks")
                        async for _chunk in result.reply.chunks:
                            pass
                else:
                    result = await client.route(req)
                success += 1
                degraded += int(result.degraded)
                invariant &= mixed or result.selected_model == model
                max_attempts = max(max_attempts, len(result.attempts))
                if result.first_token_ms is not None:
                    first_tokens.append(result.first_token_ms)
            except RoutingExhausted as error:
                failures += 1
                max_attempts = max(max_attempts, len(error.attempts))
            except BackendError as error:
                if error.status != 401:
                    raise
                failures += 1
                max_attempts = max(max_attempts, 1)
        attempts = counts.total()
    expect_failure = scenario in {"correlated_429", "auth_401"} or (
        scenario == "same_model_429" and not mixed
    )
    # Headroom is shared by the conservative subscription-level pool, not per region.
    expected_successes = (
        1 if scenario == "headroom" and not mixed else (0 if expect_failure else count)
    )
    passed = (
        success == expected_successes
        and failures == count - expected_successes
        and invariant
        and max_attempts <= config.max_attempts
        and (scenario != "same_model_429" or not mixed or degraded == count)
        and (scenario != "auth_401" or attempts == count)
    )
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
    )


async def simulate(*, count: int = 3, streaming: bool = False) -> SimulationReport:
    if not 1 <= count <= 100:
        raise ValueError("Simulation count must be between 1 and 100")
    rows = [
        await _scenario(model, mixed, scenario, count, streaming)
        for model in TARGET_MODELS
        for mixed in (False, True)
        for scenario in SCENARIOS
    ]
    return SimulationReport(
        mode="simulation",
        live_azure_calls=0,
        sdk_version=version("openai"),
        passed=all(row["passed"] for row in rows),
        scenarios=rows,
    )
