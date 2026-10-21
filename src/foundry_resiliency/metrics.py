"""Descriptive statistics shared by live smoke and controlled comparison reports."""

import math


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 < fraction <= 1:
        raise ValueError("Percentile requires samples and a fraction in (0, 1]")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]
