"""Parse server-provided cooldown and rate-limit reset headers without shortening them."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)
PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def duration_seconds(value: str) -> float:
    text = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        result = float(text)
    else:
        matches = list(PART.finditer(text))
        if not matches or "".join(m.group() for m in matches) != text:
            raise ValueError("invalid duration header")
        result = sum(
            float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]
            for match in matches
        )
    if not math.isfinite(result):
        raise ValueError("invalid nonfinite duration")
    return result


def retry_delay(headers: Mapping[str, str], *, now: float) -> float:
    lower = {key.lower(): value for key, value in headers.items()}
    delays = [0.0]
    for name in ("retry-after-ms", "retry-after"):
        if name not in lower:
            continue
        value = lower[name]
        try:
            if name == "retry-after-ms":
                delays.append(duration_seconds(value) / 1000)
            else:
                try:
                    delays.append(duration_seconds(value))
                except ValueError:
                    delays.append(max(0, parsedate_to_datetime(value).timestamp() - now))
        except (ValueError, TypeError, OverflowError):
            logger.warning("Invalid %s header; using remaining valid cooldown signals", name)
    return max(delays)
