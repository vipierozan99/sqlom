"""`MockEngine`: rowform's row layer with the driver canned.

A rowform engine touches its driver through one hook, `Driver.fetch`; swapping
that in (and skipping the pool checkout) leaves compilation, binding, planning
and hydration running exactly as in production, including the per-request
cache-key lookup. Rows are precomputed tuples, so absolutes are not comparable
to a real backend — it is the perf gate's low-noise regression tripwire.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

import rowform as rf


class _MockDriver(rf.Driver):
    """`fetch` answers from a list. Nothing else is reachable from a read."""

    def __init__(self, dialect: Any, rows: list[tuple[Any, ...]], description: Any):
        super().__init__(dialect)
        self._rows = rows
        self._description = description

    async def fetch(self, conn, sql, params, describe):
        return self._rows, self._description if describe else None

    def stream(self, conn, sql, params, chunk, query):
        raise NotImplementedError("the mock measures fetch_all, not streaming")

    async def execute(self, conn, sql, params):
        raise NotImplementedError("the mock is a read-path instrument")

    async def execute_many(self, conn, sql, params):
        raise NotImplementedError("the mock is a read-path instrument")


class MockEngine(rf.Engine):
    """An engine whose driver call is canned — see module docstring.

    Built over a `sqlite+aiosqlite` engine rather than a postgres one so the
    *processors* are sqlite's: rows arrive as 0/1 for booleans and strings for
    temporal types, exactly as the real driver hands them over, and the hydrator
    does the same work it would in production. A postgres-flavoured mock would
    silently skip every conversion and measure a row layer that never runs.

    `_connection` is overridden as well as the driver, so a read never reaches
    SQLAlchemy's pool — the ~0.4 ms checkout is exactly the cost this instrument
    exists to exclude. The engine is still real, so the dialect, the compilation
    and the cache-key lookup all are too.
    """

    def __init__(self, rows: list[tuple[Any, ...]], columns: Sequence[str] = ()):
        super().__init__(create_async_engine("sqlite+aiosqlite://"))
        self.driver = _MockDriver(self.dialect, rows, [(name, None) for name in columns])

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """There is no connection to check out, and `fetch` never looks at one."""
        yield None

    # The one-shot reads take `_direct_connection` (`rf.Engine._acquire_for`);
    # the mock has to stub both seams or `fetch_all` would reach the real pool.
    _direct_connection = _connection


async def canned_rows(shape: str, limit: int) -> list[tuple]:
    """Real rows sourced from a throwaway sqlite db, once, at setup — the
    driver term is paid only here, never inside a MockEngine contender's
    timed `request()`.

    Rebuilt rather than passed when `--isolate` spawns a child: the seeder is
    deterministic (`harness/seed.RNG_SEED`), so the child's rows are the parent's
    rows, and shipping a few thousand of them over argv is not.
    """
    import aiosqlite

    from benchmarks.backends.sqlite import EphemeralSqlite

    if shape == "flat":
        # ORDER BY: without it the fixture's row set is whatever the query
        # planner happens to scan first — deterministic per sqlite version,
        # but an accident, not a property.
        sql = (
            "SELECT id, name, email, is_active FROM users "
            "WHERE is_active = 1 AND id > 100 ORDER BY id LIMIT ?"
        )
    elif shape == "join":
        sql = (
            "SELECT a.id, a.name, a.email, a.is_active, "
            "p.id, p.author_id, p.title, p.score, p.published "
            "FROM j_authors a JOIN j_posts p ON p.author_id = a.id "
            "WHERE a.is_active = 1 AND p.score > 100 ORDER BY a.id, p.id LIMIT ?"
        )
    else:
        raise ValueError(f"no mock row source for shape {shape!r}")

    # 3x the limit: the filters above discard ~10-50% of seeded rows, and at
    # 2x a small --limit silently canned fewer rows than `limit` while
    # params recorded the full number — the check below makes any future
    # shortfall loud instead.
    db = EphemeralSqlite.create(shape, max(limit * 3, 600))
    try:
        conn = await aiosqlite.connect(db.path)
        try:
            cur = await conn.execute(sql, (limit,))
            rows = list(await cur.fetchall())
        finally:
            await conn.close()
    finally:
        db.close()
    if len(rows) != limit:
        raise RuntimeError(
            f"mock row source produced {len(rows)} rows for --limit {limit} — "
            f"seed more rows in canned_rows or lower --limit"
        )
    if shape == "flat":
        return [(r[0], r[1], r[2], bool(r[3])) for r in rows]
    return [(r[0], r[1], r[2], bool(r[3]), r[4], r[5], r[6], r[7], bool(r[8])) for r in rows]
