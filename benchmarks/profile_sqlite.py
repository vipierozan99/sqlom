#!/usr/bin/env python3
"""Profile the sqlite path — no event loop, no pool, no TLS.

The Postgres profile (docs/BENCHMARKS.md §5) found sqlom's generated code to be
only ~15% of client CPU, with 38% in the asyncio loop, 19% in the asyncpg fetch
and 15% in pool acquire/release. Those three are all *transport*: they exist
because the database is a separate process reached over a socket.

sqlite removes all of it. The driver is in-process C, there is no connection
pool, no TLS handshake and no event loop. Whatever is left is the irreducible
cost of turning rows into JSON, which is the part sqlom is actually responsible
for. This is the cleanest available measurement of the mapper's own weight.

Same two profilers as profile_pg.py, for the same reasons: cProfile with a
`process_time` timer for CPU attribution and exact call counts, pyinstrument
sampling as a low-distortion cross-check.

Usage:
    python3 benchmarks/profile_sqlite.py
    python3 benchmarks/profile_sqlite.py --compare --pin 0
"""

import argparse
import cProfile
import os
import pstats
import random
import sqlite3
import statistics
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from benchmarks.benchargs import validate
from benchmarks import profkit
from benchmarks.models import DDL, TABLE_NAME, User, UserORM, users_table
from sqlom import (
    SQLITE_CONVERTERS,
    Query,
    compile_batch_hydrator,
    compile_json_default,
)


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


def make_sqlom(db_path, limit):
    conn = sqlite3.connect(db_path)
    sql, params = (
        Query(User).where(User.is_active == 1).where(User.id > 100).limit(limit).to_sql()
    )
    hydrate_all = compile_batch_hydrator(User, SQLITE_CONVERTERS)
    to_dict = compile_json_default(User)

    def request():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(hydrate_all(rows), default=to_dict)

    return "sqlom (compiled)", request, conn.close


def make_orm(db_path, limit):
    engine = create_engine(f"sqlite:///{db_path}")
    stmt = (
        select(UserORM)
        .where(UserORM.is_active == 1)
        .where(UserORM.id > 100)
        .limit(limit)
    )
    names = [str(c.name) for c in UserORM.__table__.columns]

    def request():
        with Session(engine) as session:
            users = session.execute(stmt).scalars().all()
            payload = [{n: getattr(u, n) for n in names} for u in users]
        return orjson.dumps(payload)

    return "SQLAlchemy ORM", request, engine.dispose


def make_core(db_path, limit):
    engine = create_engine(f"sqlite:///{db_path}")
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == 1)
        .where(users_table.c.id > 100)
        .limit(limit)
    )

    def request():
        with engine.connect() as conn:
            result = conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return "SQLAlchemy Core", request, engine.dispose


BUILDERS = {"sqlom": make_sqlom, "orm": make_orm, "core": make_core}


def measure(request, n, warmup):
    for _ in range(warmup):
        request()
    w0, c0 = time.perf_counter(), time.process_time()
    for _ in range(n):
        request()
    wall = (time.perf_counter() - w0) / n * 1000
    cpu = (time.process_time() - c0) / n * 1000
    return wall, cpu


def profile_one(name, db_path, args):
    label, request, teardown = BUILDERS[name](db_path, args.limit)
    try:
        wall, cpu = measure(request, args.requests, args.warmup)

        prof = cProfile.Profile(timer=time.process_time_ns, timeunit=1e-9)
        prof.enable()
        for _ in range(args.profile_requests):
            request()
        prof.disable()
        stats = pstats.Stats(prof)
        profiled_cpu = sum(t for (_c, _n, t, _ct, _cal) in stats.stats.values())

        sampled = None
        if args.sampler:
            try:
                from pyinstrument import Profiler as SamplingProfiler

                sp = SamplingProfiler(interval=0.0005)
                sp.start()
                for _ in range(args.profile_requests):
                    request()
                sp.stop()
                sampled = sp.output_text(unicode=True, color=False, show_all=False)
            except Exception as exc:  # pragma: no cover
                sampled = f"(sampling profiler unavailable: {exc})"

        return {
            "label": label,
            "wall_ms": wall,
            "cpu_ms": cpu,
            "utilization": cpu / wall,
            "stats": stats,
            "profiled_cpu": profiled_cpu,
            "n": args.profile_requests,
            "sampled": sampled,
        }
    finally:
        teardown()


