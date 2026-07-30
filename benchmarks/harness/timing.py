"""Timing modes, one implementation each (PLAN.md §5) — the old suite had five
mutually incompatible timing-loop dialects for one concept.

    per_iteration()   micro latency samples: N calls, each timed individually
    closed_loop()     rps + cpu_ms + utilization + percentiles under concurrency

Plus `gc_control()`: PLAN.md §4 records that disabling GC collapsed stdev
5-10x on the join shape (~2000 allocations/iteration) — first-order enough to
be a harness primitive, not a per-script flag.
"""

from __future__ import annotations

import asyncio
import gc
import statistics
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

from benchmarks.harness.stats import percentiles


@contextmanager
def gc_control(mode: str):
    """`mode`: "on" leaves the collector alone, "off" disables it for the
    block and always re-enables on exit (even on exception) so a failed
    benchmark cannot leave the interpreter's GC permanently off."""
    if mode not in ("on", "off"):
        raise ValueError(f"gc mode must be 'on' or 'off', got {mode!r}")
    if mode == "on":
        yield
        return
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


async def per_iteration(
    fn: Callable[[], Awaitable[Any]], iterations: int, warmup: int = 0,
) -> list[float]:
    """Call `fn()` `warmup` times (discarded) then `iterations` times, timing
    each call individually. Returns per-call latencies in seconds — the raw
    samples micro benchmarks compute percentiles/spread from."""
    for _ in range(warmup):
        await fn()
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        await fn()
        latencies.append(time.perf_counter() - start)
    return latencies


async def closed_loop(
    fn: Callable[[], Awaitable[Any]], concurrency: int, duration: float, warmup: int = 0,
) -> dict[str, Any]:
    """`concurrency` worker tasks each call `fn()` back-to-back until the
    deadline (ported from the old `bench_pg_load.run_load`). Reports
    throughput, latency percentiles, and client-side CPU per request — the
    mechanism behind throughput differences once the box is CPU-saturated.
    """
    for _ in range(warmup):
        await fn()

    latencies: list[float] = []
    stop_at = time.perf_counter() + duration

    async def worker():
        local = []
        while True:
            start = time.perf_counter()
            if start >= stop_at:
                break
            await fn()
            local.append(time.perf_counter() - start)
        latencies.extend(local)

    started = time.perf_counter()
    cpu_started = time.process_time()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    elapsed = time.perf_counter() - started
    cpu_used = time.process_time() - cpu_started

    pct = percentiles(latencies, (50, 95, 99))
    return {
        "concurrency": concurrency,
        "completed": len(latencies),
        "elapsed_s": elapsed,
        "rps": len(latencies) / elapsed if elapsed else 0.0,
        "cpu_ms_per_request": (cpu_used / len(latencies) * 1000) if latencies else 0.0,
        "cpu_utilization": {"client": cpu_used / elapsed if elapsed else 0.0},
        "mean_ms": statistics.mean(latencies) * 1000 if latencies else 0.0,
        "p50_ms": pct[50] * 1000,
        "p95_ms": pct[95] * 1000,
        "p99_ms": pct[99] * 1000,
    }
