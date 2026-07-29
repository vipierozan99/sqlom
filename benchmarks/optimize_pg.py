#!/usr/bin/env python3
"""Measure the three optimizations the profile in docs/BENCHMARKS.md §5 points at.

The profile said rowform's own generated code is only ~15% of client CPU. The rest
is the event loop (38%), the asyncpg fetch (19%) and pool acquire/release (15%),
so those are where the remaining throughput is. Each flag here targets one:

--no-reset   asyncpg's pool runs `SELECT pg_advisory_unlock_all(); CLOSE ALL;
             UNLISTEN *; RESET ALL;` on every release, as a separate server
             round trip — verified: the default pool sends 2.01 queries per
             request, versus 1.00 with a no-op reset. Passing `reset=` a no-op
             coroutine keeps the in-process protocol reset (rollback, clear
             listeners) and drops the SQL.

             *** This is a behavioural tradeoff, not a free win. *** Session
             state now leaks between requests: SET/SET LOCAL outside a
             transaction, temp tables, cursors, LISTEN registrations, advisory
             locks. Safe only if request handlers never touch session state.
             Prefer it over `--hold-conn` because it keeps the pool's bounded
             connection count.

--hold-conn  Skip the pool entirely: each worker opens one connection and keeps
             it. Upper bound on what removing pool churn can buy. Usually not
             deployable — it makes client concurrency and DB connection count
             the same number.

--no-tls     asyncpg defaults to sslmode=prefer, and this server has ssl=on, so
             even a 127.0.0.1 connection negotiates TLSv1.3/AES-256-GCM.

--uvloop     Replace asyncio's event loop with libuv. Targets the 38%, and
             changes nothing about semantics.

Run one configuration per process (see docs/METHODOLOGY.md) and take medians.

Usage:
    python3 benchmarks/optimize_pg.py --uvloop --no-reset --no-tls --repeat 3
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

from benchmarks.bench_pg_load import DEFAULT_DSN
from benchmarks.benchargs import validate
from benchmarks.models import User
from rowform import ASYNCPG_CONVERTERS, Query, compile_batch_hydrator, compile_json_default


async def noop_reset(con):
    """Replaces asyncpg's RESET ALL round trip. See --no-reset caveats."""
    return


async def build(args):
    query = (
        Query(User).where(User.is_active == True).where(User.id > 100).limit(args.limit)
    )
    # to_sql(placeholder="$") emits $1, $2, ... already. Re-running a
    # renumbering regex over that turns $1 into $11 and the query fails with
    # "could not determine data type of parameter $1".
    sql, params = query.to_sql(placeholder="$")

    hydrate_all = compile_batch_hydrator(User, ASYNCPG_CONVERTERS)
    to_dict = compile_json_default(User)

    dsn = args.dsn
    if args.no_tls:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=disable"

    if args.hold_conn:
        conns = [await asyncpg.connect(dsn) for _ in range(args.concurrency)]

        def make_request(i):
            con = conns[i]

            async def request():
                rows = await con.fetch(sql, *params)
                return orjson.dumps(hydrate_all(rows), default=to_dict)

            return request

        async def teardown():
            for c in conns:
                await c.close()

        return make_request, teardown

    kwargs = {"min_size": args.pool_size, "max_size": args.pool_size}
    if args.no_reset:
        kwargs["reset"] = noop_reset
    pool = await asyncpg.create_pool(dsn, **kwargs)

    def make_request(_i):
        async def request():
            async with pool.acquire() as con:
                rows = await con.fetch(sql, *params)
            return orjson.dumps(hydrate_all(rows), default=to_dict)

        return request

    return make_request, pool.close


async def run(args):
    make_request, teardown = await build(args)
    try:
        for _ in range(args.warmup):
            await make_request(0)()

        async def worker(i):
            request = make_request(i)
            deadline = time.perf_counter() + args.duration
            n = 0
            while time.perf_counter() < deadline:
                await request()
                n += 1
            return n

        w0, c0 = time.perf_counter(), time.process_time()
        counts = await asyncio.gather(*[worker(i) for i in range(args.concurrency)])
        wall = time.perf_counter() - w0
        cpu = time.process_time() - c0
        total = sum(counts)
        return {
            "rps": total / wall,
            "cpu_ms": cpu / total * 1000,
            "utilization": cpu / wall,
        }
    finally:
        await teardown()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pool-size", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=4.0)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--no-reset", action="store_true")
    p.add_argument("--hold-conn", action="store_true")
    p.add_argument("--no-tls", action="store_true")
    p.add_argument("--uvloop", action="store_true")
    p.add_argument("--label", default=None)
    args = p.parse_args()
    validate(p, args)
    if args.uvloop:
        import uvloop

        uvloop.install()

    flags = [n for n, on in (("uvloop", args.uvloop), ("no-reset", args.no_reset),
                             ("hold-conn", args.hold_conn), ("no-tls", args.no_tls)) if on]
    label = args.label or ("+".join(flags) if flags else "baseline")

    trials = [asyncio.run(run(args)) for _ in range(args.repeat)]
    rps = statistics.median(t["rps"] for t in trials)
    cpu = statistics.median(t["cpu_ms"] for t in trials)
    util = statistics.median(t["utilization"] for t in trials)
    # Name the loop implementation without instantiating one: a bare
    # new_event_loop() here allocated a loop and its self-pipe file descriptors
    # purely to read a class name, and never closed either.
    loop_name = type(asyncio.get_event_loop_policy()).__module__.split(".")[0]
    print(f"RESULT\t{label}\t{rps:.0f}\t{cpu:.4f}\t{util:.2f}\t"
          f"cores={sorted(os.sched_getaffinity(0))}\tloop={loop_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
