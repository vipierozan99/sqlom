#!/usr/bin/env python3
"""Empirical answer to "what if the driver were Rust / built the objects itself?"

`psqlpy` is a PostgreSQL driver built on Rust's tokio-postgres, and its
`QueryResult.as_class()` constructs Python class instances **from Rust** — which
is exactly the hypothetical: no intermediate tuple, no Python-level hydration
loop. It works with rowform's `@model` dataclasses unchanged, because those have a
generated `__init__` and the query-expression descriptors live on the metaclass.

Two confounds had to be controlled first, both measured rather than assumed:

* **Pool reset.** With `log_statement=all`, asyncpg's default pool emits 6 log
  lines per request (the query plus its multi-statement `RESET ALL` on release);
  psqlpy emits 2. Comparing against asyncpg's default would credit Rust for a
  pool-policy difference, so the fair baseline uses `reset=` a no-op.
* **TLS.** asyncpg defaults to `sslmode=prefer` and this server has `ssl=on`, so
  it negotiates TLSv1.3; psqlpy connects in the clear (verified via
  `pg_stat_ssl`). The fair baseline therefore passes `sslmode=disable`.

Usage:
    taskset -c 0 python3 benchmarks/compare_rust_driver.py --repeat 3
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
from psqlpy import ConnectionPool

from benchmarks.benchargs import validate
from benchmarks.models import TABLE_NAME, User, UserDC
from rowform import (
    ASYNCPG_CONVERTERS,
    DATACLASS_DUMP_OPTION,
    compile_batch_hydrator,
    compile_json_default,
)

SQL = (f"SELECT id, name, email, is_active FROM {TABLE_NAME} "
       f"WHERE is_active = $1 AND id > $2 LIMIT $3")
DSN_ASYNCPG = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench"
DSN_PSQLPY = "postgres://postgres:postgres@127.0.0.1:5432/rowform_bench"


async def noop_reset(con):
    return None


async def make_asyncpg(limit, pool_size, fair):
    dsn = DSN_ASYNCPG + ("?sslmode=disable" if fair else "")
    kw = {"min_size": pool_size, "max_size": pool_size}
    if fair:
        kw["reset"] = noop_reset
    pool = await asyncpg.create_pool(dsn, **kw)
    hydrate = compile_batch_hydrator(User, ASYNCPG_CONVERTERS)
    to_dict = compile_json_default(User)

    async def request():
        async with pool.acquire() as con:
            rows = await con.fetch(SQL, True, 100, limit)
        return orjson.dumps(hydrate(rows), default=to_dict)

    return request, pool.close


async def make_asyncpg_held(limit, concurrency):
    """Upper bound for asyncpg: one dedicated connection per worker."""
    conns = [await asyncpg.connect(DSN_ASYNCPG + "?sslmode=disable")
             for _ in range(concurrency)]
    hydrate = compile_batch_hydrator(User, ASYNCPG_CONVERTERS)
    to_dict = compile_json_default(User)

    def make_request(i):
        con = conns[i]

        async def request():
            rows = await con.fetch(SQL, True, 100, limit)
            return orjson.dumps(hydrate(rows), default=to_dict)

        return request

    async def teardown():
        for c in conns:
            await c.close()

    return make_request, teardown


def make_psqlpy(limit, pool_size, concurrency, held=False, as_cls=False, prepared=None):
    """psqlpy = Rust (tokio-postgres). `as_cls` makes *Rust* build the objects."""

    async def build():
        pool = ConnectionPool(dsn=DSN_PSQLPY,
                              max_db_pool_size=max(pool_size, concurrency + 2))
        to_dict = compile_json_default(UserDC)
        conns = [await pool.connection() for _ in range(concurrency)] if held else None

        def make_request(i):
            async def request():
                con = conns[i] if held else await pool.connection()
                if prepared is None:
                    res = await con.fetch(SQL, [True, 100, limit])
                else:
                    res = await con.fetch(SQL, [True, 100, limit], prepared=prepared)
                if as_cls:
                    # Slotted dataclasses need the passthrough flag or orjson
                    # takes its slow fallback (see FINDINGS: orjson trap).
                    return orjson.dumps(res.as_class(as_class=UserDC),
                                        default=to_dict, option=DATACLASS_DUMP_OPTION)
                return orjson.dumps(res.result())

            return request

        return make_request, pool.close

    return build


def build_contenders(limit, pool_size, concurrency):
    def wrap(coro_fn):
        """Adapt a single-request builder to the (make_request, teardown) shape.

        Takes a *callable* returning a fresh coroutine — a coroutine object can
        only be awaited once, and each trial rebuilds its pool.
        """
        async def build():
            request, teardown = await coro_fn()
            return (lambda _i: request), teardown
        return build

    return {
        "asyncpg default + rowform (TLS, RESET)":
            wrap(lambda: make_asyncpg(limit, pool_size, fair=False)),
        "asyncpg fair + rowform (no TLS/RESET)":
            wrap(lambda: make_asyncpg(limit, pool_size, fair=True)),
        "asyncpg held conn + rowform":
            lambda: make_asyncpg_held(limit, concurrency),
        "psqlpy (Rust) pool -> dicts":
            make_psqlpy(limit, pool_size, concurrency),
        "psqlpy (Rust) pool prepared -> dicts":
            make_psqlpy(limit, pool_size, concurrency, prepared=True),
        "psqlpy (Rust) held conn -> dicts":
            make_psqlpy(limit, pool_size, concurrency, held=True),
        "psqlpy (Rust) held conn -> RUST-BUILT objects":
            make_psqlpy(limit, pool_size, concurrency, held=True, as_cls=True),
    }


async def load(make_request, concurrency, duration, warmup):
    for _ in range(warmup):
        await make_request(0)()

    async def worker(i):
        request = make_request(i)
        deadline = time.perf_counter() + duration
        n = 0
        while time.perf_counter() < deadline:
            await request()
            n += 1
        return n

    w0, c0 = time.perf_counter(), time.process_time()
    counts = await asyncio.gather(*[worker(i) for i in range(concurrency)])
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
    p.add_argument("--skip-equivalence", action="store_true")
    args = p.parse_args()
    validate(p, args)
    print(f"cores: {sorted(os.sched_getaffinity(0))}  "
          f"{args.limit} rows/request, pool {args.pool_size}, c={args.concurrency}, "
          f"median of {args.repeat}\n")

    contenders = build_contenders(args.limit, args.pool_size, args.concurrency)

    if not args.skip_equivalence:
        outs = {}
        for name, factory in contenders.items():
            mkreq, teardown = await factory()
            outs[name] = await mkreq(0)()
            r = teardown()
            if asyncio.iscoroutine(r):
                await r
        ref_name, ref = next(iter(outs.items()))
        for name, payload in outs.items():
            if payload != ref:
                print(f"FAIL: {name!r} differs from {ref_name!r}", file=sys.stderr)
                print(f"  ref: {ref[:130]!r}\n  got: {payload[:130]!r}", file=sys.stderr)
                return 1
        print(f"Output equivalence: all {len(outs)} contenders emit identical JSON "
              f"({len(ref)} bytes)\n")

    print(f"{'contender':<46}{'rps':>8}{'cpu ms/req':>12}{'util':>7}")
    print("-" * 73)
    results = {}
    for name, factory in contenders.items():
        trials = []
        for _ in range(args.repeat):
            mkreq, teardown = await factory()
            try:
                trials.append(await load(mkreq, args.concurrency, args.duration, args.warmup))
            finally:
                r = teardown()
                if asyncio.iscoroutine(r):
                    await r
        rps = statistics.median(t[0] for t in trials)
        cpu = statistics.median(t[1] for t in trials)
        util = statistics.median(t[2] for t in trials)
        results[name] = (rps, cpu)
        print(f"{name:<46}{rps:>8.0f}{cpu:>12.4f}{util:>7.2f}")

    fair = results["asyncpg fair + rowform (no TLS/RESET)"]
    print(f"\nvs. the fair asyncpg baseline ({fair[0]:.0f} rps, {fair[1]:.4f} ms CPU/req):")
    for name, (rps, cpu) in results.items():
        print(f"  {name:<46}{rps / fair[0]:>7.2f}x rps{fair[1] / cpu:>9.2f}x cpu")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
