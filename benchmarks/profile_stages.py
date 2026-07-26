#!/usr/bin/env python3
"""Where does the time actually go?

The README's Performance section asks for evidence about *where* time is
spent rather than a bare speedup number. This walks the pipeline one stage
at a time so each stage's marginal cost is visible, which is what justifies
(or deflates) any particular optimization.

Usage:
    python3 benchmarks/profile_stages.py [--rows N] [--limit N]
"""

import argparse
import random
import sqlite3
import sys
import tempfile
import timeit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson

from benchmarks.benchargs import validate
from benchmarks.models import DDL, TABLE_NAME, User
from sqlom import (
    SQLITE_CONVERTERS,
    Query,
    compile_batch_hydrator,
    compile_json_default,
)


def seed_database(db_path, row_count, rng_seed=42):
    rng = random.Random(rng_seed)
    conn = sqlite3.connect(db_path)
    conn.execute(DDL)
    conn.executemany(
        f"INSERT INTO {TABLE_NAME} VALUES (?, ?, ?, ?)",
        [
            (i, f"user-{i}", f"user-{i}@example.com", 1 if rng.random() > 0.1 else 0)
            for i in range(1, row_count + 1)
        ],
    )
    conn.commit()
    conn.close()


def best(fn, number, repeat=5):
    return min(timeit.repeat(fn, number=number, repeat=repeat)) / number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--number", type=int, default=200)
    args = parser.parse_args()
    validate(parser, args)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "bench.sqlite3")
        seed_database(db_path, args.rows)
        conn = sqlite3.connect(db_path)

        query = Query(User).where(User.is_active == 1).where(User.id > 100).limit(args.limit)
        sql, params = query.to_sql()
        json_sql, json_params = query.to_json_sql(dialect="sqlite")

        hydrate_all = compile_batch_hydrator(User, SQLITE_CONVERTERS)
        to_dict = compile_json_default(User)

        rows_cache = conn.execute(sql, params).fetchall()
        objs_cache = hydrate_all(rows_cache)

        stages = [
            ("1. sqlite execute + fetchall", lambda: conn.execute(sql, params).fetchall()),
            ("2. hydrate rows -> objects (cached rows)", lambda: hydrate_all(rows_cache)),
            ("3. orjson.dumps objects (cached objs)", lambda: orjson.dumps(objs_cache, default=to_dict)),
        ]

        print(f"Pipeline stages, {args.limit} rows/response, best-of-5 x {args.number}\n")
        measured = {}
        for name, fn in stages:
            t = best(fn, args.number) * 1000
            measured[name] = t
            print(f"  {name:<44}{t:>8.3f} ms")

        full = best(
            lambda: orjson.dumps(hydrate_all(conn.execute(sql, params).fetchall()), default=to_dict),
            args.number,
        ) * 1000
        db_json = best(
            lambda: conn.execute(json_sql, json_params).fetchone()[0].encode(), args.number
        ) * 1000

        print(f"\n  {'full Python pipeline (1+2+3)':<44}{full:>8.3f} ms")
        print(f"  {'sum of isolated stages':<44}{sum(measured.values()):>8.3f} ms")
        print(f"  {'DB-side JSON (replaces all 3)':<44}{db_json:>8.3f} ms")

        query_cost = measured["1. sqlite execute + fetchall"]
        hydrate_cost = measured["2. hydrate rows -> objects (cached rows)"]
        dumps_cost = measured["3. orjson.dumps objects (cached objs)"]

        print("\nShare of the full Python pipeline:")
        for label, cost in (
            ("sqlite query + row fetch", query_cost),
            ("hydration into objects", hydrate_cost),
            ("orjson serialization", dumps_cost),
        ):
            print(f"  {label:<44}{cost / full * 100:>7.1f}%")

        print(
            f"\nEven with hydration reduced to zero, the floor is "
            f"{query_cost + dumps_cost:.3f} ms "
            f"({(query_cost + dumps_cost) / full * 100:.0f}% of the current pipeline) — "
            f"which is why DB-side JSON, not faster hydration, is the big win."
        )
        conn.close()


if __name__ == "__main__":
    main()
