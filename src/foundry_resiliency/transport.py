"""OpenAI v1 Responses adapter; SDK retries are disabled so the router owns the budget."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, AsyncStream
from openai.types.responses import ResponseStreamEvent

from .config import Backend, Settings

if TYPE_CHECKING:
    from .router import Request


class BackendError(Exception):
    def __init__(
        self, status: int, headers: Mapping[str, str], code: str = "backend_error"
    ) -> None:
        super().__init__(f"Backend request failed: status={status}, code={code}")
        self.status = status
        self.headers = dict(headers)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.status in {0, 408, 429, 500, 502, 503, 504}


@dataclass
class Reply:
    payload: dict[str, object]
    headers: Mapping[str, str]
    chunks: AsyncIterator[str] | None = None
    close: Callable[[], Awaitable[None]] | None = None


class Transport(Protocol):
    async def send(self, backend: Backend, request: Request, timeout_seconds: float) -> Reply: ...


async def _chunks(stream: AsyncStream[ResponseStreamEvent]) -> AsyncIterator[str]:
    completed = False
    try:
        async for event in stream:
            if event.type == "response.output_text.delta" and event.delta:
                yield event.delta
            elif event.type == "response.completed":
                completed = True
            elif event.type == "response.refusal.delta":
                raise BackendError(400, {}, "refusal")
            elif event.type in {"response.failed", "response.incomplete", "error"}:
                raise BackendError(400, {}, "stream_failed_or_incomplete")
        if not completed:
            raise BackendError(0, {}, "stream_disconnected")
    except (httpx.TransportError, APIConnectionError) as error:
        raise BackendError(0, {}, "stream_connection_error") from error


class SDKTransport:
    def __init__(self, clients: Mapping[str, AsyncOpenAI]) -> None:
        self.clients = clients
        if any(client.max_retries != 0 for client in clients.values()):
            raise ValueError("SDK retries must be zero: the router owns retry policy")

    async def send(self, backend: Backend, request: Request, timeout_seconds: float) -> Reply:
        client = self.clients[backend.id]
        try:
            if request.stream:
                raw = await client.responses.with_raw_response.create(
                    model=backend.deployment,
                    input=request.input,
                    max_output_tokens=request.max_output_tokens,
                    store=False,
                    stream=True,
                    timeout=httpx.Timeout(
                        connect=timeout_seconds,
                        read=None,
                        write=timeout_seconds,
                        pool=timeout_seconds,
                    ),
                )
                stream = raw.parse()
                chunks = _chunks(stream)
                opened = False
                try:
                    first = await anext(chunks)
                    opened = True
                except StopAsyncIteration as error:
                    raise BackendError(400, raw.headers, "no_text_output") from error
                finally:
                    if not opened:
                        await stream.close()

                async def text() -> AsyncIterator[str]:
                    yield first
                    async for chunk in chunks:
                        yield chunk

                return Reply({}, raw.headers, text(), stream.close)
            raw_response = await client.responses.with_raw_response.create(
                model=backend.deployment,
                input=request.input,
                max_output_tokens=request.max_output_tokens,
                store=False,
                stream=False,
                timeout=timeout_seconds,
            )
            response = raw_response.parse()
            if response.status != "completed":
                raise BackendError(400, raw_response.headers, "response_not_completed")
            if not response.output_text:
                raise BackendError(400, raw_response.headers, "no_text_output_or_refusal")
            return Reply(response.model_dump(mode="json"), raw_response.headers)
        except APIStatusError as error:
            raise BackendError(error.status_code, error.response.headers, "http_error") from error
        except APIConnectionError as error:
            raise BackendError(0, {}, "connection_or_timeout") from error


@asynccontextmanager
async def live_transport(settings: Settings) -> AsyncIterator[SDKTransport]:
    async with DefaultAzureCredential(exclude_interactive_browser_credential=True) as credential:
        provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
        clients: dict[str, AsyncOpenAI] = {}
        try:
            for backend in settings.backends:
                key = os.environ.get(backend.api_key_env) if backend.api_key_env else None
                if backend.api_key_env and not key:
                    raise ValueError(f"Missing API key environment variable: {backend.api_key_env}")
                clients[backend.id] = AsyncOpenAI(
                    api_key=key or provider,
                    base_url=backend.base_url,
                    max_retries=0,
                    timeout=settings.attempt_timeout_seconds,
                )
            yield SDKTransport(clients)
        finally:
            for client in clients.values():
                await client.close()
