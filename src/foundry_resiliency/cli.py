"""Safe CLI: simulation by default, explicit confirmation and ARM checks for live inference."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import subprocess
import sys
import time
from pathlib import Path

import httpx
from azure.core.exceptions import ClientAuthenticationError

from .config import TARGET_MODELS, Backend, Settings, load_settings
from .examples import example_settings
from .preflight import preflight
from .router import Request, Router, RoutingExhausted
from .simulation import simulate
from .transport import BackendError, Reply, Transport, live_transport

logger = logging.getLogger(__name__)


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 < fraction <= 1:
        raise ValueError("Percentile requires samples and a fraction in (0, 1]")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


class FaultTransport:
    def __init__(self, inner: Transport, backend_ids: set[str]) -> None:
        self.inner = inner
        self.backend_ids = backend_ids

    async def send(self, backend: Backend, request: Request, timeout_seconds: float) -> Reply:
        if backend.id in self.backend_ids:
            raise BackendError(429, {"Retry-After": "60"}, "synthetic_429_before_live_call")
        return await self.inner.send(backend, request, timeout_seconds)


async def smoke(
    config: Settings,
    *,
    model: str,
    market: str,
    count: int,
    streaming: bool,
    allow_fallback: bool,
    fault: str,
    ttft_budget_ms: float | None,
) -> dict[str, object]:
    if not 1 <= count <= 20:
        raise ValueError("Live smoke count must be between 1 and 20")
    if model not in {backend.model for backend in config.backends}:
        raise ValueError("Requested model is not configured")
    if market not in config.region_preferences:
        raise ValueError("Market must have an explicit region preference profile")
    if ttft_budget_ms is not None and (
        not math.isfinite(ttft_budget_ms) or ttft_budget_ms <= 0 or not streaming
    ):
        raise ValueError("TTFT budgets require streaming and a positive threshold")
    preferred = config.region_preferences[market]
    candidates = sorted(
        (backend for backend in config.backends if backend.model == model),
        key=lambda backend: (
            preferred.index(backend.region) if backend.region in preferred else len(preferred),
            backend.priority,
        ),
    )
    fail_ids = (
        {backend.id for backend in candidates}
        if fault == "same-model-429"
        else {candidates[0].id}
        if fault == "primary-429"
        else set()
    )
    samples: list[dict[str, object]] = []
    ttfts: list[float] = []
    start = time.perf_counter()
    async with live_transport(config) as transport:
        client = Router(config, FaultTransport(transport, fail_ids))
        for _ in range(count):
            req = Request(
                model=model,
                input="Reply with OK only.",
                estimated_input_tokens=8,
                max_output_tokens=128,
                allow_model_fallback=allow_fallback,
                preferred_regions=preferred,
            )
            try:
                if streaming:
                    async with client.stream(req) as result:
                        if result.reply.chunks is None:
                            raise RuntimeError("Expected streaming text chunks")
                        async for _chunk in result.reply.chunks:
                            pass
                else:
                    result = await client.route(req)
                samples.append({"success": True, **result.summary()})
                if result.first_token_ms is not None:
                    ttfts.append(result.first_token_ms)
            except RoutingExhausted as error:
                samples.append(
                    {
                        "success": False,
                        "reason": error.reason,
                        "attempts": len(error.attempts),
                        "retry_after_seconds": error.retry_after_seconds,
                    }
                )
            except BackendError as error:
                samples.append({"success": False, "status": error.status, "code": error.code})
    p95 = percentile(ttfts, 0.95) if ttfts else None
    passed = all(sample["success"] for sample in samples)
    if ttft_budget_ms is not None:
        passed &= p95 is not None and p95 <= ttft_budget_ms
    return {
        "mode": "live_azure_smoke",
        "synthetic_fault": fault,
        "market": market,
        "streaming": streaming,
        "requests": count,
        "elapsed_ms": (time.perf_counter() - start) * 1000,
        "upstream_ttft_p50_ms": percentile(ttfts, 0.5) if ttfts else None,
        "upstream_ttft_p95_ms": p95,
        "ttft_budget_ms": ttft_budget_ms,
        "passed": passed,
        "samples": samples,
        "limitations": (
            "One probe location; upstream TTFT, not global user/agent end-to-end latency"
        ),
    }


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    example = commands.add_parser("example", help="Write placeholder configuration")
    example.add_argument("--mode", choices=["same", "mixed"], required=True)
    example.add_argument("--subscriptions", type=int, choices=[1, 2], default=1)
    example.add_argument("--include-expansion", action="store_true")
    example.add_argument("--regions", type=int, help="Region01..N, from 1 to 16")
    simulation = commands.add_parser("simulate", help="Run the offline HTTP/SSE chaos matrix")
    simulation.add_argument("--count", type=int, default=3)
    simulation.add_argument("--stream", action="store_true")
    simulation.add_argument("--subscriptions", type=int, choices=[1, 2], default=1)
    simulation.add_argument("--regions", type=int, default=4, help="Region01..N, from 1 to 16")
    validate = commands.add_parser("validate-config")
    arm = commands.add_parser("preflight", help="Read-only existing ARM deployment checks")
    live = commands.add_parser("smoke", help="Small, explicitly confirmed billable inference")
    for command in (validate, arm, live):
        command.add_argument("--config", type=Path, required=True)
    live.add_argument("--confirm-paid", action="store_true")
    live.add_argument("--model", choices=list(TARGET_MODELS), required=True)
    live.add_argument("--market", default="Region01", help="Region alias in region_preferences")
    live.add_argument("--count", type=int, default=1)
    live.add_argument("--stream", action="store_true")
    live.add_argument("--allow-model-fallback", action="store_true")
    live.add_argument("--fault", choices=["none", "primary-429", "same-model-429"], default="none")
    live.add_argument("--ttft-budget-ms", type=float)
    for command in (example, simulation, validate, arm, live):
        command.add_argument("--output", type=Path, required=command is example)
    return parser


def _write(report: object, output: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "example":
            config = example_settings(
                mixed=args.mode == "mixed",
                subscriptions=args.subscriptions,
                include_expansion=args.include_expansion,
                region_count=args.regions,
            )
            _write(config.model_dump(mode="json"), args.output)
            return 0
        if args.command == "simulate":
            report = asyncio.run(
                simulate(
                    count=args.count,
                    streaming=args.stream,
                    subscriptions=args.subscriptions,
                    regions=args.regions,
                )
            )
            _write(report, args.output)
            return 0 if report["passed"] else 1
        config = load_settings(args.config)
        if args.command == "validate-config":
            _write(
                {
                    "valid": True,
                    "backends": len(config.backends),
                    "models": sorted({backend.model for backend in config.backends}),
                    "live_azure_calls": 0,
                },
                args.output,
            )
            return 0
        if args.command == "smoke" and not args.confirm_paid:
            logger.error("smoke requires --confirm-paid; no Azure calls were made")
            return 2
        evidence = preflight(config)
        if args.command == "preflight":
            _write(evidence, args.output)
            return 0
        live_report = asyncio.run(
            smoke(
                config,
                model=args.model,
                market=args.market,
                count=args.count,
                streaming=args.stream,
                allow_fallback=args.allow_model_fallback,
                fault=args.fault,
                ttft_budget_ms=args.ttft_budget_ms,
            )
        )
        live_report["arm_preflight"] = evidence
        _write(live_report, args.output)
        return 0 if live_report["passed"] else 1
    except KeyboardInterrupt:
        return 130
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ClientAuthenticationError,
        httpx.HTTPError,
    ) as error:
        logger.error("%s: %s", type(error).__name__, error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
