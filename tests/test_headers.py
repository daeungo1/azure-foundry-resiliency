"""Header parsing contract, including Azure reset durations and HTTP dates."""

import pytest

from foundry_resiliency.headers import duration_seconds, retry_delay


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10", 10), ("250ms", 0.25), ("1m30s", 90), ("1h2m3.5s", 3723.5)],
)
def test_duration(value: str, expected: float) -> None:
    assert duration_seconds(value) == expected


@pytest.mark.parametrize("value", ["-1", "NaN", "inf", "bad", "10garbage", ""])
def test_invalid_duration_is_not_accepted(value: str) -> None:
    with pytest.raises(ValueError):
        duration_seconds(value)


def test_retry_after_never_shortens_server_delay() -> None:
    assert retry_delay({"retry-after-ms": "250", "Retry-After": "3"}, now=0) == 3
    assert retry_delay({"Retry-After": "Thu, 01 Jan 1970 00:00:10 GMT"}, now=2) == 8


def test_malformed_retry_header_warns(caplog: pytest.LogCaptureFixture) -> None:
    assert retry_delay({"retry-after-ms": "NaN"}, now=0) == 0
    assert "invalid" in caplog.text.lower()
