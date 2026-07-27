#!/usr/bin/env python3
"""Micro-benchmark: selecting **two models across a join**, sqlom vs SQLAlchemy.

Every other benchmark in this repo measures one flat table. That is the shape sqlom
was built for and the shape it looks best in, so it is also the shape least likely to
generalise. A join is the obvious next question: two entities to construct per row
instead of one, a wider select list, and — for the ORM — the machinery that makes a
`Row` of two mapped instances.

Same file, same driver, same connection type for every contender, and the same
fairness corrections the single-table suite already carries:

* **Connections and `Session`s are hoisted or not, deliberately.** The raw
  `sqlite3.Connection` sqlom uses is created once, so Core's connection is checked out
  once too — timing `engine.connect()` inside the loop charges Core for something
  sqlom never pays. The ORM's `Session`, by contrast, is created *per iteration*:
  hoisting it lets its identity map survive between iterations, so every iteration
  after the first returns already-hydrated instances and skips the work under
  measurement.
* **Byte-identical JSON is enforced before timing.** A join makes this matter more
  than usual: `bool` appears on both sides of the join, so a mapper that coerces only
  the driving entity's columns emits `"published":1` against `"published":false` and
  would otherwise be credited with a speedup for skipping the conversion.
* **The equivalence gate also checks self-consistency.** `LIMIT` without `ORDER BY` is
  not promised to return the same rows twice, so each contender is called repeatedly
  and compared with itself as well as with the others.

What this does **not** measure: Postgres, asyncpg, concurrency, or the pool. It is the
Python-side shaping cost of a join, single-threaded, and nothing else.

Usage:
    python3 benchmarks/bench_sqlite_join.py [--limit N] [--iterations N] [--repeat N]
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

from benchmarks.benchargs import validate
from benchmarks.join_models import (
    AUTHOR_FIELDS,
    AUTHORS_TABLE,
    DDL,
    POST_FIELDS,
    POSTS_TABLE,
    Author,
    AuthorORM,
    Post,
    PostORM,
    authors_table,
    posts_table,
)
from sqlom import (
    SQLITE_CONVERTERS,
    Query,
    compile_join_hydrator,
    compile_json_default,
)

# Posts per author. 5 makes the join one-to-many, so the driving row is repeated and
# the hydrator builds a fresh pair per output row rather than one pair per author.
POSTS_PER_AUTHOR = 5


def seed_database(db_path, authors, rng_seed=42):
    rng = random.Random(rng_seed)
    conn = sqlite3.connect(db_path)
    for statement in DDL:
        conn.execute(statement)
    conn.executemany(
        f"INSERT INTO {AUTHORS_TABLE} VALUES (?, ?, ?, ?)",
        [
            (i, f"author-{i}", f"author-{i}@example.com",
             1 if rng.random() > 0.1 else 0)
            for i in range(1, authors + 1)
        ],
    )
    conn.executemany(
        f"INSERT INTO {POSTS_TABLE} VALUES (?, ?, ?, ?, ?)",
        [
            (post_id, author_id, f"post-{post_id}", post_id % 1000,
             1 if rng.random() > 0.2 else 0)
            for author_id in range(1, authors + 1)
            for post_id in range(
                (author_id - 1) * POSTS_PER_AUTHOR + 1,
                author_id * POSTS_PER_AUTHOR + 1,
            )
        ],
    )
    conn.commit()
    conn.close()


# --- the query, expressed three ways --------------------------------------
# Identical predicates and identical column order in all three, because the select
# list is part of what is being timed: a contender fetching fewer columns would look
# faster for a reason that has nothing to do with object mapping.


def sqlom_query(limit):
    return (Query(Author, Post)
            .join(Post, Post.author_id == Author.id)
            .where(Author.is_active == True)     # noqa: E712 - builds SQL, not a bool
            .where(Post.score > 100)
            .limit(limit))


def core_statement(limit):
    return (select(authors_table, posts_table)
            .join(posts_table, posts_table.c.author_id == authors_table.c.id)
            .where(authors_table.c.is_active == True)   # noqa: E712
            .where(posts_table.c.score > 100)
            .limit(limit))


def orm_statement(limit):
    return (select(AuthorORM, PostORM)
            .join(PostORM, PostORM.author_id == AuthorORM.id)
            .where(AuthorORM.is_active == True)         # noqa: E712
            .where(PostORM.score > 100)
            .limit(limit))


# --- contenders -----------------------------------------------------------


def run_sqlom(conn, limit):
    """sqlom: compiled join hydrator, then orjson with a compiled default hook.

    The hydrator and the hook are built once, which is what sqlom does at runtime —
    both are cached per model shape, so building them inside the loop would time a
    cost a real service pays once per process.
    """
    query = sqlom_query(limit)
    sql, params = query.to_sql()
    hydrate_rows = compile_join_hydrator(
        query.hydration_spec(), SQLITE_CONVERTERS, wrap=query.is_multi_entity
    )
    author_default = compile_json_default(Author)
    post_default = compile_json_default(Post)

    def _default(obj):
        # One hook for two models. `compile_json_default` is per-model, so the pair
        # has to be dispatched on; a single-model response never pays this.
        return author_default(obj) if type(obj) is Author else post_default(obj)

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        pairs = hydrate_rows(rows)
        return orjson.dumps(
            [{"author": author, "post": post} for author, post in pairs],
            default=_default,
        )

    return _iteration


def run_sqlom_tuples(conn, limit):
    """sqlom without the JSON hook: dicts built directly from the hydrated objects.

    Included because the hook is an orjson-specific trick and its cost is not
    obviously separable from hydration. This variant shows what the same objects cost
    when shaped by ordinary attribute access, which is what any non-orjson serializer
    would do.
    """
    query = sqlom_query(limit)
    sql, params = query.to_sql()
    hydrate_rows = compile_join_hydrator(
        query.hydration_spec(), SQLITE_CONVERTERS, wrap=query.is_multi_entity
    )

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        pairs = hydrate_rows(rows)
        return orjson.dumps([
            {
                "author": {name: getattr(a, name) for name in AUTHOR_FIELDS},
                "post": {name: getattr(p, name) for name in POST_FIELDS},
            }
            for a, p in pairs
        ])

    return _iteration


def run_sqlalchemy_core(sa_conn, limit):
    """SQLAlchemy Core over a hoisted connection.

    A joined Core row is flat, and both tables have an `id`, so `.mappings()` would
    collide on the duplicate key. Slicing positionally is the idiomatic answer and is
    also the cheapest one available to Core, so this is Core at its best rather than
    Core made to look slow.
    """
    stmt = core_statement(limit)
    width = len(AUTHOR_FIELDS)

    def _iteration():
        result = sa_conn.execute(stmt)
        payload = [
            {
                "author": dict(zip(AUTHOR_FIELDS, row[:width])),
                "post": dict(zip(POST_FIELDS, row[width:])),
            }
            for row in result
        ]
        return orjson.dumps(payload)

    return _iteration


def run_sqlalchemy_orm(sa_conn, limit):
    """SQLAlchemy ORM selecting two entities: a fresh `Session` per iteration.

    See the module docstring for why the `Session` is not hoisted and the connection
    is. `.all()` on a two-entity select yields `Row` objects that unpack to
    `(AuthorORM, PostORM)`.
    """
    stmt = orm_statement(limit)

    def _iteration():
        with Session(bind=sa_conn) as session:
            rows = session.execute(stmt).all()
            payload = [
                {
                    "author": {name: getattr(a, name) for name in AUTHOR_FIELDS},
                    "post": {name: getattr(p, name) for name in POST_FIELDS},
                }
                for a, p in rows
            ]
        return orjson.dumps(payload)

    return _iteration


def run_dict_floor(conn, limit):
    """No object mapping at all: sqlite3 rows straight into dicts.

    Called a floor because it builds no model instances, but note what it is *not* a
    floor for: it still materialises two dicts per row, and `sqlom (hook)` materialises
    none — orjson walks the objects directly. So the hook path can and does come out
    ahead of this. The honest reading is that this bounds the *object construction*
    cost, not the response-building cost.
    """
    sql, params = sqlom_query(limit).to_sql()
    width = len(AUTHOR_FIELDS)
    author_bool = AUTHOR_FIELDS.index("is_active")
    post_bool = POST_FIELDS.index("published")

    def _iteration():
        rows = conn.execute(sql, params).fetchall()
        payload = []
        for row in rows:
            author = dict(zip(AUTHOR_FIELDS, row[:width]))
            post = dict(zip(POST_FIELDS, row[width:]))
            # The coercion every other contender's mapper performs. Skipping it here
            # would make the floor look cheaper than it is *and* fail the gate.
            author[AUTHOR_FIELDS[author_bool]] = bool(author[AUTHOR_FIELDS[author_bool]])
            post[POST_FIELDS[post_bool]] = bool(post[POST_FIELDS[post_bool]])
            payload.append({"author": author, "post": post})
        return orjson.dumps(payload)

    return _iteration


# --- harness --------------------------------------------------------------


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
        "p95_ms": (statistics.quantiles(samples, n=20)[18] * 1000
                   if len(samples) >= 20 else max(samples) * 1000),
        "stdev_ms": (statistics.stdev(samples) * 1000) if len(samples) > 1 else 0.0,
        "responses_per_sec": 1 / mean if mean > 0 else float("inf"),
    }


def print_table(results, baseline_name):
    def med(name, key):
        vals = [r[key] for r in results if r["approach"] == name]
        return statistics.median(vals) if vals else float("nan")

    have_baseline = any(r["approach"] == baseline_name for r in results)
    baseline = med(baseline_name, "mean_ms") if have_baseline else float("nan")
    header = (f"{'approach':<30}{'mean ms':>9}{'median':>9}{'p95':>9}"
              f"{'resp/sec':>10}{'vs ORM':>9}")
    print(header)
    print("-" * len(header))
    names = sorted({r["approach"] for r in results}, key=lambda n: med(n, "mean_ms"))
    for name in names:
        mean = med(name, "mean_ms")
        ratio = f"{baseline / mean:>8.2f}x" if have_baseline else f"{'-':>9}"
        print(f"{name:<30}{mean:>9.3f}{med(name, 'median_ms'):>9.3f}"
              f"{med(name, 'p95_ms'):>9.3f}"
              f"{med(name, 'responses_per_sec'):>10.1f}{ratio}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=40_000,
                        help=f"authors seeded; posts are {POSTS_PER_AUTHOR}x that")
    parser.add_argument("--limit", type=int, default=1000,
                        help="joined rows returned per simulated request")
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--repeat", type=int, default=1,
                        help="repeat each approach N times; quote medians")
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--skip-equivalence", action="store_true")
    parser.add_argument("--only", default=None,
                        help="run only approaches whose name contains this substring, "
                             "to isolate one from in-process ordering effects")
    parser.add_argument("--reverse", action="store_true",
                        help="reverse approach order, to expose ordering bias")
    args = parser.parse_args()
    validate(parser, args)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "join.sqlite3")
        seed_database(db_path, args.rows)

        raw_conn = sqlite3.connect(db_path)
        core_engine = create_engine(f"sqlite:///{db_path}")
        orm_engine = create_engine(f"sqlite:///{db_path}")
        core_conn = core_engine.connect()
        orm_conn = orm_engine.connect()

        cases = [
            ("sqlom (hook)", run_sqlom(raw_conn, args.limit)),
            ("sqlom (attribute dicts)", run_sqlom_tuples(raw_conn, args.limit)),
            ("dict floor (no objects)", run_dict_floor(raw_conn, args.limit)),
            ("SQLAlchemy Core", run_sqlalchemy_core(core_conn, args.limit)),
            ("SQLAlchemy ORM", run_sqlalchemy_orm(orm_conn, args.limit)),
        ]
        if args.only:
            cases = [c for c in cases if args.only.lower() in c[0].lower()]
            if not cases:
                print(f"no approach matches {args.only!r}", file=sys.stderr)
                return 1
        if args.reverse:
            cases = list(reversed(cases))

        if not args.skip_equivalence:
            reference_name, reference = cases[0][0], cases[0][1]()
            if reference == b"[]":
                # An empty result set makes every contender "agree" while measuring
                # nothing. The single-table suite hit this; it is worth an explicit
                # check rather than a passing gate over no data.
                print("FAIL: the query returned no rows, so the gate proves nothing",
                      file=sys.stderr)
                return 1
            for name, fn in cases:
                for _ in range(3):
                    if fn() != reference:
                        print(f"FAIL: {name!r} is not deterministic across calls, or "
                              f"differs from {reference_name!r}", file=sys.stderr)
                        print(f"  {reference_name}: {reference[:200]!r}",
                              file=sys.stderr)
                        print(f"  {name}: {fn()[:200]!r}", file=sys.stderr)
                        return 1
            print(f"Output equivalence: all {len(cases)} approaches emit identical "
                  f"JSON ({len(reference)} bytes), stable over 3 repeats each")
            print(f"SQL under test:\n  {sqlom_query(args.limit).to_sql()[0]}\n")

        results = []
        for name, fn in cases:
            for trial in range(args.repeat):
                samples = time_it(fn, args.iterations, args.warmup)
                row = summarize(name, samples, args.limit)
                row["trial"] = trial
                results.append(row)

        core_conn.close()
        orm_conn.close()
        core_engine.dispose()
        orm_engine.dispose()
        raw_conn.close()

    env = {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "sqlalchemy_version": sqlalchemy.__version__,
        "orjson_version": orjson.__version__,
        "seeded_authors": args.rows,
        "posts_per_author": POSTS_PER_AUTHOR,
        "rows_per_response": args.limit,
        "iterations": args.iterations,
        "warmup": args.warmup,
        "repeat": args.repeat,
    }
    print("Environment:")
    for key, value in env.items():
        print(f"  {key}: {value}")
    print()
    print_table(results, "SQLAlchemy ORM")

    if args.out:
        Path(args.out).write_text(json.dumps({"env": env, "results": results},
                                             indent=2))
        print(f"\nWrote results to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
