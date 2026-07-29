#!/usr/bin/env python3
"""Connection hold time: rowform releases before hydrating, SQLAlchemy does not.

Both engines in this repo take a pooled connection per request via `async with`,
exactly as SQLAlchemy does — there is no connection reuse in the library. But there
is a shape difference worth measuring rather than hand-waving:

    # rowform.PsycopgEngine.fetch_all
    async with pool.connection() as conn:
        rows = await (await conn.execute(sql, params)).fetchall()
    return hydrate(rows)              # <- connection already back in the pool

    # the SQLAlchemy Core path in bench_psycopg.py
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [...]               # <- still holding the connection

So rowform occupies a pooled connection for less of each request. Does that flatter
it? Measured answer: **no, in any configuration tested.**

Releasing early is consistently worth slightly *less* than nothing to Core
(-1.7% to -3.8% at 1000 rows), because making it symmetric means materialising
`result.mappings().all()` into an intermediate list, and there is no contention to
win back in exchange. So the shape the other benchmarks use is not unfair to
SQLAlchemy — it is marginally the better of the two for it.

The reason hold time cannot pay here is the same fact the rest of this suite keeps
running into: **the client is CPU-bound, not connection-bound.** Starving the pool
to 2 connections against 8 workers barely moves Core at all (208 vs 204 rps at
1000 rows) — the workers queue on the GIL, not on the pool. Connection hold time
would only start to matter where the pool is the binding constraint, which would
require a client that is *not* saturated, i.e. a different benchmark than any here.

Cautionary note on how this was measured, because the first attempt got it wrong:
at `--repeat 3 --limit 100` two consecutive runs disagreed in *sign* on both
regimes (-5.4% then +4.5% at pool 10; +13.6% then +0.1% at pool 2). The +13.6%
looked like a real starved-pool advantage and was not. Resolving it needed a larger
payload, where the shaping work being moved is ~10x bigger relative to the noise,
plus more repeats.

Usage:
    taskset -c 0 python3 benchmarks/bench_hold_time.py --repeat 3 --pools 10,2
"""
import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from benchmarks.benchargs import validate
from benchmarks.models import User, users_table
from rowform import PsycopgEngine, Query, compile_json_default

CONNINFO = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"
SA_DSN = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"


def stmt_for(limit):
    return (select(users_table)
            .where(users_table.c.is_active == True)
            .where(users_table.c.id > 100)
            .limit(limit))


async def make_rowform(limit, pool):
    db = PsycopgEngine(CONNINFO, min_size=pool, max_size=pool)
    await db.connect()
    q = Query(User).where(User.is_active == True).where(User.id > 100).limit(limit)
    to_dict = compile_json_default(User)

    async def request():
        return orjson.dumps(await db.fetch_all(q), default=to_dict)
    return request, db.close


async def make_core_hold(limit, pool):
    """Payload built INSIDE the context manager — what the benchmark does today."""
    engine = create_async_engine(SA_DSN, pool_size=pool, max_overflow=0)
    stmt = stmt_for(limit)

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)
    return request, engine.dispose


async def make_core_release(limit, pool):
    """Rows out inside, shaping outside — symmetric with rowform.fetch_all."""
    engine = create_async_engine(SA_DSN, pool_size=pool, max_overflow=0)
    stmt = stmt_for(limit)

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = result.mappings().all()
        payload = [{str(k): v for k, v in m.items()} for m in rows]
        return orjson.dumps(payload)
    return request, engine.dispose


VARIANTS = [
    ("rowform (releases before hydrate)", make_rowform),
    ("Core: payload inside `async with`", make_core_hold),
    ("Core: payload after release", make_core_release),
]


async def load(request, c, duration, warmup):
    for _ in range(warmup):
        await request()

    async def worker():
        deadline = time.perf_counter() + duration
        n = 0
        while time.perf_counter() < deadline:
            await request()
            n += 1
        return n
    w0, c0 = time.perf_counter(), time.process_time()
    counts = await asyncio.gather(*[worker() for _ in range(c)])
    wall, cpu = time.perf_counter() - w0, time.process_time() - c0
    total = sum(counts)
    return total / wall, cpu / total * 1000


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=4.0)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--pools", default="10,2")
    args = p.parse_args()
    validate(p, args)

    print(f"cores {sorted(os.sched_getaffinity(0))}  {args.limit} rows  "
          f"c={args.concurrency}  median of {args.repeat}\n")

    # equivalence
    outs = {}
    for name, factory in VARIANTS:
        req, close = await factory(args.limit, 10)
        outs[name] = await req()
        r = close()
        if asyncio.iscoroutine(r):
            await r
    ref = next(iter(outs.values()))
    for name, got in outs.items():
        if got != ref:
            print(f"FAIL: {name} differs", file=sys.stderr)
            return 1
    print(f"all three emit identical JSON ({len(ref)} bytes)\n")

    for pool in (int(x) for x in args.pools.split(",")):
        starved = pool < args.concurrency
        print(f"--- pool {pool}"
              f"{'  (STARVED: fewer connections than workers)' if starved else ''} ---")
        print(f"{'variant':<36}{'rps':>8}{'cpu ms/req':>12}")
        print("-" * 56)
        res = {}
        for name, factory in VARIANTS:
            trials = []
            for _ in range(args.repeat):
                req, close = await factory(args.limit, pool)
                try:
                    trials.append(await load(req, args.concurrency,
                                             args.duration, args.warmup))
                finally:
                    r = close()
                    if asyncio.iscoroutine(r):
                        await r
            rps = statistics.median(t[0] for t in trials)
            cpu = statistics.median(t[1] for t in trials)
            res[name] = rps
            print(f"{name:<36}{rps:>8.0f}{cpu:>12.4f}")
        hold = res["Core: payload inside `async with`"]
        rel = res["Core: payload after release"]
        print(f"\n  releasing early is worth {rel / hold:.3f}x to Core "
              f"({(rel / hold - 1) * 100:+.1f}%)")
        print(f"  rowform vs Core: {res['rowform (releases before hydrate)'] / hold:.2f}x "
              f"(hold)  vs  {res['rowform (releases before hydrate)'] / rel:.2f}x "
              f"(symmetric)\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
