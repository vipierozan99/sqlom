#!/usr/bin/env python3
"""Bound two hypotheticals with measurement instead of guessing.

1. **What if the driver constructed slotted objects directly**, skipping the
   intermediate tuple and the Python-level hydration loop?
2. **What if the mapper were a Rust extension?**

Neither can be measured directly without building it, but both can be *bounded*
by decomposing what the current pipeline pays for:

  * Vary column count at fixed row count -> per-value cost (creating the Python
    int/str objects) versus per-row cost (allocating the row tuple, stepping the
    statement). A driver that builds objects directly removes the per-row tuple
    but *cannot* remove per-value object creation: the values are the point.
  * Vary row count at fixed columns -> per-statement overhead.
  * Time hydration and dict-building in isolation -> the Python-level work a
    native implementation would replace.

The result is an upper bound, because a native implementation still has to do
the allocation and slot stores, just in C rather than bytecode.

Usage:
    python3 benchmarks/estimate_ceilings.py
"""

import argparse
import random
import sqlite3
import statistics
import sys
import tempfile
import timeit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson

from benchmarks.benchargs import validate
from benchmarks.models import DDL, TABLE_NAME, User
from sqlom import SQLITE_CONVERTERS, compile_batch_hydrator, compile_json_default

COLS = ["id", "name", "email", "is_active"]


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


def best(fn, number, repeat=7):
    return min(timeit.repeat(fn, number=number, repeat=repeat)) / number


