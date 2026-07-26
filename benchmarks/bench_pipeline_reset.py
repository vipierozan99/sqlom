#!/usr/bin/env python3
"""Why pipelining the pool reset loses — and when pipelining does pay.

An earlier revision of docs/BENCHMARKS.md §12 said pipelining the session reset
into the query's round trip lost because "pipeline bookkeeping costs more per
statement than the round trip saves". That explanation was wrong. Reusing cursors
instead of allocating five per request changes nothing (1636 -> 1651 us), so the
cost is not per-statement at all: **entering and leaving pipeline mode has a large
fixed cost**, paid once per request, and it dwarfs the loopback round trip saved.

This isolates that with an empty pipeline, then shows the flip side. psycopg3's
`executemany` is itself implemented on pipeline mode — it even "rides" an existing
pipeline "in order to avoid sending unnecessary Sync" (cursor_async.py:129) — and
it wins precisely because it amortises that one fixed cost over many parameter
sets. Pipelining pays when the fixed cost is amortised, not when it is paid per
request to save one statement's round trip.

`executemany` also cannot express this problem: it runs *one* command with a
sequence of inputs, so it cannot batch a reset and a query together.

Sequential figures isolate per-request cost; the c=8 block is the async
single-threaded throughput view used everywhere else (docs/METHODOLOGY.md).

Usage:
    taskset -c 0 python3 benchmarks/bench_pipeline_reset.py --repeat 5
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/sqlom_bench?sslmode=disable"
SQL = "SELECT id,name,email,is_active FROM users WHERE is_active=%s AND id>%s LIMIT %s"
MULTI = "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"
SPLIT = ["SELECT pg_advisory_unlock_all()", "CLOSE ALL", "UNLISTEN *", "RESET ALL"]


async def timed(fn, n, warmup):
    for _ in range(warmup):
        await fn()
    t = time.perf_counter()
    for _ in range(n):
        await fn()
    return (time.perf_counter() - t) / n * 1e6


async def per_request_costs(args):
    conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
    qc = conn.cursor()
    mc = conn.cursor()
    rc = [conn.cursor() for _ in SPLIT]

    async def plain():
        await qc.execute(SQL, (True, 100, args.limit)); await qc.fetchall()

    async def empty_pipeline():
        async with conn.pipeline():
            pass

    async def multi_seq():
        await mc.execute(MULTI)
        await qc.execute(SQL, (True, 100, args.limit)); await qc.fetchall()

    async def split_pipe():
        async with conn.pipeline():
            for cur, s in zip(rc, SPLIT):
                await cur.execute(s)
            await qc.execute(SQL, (True, 100, args.limit)); await qc.fetchall()

    async def reset_all_pipe():
        async with conn.pipeline():
            await rc[0].execute("RESET ALL")
            await qc.execute(SQL, (True, 100, args.limit)); await qc.fetchall()

    cases = [
        ("query only, no pipeline", 1, 1, plain),
        ("EMPTY pipeline (fixed cost, no statements)", 0, 0, empty_pipeline),
        ("multi-stmt reset + query, sequential", 2, 2, multi_seq),
        ("RESET ALL + query, PIPELINED", 1, 2, reset_all_pipe),
        ("split reset + query, PIPELINED", 1, 5, split_pipe),
    ]
    print(f"Per-request cost ({args.limit} rows, sequential, median of {args.repeat})\n")
    print(f"  {'variant':<44}{'RT':>4}{'stmt':>6}{'us/req':>10}")
    print(f"  {'-' * 64}")
    out = {}
    for name, rt, stmt, fn in cases:
        v = statistics.median([await timed(fn, args.number, args.warmup)
                               for _ in range(args.repeat)])
        out[name] = v
        print(f"  {name:<44}{rt:>4}{stmt:>6}{v:>10.1f}")
    await conn.close()

    plain_c = out["query only, no pipeline"]
    empty_c = out["EMPTY pipeline (fixed cost, no statements)"]
    seq_c = out["multi-stmt reset + query, sequential"]
    print(f"\n  the reset itself, as a second round trip : "
          f"{seq_c - plain_c:>7.1f} us")
    print(f"  entering/leaving pipeline mode           : {empty_c:>7.1f} us")
    print(f"  -> pipeline overhead is {empty_c / max(1e-9, seq_c - plain_c):.1f}x the cost "
          f"it is meant to remove")
    return out


async def amortisation(args):
    """Where pipelining does pay: one statement, many parameter sets."""
    conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
    await conn.execute("DROP TABLE IF EXISTS pipe_demo")
    await conn.execute("CREATE TABLE pipe_demo (a int, b text)")
    cur = conn.cursor()
    rows = [(i, f"v{i}") for i in range(args.batch)]
    INS = "INSERT INTO pipe_demo (a,b) VALUES (%s,%s)"

    async def one_by_one():
        for r in rows:
            await cur.execute(INS, r)

    async def em():
        await cur.executemany(INS, rows)

    print(f"\nAmortisation: {args.batch} INSERTs (median of {args.repeat})\n")
    print(f"  {'variant':<44}{'us/batch':>10}{'us/stmt':>10}")
    print(f"  {'-' * 64}")
    res = {}
    for name, fn in (("execute() one at a time", one_by_one),
                     ("executemany() (pipelined internally)", em)):
        v = statistics.median([await timed(fn, max(2, args.number // args.batch), 2)
                               for _ in range(args.repeat)])
        res[name] = v
        print(f"  {name:<44}{v:>10.1f}{v / args.batch:>10.2f}")
    a, b = res["execute() one at a time"], res["executemany() (pipelined internally)"]
    print(f"\n  -> {a / b:.1f}x faster, because ONE pipeline setup is amortised over "
          f"{args.batch} statements")
    await conn.execute("DROP TABLE IF EXISTS pipe_demo")
    await conn.close()


async def throughput(args):
    """The async single-threaded c=8 view used elsewhere in this repo."""
    print(f"\nThroughput, async single-thread c={args.concurrency} "
          f"(median of {args.repeat})\n")
    print(f"  {'variant':<44}{'rps':>9}{'cpu ms/req':>12}")
    print(f"  {'-' * 64}")

    async def run(kind):
        conns = [await psycopg.AsyncConnection.connect(DSN, autocommit=True)
                 for _ in range(args.concurrency)]
        try:
            async def worker(i):
                conn = conns[i]
                qc, mc = conn.cursor(), conn.cursor()
                rcs = [conn.cursor() for _ in SPLIT]
                deadline = time.perf_counter() + args.duration
                n = 0
                while time.perf_counter() < deadline:
                    if kind == "plain":
                        await qc.execute(SQL, (True, 100, args.limit)); await qc.fetchall()
                    elif kind == "seq":
                        await mc.execute(MULTI)
                        await qc.execute(SQL, (True, 100, args.limit)); await qc.fetchall()
                    else:
                        async with conn.pipeline():
                            for c, s in zip(rcs, SPLIT):
                                await c.execute(s)
                            await qc.execute(SQL, (True, 100, args.limit))
                            await qc.fetchall()
                    n += 1
                return n

            for i in range(args.concurrency):  # warm each connection
                c = conns[i].cursor()
                for _ in range(10):
                    await c.execute(SQL, (True, 100, args.limit)); await c.fetchall()
            w0, c0 = time.perf_counter(), time.process_time()
            counts = await asyncio.gather(*[worker(i) for i in range(args.concurrency)])
            wall, cpu = time.perf_counter() - w0, time.process_time() - c0
            total = sum(counts)
            return total / wall, cpu / total * 1000
        finally:
            for c in conns:
                await c.close()

    for name, kind in (("query only, no reset", "plain"),
                       ("multi-stmt reset + query, sequential", "seq"),
                       ("split reset + query, PIPELINED", "pipe")):
        trials = [await run(kind) for _ in range(args.repeat)]
        rps = statistics.median(t[0] for t in trials)
        cpu = statistics.median(t[1] for t in trials)
        print(f"  {name:<44}{rps:>9.0f}{cpu:>12.4f}")


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--number", type=int, default=400)
    p.add_argument("--warmup", type=int, default=40)
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--batch", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--duration", type=float, default=3.0)
    args = p.parse_args()

    print(f"cores: {sorted(os.sched_getaffinity(0))}   psycopg {psycopg.__version__}\n")
    await per_request_costs(args)
    await amortisation(args)
    await throughput(args)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
