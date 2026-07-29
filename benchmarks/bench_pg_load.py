#!/usr/bin/env python3
"""Concurrent load benchmark: rowform vs SQLAlchemy 2.0 async, over asyncpg.

This is the benchmark the README's "high-throughput HTTP services" framing
actually needs, and it closes two gaps the sqlite micro-benchmark left open:
a real Postgres round trip, and concurrency against a shared connection pool.

Closed-loop design: `concurrency` worker tasks each issue request-shaped
queries back-to-back for `duration` seconds. We report completed requests per
second plus per-request latency percentiles. Every contender gets an equally
sized pool so the comparison isn't just pool-configuration noise.

A "request" is the whole endpoint's work: run the query, materialize whatever
the approach materializes, and produce the JSON response bytes.

As in the sqlite benchmark, all contenders are checked to emit byte-identical
JSON before timing starts. `raw asyncpg` is the floor — it does no mapping.

Setup (Postgres must be reachable):
    createdb rowform_bench
    python3 benchmarks/bench_pg_load.py --seed-only

Usage:
    python3 benchmarks/bench_pg_load.py --concurrency 1,8,32,64 --duration 5
"""

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
import orjson
import sqlalchemy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmarks.benchargs import validate
from benchmarks.models import TABLE_NAME, User, UserORM, users_table
from rowform import (
    ASYNCPG_CONVERTERS,
    Query,
    compile_batch_hydrator,
    compile_json_default,
)

DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench"

SEED_SQL = f"""
DROP TABLE IF EXISTS {TABLE_NAME};
CREATE TABLE {TABLE_NAME} (
    id integer PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL,
    is_active boolean NOT NULL
);
INSERT INTO {TABLE_NAME} (id, name, email, is_active)
SELECT i, 'user-' || i, 'user-' || i || '@example.com', (i %% 10) <> 0
FROM generate_series(1, %s) AS s(i);
CREATE INDEX {TABLE_NAME}_active_id ON {TABLE_NAME} (is_active, id);
ANALYZE {TABLE_NAME};
"""


async def seed(dsn, rows):
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SEED_SQL % rows)
        total = await conn.fetchval(f"SELECT count(*) FROM {TABLE_NAME}")
        print(f"Seeded {total} rows into {TABLE_NAME}")
    finally:
        await conn.close()


# --------------------------------------------------------------- contenders
# Each factory returns an async `request()` coroutine function plus a teardown.


async def make_rowform(dsn, pool_size, limit):
    pool = await asyncpg.create_pool(dsn, min_size=pool_size, max_size=pool_size)
    query = Query(User).where(User.is_active == True).where(User.id > 100).limit(limit)
    sql, params = query.to_sql(placeholder="$")
    numbered = _number(sql)
    hydrate_all = compile_batch_hydrator(User, ASYNCPG_CONVERTERS)
    to_dict = compile_json_default(User)

    async def request():
        async with pool.acquire() as conn:
            rows = await conn.fetch(numbered, *params)
        return orjson.dumps(hydrate_all(rows), default=to_dict)

    return request, pool.close


async def make_raw_asyncpg(dsn, pool_size, limit):
    """Naive no-mapping baseline: `dict(record)` per row.

    This is what you'd write by hand without a mapper. It is NOT a floor —
    rowform's compiled path beats it, because `dict(Record)` rebuilds each dict
    through asyncpg's key machinery while the compiled hook emits a dict
    literal with the keys baked in.
    """
    pool = await asyncpg.create_pool(dsn, min_size=pool_size, max_size=pool_size)
    sql = (
        f"SELECT id, name, email, is_active FROM {TABLE_NAME} "
        f"WHERE is_active = $1 AND id > $2 LIMIT $3"
    )

    async def request():
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, True, 100, limit)
        return orjson.dumps([dict(r) for r in rows])

    return request, pool.close


