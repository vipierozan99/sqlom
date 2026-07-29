#!/usr/bin/env python3
"""Can the pool's session reset be avoided *without* changing behaviour?

asyncpg's pool runs `SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *;
RESET ALL;` on every release as its own round trip, costing ~20-30% of
throughput. Three routes out, and only one of them is free:

1. `reset=` no-op — 1.45x, but session state now leaks between requests.
2. Move the reset to acquire — measured: no gain at all, because it is still a
   separate round trip. Where it happens does not matter; that it happens does.
3. Batch it with the query in one round trip — right in principle. asyncpg has
   no pipeline API; psycopg3 does, and measured there it *loses*, because
   pipeline mode costs more per statement than the round trip it saves.
4. **Run it only when the connection could have been dirtied** — what
   `DatabaseEngine(conditional_reset=True)` does. `fetch_all`/`fetch_json` only
   ever execute generated SELECTs, which cannot leave session state behind, so
   those connections are provably clean. `engine.acquire()` marks a connection
   dirty and its release pays the full reset.

This measures 4 against 1 and against asyncpg's default, including a mixed
workload where some fraction of requests use the raw escape hatch. All runs are
async single-threaded at c=8 (see docs/METHODOLOGY.md).

Usage:
    taskset -c 0 python3 benchmarks/bench_conditional_reset.py --repeat 3
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
import orjson

from benchmarks.benchargs import validate
from benchmarks.models import User
from rowform import (
    ASYNCPG_CONVERTERS,
    DatabaseEngine,
    Query,
    compile_batch_hydrator,
    compile_json_default,
)

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"


async def noop_reset(con):
    return None


def query(limit):
    return Query(User).where(User.is_active == True).where(User.id > 100).limit(limit)


async def make_engine(limit, pool_size, conditional, dirty_every=0):
    """dirty_every=N: every Nth request goes through acquire(), dirtying the
    connection so its release pays the reset. 0 = never."""
    db = DatabaseEngine(dsn=DSN, conditional_reset=conditional,
                        min_size=pool_size, max_size=pool_size)
    await db.connect()
    q = query(limit)
    to_dict = compile_json_default(User)
    hydrate = compile_batch_hydrator(User, ASYNCPG_CONVERTERS)
    sql, params = q.to_sql(placeholder="$")
    sql = DatabaseEngine._number_placeholders(sql)
    counter = {"n": 0}

    async def request():
        counter["n"] += 1
        if dirty_every and counter["n"] % dirty_every == 0:
            # Realistic escape-hatch use: raw connection for something rowform
            # does not model. Marks the connection dirty, so its release pays
            # the reset. Must do the *same* query work as the normal path, or a
            # higher dirty rate would simply be doing less and look faster.
            async with db.acquire() as con:
                await con.execute("SET statement_timeout = '5s'")
                rows = await con.fetch(sql, *params)
            return orjson.dumps(hydrate(rows), default=to_dict)
        return orjson.dumps(await db.fetch_all(q), default=to_dict)

    return request, db.close, db


async def make_raw(limit, pool_size, reset):
    """asyncpg directly: reset=None means the library default."""
    kw = dict(min_size=pool_size, max_size=pool_size)
    if reset is not None:
        kw["reset"] = reset
    pool = await asyncpg.create_pool(DSN, **kw)
    q = query(limit)
    sql, params = q.to_sql(placeholder="$")
    sql = DatabaseEngine._number_placeholders(sql)
    hydrate = compile_batch_hydrator(User, ASYNCPG_CONVERTERS)
    to_dict = compile_json_default(User)

    async def request():
        async with pool.acquire() as con:
            rows = await con.fetch(sql, *params)
        return orjson.dumps(hydrate(rows), default=to_dict)

    return request, pool.close, None


async def load(request, concurrency, duration, warmup):
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
    counts = await asyncio.gather(*[worker() for _ in range(concurrency)])
    wall = time.perf_counter() - w0
    cpu = time.process_time() - c0
    total = sum(counts)
    return total / wall, cpu / total * 1000, cpu / wall


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pool-size", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=4.0)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--repeat", type=int, default=3)
    args = p.parse_args()
    validate(p, args)
    variants = [
        ("asyncpg default (always RESET)",
         lambda: make_raw(args.limit, args.pool_size, None)),
        ("reset=no-op (LEAKS session state)",
         lambda: make_raw(args.limit, args.pool_size, noop_reset)),
        ("conditional, pure rowform traffic",
         lambda: make_engine(args.limit, args.pool_size, True)),
        ("conditional, 1 in 10 uses acquire()",
         lambda: make_engine(args.limit, args.pool_size, True, dirty_every=10)),
        ("conditional, 1 in 2 uses acquire()",
         lambda: make_engine(args.limit, args.pool_size, True, dirty_every=2)),
        ("conditional=False (engine, always RESET)",
         lambda: make_engine(args.limit, args.pool_size, False)),
    ]
    # `DatabaseEngine.reset_count` only counts resets issued by the *conditional*
    # hook. When conditional_reset=False the hook is never installed, so asyncpg
    # runs its own built-in reset and the counter stays at 0 — which reads as
    # "this variant skips the reset", the exact opposite of the truth. Report n/a
    # for any variant the counter does not instrument.
    instrumented = {"conditional, pure rowform traffic",
                    "conditional, 1 in 10 uses acquire()",
                    "conditional, 1 in 2 uses acquire()"}

    print(f"cores: {sorted(os.sched_getaffinity(0))}  {args.limit} rows/request, "
          f"pool {args.pool_size}, async single-thread c={args.concurrency}, "
          f"median of {args.repeat}\n")
    print(f"{'variant':<42}{'rps':>8}{'cpu ms/req':>12}{'resets/req':>12}")
    print("-" * 74)

    results = {}
    for name, factory in variants:
        trials, resets = [], []
        for _ in range(args.repeat):
            request, teardown, db = await factory()
            try:
                before = db.reset_count if db else 0
                rps, cpu, util = await load(request, args.concurrency,
                                            args.duration, args.warmup)
                trials.append((rps, cpu))
                if db is not None and name in instrumented:
                    # requests completed this trial ~= rps * duration
                    resets.append((db.reset_count - before) / max(1, rps * args.duration))
            finally:
                r = teardown()
                if asyncio.iscoroutine(r):
                    await r
        rps = statistics.median(t[0] for t in trials)
        cpu = statistics.median(t[1] for t in trials)
        # n/a means "not instrumented", never "zero resets" — see above.
        rst = f"{statistics.median(resets):.2f}" if resets else "n/a"
        results[name] = rps
        print(f"{name:<42}{rps:>8.0f}{cpu:>12.4f}{rst:>12}")

    base = results["asyncpg default (always RESET)"]
    print(f"\nvs. asyncpg's default ({base:.0f} rps):")
    for name, rps in results.items():
        print(f"  {name:<42}{rps / base:>7.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
