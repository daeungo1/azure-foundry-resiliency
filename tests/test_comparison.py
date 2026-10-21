"""Compare complete-stream reliability and measured first-text timing on delayed SSE."""

import pytest

from foundry_resiliency.comparison import compare_streaming
from foundry_resiliency.config import TARGET_MODELS

pytestmark = pytest.mark.anyio


async def test_comparison_uses_measured_sse_and_preserves_failure_denominators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    report = await compare_streaming(count=2, fast_delay_seconds=0.003, slow_delay_seconds=0.06)
    assert report["passed"]
    assert report["mode"] == "controlled_streaming_comparison"
    assert report["timing_mode"] == "real_monotonic_clock"
    assert report["live_azure_calls"] == 0
    assert not report["cold_sdk_initialization_included"]
    assert len(report["scenarios"]) == len(TARGET_MODELS) * 5
    summary = {row["scenario"]: row for row in report["summary"]}
    for scenario, row in summary.items():
        before, after = row["before"], row["after"]
        assert before["requests"] == after["requests"] == 8
        assert after["successes"] == 8
        assert after["ttft_p95_ms"] is not None
        if scenario in {"healthy", "slow_primary"}:
            assert before["successes"] == 8
            assert before["ttft_p95_ms"] is not None
        else:
            assert before["successes"] == 0
            assert before["failures"] == 8
            assert before["ttft_p95_ms"] is None
    slow = summary["slow_primary"]
    assert slow["before"]["ttft_p95_ms"] is not None
    assert slow["after"]["ttft_p95_ms"] is not None
    assert slow["before"]["ttft_p95_ms"] >= 60
    assert slow["after"]["ttft_p95_ms"] < slow["before"]["ttft_p95_ms"]
    for row in report["scenarios"]:
        if row["scenario"] == "slow_primary":
            assert row["region_preferences"][0] != "Region01"
        for arm in ("before", "after"):
            for sample in row[arm]["samples"]:
                assert sample["attempts"]
                assert sample["success"] == sample["output_verified"]
                if sample["success"]:
                    assert sample["first_token_ms"] is not None
                    assert sample["first_token_ms"] >= 3
                else:
                    assert sample["failure_reason"]
        degraded = row["after"]["metrics"]["degraded_responses"]
        assert degraded == (2 if row["scenario"] == "same_model_429" else 0)


@pytest.mark.parametrize("count", [0, 101])
async def test_comparison_rejects_unbounded_counts(count: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        await compare_streaming(count=count)


@pytest.mark.parametrize("fast,slow", [(0, 0.25), (0.3, 0.25), (0.03, float("nan"))])
async def test_comparison_rejects_invalid_delays(fast: float, slow: float) -> None:
    with pytest.raises(ValueError, match="Controlled delays"):
        await compare_streaming(fast_delay_seconds=fast, slow_delay_seconds=slow)
