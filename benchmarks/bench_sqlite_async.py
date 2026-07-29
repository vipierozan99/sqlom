#!/usr/bin/env python3
"""Does asyncio concurrency or uvloop help the sqlite path? Single thread.

There was no async sqlite benchmark before this: every sqlite measurement in
docs/BENCHMARKS.md is synchronous. That is not an oversight, and the reason is
the point of this file — **`sqlite3` is a synchronous in-process C library, so a
sqlite request has nothing to await.** asyncio's benefit is overlapping I/O
waits; §5a measured 35% of a lone *Postgres* request as socket wait, and §7
measured sqlite's equivalent at zero.

So the honest question is not "how much does concurrency help" but "how much does
it cost", and these variants separate the mechanisms:

  sync                  no asyncio at all — the reference
  coroutine (no yield)  the request wrapped in a coroutine that never awaits.
                        N tasks cannot interleave, so this isolates pure
                        coroutine/await machinery with zero scheduling.
  coroutine (yield)     one `await asyncio.sleep(0)` per request, modelling a
                        real async driver's yield point. Tasks genuinely
                        interleave, so this isolates event-loop scheduling.
  aiosqlite             the usual answer for "async sqlite". Offloads to a
                        worker thread, so it is NOT single-threaded — included
                        because it is what people reach for, and labelled.

Each is run under both the default asyncio loop and uvloop. Every worker does an
equal fixed number of requests (not a deadline) so that variants where tasks
cannot interleave still perform the same total work.

Usage:
    taskset -c 0 python3 benchmarks/bench_sqlite_async.py --repeat 3
"""

import argparse
import asyncio
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson

from benchmarks.benchargs import validate
from benchmarks.models import DDL, TABLE_NAME, User
from rowform import SQLITE_CONVERTERS, Query, compile_batch_hydrator, compile_json_default


def seed(db_path, rows, rng_seed=42):
    rng = random.Random(rng_seed)
    conn = sqlite3.connect(db_path)
    conn.execute(DDL)
    conn.executemany(
        f"INSERT INTO {TABLE_NAME} VALUES (?, ?, ?, ?)",
        [(i, f"user-{i}", f"user-{i}@example.com", 1 if rng.random() > 0.1 else 0)
         for i in range(1, rows + 1)],
    )
    conn.commit()
    conn.close()


def rowform_pieces(limit):
    sql, params = (
        Query(User).where(User.is_active == 1).where(User.id > 100).limit(limit).to_sql()
    )
    return sql, params, compile_batch_hydrator(User, SQLITE_CONVERTERS), \
        compile_json_default(User)


# --------------------------------------------------------------- variants
def make_sync(db_path, limit, _conc):
    conn = sqlite3.connect(db_path)
    sql, params, hydrate, to_dict = rowform_pieces(limit)

    def request():
        return orjson.dumps(hydrate(conn.execute(sql, params).fetchall()),
                            default=to_dict)

    return request, conn.close, "sync"


def make_coro(db_path, limit, _conc, yield_point):
    conn = sqlite3.connect(db_path)
    sql, params, hydrate, to_dict = rowform_pieces(limit)

    async def request():
        if yield_point:
            # Stands in for a real async driver's suspension point. Without it a
            # task never gives the loop a chance to run another task.
            await asyncio.sleep(0)
        return orjson.dumps(hydrate(conn.execute(sql, params).fetchall()),
                            default=to_dict)

    return request, conn.close, "async"


def make_aiosqlite(db_path, limit, _conc):
    """NOT single-threaded: aiosqlite runs sqlite3 on a helper thread."""
    import aiosqlite

    sql, params, hydrate, to_dict = rowform_pieces(limit)
    holder = {}

    async def setup():
        holder["conn"] = await aiosqlite.connect(db_path)

    async def request():
        async with holder["conn"].execute(sql, params) as cur:
            rows = await cur.fetchall()
        return orjson.dumps(hydrate(rows), default=to_dict)

    async def teardown():
        await holder["conn"].close()

    return request, teardown, "async", setup


# --------------------------------------------------------------- harness
def run_sync(request, per_worker, workers, warmup):
    for _ in range(warmup):
        request()
    total = per_worker * workers
    w0, c0 = time.perf_counter(), time.process_time()
    for _ in range(total):
        request()
    wall, cpu = time.perf_counter() - w0, time.process_time() - c0
    return total / wall, cpu / total * 1000, cpu / wall


