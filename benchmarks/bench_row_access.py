"""Positional vs key row access, on the row type each driver actually returns.

METHODOLOGY correction 8 measured key-based access at 2.6x positional on one path, but
that number bundled three costs: the key lookup, the `str()` cast that `quoted_name`
keys force, and building a dict per row. This separates them, and does it per driver,
because an asyncpg `Record` is a C-level object with its own lookup and has no
obligation to behave like a Python dict.

Rows are **fetched before timing starts**, so this measures only the Python-side access
and shaping — not the driver materializing values off the wire, which the profiles put
at ~64% of a sqlite request. The ratios here are therefore much larger than any
end-to-end ratio and must not be quoted as one; see correction 7 on mixing a bottom-up
measurement into a top-down one.

Postgres sections are skipped if no server is reachable.

Results: benchmarks/results/row_access.txt
"""
import asyncio
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/home/user/rowform")

ROWS, N = 1000, 300
FIELDS = ["id", "name", "email", "is_active"]


def bench(label, fn):
    for _ in range(30):
        fn()
    s = []
    for _ in range(N):
        t = time.perf_counter(); fn(); s.append(time.perf_counter() - t)
    per_row = statistics.median(s) / ROWS * 1e9
    print(f"  {label:<44}{statistics.median(s)*1000:8.3f} ms  {per_row:7.1f} ns/row")
    return statistics.median(s)


print("Reading 4 fields from 1000 rows, into a 4-slot list. Lower is better.\n")

with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / "a.sqlite3")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE users (id INTEGER, name TEXT, email TEXT, is_active INTEGER)")
    c.executemany("INSERT INTO users VALUES (?,?,?,?)",
                  [(i, f"u{i}", f"u{i}@e.com", i % 2) for i in range(ROWS)])
    c.commit()

    print("sqlite3 (rows are plain tuples):")
    plain = c.execute("SELECT id, name, email, is_active FROM users").fetchall()
    bench("unpack   for a,b,c,d in rows", lambda: [(a, b, cc, d) for a, b, cc, d in plain])
    bench("index    row[0], row[1], ...", lambda: [(r[0], r[1], r[2], r[3]) for r in plain])
    bench("zip      dict(zip(names, row))", lambda: [dict(zip(FIELDS, r)) for r in plain])

    c.row_factory = sqlite3.Row
    rowobjs = c.execute("SELECT id, name, email, is_active FROM users").fetchall()
    bench("sqlite3.Row by key r['id'], ...",
          lambda: [(r["id"], r["name"], r["email"], r["is_active"]) for r in rowobjs])
    bench("sqlite3.Row positional r[0], ...",
          lambda: [(r[0], r[1], r[2], r[3]) for r in rowobjs])
    c.close()

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"
SQL = f"SELECT id, name, email, is_active FROM users LIMIT {ROWS}"


async def pg():
    import asyncpg
    conn = await asyncpg.connect(DSN)
    recs = await conn.fetch(SQL)
    print("\nasyncpg (rows are C-level Record objects):")
    bench("unpack   for a,b,c,d in records",
          lambda: [(a, b, cc, d) for a, b, cc, d in recs])
    bench("index    rec[0], rec[1], ...",
          lambda: [(r[0], r[1], r[2], r[3]) for r in recs])
    bench("by key   rec['id'], rec['name'], ...",
          lambda: [(r["id"], r["name"], r["email"], r["is_active"]) for r in recs])
    bench("dict(rec)", lambda: [dict(r) for r in recs])
    await conn.close()

    import psycopg
    from psycopg.rows import dict_row
    aconn = await psycopg.AsyncConnection.connect(DSN)
    cur = await aconn.execute(SQL)
    tuples = await cur.fetchall()
    print("\npsycopg3 (default row factory: plain tuples):")
    bench("unpack   for a,b,c,d in rows",
          lambda: [(a, b, cc, d) for a, b, cc, d in tuples])
    bench("zip      dict(zip(names, row))",
          lambda: [dict(zip(FIELDS, r)) for r in tuples])
    await aconn.close()

    dconn = await psycopg.AsyncConnection.connect(DSN, row_factory=dict_row)
    cur = await dconn.execute(SQL)
    dicts = await cur.fetchall()
    print("\npsycopg3 with row_factory=dict_row (server builds dicts):")
    bench("by key   row['id'], row['name'], ...",
          lambda: [(r["id"], r["name"], r["email"], r["is_active"]) for r in dicts])
    await dconn.close()

try:
    asyncio.run(pg())
except OSError as exc:
    print(f"\nPostgres sections skipped: {exc}", file=sys.stderr)
