"""`benchmarks/harness/memory.py` — the allocation instrument.

Two things worth pinning, both of which a plausible-looking implementation gets
wrong. Warm-up has to be untraced, or one-off setup (a compiled statement, a
hydrator, a pool) lands in the first read's peak and the instrument reports setup
cost as per-read cost. And `peak` has to be the high-water mark *above the
baseline*, not the absolute traced total, or every figure carries whatever the
interpreter happened to be holding when tracing started.
"""

from __future__ import annotations

import pytest

from benchmarks.harness import memory


async def test_it_reports_the_peak_above_the_baseline():
    held = [bytearray(200_000)]  # resident before tracing starts

    async def target():
        return bytearray(400_000)

    alloc = await memory.measure(target, calls=2, warmup=1)
    assert 300_000 < alloc.peak_bytes < 900_000, alloc
    assert held  # keep it alive to the end, so it is baseline and not churn


async def test_setup_on_the_first_call_is_not_counted():
    """The warm-up contract: a target that allocates a big cache on its first
    call and nothing afterwards must report the *steady-state* peak."""
    cache: list[bytearray] = []

    async def target():
        if not cache:
            cache.append(bytearray(2_000_000))
        return bytearray(1_000)

    alloc = await memory.measure(target, calls=2, warmup=1)
    assert alloc.peak_bytes < 500_000, alloc


async def test_what_a_target_keeps_shows_up_as_net():
    kept: list[bytearray] = []

    async def target():
        kept.append(bytearray(300_000))

    alloc = await memory.measure(target, calls=2, warmup=1)
    assert alloc.net_bytes > 500_000, alloc


async def test_a_target_that_keeps_nothing_nets_about_zero():
    async def target():
        return bytearray(300_000)

    alloc = await memory.measure(target, calls=3, warmup=1)
    assert alloc.net_bytes < 100_000, alloc


@pytest.mark.parametrize("rows", [0, 1000])
def test_per_row_is_zero_rather_than_a_zero_division(rows):
    alloc = memory.Allocation(peak_bytes=1_000_000, net_bytes=0, calls=1)
    assert alloc.peak_per_row(rows) == (0.0 if rows == 0 else 1000.0)
