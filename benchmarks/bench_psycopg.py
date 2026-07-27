#!/usr/bin/env python3
"""sqlom vs SQLAlchemy on one driver, both with default pool behaviour.

The comparison in §13 tuned both sides and ran sqlom on asyncpg — which
SQLAlchemy cannot use simultaneously, so mapper and driver were confounded, and
the tuning included a behavioural change (skipping the pool's session reset).

Here everything is held constant except the mapper:

* **Same driver.** psycopg3 async for both: `psycopg_pool.AsyncConnectionPool`
  for sqlom, `postgresql+psycopg` for SQLAlchemy.
* **Default pool behaviour on both sides.** No `reset=` override, no
  `conditional_reset`, no `AUTOCOMMIT`, no `pool_reset_on_return`. Verified with
  `log_statement=all` that both send the same three statements per request:
  sqlom `BEGIN`/`SELECT`/`COMMIT`, SQLAlchemy `BEGIN`/`SELECT`/`ROLLBACK`.
* Same serializer (orjson), same query, byte-identical output.

What remains different is only the mapping layer: sqlom's compiled hydrator and
compiled orjson hook against SQLAlchemy Core's `RowMapping` and the ORM's
identity map and instrumented attributes.

Both event loops are reported, since uvloop is a runtime choice rather than a
pool policy and applies equally to both.

Async single-threaded at c=8 (docs/METHODOLOGY.md).

Usage:
    taskset -c 0 python3 benchmarks/bench_psycopg.py --repeat 3
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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmarks.benchargs import validate
from benchmarks.models import User, UserORM, users_table
from sqlom import PsycopgEngine, Query, compile_json_default

CONNINFO = "postgresql://postgres:postgres@127.0.0.1:5432/sqlom_bench?sslmode=disable"
SA_DSN = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/sqlom_bench?sslmode=disable"


async def make_sqlom(limit, pool_size):
    db = PsycopgEngine(CONNINFO, min_size=pool_size, max_size=pool_size)
    await db.connect()
    q = Query(User).where(User.is_active == True).where(User.id > 100).limit(limit)
    to_dict = compile_json_default(User)

    async def request():
        return orjson.dumps(await db.fetch_all(q), default=to_dict)

    return request, db.close


async def make_core(limit, pool_size):
    engine = create_async_engine(SA_DSN, pool_size=pool_size, max_overflow=0)
    stmt = (select(users_table)
            .where(users_table.c.is_active == True)
            .where(users_table.c.id > 100)
            .limit(limit))

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return request, engine.dispose


async def make_core_fast(limit, pool_size):
    """Core with positional row shaping instead of `.mappings()`.

    `.mappings()` keys are `quoted_name`, which orjson refuses, so the variant above
    casts every key of every row. That cast is not row shaping and it is expensive:
    62% of Core's whole time on sqlite, and 42-49% of its throughput end-to-end.
    Zipping the flat row against names captured once is equally idiomatic Core and
    emits identical bytes, so this is the version to quote. See METHODOLOGY
    correction 8.
    """
    engine = create_async_engine(SA_DSN, pool_size=pool_size, max_overflow=0)
    stmt = (select(users_table)
            .where(users_table.c.is_active == True)
            .where(users_table.c.id > 100)
            .limit(limit))
    names = [str(c.name) for c in users_table.columns]

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [dict(zip(names, row)) for row in result]
        return orjson.dumps(payload)

    return request, engine.dispose


async def make_orm(limit, pool_size):
    engine = create_async_engine(SA_DSN, pool_size=pool_size, max_overflow=0)
    stmt = (select(UserORM)
            .where(UserORM.is_active == True)
            .where(UserORM.id > 100)
            .limit(limit))
    names = [str(c.name) for c in UserORM.__table__.columns]

    async def request():
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            payload = [{n: getattr(u, n) for n in names} for u in users]
        return orjson.dumps(payload)

    return request, engine.dispose


CONTENDERS = [
    ("sqlom (psycopg, default pool)", make_sqlom),
    ("SQLAlchemy Core (psycopg, default)", make_core),
    ("SQLAlchemy Core positional (psycopg, default)", make_core_fast),
    ("SQLAlchemy ORM (psycopg, default)", make_orm),
]


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
    wall, cpu = time.perf_counter() - w0, time.process_time() - c0
    total = sum(counts)
    return total / wall, cpu / total * 1000, cpu / wall


async def run_all(args, label):
    if not args.skip_equivalence:
        outs = {}
        for name, factory in CONTENDERS:
            req, teardown = await factory(args.limit, args.pool_size)
            outs[name] = await req()
            r = teardown()
            if asyncio.iscoroutine(r):
                await r
        ref_name, ref = next(iter(outs.items()))
        for name, payload in outs.items():
            if payload != ref:
                print(f"FAIL: {name!r} differs from {ref_name!r}", file=sys.stderr)
                return None
        print(f"Output equivalence: all {len(outs)} contenders emit identical JSON "
              f"({len(ref)} bytes)\n")

    print(f"--- loop: {label} ---")
    print(f"{'contender':<38}{'rps':>8}{'cpu ms/req':>12}{'util':>7}")
    print("-" * 65)
    results = {}
    for name, factory in CONTENDERS:
        trials = []
        for _ in range(args.repeat):
            req, teardown = await factory(args.limit, args.pool_size)
            try:
                trials.append(await load(req, args.concurrency, args.duration, args.warmup))
            finally:
                r = teardown()
                if asyncio.iscoroutine(r):
                    await r
        rps = statistics.median(t[0] for t in trials)
        cpu = statistics.median(t[1] for t in trials)
        util = statistics.median(t[2] for t in trials)
        results[name] = rps
        print(f"{name:<38}{rps:>8.0f}{cpu:>12.4f}{util:>7.2f}")

    s = results["sqlom (psycopg, default pool)"]
    print(f"\n  sqlom vs Core  {s / results['SQLAlchemy Core (psycopg, default)']:>6.2f}x"
          f"      sqlom vs ORM  {s / results['SQLAlchemy ORM (psycopg, default)']:>6.2f}x")
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pool-size", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=4.0)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--skip-equivalence", action="store_true")
    p.add_argument("--loops", default="asyncio,uvloop")
    args = p.parse_args()
    validate(p, args)
    print(f"cores: {sorted(os.sched_getaffinity(0))}   {args.limit} rows/request   "
          f"pool {args.pool_size}   async single-thread c={args.concurrency}   "
          f"median of {args.repeat}")
    print("driver: psycopg3 async for BOTH sides; default pool behaviour on both\n")

    for loop in args.loops.split(","):
        if loop == "uvloop":
            import uvloop
            uvloop.run(run_all(args, "uvloop"))
        else:
            asyncio.run(run_all(args, "asyncio"))
        print()
        args.skip_equivalence = True  # only check once
    return 0


if __name__ == "__main__":
    sys.exit(main())
