#!/usr/bin/env python3
"""Behavioural tests for sqlom transactions, on both engines.

There is no test suite in this repo, so the transaction semantics are verified by
a script that asserts them against a live Postgres and says which engine failed.
Each check states the property it is defending:

  commit / rollback         the block is atomic in both directions
  read-your-writes          tx.fetch_all sees the transaction's own uncommitted work
  isolation from others     nobody outside sees it until commit
  savepoints                a failed inner block does not lose the outer one
  the footgun guard         engine.fetch_all() inside a transaction raises rather
                            than silently reading a different connection
  session reset (asyncpg)   a transaction marks the connection dirty, so `SET`
                            inside a block cannot leak to the next borrower --
                            this is the conditional-reset invariant, and it is the
                            reason transaction() routes through acquire()

Usage:
    python3 benchmarks/verify_transactions.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.models import User
from sqlom import DatabaseEngine, PsycopgEngine, Query, active_transaction

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/sqlom_bench?sslmode=disable"
TABLE = "tx_probe"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


async def setup(engine):
    async with engine.acquire() as conn:
        for sql in (f"DROP TABLE IF EXISTS {TABLE}",
                    f"CREATE TABLE {TABLE} (id int primary key, n int)"):
            await conn.execute(sql)


async def count(engine):
    """Row count read on a *fresh* connection — what another request would see."""
    async with engine.acquire() as conn:
        row = await conn.fetchrow(f"SELECT count(*) AS c FROM {TABLE}") \
            if hasattr(conn, "fetchrow") else None
        if row is not None:
            return row["c"]
        cur = await conn.execute(f"SELECT count(*) FROM {TABLE}")
        return (await cur.fetchone())[0]


async def run(label, engine, ins):
    print(f"\n--- {label} ---")
    await setup(engine)

    # 1. commit
    async with engine.transaction() as tx:
        await tx.execute(ins(1), 1, 10)
        await tx.execute(ins(2), 2, 20)
    check("commit persists both statements", await count(engine) == 2)

    # 2. rollback on exception
    class Boom(Exception):
        pass

    try:
        async with engine.transaction() as tx:
            await tx.execute(ins(1), 3, 30)
            raise Boom
    except Boom:
        pass
    check("exception rolls the whole block back", await count(engine) == 2)

    # 3. read-your-writes inside, invisible outside
    seen_inside = outside_during = None
    async with engine.transaction() as tx:
        await tx.execute(ins(1), 4, 40)
        cur = await tx._fetch_rows(f"SELECT count(*) FROM {TABLE}", ())
        seen_inside = cur[0][0]
        outside_during = await count(engine)
        raise_after = False
    check("tx sees its own uncommitted rows", seen_inside == 3,
          f"saw {seen_inside}")
    check("others do not see it before commit", outside_during == 2,
          f"outside saw {outside_during}")
    check("visible after commit", await count(engine) == 3)

    # 4. Query objects work inside a transaction and use the compiled hydrator
    async with engine.transaction() as tx:
        rows = await tx.fetch_all(Query(User).where(User.id > 100).limit(3))
    check("tx.fetch_all hydrates models", len(rows) == 3 and rows[0].id == 101,
          f"ids={[r.id for r in rows]}")
    async with engine.transaction() as tx:
        payload = await tx.fetch_json(Query(User).where(User.id > 100).limit(2))
    check("tx.fetch_json returns bytes", isinstance(payload, bytes) and payload[:1] == b"[",
          f"{payload[:28]!r}")

    # 5. savepoints: inner failure keeps outer work
    async with engine.transaction() as tx:
        await tx.execute(ins(1), 5, 50)
        try:
            async with tx.transaction() as sp:
                check("savepoint reports depth 1", sp.depth == 1, f"depth={sp.depth}")
                await sp.execute(ins(1), 6, 60)
                raise Boom
        except Boom:
            pass
    n = await count(engine)
    check("savepoint rollback keeps the outer insert", n == 4, f"count={n}")

    # 6. the footgun guard
    raised = None
    async with engine.transaction() as tx:
        try:
            await engine.fetch_all(Query(User).limit(1))
        except RuntimeError as exc:
            raised = str(exc)
    check("engine.fetch_all() inside a transaction raises", raised is not None,
          (raised or "")[:60])
    check("active_transaction() is None outside", active_transaction() is None)

    # 7. isolation level accepted and applied
    try:
        async with engine.transaction(isolation="serializable") as tx:
            await tx.execute(ins(1), 7, 70)
        ok, detail = await count(engine) == 5, ""
    except Exception as exc:
        ok, detail = False, repr(exc)
    check("serializable isolation works", ok, detail)

    try:
        async with engine.transaction(isolation="nonsense"):
            pass
        check("bad isolation name rejected", False, "no error raised")
    except (ValueError, KeyError, Exception) as exc:
        check("bad isolation name rejected", isinstance(exc, (ValueError, KeyError)),
              type(exc).__name__)

    # 8. after the block, the engine is usable again on the normal path
    rows = await engine.fetch_all(Query(User).where(User.id > 100).limit(2))
    check("engine.fetch_all works again after the block", len(rows) == 2)


async def leak_check():
    """The conditional-reset invariant: a transaction must mark the connection
    dirty, or a `SET` inside a block leaks to whoever borrows it next. Pool of 1
    so the next borrow is guaranteed to be the same connection."""
    print("\n--- asyncpg: transaction marks the connection dirty ---")
    db = DatabaseEngine(dsn=DSN, conditional_reset=True, min_size=1, max_size=1)
    await db.connect()
    try:
        before = db.reset_count
        async with db.transaction() as tx:
            await tx.execute("SET statement_timeout = '7s'")
        after_resets = db.reset_count - before
        async with db.acquire() as conn:
            leaked = await conn.fetchval("SHOW statement_timeout")
        check("transaction triggers the SQL reset", after_resets >= 1,
              f"resets={after_resets}")
        check("SET inside a transaction does not leak", leaked != "7s",
              f"next borrower saw {leaked!r}")

        # And the fast path still skips the reset
        before = db.reset_count
        for _ in range(5):
            await db.fetch_all(Query(User).limit(1))
        check("plain fetch_all still skips the reset",
              db.reset_count - before == 0, f"resets={db.reset_count - before}")
    finally:
        await db.close()


async def main():
    apg = DatabaseEngine(dsn=DSN, min_size=2, max_size=4)
    await apg.connect()
    try:
        await run("asyncpg (DatabaseEngine)", apg, lambda n: f"INSERT INTO {TABLE} VALUES ($1, $2)")
    finally:
        await apg.close()

    psy = PsycopgEngine(DSN, min_size=2, max_size=4)
    await psy.connect()
    try:
        await run("psycopg3 (PsycopgEngine)", psy, lambda n: f"INSERT INTO {TABLE} VALUES (%s, %s)")
    finally:
        await psy.close()

    await leak_check()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
