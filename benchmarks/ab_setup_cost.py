#!/usr/bin/env python3
"""How much did timing SQLAlchemy's connection setup inflate the sqlite table?

`bench_sqlite.py` originally handed the sqlom paths a `sqlite3.Connection`
created once, but built SQLAlchemy's connection (Core) and `Session` (ORM)
*inside* the timed closure. So SQLAlchemy was charged for pool checkout and
session construction that sqlom never paid, and the difference was reported as
object-mapping cost. This measures exactly what that was worth.

Why a separate script rather than comparing two runs of the suite: at 1000
rows/request the whole suite swings ±6% run to run — larger than the effect being
measured — so comparing two runs cannot resolve it. Every variant here is timed
in one process, against one database, alternating between variants across rounds
so drift and thermal effects hit both equally. That is the only way a 1-5% effect
is measurable at all.

Also measures the opposite mistake, because it is bigger and less obvious:
hoisting the `Session` out of the loop too. Its identity map then survives
between iterations, so every iteration after the first returns already-hydrated
instances and skips the work being measured.

Usage:
    python3 benchmarks/ab_setup_cost.py --limits 100,1000 --rounds 7
"""

import argparse
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.benchargs import validate
import orjson
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from benchmarks.models import DDL, TABLE_NAME, User, UserORM, users_table
from sqlom import SQLITE_CONVERTERS, Query, compile_batch_hydrator, compile_json_default


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


def build_variants(db_path, engine, held_conn, limit):
    """Return {name: (callable, note)} for one row count."""
    core_stmt = (select(users_table)
                 .where(users_table.c.is_active == 1)
                 .where(users_table.c.id > 100)
                 .limit(limit))
    orm_stmt = (select(UserORM)
                .where(UserORM.is_active == 1)
                .where(UserORM.id > 100)
                .limit(limit))
    names = [str(c.name) for c in UserORM.__table__.columns]

    raw = sqlite3.connect(db_path)
    sql, params = (Query(User).where(User.is_active == 1)
                   .where(User.id > 100).limit(limit).to_sql())
    hydrate_all = compile_batch_hydrator(User, SQLITE_CONVERTERS)
    to_dict = compile_json_default(User)

    def sqlom():
        return orjson.dumps(hydrate_all(raw.execute(sql, params).fetchall()),
                            default=to_dict)

    def core_inside():
        with engine.connect() as conn:
            result = conn.execute(core_stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    def core_hoisted():
        result = held_conn.execute(core_stmt)
        payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    def orm_inside():
        with Session(engine) as session:
            users = session.execute(orm_stmt).scalars().all()
            payload = [{n: getattr(u, n) for n in names} for u in users]
        return orjson.dumps(payload)

    def orm_hoisted_conn():
        with Session(bind=held_conn) as session:
            users = session.execute(orm_stmt).scalars().all()
            payload = [{n: getattr(u, n) for n in names} for u in users]
        return orjson.dumps(payload)

    shared_session = Session(bind=held_conn)

    def orm_session_hoisted():
        users = shared_session.execute(orm_stmt).scalars().all()
        payload = [{n: getattr(u, n) for n in names} for u in users]
        return orjson.dumps(payload)

    return {
        "sqlom compiled (batch)":        sqlom,
        "Core: connect() in loop (old)": core_inside,
        "Core: hoisted (new)":           core_hoisted,
        "ORM: Session(engine) in loop (old)": orm_inside,
        "ORM: fresh Session, hoisted conn (new)": orm_hoisted_conn,
        "ORM: Session hoisted too (WRONG)": orm_session_hoisted,
    }, (raw, shared_session)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", type=int, default=200_000)
    p.add_argument("--limits", default="100,1000")
    p.add_argument("--iterations", type=int, default=120)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--rounds", type=int, default=7,
                   help="interleaved rounds; the median across rounds is reported")
    args = p.parse_args()
    validate(p, args, extra_positive=("rounds",))

    print(f"paired A/B, one process, {args.rounds} interleaved rounds x "
          f"{args.iterations} iterations, median of rounds")
    print("variants alternate within each round, so drift hits all of them equally\n")

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "b.sqlite3")
        seed(db, args.rows)

        for limit in (int(x) for x in args.limits.split(",")):
            engine = create_engine(f"sqlite:///{db}")
            held = engine.connect()
            variants, closeable = build_variants(db, engine, held, limit)

            # Equivalence: the identity-map variant is excluded from the gate
            # because it is included precisely as an example of a wrong shape,
            # but it must still emit the same bytes on a cold session.
            outputs = {name: fn() for name, fn in variants.items()}
            ref_name, ref = next(iter(outputs.items()))
            for name, got in outputs.items():
                if got != ref:
                    print(f"FAIL: {name!r} differs from {ref_name!r}", file=sys.stderr)
                    return 1

            for fn in variants.values():
                for _ in range(args.warmup):
                    fn()

            samples = {name: [] for name in variants}
            for _ in range(args.rounds):
                for name, fn in variants.items():
                    t0 = time.perf_counter()
                    for _ in range(args.iterations):
                        fn()
                    samples[name].append((time.perf_counter() - t0) / args.iterations)

            med = {n: statistics.median(v) * 1000 for n, v in samples.items()}
            spread = {n: (max(v) - min(v)) / statistics.median(v) * 100
                      for n, v in samples.items()}

            print(f"--- {limit} rows/request ({len(ref)} bytes) ---")
            print(f"{'variant':<40}{'ms':>9}{'spread':>9}")
            print("-" * 58)
            for name in variants:
                print(f"{name:<40}{med[name]:>9.3f}{spread[name]:>8.1f}%")

            s = med["sqlom compiled (batch)"]
            old_core = med["Core: connect() in loop (old)"]
            new_core = med["Core: hoisted (new)"]
            old_orm = med["ORM: Session(engine) in loop (old)"]
            new_orm = med["ORM: fresh Session, hoisted conn (new)"]
            bad_orm = med["ORM: Session hoisted too (WRONG)"]

            print(f"\n  setup cost charged to Core only: "
                  f"{(old_core - new_core) * 1000:>6.0f} us "
                  f"({(old_core / new_core - 1) * 100:.1f}% of Core)")
            print(f"  setup cost charged to ORM only : "
                  f"{(old_orm - new_orm) * 1000:>6.0f} us "
                  f"({(old_orm / new_orm - 1) * 100:.1f}% of the ORM)")
            print(f"  identity-map reuse would flatter the ORM by "
                  f"{(new_orm / bad_orm - 1) * 100:.1f}%")
            print(f"\n  sqlom vs Core   {old_core / s:>5.2f}x before  ->  "
                  f"{new_core / s:>5.2f}x after   "
                  f"({(new_core / s) / (old_core / s) - 1:+.1%})")
            print(f"  sqlom vs ORM    {old_orm / s:>5.2f}x before  ->  "
                  f"{new_orm / s:>5.2f}x after   "
                  f"({(new_orm / s) / (old_orm / s) - 1:+.1%})\n")

            closeable[1].close()
            closeable[0].close()
            held.close()
            engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