async def make_raw_codegen(dsn, pool_size, limit):
    """Actual floor for producing this JSON: no objects, compiled row->dict."""
    pool = await asyncpg.create_pool(dsn, min_size=pool_size, max_size=pool_size)
    sql = (
        f"SELECT id, name, email, is_active FROM {TABLE_NAME} "
        f"WHERE is_active = $1 AND id > $2 LIMIT $3"
    )
    src = (
        "def _rows_to_dicts(rows):\n"
        "    return [{'id': f0, 'name': f1, 'email': f2, 'is_active': f3}\n"
        "            for f0, f1, f2, f3 in rows]\n"
    )
    ns = {}
    exec(src, ns)
    rows_to_dicts = ns["_rows_to_dicts"]

    async def request():
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, True, 100, limit)
        return orjson.dumps(rows_to_dicts(rows))

    return request, pool.close


async def make_sa_core(dsn, pool_size, limit):
    engine = create_async_engine(
        _sa_dsn(dsn), pool_size=pool_size, max_overflow=0, echo=False
    )
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return request, engine.dispose


async def make_sa_orm(dsn, pool_size, limit):
    engine = create_async_engine(
        _sa_dsn(dsn), pool_size=pool_size, max_overflow=0, echo=False
    )
    stmt = (
        select(UserORM)
        .where(UserORM.is_active == True)
        .where(UserORM.id > 100)
        .limit(limit)
    )
    names = [str(c.name) for c in UserORM.__table__.columns]

    async def request():
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            payload = [{n: getattr(u, n) for n in names} for u in users]
        return orjson.dumps(payload)

    return request, engine.dispose


CONTENDERS = {
    "rowform (compiled)": make_rowform,
    "raw asyncpg + codegen dict": make_raw_codegen,
    "raw asyncpg + dict(Record)": make_raw_asyncpg,
    "SQLAlchemy async Core": make_sa_core,
    "SQLAlchemy async ORM": make_sa_orm,
}


def _number(sql):
    """Number bare "$" placeholders, but leave already-numbered SQL alone.

    `Query.to_sql(placeholder="$")` numbers them itself now; re-running this
    regex over "$1" produces "$11".
    """
    import itertools
    import re

    if "$1" in sql or "$" not in sql:
        return sql
    counter = itertools.count(1)
    return re.sub(r"\$", lambda _: f"${next(counter)}", sql)


def _sa_dsn(dsn):
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


# ------------------------------------------------------------------ harness