def linfit(xs, ys):
    """Least squares slope/intercept — enough for a 4-point line."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return slope, my - slope * mx


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", type=int, default=200_000)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--number", type=int, default=2000)
    p.add_argument("--pin", default=None)
    args = p.parse_args()
    validate(p, args)
    if args.pin:
        import os
        os.sched_setaffinity(0, {int(c) for c in args.pin.split(",")})

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "b.sqlite3")
        seed(db, args.rows)
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        L = args.limit
        where = f"WHERE is_active = 1 AND id > 100 LIMIT {L}"

        print(f"{L} rows/request, best-of-7 x {args.number}, single core\n")

        # ---- 1. per-value vs per-row fetch cost -------------------------
        print("1. Fetch cost vs column count (fixed 100 rows)")
        print(f"   {'columns':<10}{'us/request':>12}{'us/row':>10}")
        ys = []
        for k in range(1, 5):
            sel = ", ".join(COLS[:k])
            sql = f"SELECT {sel} FROM {TABLE_NAME} {where}"
            t = best(lambda s=sql: cur.execute(s).fetchall(), args.number)
            ys.append(t)
            print(f"   {k:<10}{t * 1e6:>12.2f}{t / L * 1e6:>10.3f}")
        per_value, per_row_fixed = linfit([1, 2, 3, 4], ys)
        print(f"\n   per extra column : {per_value / L * 1e9:>7.1f} ns/row"
              f"   (creating one Python value)")
        print(f"   fixed per request: {per_row_fixed * 1e6:>7.2f} us"
              f"   (tuple alloc x{L} + stepping + statement)")
        print(f"   -> of the 4-column fetch, {4 * per_value / ys[3] * 100:.0f}% is value "
              f"creation and {per_row_fixed / ys[3] * 100:.0f}% is row/statement overhead")

        # ---- 2. per-statement vs per-row --------------------------------
        print("\n2. Fetch cost vs row count (4 columns)")
        print(f"   {'rows':<10}{'us/request':>12}{'ns/row':>10}")
        rs, ts = [], []
        for n in (1, 10, 100, 1000):
            sql = f"SELECT {', '.join(COLS)} FROM {TABLE_NAME} WHERE is_active = 1 AND id > 100 LIMIT {n}"
            t = best(lambda s=sql: cur.execute(s).fetchall(), max(200, args.number // (n or 1)))
            rs.append(n); ts.append(t)
            print(f"   {n:<10}{t * 1e6:>12.2f}{t / n * 1e9:>10.1f}")
        per_row, per_stmt = linfit(rs, ts)
        print(f"\n   per row          : {per_row * 1e9:>7.1f} ns")
        print(f"   per statement    : {per_stmt * 1e6:>7.2f} us  (execute + prepare lookup)")

        # ---- 3. the Python-level work a native impl would replace -------
        sql4 = f"SELECT {', '.join(COLS)} FROM {TABLE_NAME} {where}"
        rows = cur.execute(sql4).fetchall()
        hydrate = compile_batch_hydrator(User, SQLITE_CONVERTERS)
        to_dict = compile_json_default(User)
        objs = hydrate(rows)

        t_fetch = best(lambda: cur.execute(sql4).fetchall(), args.number)
        t_hyd = best(lambda: hydrate(rows), args.number)
        t_json_obj = best(lambda: orjson.dumps(objs, default=to_dict), args.number)
        t_full = best(lambda: orjson.dumps(hydrate(cur.execute(sql4).fetchall()),
                                          default=to_dict), args.number)

        print("\n3. Stage costs in isolation")
        print(f"   {'stage':<34}{'us/req':>9}{'ns/row':>9}{'share':>8}")
        for name, t in (("sqlite3 fetch (tuples)", t_fetch),
                        ("hydrate tuples -> slotted objects", t_hyd),
                        ("orjson.dumps + per-row _default", t_json_obj)):
            print(f"   {name:<34}{t * 1e6:>9.2f}{t / L * 1e9:>9.1f}{t / t_full * 100:>7.1f}%")
        print(f"   {'measured full pipeline':<34}{t_full * 1e6:>9.2f}"
              f"{t_full / L * 1e9:>9.1f}{100.0:>7.1f}%")

        # ---- 4. the two ceilings ----------------------------------------
        value_creation = 4 * per_value
        tuple_overhead = max(0.0, t_fetch - value_creation - per_stmt)

        print("\n" + "=" * 74)
        print("CEILING 1: driver constructs slotted objects directly")
        print("=" * 74)
        print(f"  removable: the row tuple ({tuple_overhead * 1e6:.2f} us) and the Python")
        print(f"             hydration loop ({t_hyd * 1e6:.2f} us)")
        print(f"  NOT removable: creating {4 * L} Python values "
              f"({value_creation * 1e6:.2f} us), the statement ({per_stmt * 1e6:.2f} us),")
        print(f"             and the JSON step ({t_json_obj * 1e6:.2f} us)")

        # The stages are timed in isolation and do not add up to the pipeline
        # measured end to end -- composing them costs something the parts don't
        # show. An earlier version of this script built the floor by *summing*
        # the surviving stages and then divided it into the *measured* total,
        # which quietly credited the whole residual to the speedup and published
        # 1.51x instead of ~1.42x. Subtract from the measured total instead, so
        # the numerator and denominator sit on the same basis, and print the
        # residual rather than burying it.
        stage_sum = t_fetch + t_hyd + t_json_obj
        residual = t_full - stage_sum
        removable = tuple_overhead + t_hyd
        floor1 = t_full - removable
        print(f"\n  stage sum {stage_sum * 1e6:.2f} us vs measured pipeline "
              f"{t_full * 1e6:.2f} us -> {residual * 1e6:+.2f} us unattributed")
        print(f"  ({abs(residual) / t_full * 100:.1f}% of the request is composition cost the "
              f"isolated stages\n   do not capture; it is left in the floor rather than "
              f"assumed removable)")
        print(f"\n  optimistic floor: {floor1 * 1e6:.2f} us vs {t_full * 1e6:.2f} us now"
              f"  ->  {t_full / floor1:.2f}x")
        print(f"  = measured {t_full * 1e6:.2f} - tuple {tuple_overhead * 1e6:.2f} "
              f"- hydration {t_hyd * 1e6:.2f}")
        print("  (optimistic because a native builder still allocates the object and")
        print("   writes its slots; it does that in C rather than bytecode, not for free)")

        # Measured anchor: shaping and encoding entirely below Python, same row
        # count. This is what "no Python object per row" actually costs.
        from sqlom import Query as _Q

        json_sql, json_params = (
            _Q(User).where(User.is_active == 1).where(User.id > 100).limit(L)
            .to_json_sql(dialect="sqlite")
        )
        t_dbjson = best(lambda: cur.execute(json_sql, json_params).fetchone()[0].encode(),
                        args.number)

        print("\n" + "=" * 74)
        print("CEILING 2: Rust extension")
        print("=" * 74)
        print(f"  orjson is ALREADY Rust and sqlite3 is already C, so a Rust rewrite can")
        print(f"  only replace the {t_hyd * 1e6:.2f} us hydration loop and the per-row")
        print(f"  _default callback inside the {t_json_obj * 1e6:.2f} us JSON step.")
        print()
        print(f"  (a) Rust mapper still returning Python objects:")
        print(f"      bounded by ceiling 1 — {t_full / floor1:.2f}x — because creating "
              f"{4 * L} Python")
        print(f"      values costs {value_creation * 1e6:.2f} us ({value_creation / t_full * 100:.0f}% "
              f"of the request) and is unavoidable")
        print(f"      the moment the API hands back objects with Python field values.")
        print()
        print(f"  (b) Rust returning JSON bytes, no Python objects at all:")
        print(f"      Python value creation vanishes too. Measured anchor for exactly")
        print(f"      that shape — sqlite's json_group_array, all work below Python:")
        print(f"      {t_dbjson * 1e6:.2f} us vs {t_full * 1e6:.2f} us  ->  "
              f"{t_full / t_dbjson:.2f}x")
        print(f"      A Rust extension doing rows->JSON could approach this, and it is")
        print(f"      already reachable today in SQL with no Rust at all.")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
