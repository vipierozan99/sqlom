#!/usr/bin/env python3
"""Bottom line: rowform vs SQLAlchemy 2.0, both fully tuned. Postgres, one core.

Every optimization found in docs/BENCHMARKS.md is applied to rowform *and* the
equivalent applied to SQLAlchemy, because a comparison where only one side is
tuned is not a comparison.

What SQLAlchemy gets, and why it matters:

* `isolation_level="AUTOCOMMIT"`. By default SQLAlchemy wraps every request in
  `BEGIN ... ROLLBACK`, so it sends **3 statements per request** against rowform's
  1 (verified with `log_statement=all`). A read-only endpoint does not need a
  transaction, and billing those two extra round trips as "ORM overhead" would be
  dishonest. With AUTOCOMMIT it sends exactly 1, same as rowform.
* `pool_reset_on_return=None` — the analogue of rowform's conditional reset.
* uvloop, the same as rowform gets.
* A statement object built once and reused, so SQLAlchemy's compiled-SQL cache
  hits every time.
* `orjson` for serialization, the same encoder rowform uses.

rowform gets: compiled batch hydrator, compiled orjson hook, conditional session
reset, uvloop.

Both default and tuned configurations are reported, because "what you get if you
just write it" and "what you get if you tune it" are different questions.

Async single-threaded at c=8 (docs/METHODOLOGY.md). Byte-identical JSON enforced.

Usage:
    taskset -c 0 python3 benchmarks/bench_final.py --repeat 3
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
from benchmarks.models import UserORM, users_table
from benchmarks.models import User
from rowform import DatabaseEngine, Query, compile_json_default

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"
SA_DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/rowform_bench"


def sa_engine(pool_size, tuned):
    kwargs = dict(pool_size=pool_size, max_overflow=0,
                  connect_args={"ssl": False})
    if tuned:
        # 3 statements/request -> 1. See module docstring.
        kwargs["isolation_level"] = "AUTOCOMMIT"
        kwargs["pool_reset_on_return"] = None
    return create_async_engine(SA_DSN, **kwargs)


async def make_rowform(limit, pool_size, tuned):
    db = DatabaseEngine(dsn=DSN, conditional_reset=tuned,
                        min_size=pool_size, max_size=pool_size)
    await db.connect()
    q = Query(User).where(User.is_active == True).where(User.id > 100).limit(limit)
    to_dict = compile_json_default(User)

    async def request():
        return orjson.dumps(await db.fetch_all(q), default=to_dict)

    return request, db.close


async def make_core(limit, pool_size, tuned):
    engine = sa_engine(pool_size, tuned)
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


async def make_core_fast(limit, pool_size, tuned):
    """Core with positional row shaping instead of `.mappings()`.

    `.mappings()` keys are `quoted_name`, which orjson refuses, so the variant above
    casts every key of every row. That cast is not row shaping and it is expensive:
    62% of Core's whole time on sqlite, and 42-49% of its throughput end-to-end.
    Zipping the flat row against names captured once is equally idiomatic Core and
    emits identical bytes, so this is the version to quote. See METHODOLOGY
    correction 8.
    """
    engine = sa_engine(pool_size, tuned)
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


async def make_orm(limit, pool_size, tuned):
    engine = sa_engine(pool_size, tuned)
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
    ("rowform (all optimizations)", make_rowform, True),
    ("rowform (unoptimized engine)", make_rowform, False),
    ("SQLAlchemy Core (tuned)", make_core, True),
    ("SQLAlchemy Core (default)", make_core, False),
    ("SQLAlchemy Core positional (tuned)", make_core_fast, True),
    ("SQLAlchemy Core positional (default)", make_core_fast, False),
    ("SQLAlchemy ORM (tuned)", make_orm, True),
    ("SQLAlchemy ORM (default)", make_orm, False),
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


async def run_all(args):
    if not args.skip_equivalence:
        outs = {}
        for name, factory, tuned in CONTENDERS:
            req, teardown = await factory(args.limit, args.pool_size, tuned)
            outs[name] = await req()
            r = teardown()
            if asyncio.iscoroutine(r):
                await r
        ref_name, ref = next(iter(outs.items()))
        for name, payload in outs.items():
            if payload != ref:
                print(f"FAIL: {name!r} differs from {ref_name!r}", file=sys.stderr)
                print(f"  ref: {ref[:120]!r}\n  got: {payload[:120]!r}", file=sys.stderr)
                return 1
        print(f"Output equivalence: all {len(outs)} contenders emit identical JSON "
              f"({len(ref)} bytes)\n")

    print(f"cores: {sorted(os.sched_getaffinity(0))}   {args.limit} rows/request   "
          f"pool {args.pool_size}   async single-thread c={args.concurrency}   "
          f"median of {args.repeat}")
    print(f"loop: {'uvloop' if args.uvloop else 'asyncio'}\n")
    print(f"{'contender':<32}{'rps':>9}{'cpu ms/req':>12}{'util':>7}")
    print("-" * 60)

    results = {}
    for name, factory, tuned in CONTENDERS:
        trials = []
        for _ in range(args.repeat):
            req, teardown = await factory(args.limit, args.pool_size, tuned)
            try:
                trials.append(await load(req, args.concurrency, args.duration, args.warmup))
            finally:
                r = teardown()
                if asyncio.iscoroutine(r):
                    await r
        rps = statistics.median(t[0] for t in trials)
        cpu = statistics.median(t[1] for t in trials)
        util = statistics.median(t[2] for t in trials)
        results[name] = (rps, cpu)
        print(f"{name:<32}{rps:>9.0f}{cpu:>12.4f}{util:>7.2f}")

    print()
    for base_name in ("SQLAlchemy ORM (tuned)", "SQLAlchemy ORM (default)",
                      "SQLAlchemy Core (tuned)"):
        b = results[base_name][0]
        s = results["rowform (all optimizations)"][0]
        print(f"  rowform vs {base_name:<28}{s / b:>6.2f}x")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pool-size", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=4.0)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--uvloop", action="store_true", default=True)
    p.add_argument("--no-uvloop", dest="uvloop", action="store_false")
    p.add_argument("--skip-equivalence", action="store_true")
    args = p.parse_args()
    validate(p, args)
    if args.uvloop:
        import uvloop
        return uvloop.run(run_all(args))
    return asyncio.run(run_all(args))


if __name__ == "__main__":
    sys.exit(main())
