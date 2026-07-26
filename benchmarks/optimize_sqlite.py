#!/usr/bin/env python3
"""What is left to optimize once transport is gone?

The sqlite profile (docs/BENCHMARKS.md §7) puts ~50% of the request in sqlom's
generated code, ~30% in the sqlite3 driver and ~15% in orjson, and names three
specific costs:

  * `_default` is called **once per row** — 80,000 times per 800 requests —
    because orjson has to call back into Python for every non-native object.
  * the hydrator calls `bool()` once per row to turn sqlite's 0/1 into a real
    boolean (sqlite has no boolean type).
  * `conn.execute(...)` constructs a fresh cursor per request.

Each variant below attacks one of those, and every variant is checked to emit
byte-identical JSON before timing. Medians of --repeat runs.

Usage:
    python3 benchmarks/optimize_sqlite.py --repeat 5
"""

import argparse
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import fields as dc_fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson

from benchmarks.models import DDL, TABLE_NAME, User
from sqlom import (
    DATACLASS_DUMP_OPTION,
    SQLITE_CONVERTERS,
    Query,
    compile_batch_hydrator,
    compile_json_default,
    model,
)

COLS = ["id", "name", "email", "is_active"]


# A slotted model whose hydrator uses a tuple lookup instead of calling bool().
class UserTupleBool(metaclass=type(User)):
    __tablename__ = TABLE_NAME
    from sqlom import Column as _C

    id = _C(int)
    name = _C(str)
    email = _C(str)
    is_active = _C(bool)
    del _C


@model(slots=False)
class UserNoSlots:
    __tablename__ = TABLE_NAME
    id: int
    name: str
    email: str
    is_active: bool


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


def sql_for(m, limit):
    return Query(m).where(m.is_active == 1).where(m.id > 100).limit(limit).to_sql()


def compile_tuple_bool_hydrator(m):
    """Like compile_batch_hydrator but indexes a (False, True) tuple.

    `bool(x)` is a C call per row; `_B[x]` is a BINARY_SUBSCR on a 2-tuple, which
    the specializing interpreter quickens. Only valid because sqlite hands back
    exactly 0 or 1 for these columns.
    """
    cols = list(m.__columns__.values())
    ns = {"_new": object.__new__, "_cls": m, "_B": (False, True)}
    vars_ = [f"f{i}" for i in range(len(cols))]
    lines = ["def _h(rows):", "    out = []", "    ap = out.append",
             f"    for {', '.join(vars_)} in rows:", "        o = _new(_cls)"]
    for col, v in zip(cols, vars_):
        expr = f"_B[{v}]" if col.py_type is bool else v
        lines.append(f"        o.{col._storage_name} = {expr}")
    lines += ["        ap(o)", "    return out"]
    src = "\n".join(lines)
    exec(src, ns)
    fn = ns["_h"]
    fn.__source__ = src
    return fn


def compile_obj_to_dict_list(m):
    """One pass over the objects producing a list of dicts — zero orjson callbacks.

    orjson invokes `default=` once per object. Building the dicts ourselves in a
    comprehension pays the same dict construction but skips N transitions across
    the Rust/Python boundary.
    """
    items = ", ".join(f"{n!r}: o.{c._storage_name}" for n, c in m.__columns__.items())
    src = f"def _f(objs):\n    return [{{{items}}} for o in objs]"
    ns = {}
    exec(src, ns)
    fn = ns["_f"]
    fn.__source__ = src
    return fn


def compile_row_factory(m, tuple_bool=True):
    """A per-row hydrator shaped for sqlite3's `row_factory` hook.

    sqlite3 calls row_factory(cursor, row) from its own C fetch loop, so the
    per-row iteration happens in C instead of Python bytecode. Costs one Python
    call per row in exchange for losing the interpreted loop.
    """
    cols = list(m.__columns__.values())
    ns = {"_new": object.__new__, "_cls": m, "_B": (False, True)}
    lines = ["def _f(cursor, row):", "    o = _new(_cls)"]
    for i, col in enumerate(cols):
        expr = (f"_B[row[{i}]]" if (col.py_type is bool and tuple_bool)
                else (f"bool(row[{i}])" if col.py_type is bool else f"row[{i}]"))
        lines.append(f"    o.{col._storage_name} = {expr}")
    lines.append("    return o")
    src = "\n".join(lines)
    exec(src, ns)
    fn = ns["_f"]
    fn.__source__ = src
    return fn


def compile_rows_to_dicts(m):
    """Skip objects entirely: rows straight to dicts."""
    cols = list(m.__columns__.items())
    vars_ = [f"f{i}" for i in range(len(cols))]
    pairs = ", ".join(
        f"{n!r}: " + (f"_B[{v}]" if c.py_type is bool else v)
        for (n, c), v in zip(cols, vars_)
    )
    src = (f"def _f(rows):\n    return [{{{pairs}}}"
           f" for {', '.join(vars_)} in rows]")
    ns = {"_B": (False, True)}
    exec(src, ns)
    fn = ns["_f"]
    fn.__source__ = src
    return fn