def report(r, args):
    print(f"\n{'=' * 78}\n{r['label']}\n{'=' * 78}")
    print(f"  {r['wall_ms']:.3f} ms wall/req, {r['cpu_ms']:.3f} ms CPU/req, "
          f"utilization {r['utilization']:.2f}, {1000 / r['wall_ms']:.0f} req/s single-threaded")
    print(f"      -> utilization ~1.0 with no event loop: a synchronous sqlite call")
    print(f"         is CPU, not I/O wait. There is nothing to overlap.")
    print()
    profkit.print_rollup(r["stats"], r["profiled_cpu"], r["n"], r["cpu_ms"])
    if r.get("sampled"):
        print(f"\n  Sampling cross-check (pyinstrument, 0.5 ms interval):")
        for line in r["sampled"].splitlines()[:args.top + 6]:
            print(f"    {line}")
    profkit.print_top(r["stats"], args.top)


def compare(results):
    print(f"\n{'=' * 78}\nWHERE THE DIFFERENCE GOES (shares rescaled onto measured CPU/req)\n{'=' * 78}")
    labels = [r["label"] for r in results]
    rolls = [dict(profkit.rollup(r["stats"])) for r in results]
    libs = sorted({k for d in rolls for k in d},
                  key=lambda l: -max(d.get(l, 0) / r["profiled_cpu"] * r["cpu_ms"]
                                     for d, r in zip(rolls, results)))
    width = 18
    print("  " + f"{'library':<22}" + "".join(f"{l[:width]:>{width}}" for l in labels))
    print("  " + "-" * (22 + width * len(labels)))
    for lib in libs:
        vals = [d.get(lib, 0) / r["profiled_cpu"] * r["cpu_ms"] for d, r in zip(rolls, results)]
        if max(vals) < 0.002:
            continue
        print("  " + f"{lib:<22}" + "".join(f"{v:>{width}.3f}" for v in vals))
    print("  " + "-" * (22 + width * len(labels)))
    print("  " + f"{'TOTAL CPU ms/req':<22}" + "".join(f"{r['cpu_ms']:>{width}.3f}" for r in results))
    print("  " + f"{'req/s (1 thread)':<22}" + "".join(f"{1000 / r['wall_ms']:>{width}.0f}" for r in results))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", type=int, default=200_000)
    p.add_argument("--limit", type=int, default=100,
                   help="rows per request (100 matches the Postgres profile)")
    p.add_argument("--requests", type=int, default=3000)
    p.add_argument("--profile-requests", type=int, default=800)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--top", type=int, default=16)
    p.add_argument("--only", default="sqlom", choices=list(BUILDERS))
    p.add_argument("--compare", action="store_true", help="sqlom vs Core vs ORM")
    p.add_argument("--sampler", action="store_true")
    p.add_argument("--pin", default=None, help="pin this process to these cores, e.g. 0")
    args = p.parse_args()
    validate(p, args)
    if args.pin:
        os.sched_setaffinity(0, {int(c) for c in args.pin.split(",")})
    print(f"cores: {sorted(os.sched_getaffinity(0))}   rows/request: {args.limit}   "
          f"table: {args.rows} rows")
    print("no event loop, no connection pool, no TLS — in-process sqlite3 only")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "bench.sqlite3")
        seed(db_path, args.rows)

        names = ["sqlom", "core", "orm"] if args.compare else [args.only]
        results = []
        for n in names:
            results.append(profile_one(n, db_path, args))
            report(results[-1], args)
        if len(results) > 1:
            compare(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
