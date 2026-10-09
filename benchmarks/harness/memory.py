"""Peak traced allocation per read — the row layer's cost in bytes rather than time.

`tracemalloc` taxes the path it measures, so these numbers never share a table with
milliseconds.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Allocation:
    peak_bytes: int
    net_bytes: int  # still held after the calls; growing across calls is a leak
    calls: int

    def peak_per_row(self, rows: int) -> float:
        return self.peak_bytes / rows if rows else 0.0


async def measure(
    target: Callable[[], Awaitable[object]], *, calls: int = 3, warmup: int = 3
) -> Allocation:
    """Untraced warm-up first, so one-off setup (compiled statement, hydrator, pool)
    is not reported as per-read cost; peak is measured above the traced baseline."""
    for _ in range(warmup):
        await target()
    gc.collect()

    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        for _ in range(calls):
            payload = await target()
            del payload
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return Allocation(peak_bytes=peak - base, net_bytes=current - base, calls=calls)