def build_variants(db_path, limit):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    sql, params = sql_for(User, limit)
    hydrate = compile_batch_hydrator(User, SQLITE_CONVERTERS)
    to_dict = compile_json_default(User)
    hydrate_tb = compile_tuple_bool_hydrator(UserTupleBool)
    objs_to_dicts = compile_obj_to_dict_list(User)
    rows_to_dicts = compile_rows_to_dicts(User)

    sql_ns, params_ns = sql_for(UserNoSlots, limit)
    hydrate_ns = compile_batch_hydrator(UserNoSlots, SQLITE_CONVERTERS)

    variants = {}

    # ---- baseline -------------------------------------------------------
    def baseline():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(hydrate(rows), default=to_dict)
    variants["baseline (objects, N orjson callbacks)"] = baseline

    # ---- one target at a time -------------------------------------------
    def tuple_bool():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(hydrate_tb(rows), default=to_dict)
    variants["+ tuple-index bool (no bool() call)"] = tuple_bool

    def reuse_cursor():
        cursor.execute(sql, params)
        return orjson.dumps(hydrate(cursor.fetchall()), default=to_dict)
    variants["+ reuse one cursor"] = reuse_cursor

    def one_pass():
        rows = conn.execute(sql, params).fetchall()
        return orjson.dumps(objs_to_dicts(hydrate(rows)))
    variants["+ dicts in one pass (0 callbacks)"] = one_pass

    def noslots_native():
        rows = conn.execute(sql_ns, params_ns).fetchall()
        return orjson.dumps(hydrate_ns(rows))
    variants["no-slots dataclass, orjson native"] = noslots_native

    # ---- push the per-row loop into the driver's C code -----------------
    rf_conn = sqlite3.connect(db_path)
    rf_conn.row_factory = compile_row_factory(User)
    rf_cursor = rf_conn.cursor()

    def row_factory():
        rf_cursor.execute(sql, params)
        return orjson.dumps(rf_cursor.fetchall(), default=to_dict)
    variants["row_factory (C-driven per-row loop)"] = row_factory

    # ---- stacked --------------------------------------------------------
    def stacked_objects():
        cursor.execute(sql, params)
        return orjson.dumps(objs_to_dicts(hydrate_tb(cursor.fetchall())))
    variants["STACKED objects (cursor+tuple+1pass)"] = stacked_objects

    # ---- give up the objects --------------------------------------------
    def no_objects():
        cursor.execute(sql, params)
        return orjson.dumps(rows_to_dicts(cursor.fetchall()))
    variants["no objects at all (rows -> dicts)"] = no_objects

    def floor():
        cursor.execute(sql, params)
        return cursor.fetchall()
    variants["FLOOR: fetch only, no JSON"] = floor

    def teardown():
        conn.close()
        rf_conn.close()

    return variants, teardown


def bench(fn, n, warmup):
    for _ in range(warmup):
        fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1000


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", type=int, default=200_000)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--requests", type=int, default=3000)
    p.add_argument("--warmup", type=int, default=200)
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--pin", default=None)
    args = p.parse_args()

    if args.pin:
        import os
        os.sched_setaffinity(0, {int(c) for c in args.pin.split(",")})

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "b.sqlite3")
        seed(db_path, args.rows)
        variants, teardown = build_variants(db_path, args.limit)
        try:
            # equivalence gate — the floor is exempt, it produces no JSON
            ref_name = "baseline (objects, N orjson callbacks)"
            ref = variants[ref_name]()
            for name, fn in variants.items():
                if name.startswith("FLOOR"):
                    continue
                got = fn()
                if got != ref:
                    print(f"FAIL: {name!r} differs from {ref_name!r}", file=sys.stderr)
                    print(f"  ref: {ref[:120]!r}\n  got: {got[:120]!r}", file=sys.stderr)
                    return 1
            print(f"Output equivalence: all variants emit identical JSON "
                  f"({len(ref)} bytes)\n")

            print(f"{args.limit} rows/request, median of {args.repeat} x "
                  f"{args.requests} requests, single-threaded\n")
            print(f"{'variant':<40}{'ms/req':>9}{'req/s':>9}{'vs base':>9}")
            print("-" * 67)
            results = {}
            for name, fn in variants.items():
                results[name] = statistics.median(
                    bench(fn, args.requests, args.warmup) for _ in range(args.repeat)
                )
            base = results[ref_name]
            for name, ms in results.items():
                print(f"{name:<40}{ms:>9.4f}{1000 / ms:>9.0f}{base / ms:>8.2f}x")
        finally:
            teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
