"""The timing mode, one implementation — the old suite had five
mutually incompatible timing-loop dialects for one concept.

    per_iteration()   micro latency samples: N calls, each timed individually

Plus `gc_control()`: disabling GC collapsed stdev
5-10x on the join shape (~2000 allocations/iteration) — first-order enough to
be a harness primitive, not a per-script flag.
"""

from __future__ import annotations

import gc
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any


def assert_unpatched_threading() -> None:
    """Refuse to time anything inside a gevent-monkey-patched interpreter.

    `locust` calls `gevent.monkey.patch_all()` at *import* time, which swaps
    `threading.Thread` for a greenlet across the whole process. aiosqlite gives
    every connection a worker thread, so under the patch the driver stops using
    real threads and every number moves: measured +27% on a hand-rolled floor and
    +33% on rowform, which skews the ratio between them as well as the absolutes.

    This is checked rather than noticed because nothing about the output looks
    wrong — the run completes, the equivalence gate passes, the spread stays
    tight, and the table is simply 30% slow. `bench micro` reached this state
    once already, by way of `__main__` importing every subcommand eagerly.

    `gevent.monkey` is read out of `sys.modules` rather than imported, so this
    costs nothing and never causes the patch it is looking for.
    """
    monkey = sys.modules.get("gevent.monkey")
    if monkey is not None and monkey.is_module_patched("threading"):
        raise RuntimeError(
            "threading is gevent-monkey-patched — measurements taken here are "
            "~30% slow and the ratios are skewed. Something imported locust "
            "(benchmarks.cli.load / .profile) into a timing process; see "
            "benchmarks/__main__.py, which mounts those lazily for this reason."
        )


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
