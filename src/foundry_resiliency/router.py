"""Process-local admission control, circuit breaking and explicitly authorized model fallback."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass

from pydantic import Field

from .config import Backend, Settings, Validated
from .headers import duration_seconds, retry_delay
from .transport import BackendError, Reply, Transport

logger = logging.getLogger(__name__)


class Request(Validated):
    model: str
    input: str = Field(min_length=1)
    estimated_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(default=128, ge=16)
    allow_model_fallback: bool = False
    preferred_regions: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=lambda: ["text"])
    stream: bool = False


@dataclass(frozen=True)
class Attempt:
    backend_id: str
    model: str
    region: str
    status: int
    elapsed_ms: float


@dataclass
class RoutedResult:
    requested_model: str
    selected_model: str
    selected_version: str
    backend_id: str
    resource_region: str
    attempts: tuple[Attempt, ...]
    elapsed_ms: float
    first_token_ms: float | None
    reply: Reply

    @property
    def degraded(self) -> bool:
        return self.requested_model != self.selected_model

    def summary(self) -> dict[str, object]:
        return {
            "requested_model": self.requested_model,
            "selected_model": self.selected_model,
            "selected_version": self.selected_version,
            "backend_id": self.backend_id,
            "resource_region": self.resource_region,
            "degraded": self.degraded,
            "attempts": [asdict(attempt) for attempt in self.attempts],
            "elapsed_ms": self.elapsed_ms,
            "first_token_ms": self.first_token_ms,
        }


class RoutingExhausted(Exception):
    def __init__(
        self, reason: str, attempts: list[Attempt], retry_after_seconds: float = 0
    ) -> None:
        super().__init__(f"Routing exhausted: {reason}; attempts={len(attempts)}")
        self.reason = reason
        self.attempts = tuple(attempts)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class Budget:
    remaining: int | None = None
    reset_at: float = 0

    def expire(self, now: float) -> None:
        if now >= self.reset_at:
            self.remaining = None


@dataclass
class State:
    in_flight: int = 0
    last_used: int = 0
    failures: int = 0
    blocked_until: float = 0
    open_until: float = 0
    probing: bool = False


class Router:
    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_time: Callable[[], float] = time.time,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.now = now
        self.sleep = sleep
        self.wall_time = wall_time
        self.random_value = random_value
        self.states = {backend.id: State() for backend in settings.backends}
        pools: dict[str, tuple[Budget, Budget]] = {}
        # Shared pool IDs alias the same budgets; endpoint circuit state remains independent.
        self.budgets = {
            backend.id: pools.setdefault(backend.quota_pool or backend.id, (Budget(), Budget()))
            for backend in settings.backends
        }
        self.lock = asyncio.Lock()
        self.sequence = 0

    def _candidates(self, request: Request) -> list[Backend]:
        if not any(backend.model == request.model for backend in self.settings.backends):
            raise ValueError("Requested model is not configured")
        if set(request.required_capabilities) != {"text"}:
            raise ValueError("Only text capabilities are implemented by this Responses adapter")
        models = [request.model]
        if request.allow_model_fallback:
            models += self.settings.fallbacks.get(request.model, [])
        regions = request.preferred_regions
        # Model rank comes first, so a preferred region cannot promote a fallback model.
        return sorted(
            (
                backend
                for backend in self.settings.backends
                if backend.model in models
                and set(request.required_capabilities) <= set(backend.capabilities)
            ),
            key=lambda backend: (
                models.index(backend.model),
                regions.index(backend.region) if backend.region in regions else len(regions),
                backend.priority,
                self.states[backend.id].in_flight,
                self.states[backend.id].last_used,
            ),
        )

    async def _reserve(self, request: Request) -> tuple[Backend | None, bool, float]:
        # Selection and debit are atomic within this router, not across process replicas.
        async with self.lock:
            now = self.now()
            waits: list[float] = []
            for backend in self._candidates(request):
                state = self.states[backend.id]
                requests, tokens = self.budgets[backend.id]
                requests.expire(now)
                tokens.expire(now)
                ready_at = max(state.blocked_until, state.open_until)
                if requests.remaining is not None and requests.remaining < 1:
                    ready_at = max(ready_at, requests.reset_at)
                estimate = request.estimated_input_tokens + request.max_output_tokens
                if tokens.remaining is not None and tokens.remaining < estimate:
                    ready_at = max(ready_at, tokens.reset_at)
                if ready_at > now:
                    waits.append(ready_at - now)
                    continue
                probe = state.open_until > 0
                # An expired open circuit admits one probe, not a burst of recovery traffic.
                if (
                    state.in_flight >= backend.max_in_flight
                    or state.probing
                    or (probe and state.in_flight > 0)
                ):
                    continue
                state.probing = probe
                state.in_flight += 1
                self.sequence += 1
                state.last_used = self.sequence
                if requests.remaining is not None:
                    requests.remaining -= 1
                if tokens.remaining is not None:
                    tokens.remaining -= estimate
                return backend, probe, 0
            return None, False, min(waits) if waits else 0

    def _headers(self, backend: Backend, headers: Mapping[str, str]) -> None:
        lower = {key.lower(): value for key, value in headers.items()}
        for kind, budget in zip(("requests", "tokens"), self.budgets[backend.id], strict=True):
            name = f"x-ratelimit-remaining-{kind}"
            if name not in lower:
                continue
            try:
                remaining = int(lower[name])
                if remaining < 0:
                    raise ValueError("negative remaining budget")
            except ValueError:
                logger.warning("Invalid %s for backend %s", name, backend.id)
                continue
            try:
                reset = duration_seconds(lower[f"x-ratelimit-reset-{kind}"])
            except (KeyError, ValueError):
                logger.warning("Missing/invalid %s reset for %s; holding for 60s", kind, backend.id)
                reset = 60
            now = self.now()
            budget.expire(now)
            # Late responses may lower a live budget, but cannot refund concurrent reservations.
            budget.remaining = (
                remaining if budget.remaining is None else min(budget.remaining, remaining)
            )
            budget.reset_at = max(budget.reset_at, now + reset)

    async def _observe(
        self, backend: Backend, probe: bool, error: BackendError | None, headers: Mapping[str, str]
    ) -> None:
        async with self.lock:
            self._headers(backend, headers)
            state = self.states[backend.id]
            if error is None:
                if probe or state.open_until == 0:
                    state.failures = 0
                    state.open_until = 0
                return
            if not error.retryable:
                return
            state.failures += 1
            jitter = self.random_value() * min(
                self.settings.backoff_cap_seconds,
                self.settings.backoff_base_seconds * 2 ** min(state.failures - 1, 16),
            )
            delay = max(0.001, jitter, retry_delay(headers, now=self.wall_time()))
            state.blocked_until = max(state.blocked_until, self.now() + delay)
            if probe or state.failures >= self.settings.circuit_failure_threshold:
                state.open_until = max(
                    state.open_until,
                    state.blocked_until,
                    self.now() + self.settings.circuit_break_seconds,
                )

    async def _release(self, backend: Backend, probe: bool) -> None:
        async with self.lock:
            state = self.states[backend.id]
            state.in_flight -= 1
            if probe:
                state.probing = False

    async def _open(self, request: Request) -> tuple[RoutedResult, Backend, bool]:
        self._candidates(request)
        start = self.now()
        deadline = start + self.settings.deadline_seconds
        attempts: list[Attempt] = []
        while len(attempts) < self.settings.max_attempts:
            remaining = deadline - self.now()
            if remaining <= 0:
                raise RoutingExhausted("deadline", attempts)
            backend, probe, wait = await self._reserve(request)
            remaining = deadline - self.now()
            if remaining <= 0:
                if backend is not None:
                    await self._release(backend, probe)
                raise RoutingExhausted("deadline", attempts)
            if backend is None:
                if wait == 0:
                    raise RoutingExhausted("overloaded_or_no_compatible_backend", attempts)
                if wait >= remaining:
                    raise RoutingExhausted("deadline", attempts, wait)
                await self.sleep(wait)
                continue
            timeout = min(
                remaining,
                self.settings.first_token_timeout_seconds
                if request.stream
                else self.settings.attempt_timeout_seconds,
            )
            attempt_start = self.now()
            keep_reserved = False
            try:
                try:
                    async with asyncio.timeout(timeout):
                        reply = await self.transport.send(backend, request, timeout)
                except TimeoutError:
                    error = BackendError(0, {}, "attempt_timeout")
                except BackendError as backend_error:
                    error = backend_error
                else:
                    # Synchronous SDK work can overrun the deadline before asyncio can cancel it.
                    if self.now() >= deadline:
                        if reply.close is not None:
                            await reply.close()
                        attempts.append(
                            Attempt(
                                backend.id,
                                backend.model,
                                backend.region,
                                0,
                                (self.now() - attempt_start) * 1000,
                            )
                        )
                        raise RoutingExhausted("deadline", attempts)
                    await self._observe(backend, probe, None, reply.headers)
                    elapsed = (self.now() - start) * 1000
                    attempts.append(
                        Attempt(
                            backend.id,
                            backend.model,
                            backend.region,
                            200,
                            (self.now() - attempt_start) * 1000,
                        )
                    )
                    keep_reserved = True
                    return (
                        RoutedResult(
                            request.model,
                            backend.model,
                            backend.version,
                            backend.id,
                            backend.region,
                            tuple(attempts),
                            elapsed,
                            elapsed if request.stream else None,
                            reply,
                        ),
                        backend,
                        probe,
                    )
                await self._observe(backend, probe, error, error.headers)
                attempts.append(
                    Attempt(
                        backend.id,
                        backend.model,
                        backend.region,
                        error.status,
                        (self.now() - attempt_start) * 1000,
                    )
                )
                logger.warning(
                    "backend_attempt backend=%s model=%s region=%s status=%s attempt=%s",
                    backend.id,
                    backend.model,
                    backend.region,
                    error.status,
                    len(attempts),
                )
                if not error.retryable:
                    raise error
            finally:
                if not keep_reserved:
                    await self._release(backend, probe)
        raise RoutingExhausted("attempt_budget", attempts)

    async def route(self, request: Request) -> RoutedResult:
        if request.stream:
            raise ValueError("Use Router.stream for streaming requests")
        result, backend, probe = await self._open(request)
        await self._release(backend, probe)
        return result

    @asynccontextmanager
    async def stream(self, request: Request) -> AsyncIterator[RoutedResult]:
        result, backend, probe = await self._open(request.model_copy(update={"stream": True}))
        try:
            # Once a text delta is exposed, consumer failures must never replay the request.
            async with asyncio.timeout(self.settings.stream_timeout_seconds):
                yield result
        except TimeoutError as timeout_error:
            error = BackendError(0, {}, "stream_deadline")
            await self._observe(backend, False, error, error.headers)
            raise error from timeout_error
        except BackendError as error:
            await self._observe(backend, False, error, error.headers)
            raise
        finally:
            try:
                if result.reply.close is not None:
                    await result.reply.close()
            finally:
                # Hold capacity through consumption and release it even if stream close fails.
                await self._release(backend, probe)
