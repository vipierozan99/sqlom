"""Medians, spread, and tie-aware ratios: report both, and flag ties instead
of ranking them. Consolidates two duplicate percentile implementations
and the `summarize()`/`print_table()` pair that only differed in column widths.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

# Two medians within this fraction of each other are a tie, not a ranking —
# ordinary run-to-run jitter on this class of benchmark is routinely 3-5%.
DEFAULT_TIE_THRESHOLD_PCT = 5.0


def median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else float("nan")


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile: the `ceil(n*p/100)`-th order statistic. The
    old suite's `pct()` helpers used `int(n*p/100)` — floor plus one rank, a
    consistent upward bias that was worst at small n (n=4, p=50 returned the
    3rd value)."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = math.ceil(len(ordered) * p / 100)
    return ordered[min(max(rank, 1), len(ordered)) - 1]


def percentiles(values: Sequence[float], ps: Sequence[float] = (50, 95, 99)) -> dict[float, float]:
    ordered = sorted(values)
    return {p: percentile(ordered, p) for p in ps}


def spread_pct(values: Sequence[float]) -> float:
    """`(max - min) / median * 100` over **one value per trial** — the
    trial-to-trial reproducibility figure carried on every result.

    Not for raw within-run samples: as a full range it grows with sample count
    (E[max-min] ~= 6.5σ at n=1000 for a Gaussian, ~7.7σ at n=10000), so on a
    thousand per-iteration timings it reports the single worst interruption in
    the run rather than dispersion. Use `sample_shape()` there.
    """
    if not values:
        return float("nan")
    m = median(values)
    if not m:
        # A zero median makes the figure undefined — returning 0.0 here would
        # report perfect reproducibility for e.g. [-1, 0, 1].
        return float("nan") if max(values) != min(values) else 0.0
    return (max(values) - min(values)) / m * 100


@dataclass(frozen=True, slots=True)
class SampleShape:
    """The distribution of raw within-run latency samples.

    Carries no range or stdev on purpose. Both are dominated by the handful of
    samples an outside interruption produced: on this suite the same cell
    printed 787%, 698% and 74% range across three consecutive identical runs
    while its median moved 1.4%. `iqr_pct` is the dispersion figure (pyperf
    falls back to median+MAD and pytest-benchmark to IQR for exactly this
    reason); `max_over_p50` is retained as an *interference detector* and must
    not be read as dispersion.
    """

    median_ms: float
    iqr_pct: float  # middle 50% as a percentage of the median — the dispersion figure
    p95_over_p50: float  # tail, in units of the median
    outliers_mild: int  # outside Tukey's inner fences (1.5x IQR), either tail
    outliers_severe: int  # outside the outer fences (3x IQR), either tail
    max_over_p50: float  # interference detector, NOT dispersion


def sample_shape(values: Sequence[float]) -> SampleShape:
    """Summarize raw per-iteration samples — see `SampleShape` for why this
    reports quartiles and Tukey outlier counts instead of stdev and range.

    Fences follow criterion.rs' two-tier split (inner 1.5x IQR = "mild",
    outer 3x IQR = "severe"); both tails count, since an anomalously *fast*
    sample is a boost spike, i.e. the same isolation failure seen from the
    other side.
    """
    if not values:
        nan = float("nan")
        return SampleShape(nan, nan, nan, 0, 0, nan)
    med = median(values)
    q1, q3 = percentile(values, 25), percentile(values, 75)
    iqr = q3 - q1
    inner_lo, inner_hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    outer_lo, outer_hi = q1 - 3.0 * iqr, q3 + 3.0 * iqr
    severe = sum(1 for v in values if v < outer_lo or v > outer_hi)
    mild = sum(1 for v in values if v < inner_lo or v > inner_hi) - severe
    return SampleShape(
        median_ms=med,
        iqr_pct=iqr / med * 100 if med else float("nan"),
        p95_over_p50=percentile(values, 95) / med if med else float("nan"),
        outliers_mild=mild,
        outliers_severe=severe,
        max_over_p50=max(values) / med if med else float("nan"),
    )


def _within(a: float, b: float, threshold_pct: float) -> bool:
    base = max(abs(a), abs(b)) or 1.0
    return abs(a - b) / base * 100 <= threshold_pct


def ratio_with_spread(numerator_trials: Sequence[float], denominator_trials: Sequence[float],
                       threshold_pct: float = DEFAULT_TIE_THRESHOLD_PCT) -> dict[str, float | bool]:
    """`numerator / denominator` on medians, bracketed by the worst-case
    interval the observed trials allow (`low`/`high`), with a `tie` flag — the
    shape recorded in every `ratios[]` entry.

    `tie` is true when that interval contains 1.0, i.e. when the trials do not
    order the two contenders at all, or when the medians are within
    `threshold_pct` (METHODOLOGY.md's "group ties instead of ranking them").

    Takes one value per *trial*, never raw within-run samples. The previous
    version summed the two `spread_pct` figures and tied whenever that sum
    exceeded `|ratio - 1| * 100`, which compares a dispersion against a ratio
    magnitude: fed 1000 clean Gaussian samples per side with a true 1.85x
    separation — the rowform vs. SQLAlchemy-Core-positional gap — it returned
    `tie: True`, and could not resolve anything under ~2.5x.
    """
    if not numerator_trials or not denominator_trials:
        nan = float("nan")
        return {"value": nan, "low": nan, "high": nan, "interval_pct": nan, "tie": True}

    num_med, den_med = median(numerator_trials), median(denominator_trials)
    value = num_med / den_med if den_med else float("nan")
    den_lo, den_hi = min(denominator_trials), max(denominator_trials)
    low = min(numerator_trials) / den_hi if den_hi else float("nan")
    high = max(numerator_trials) / den_lo if den_lo else float("nan")
    return {
        "value": value,
        "low": low,
        "high": high,
        # Named `interval_pct`, not `spread_pct`: `spread_pct` (above) is
        # (max-min)/median over one metric's trials, this is the width of a
        # *ratio's* worst-case interval — two different formulas were being
        # recorded under one name in the same run.json.
        "interval_pct": (high - low) / value * 100 if value else float("nan"),
        "tie": bool(low <= 1.0 <= high) or _within(num_med, den_med, threshold_pct),
    }