async def run_async(request, per_worker, workers, warmup):
    for _ in range(warmup):
        await request()

    async def worker():
        for _ in range(per_worker):
            await request()

    total = per_worker * workers
    w0, c0 = time.perf_counter(), time.process_time()
    await asyncio.gather(*[worker() for _ in range(workers)])
    wall, cpu = time.perf_counter() - w0, time.process_time() - c0
    return total / wall, cpu / total * 1000, cpu / wall


def execute(builder, db_path, args, workers, use_uvloop):
    """Run one cell in a fresh loop, optionally uvloop."""
    built = builder(db_path, args.limit, workers)
    request, teardown, kind = built[0], built[1], built[2]
    setup = built[3] if len(built) > 3 else None

    if kind == "sync":
        try:
            return run_sync(request, args.per_worker, workers, args.warmup)
        finally:
            teardown()

    async def main():
        if setup:
            await setup()
        try:
            return await run_async(request, args.per_worker, workers, args.warmup)
        finally:
            r = teardown()
            if asyncio.iscoroutine(r):
                await r

    if use_uvloop:
        import uvloop

        return uvloop.run(main())
    return asyncio.run(main())


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", type=int, default=200_000)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--per-worker", type=int, default=400)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--concurrency", default="1,8,32")
    p.add_argument("--pin", default=None)
    p.add_argument("--skip-equivalence", action="store_true")
    args = p.parse_args()
    validate(p, args)
    if args.pin:
        os.sched_setaffinity(0, {int(c) for c in args.pin.split(",")})
    levels = [int(c) for c in args.concurrency.split(",")]

    variants = [
        ("sync (no asyncio)",           make_sync,                                False),
        ("coroutine, no yield",         lambda d, l, c: make_coro(d, l, c, False), False),
        ("coroutine, no yield + uvloop", lambda d, l, c: make_coro(d, l, c, False), True),
        ("coroutine, yield",            lambda d, l, c: make_coro(d, l, c, True),  False),
        ("coroutine, yield + uvloop",   lambda d, l, c: make_coro(d, l, c, True),  True),
        ("aiosqlite (uses a THREAD)",   make_aiosqlite,                           False),
        ("aiosqlite + uvloop (THREAD)", make_aiosqlite,                           True),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "b.sqlite3")
        seed(db_path, args.rows)

        if not args.skip_equivalence:
            outs = {}
            for name, builder, uv in variants:
                built = builder(db_path, args.limit, 1)
                request, teardown, kind = built[0], built[1], built[2]
                setup = built[3] if len(built) > 3 else None
                if kind == "sync":
                    outs[name] = request()
                    teardown()
                else:
                    async def once(setup=setup, request=request, teardown=teardown):
                        if setup:
                            await setup()
                        try:
                            return await request()
                        finally:
                            r = teardown()
                            if asyncio.iscoroutine(r):
                                await r
                    outs[name] = asyncio.run(once())
            ref_name, ref = next(iter(outs.items()))
            for name, payload in outs.items():
                if payload != ref:
                    print(f"FAIL: {name!r} differs from {ref_name!r}", file=sys.stderr)
                    return 1
            print(f"Output equivalence: all {len(outs)} variants emit identical JSON "
                  f"({len(ref)} bytes)\n")

        print(f"cores: {sorted(os.sched_getaffinity(0))}   {args.limit} rows/request   "
              f"{args.per_worker} requests/worker   median of {args.repeat}\n")
        head = f"{'variant':<32}" + "".join(f"{'c=' + str(c):>19}" for c in levels)
        print(head)
        print(f"{'':<32}" + "".join(f"{'rps':>10}{'cpu ms':>9}" for _ in levels))
        print("-" * len(head))

        table = {}
        for name, builder, uv in variants:
            cells = []
            for c in levels:
                trials = [execute(builder, db_path, args, c, uv) for _ in range(args.repeat)]
                rps = statistics.median(t[0] for t in trials)
                cpu = statistics.median(t[1] for t in trials)
                cells.append((rps, cpu))
            table[name] = cells
            print(f"{name:<32}" + "".join(f"{r:>10.0f}{c:>9.4f}" for r, c in cells))

        base = table["sync (no asyncio)"][0][0]
        print(f"\nvs. the synchronous reference ({base:.0f} rps):")
        for name, cells in table.items():
            print(f"  {name:<32}" + "".join(f"{r / base:>8.2f}x" for r, _ in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
