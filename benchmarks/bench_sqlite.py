#!/usr/bin/env python3
"""Micro-benchmark: sqlom hydration strategies vs SQLAlchemy 2.0 Core/ORM.

Isolates the Python-side hydration + JSON-serialization cost for an API
response by running every approach against the *same* sqlite file through
the *same* driver, so the database round trip is held roughly constant and
the measured delta is the object-shaping path.

Every approach is checked to emit **byte-identical JSON** before timing
starts (`--skip-equivalence` to bypass). This matters: an earlier revision
of this script had sqlom emitting `"is_active":1` while SQLAlchemy emitted
`"is_active":true`, so sqlom was skipping the int->bool coercion its
competitors were paying for, which inflated its numbers.

This does NOT exercise asyncpg/Postgres or connection-pool concurrency;
see README "Performance" for what that would still require.

Usage:
    python3 benchmarks/bench_sqlite.py [--rows N] [--limit N] [--iterations N]
"""

import argparse
import json
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson
import sqlalchemy
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from benchmarks.models import DDL, TABLE_NAME, User, UserDC, UserORM, users_table
from sqlom import (
    DATACLASS_DUMP_OPTION,
    SQLITE_CONVERTERS,
    Query,
    as_dict,
    compile_batch_hydrator,
    compile_hydrator,
    compile_json_default,
    hydrate,
)


def seed_database(db_path, row_count, rng_seed=42):
    rng = random.Random(rng_seed)
    conn = sqlite3.connect(db_path)
    conn.execute(DDL)
    rows = [
        (i, f"user-{i}", f"user-{i}@example.com", 1 if rng.random() > 0.1 else 0)
        for i in range(1, row_count + 1)
    ]
    conn.executemany(f"INSERT INTO {TABLE_NAME} VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def build_query(limit):
    return Query(User).where(User.is_active == 1).where(User.id > 100).limit(limit)


def run_sqlom_reflective(conn, limit):
    """Generic hydrate()/as_dict(): setattr/getattr per field, per row."""
    sql, params = build_query(limit).to_sql()

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        objs = [hydrate(User, row) for row in rows]
        # bool() here so output matches the compiled path, which converts
        # during hydration; without it sqlite's 0/1 leaks into the JSON.
        return orjson.dumps(objs, default=lambda o: {
            k: (bool(v) if k == "is_active" else v) for k, v in as_dict(o).items()
        })

    return _iteration


def run_sqlom_compiled(conn, limit):
    """Codegen'd per-row hydrator + codegen'd orjson hook."""
    sql, params = build_query(limit).to_sql()
    hydrate_row = compile_hydrator(User, SQLITE_CONVERTERS)
    to_dict = compile_json_default(User)

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps([hydrate_row(row) for row in rows], default=to_dict)

    return _iteration


def run_sqlom_compiled_batch(conn, limit):
    """Codegen'd batch hydrator (tuple unpacked by the `for` statement)."""
    sql, params = build_query(limit).to_sql()
    hydrate_all = compile_batch_hydrator(User, SQLITE_CONVERTERS)
    to_dict = compile_json_default(User)

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(hydrate_all(rows), default=to_dict)

    return _iteration


def run_dataclass_native(conn, limit):
    """Real stdlib @dataclass(slots=True); orjson's native dataclass path.

    Included to show the trap: orjson ignores `default=` for dataclasses and
    its native path has no fast route for slotted ones.
    """
    query = Query(UserDC).where(UserDC.is_active == 1).where(UserDC.id > 100).limit(limit)
    sql, params = query.to_sql()
    hydrate_all = compile_batch_hydrator(UserDC, SQLITE_CONVERTERS)

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(hydrate_all(rows))

    return _iteration


def run_dataclass_passthrough(conn, limit):
    """Same models, but OPT_PASSTHROUGH_DATACLASS routes them to our codegen."""
    query = Query(UserDC).where(UserDC.is_active == 1).where(UserDC.id > 100).limit(limit)
    sql, params = query.to_sql()
    hydrate_all = compile_batch_hydrator(UserDC, SQLITE_CONVERTERS)
    to_dict = compile_json_default(UserDC)

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(hydrate_all(rows), default=to_dict, option=DATACLASS_DUMP_OPTION)

    return _iteration


def run_sqlom_db_json(conn, limit):
    """Let the database shape and encode the JSON; no Python objects at all."""
    sql, params = build_query(limit).to_json_sql(dialect="sqlite")

    def _iteration():
        return conn.execute(sql, params).fetchone()[0].encode()

    return _iteration


def run_sqlalchemy_core(engine, limit):
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == 1)
        .where(users_table.c.id > 100)
        .limit(limit)
    )

    def _iteration():
        with engine.connect() as conn:
            result = conn.execute(stmt)
            # orjson requires exact `str` keys; SQLAlchemy's RowMapping keys
            # are `quoted_name` (a str subclass), so cast explicitly.
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return _iteration


