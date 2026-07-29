"""Medians, spread, and tie-grouping — PLAN.md §4: "medians + spread; group
ties instead of ranking." Consolidates two duplicate percentile implementations
and the `summarize()`/`print_table()` pair that only differed in column widths.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

# Two medians within this fraction of each other are a tie, not a ranking —
# ordinary run-to-run jitter on this class of benchmark is routinely 3-5%.
DEFAULT_TIE_THRESHOLD_PCT = 5.0


def median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else float("nan")


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile. Matches the old suite's `pct()` helpers
    (`min(int(len * p / 100), len - 1)`), not an interpolated one — kept
    identical so this isn't a second, subtly different percentile
    implementation."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(int(len(ordered) * p / 100), len(ordered) - 1)
    return ordered[idx]


def percentiles(values: Sequence[float], ps: Sequence[float] = (50, 95, 99)) -> dict[float, float]:
    ordered = sorted(values)
    return {p: percentile(ordered, p) for p in ps}


def spread_pct(values: Sequence[float]) -> float:
    """`(max - min) / median * 100` — a plain-language "how noisy was this
    cell" figure carried on every result (PLAN.md §6 `spread_pct`)."""
    if not values:
        return float("nan")
    m = median(values)
    if not m:
        return 0.0
    return (max(values) - min(values)) / m * 100


def tie_group(cells: Sequence[float], threshold_pct: float = DEFAULT_TIE_THRESHOLD_PCT) -> list[list[int]]:
    """Group cell *medians* (already one number per contender) into tie groups:
    indices whose values are within `threshold_pct` of each other, chained
    transitively (A ties B, B ties C -> A/B/C are one group even if A and C
    alone would not).

    Returns groups of indices into `cells`, ordered by group median descending
    (fastest first) — the presentation `bench report` wants, not a numbered
    ranking within a tie.
    """
    if not cells:
        return []
    order = sorted(range(len(cells)), key=lambda i: -cells[i])
    groups: list[list[int]] = []
    for i in order:
        placed = False
        for group in groups:
            if any(_within(cells[i], cells[j], threshold_pct) for j in group):
                group.append(i)
                placed = True
                break
        if not placed:
            groups.append([i])
    return groups


def _within(a: float, b: float, threshold_pct: float) -> bool:
    base = max(abs(a), abs(b)) or 1.0
    return abs(a - b) / base * 100 <= threshold_pct


def ratio_with_spread(numerator_trials: Sequence[float], denominator_trials: Sequence[float],
                       threshold_pct: float = DEFAULT_TIE_THRESHOLD_PCT) -> dict[str, float | bool]:
    """`numerator / denominator` on medians, with spread propagated from both
    sides (worst case: spreads add) and a `tie` flag — the shape recorded in
    every `ratios[]` entry (PLAN.md §6)."""
    num_med, den_med = median(numerator_trials), median(denominator_trials)
    value = num_med / den_med if den_med else float("nan")
    spread = spread_pct(numerator_trials) + spread_pct(denominator_trials)
    return {
        "value": value,
        "spread_pct": spread,
        "tie": spread >= abs(value - 1.0) * 100 or _within(num_med, den_med, threshold_pct),
    }
