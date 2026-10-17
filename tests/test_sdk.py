"""Exercise the actual SDK HTTP and SSE path rather than mocking the SDK itself."""

import json

import httpx
import pytest
from openai import AsyncOpenAI
from test_router import PRIMARY, settings

from foundry_resiliency.config import Backend
from foundry_resiliency.router import Request, Router
from foundry_resiliency.transport import BackendError, SDKTransport

pytestmark = pytest.mark.anyio


def sse(*events: dict[str, object]) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def response_body() -> dict[str, object]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": PRIMARY,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}],
            }
        ],
    }


async def test_real_sdk_wire_shape_and_disabled_retries() -> None:
    calls: list[httpx.Request] = []

    async def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.host == "a.openai.azure.com":
            return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {}})
        return httpx.Response(200, json=response_body())

    clients = {
        backend.id: AsyncOpenAI(
            api_key="test",
            base_url=backend.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        for backend in settings().backends
    }
    try:
        result = await Router(settings(), SDKTransport(clients)).route(
            Request(
                model=PRIMARY,
                input="test",
                estimated_input_tokens=4,
                max_output_tokens=16,
            )
        )
        assert result.backend_id == "b"
        assert len(calls) == 2
        body = json.loads(calls[-1].content)
        assert body["model"] == "b"  # Azure requires deployment name, not catalog model ID.
        assert body["store"] is False
        assert str(calls[-1].url).endswith("/openai/v1/responses")
    finally:
        for client in clients.values():
            await client.close()


async def test_stream_ttft_uses_text_delta_not_response_created_and_never_replays() -> None:
    calls: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url.host))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {"type": "response.created", "response": response_body()},
                {
                    "type": "response.output_text.delta",
                    "delta": "OK",
                    "sequence_number": 1,
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "logprobs": [],
                },
                # A disconnect after the first text token must reach the caller, not another model.
            ),
        )

    backend: Backend = settings().backends[0]
    async with AsyncOpenAI(
        api_key="test",
        base_url=backend.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        route = Router(settings(), SDKTransport({"a": client}))
        with pytest.raises(BackendError, match="stream_disconnected"):
            async with route.stream(
                Request(
                    model=PRIMARY,
                    input="test",
                    estimated_input_tokens=4,
                    max_output_tokens=16,
                    allow_model_fallback=True,
                )
            ) as result:
                assert result.first_token_ms is not None
                assert result.reply.chunks is not None
                text = [chunk async for chunk in result.reply.chunks]
                assert text == ["OK"]
        assert calls == ["a.openai.azure.com"]
        assert route.states["a"].in_flight == 0


async def test_stream_reserves_bulkhead_until_context_closes() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {
                    "type": "response.output_text.delta",
                    "delta": "OK",
                    "sequence_number": 1,
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "logprobs": [],
                },
                {"type": "response.completed", "response": response_body()},
            ),
        )

    clients = {
        backend.id: AsyncOpenAI(
            api_key="test",
            base_url=backend.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        for backend in settings().backends
    }
    try:
        route = Router(settings(), SDKTransport(clients))
        req = Request(model=PRIMARY, input="test", estimated_input_tokens=4, max_output_tokens=16)
        async with route.stream(req) as first:
            assert route.states["a"].in_flight == 1
            async with route.stream(req) as second:
                assert second.backend_id == "b"
                assert second.reply.chunks is not None
                assert [chunk async for chunk in second.reply.chunks] == ["OK"]
            assert first.backend_id == "a"
        assert route.states["a"].in_flight == 0
    finally:
        for client in clients.values():
            await client.close()


async def test_sdk_retries_cannot_be_accidentally_enabled() -> None:
    async with AsyncOpenAI(api_key="test") as client:
        with pytest.raises(ValueError, match="retries"):
            SDKTransport({"a": client})


async def test_stream_deadline_is_an_explicit_failure_without_replay() -> None:
    import asyncio

    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {
                    "type": "response.output_text.delta",
                    "delta": "OK",
                    "sequence_number": 1,
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "logprobs": [],
                }
            ),
        )

    config = settings(stream_timeout_seconds=0.01)
    async with AsyncOpenAI(
        api_key="test",
        base_url=config.backends[0].base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        route = Router(config, SDKTransport({"a": client}))
        with pytest.raises(BackendError, match="stream_deadline"):
            async with route.stream(
                Request(
                    model=PRIMARY,
                    input="test",
                    estimated_input_tokens=4,
                    max_output_tokens=16,
                )
            ):
                await asyncio.sleep(1)
        assert route.states["a"].in_flight == 0