def run_sqlalchemy_orm(engine, limit):
    stmt = (
        select(UserORM)
        .where(UserORM.is_active == 1)
        .where(UserORM.id > 100)
        .limit(limit)
    )
    column_names = [str(c.name) for c in UserORM.__table__.columns]

    def _iteration():
        with Session(engine) as session:
            users = session.execute(stmt).scalars().all()
            payload = [{name: getattr(u, name) for name in column_names} for u in users]
        return orjson.dumps(payload)

    return _iteration


def time_it(fn, iterations, warmup):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def summarize(name, samples, rows_returned):
    mean = statistics.mean(samples)
    return {
        "approach": name,
        "iterations": len(samples),
        "rows_per_response": rows_returned,
        "mean_ms": mean * 1000,
        "median_ms": statistics.median(samples) * 1000,
        "p95_ms": statistics.quantiles(samples, n=20)[18] * 1000
        if len(samples) >= 20
        else max(samples) * 1000,
        "stdev_ms": (statistics.stdev(samples) * 1000) if len(samples) > 1 else 0.0,
        "responses_per_sec": 1 / mean if mean > 0 else float("inf"),
    }


def print_table(results, baseline_name):
    baseline = next(r for r in results if r["approach"] == baseline_name)["mean_ms"]
    header = (
        f"{'approach':<34}{'mean ms':>9}{'median':>9}{'p95':>9}"
        f"{'resp/sec':>10}{'vs ORM':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda r: r["mean_ms"]):
        print(
            f"{r['approach']:<34}{r['mean_ms']:>9.3f}{r['median_ms']:>9.3f}"
            f"{r['p95_ms']:>9.3f}{r['responses_per_sec']:>10.1f}"
            f"{baseline / r['mean_ms']:>8.2f}x"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=200_000, help="rows seeded into the table")
    parser.add_argument("--limit", type=int, default=1000, help="rows returned per simulated request")
    parser.add_argument("--iterations", type=int, default=300, help="timed repetitions per approach")
    parser.add_argument("--warmup", type=int, default=30, help="untimed repetitions per approach")
    parser.add_argument("--out", type=str, default=None, help="optional path to write JSON results")
    parser.add_argument("--skip-equivalence", action="store_true", help="don't enforce identical output")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "bench.sqlite3")
        seed_database(db_path, args.rows)

        raw_conn = sqlite3.connect(db_path)
        core_engine = create_engine(f"sqlite:///{db_path}")
        orm_engine = create_engine(f"sqlite:///{db_path}")

        cases = [
            ("sqlom reflective (hydrate)", run_sqlom_reflective(raw_conn, args.limit)),
            ("sqlom compiled (per-row)", run_sqlom_compiled(raw_conn, args.limit)),
            ("sqlom compiled (batch)", run_sqlom_compiled_batch(raw_conn, args.limit)),
            ("dataclass slots (orjson native)", run_dataclass_native(raw_conn, args.limit)),
            ("dataclass slots (passthrough)", run_dataclass_passthrough(raw_conn, args.limit)),
            ("sqlom DB-side JSON", run_sqlom_db_json(raw_conn, args.limit)),
            ("SQLAlchemy Core", run_sqlalchemy_core(core_engine, args.limit)),
            ("SQLAlchemy ORM", run_sqlalchemy_orm(orm_engine, args.limit)),
        ]

        # Fairness gate: identical bytes, or the comparison is meaningless.
        if not args.skip_equivalence:
            reference_name, reference = cases[0][0], cases[0][1]()
            for name, fn in cases[1:]:
                got = fn()
                if got != reference:
                    print(f"FAIL: {name!r} output differs from {reference_name!r}", file=sys.stderr)
                    print(f"  {reference_name}: {reference[:160]!r}", file=sys.stderr)
                    print(f"  {name}: {got[:160]!r}", file=sys.stderr)
                    return 1
            print(f"Output equivalence: all {len(cases)} approaches emit identical JSON "
                  f"({len(reference)} bytes)\n")

        results = []
        for name, fn in cases:
            samples = time_it(fn, args.iterations, args.warmup)
            results.append(summarize(name, samples, args.limit))

        raw_conn.close()

    env = {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "sqlalchemy_version": sqlalchemy.__version__,
        "orjson_version": orjson.__version__,
        "seeded_rows": args.rows,
        "rows_per_response": args.limit,
        "iterations": args.iterations,
        "warmup": args.warmup,
    }

    print("Environment:")
    for k, v in env.items():
        print(f"  {k}: {v}")
    print()
    print_table(results, "SQLAlchemy ORM")

    if args.out:
        Path(args.out).write_text(json.dumps({"env": env, "results": results}, indent=2))
        print(f"\nWrote results to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
