"""Allocation, not time: what one read costs the allocator.

Every number in this suite is a duration, and the claim they defend is about what
a row layer *builds* — slotted dataclasses against `Row`s, `RowMapping`s, or ORM
instances with an identity map behind them. Peak allocation is that same claim in
bytes, and it is the one a service feels as GC pressure rather than as latency.

`tracemalloc` is the instrument, so **this is not a timing run**: the tracer taxes
an allocation-heavy path several-fold. Uniformly enough that the comparison
between two contenders holds; not uniformly enough for the absolutes to belong
beside a timed table. Hence a separate command and a separate table, rather than
another column in the published one.

Two figures per contender, and the second is a check on the first:

* **peak** — the high-water mark of traced memory during the call, which is what
  the read needed at once.
* **net** — what is still held after it returns. A contender caching something
  (a compiled statement, a hydrator, an identity map that outlives the block)
  shows up here, and a *growing* net across calls is a leak rather than a cache.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Allocation:
    """One contender's allocation profile. Bytes, as tracemalloc reports them."""

    peak_bytes: int
    net_bytes: int
    calls: int

    def peak_per_row(self, rows: int) -> float:
        return self.peak_bytes / rows if rows else 0.0


async def measure(
    target: Callable[[], Awaitable[object]], *, calls: int = 3, warmup: int = 3
) -> Allocation:
    """Peak and net traced allocation over `calls` reads of `target`.

    `warmup` runs first and untraced, for the same reason the timing loop warms
    up: the first call through a contender builds its compiled statement, its
    hydrator, its pooled connections and every cache SQLAlchemy keeps, and
    counting those once would make one-off setup look like per-read cost.

    `gc.collect()` before tracing, so what an earlier contender left unreclaimed
    lands on that contender rather than on this one — the allocation-side version
    of why the timed runs isolate one contender per process.
    """
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