async def run_load(request, concurrency, duration, warmup):
    """Closed loop: `concurrency` workers issue requests until the deadline."""
    for _ in range(warmup):
        await request()

    latencies = []
    stop_at = time.perf_counter() + duration

    async def worker():
        local = []
        while True:
            start = time.perf_counter()
            if start >= stop_at:
                break
            await request()
            local.append(time.perf_counter() - start)
        latencies.extend(local)

    started = time.perf_counter()
    cpu_started = time.process_time()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    elapsed = time.perf_counter() - started
    cpu_used = time.process_time() - cpu_started

    latencies.sort()

    def pct(p):
        if not latencies:
            return 0.0
        idx = min(int(len(latencies) * p / 100), len(latencies) - 1)
        return latencies[idx] * 1000

    return {
        "concurrency": concurrency,
        "completed": len(latencies),
        "elapsed_s": elapsed,
        "throughput_rps": len(latencies) / elapsed if elapsed else 0.0,
        # Client-side CPU burned per request. This is the mechanism behind the
        # throughput differences: once the box is CPU-saturated, Python-side
        # work per request is what caps requests/sec.
        "cpu_ms_per_request": (cpu_used / len(latencies) * 1000) if latencies else 0.0,
        "cpu_utilization": cpu_used / elapsed if elapsed else 0.0,
        "mean_ms": statistics.mean(latencies) * 1000 if latencies else 0.0,
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--rows", type=int, default=200_000, help="rows to seed")
    parser.add_argument("--limit", type=int, default=100, help="rows per request")
    parser.add_argument("--concurrency", default="1,8,32,64")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per cell")
    parser.add_argument("--pool-size", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--skip-equivalence", action="store_true")
    parser.add_argument(
        "--only",
        default=None,
        help="run only contenders whose name contains this substring. Use to "
             "isolate a contender from ordering effects (earlier contenders can "
             "warm caches / shift CPU frequency for later ones).",
    )
    parser.add_argument("--repeat", type=int, default=1, help="repeat each cell N times")
    args = parser.parse_args()
    validate(parser, args)
    if args.seed_only:
        await seed(args.dsn, args.rows)
        return 0

    levels = [int(c) for c in args.concurrency.split(",")]
    contenders = {
        k: v for k, v in CONTENDERS.items()
        if args.only is None or args.only.lower() in k.lower()
    }
    if not contenders:
        print(f"no contender matches {args.only!r}", file=sys.stderr)
        return 1

    # Fairness gate: identical bytes, or the comparison means nothing.
    if not args.skip_equivalence:
        outputs = {}
        for name, factory in contenders.items():
            request, teardown = await factory(args.dsn, args.pool_size, args.limit)
            outputs[name] = await request()
            await teardown()
        reference_name, reference = next(iter(outputs.items()))
        for name, payload in outputs.items():
            if payload != reference:
                print(f"FAIL: {name!r} differs from {reference_name!r}", file=sys.stderr)
                print(f"  {reference_name}: {reference[:140]!r}", file=sys.stderr)
                print(f"  {name}: {payload[:140]!r}", file=sys.stderr)
                return 1
        print(
            f"Output equivalence: all {len(outputs)} contenders emit identical JSON "
            f"({len(reference)} bytes)\n"
        )

    env = {
        "python_version": sys.version.split()[0],
        # Recorded so pinned and unpinned runs are distinguishable after the
        # fact; see benchmarks/pin_and_run.sh.
        "client_cpu_affinity": sorted(os.sched_getaffinity(0)),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "sqlalchemy_version": sqlalchemy.__version__,
        "asyncpg_version": asyncpg.__version__,
        "orjson_version": orjson.__version__,
        "rows_per_request": args.limit,
        "pool_size": args.pool_size,
        "duration_s_per_cell": args.duration,
        "concurrency_levels": levels,
    }
    print("Environment:")
    for k, v in env.items():
        print(f"  {k}: {v}")
    print()

    results = []
    for name, factory in contenders.items():
        for level in levels:
            for trial in range(args.repeat):
                request, teardown = await factory(args.dsn, args.pool_size, args.limit)
                try:
                    stats = await run_load(request, level, args.duration, args.warmup)
                finally:
                    await teardown()
                stats["approach"] = name
                stats["trial"] = trial
                results.append(stats)
                tag = f" t{trial}" if args.repeat > 1 else ""
                print(
                    f"  {name:<32} c={level:<4}{tag} "
                    f"{stats['throughput_rps']:>9.0f} rps  "
                    f"p50 {stats['p50_ms']:>7.2f} ms  p99 {stats['p99_ms']:>7.2f} ms  "
                    f"cpu {stats['cpu_ms_per_request']:>6.3f} ms/req"
                )
        print()

    def agg(name, c, key):
        vals = [r[key] for r in results if r["approach"] == name and r["concurrency"] == c]
        return statistics.median(vals) if vals else float("nan")

    print("=" * 78)
    label = "throughput (req/s)" + (" [median]" if args.repeat > 1 else "")
    print(f"{label:<32}" + "".join(f"{f'c={c}':>11}" for c in levels))
    print("-" * 78)
    for name in contenders:
        print(f"{name:<32}" + "".join(f"{agg(name, c, 'throughput_rps'):>11.0f}" for c in levels))

    baseline = "SQLAlchemy async ORM"
    if baseline in contenders:
        print()
        print(f"{'speedup vs ' + baseline:<32}" + "".join(f"{f'c={c}':>11}" for c in levels))
        print("-" * 78)
        for name in contenders:
            cells = "".join(
                f"{agg(name, c, 'throughput_rps') / agg(baseline, c, 'throughput_rps'):>10.2f}x"
                for c in levels
            )
            print(f"{name:<32}{cells}")

    print()
    print(f"{'client CPU ms per request':<32}" + "".join(f"{f'c={c}':>11}" for c in levels))
    print("-" * 78)
    for name in contenders:
        print(f"{name:<32}" + "".join(f"{agg(name, c, 'cpu_ms_per_request'):>11.3f}" for c in levels))

    if args.out:
        Path(args.out).write_text(json.dumps({"env": env, "results": results}, indent=2))
        print(f"\nWrote results to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
