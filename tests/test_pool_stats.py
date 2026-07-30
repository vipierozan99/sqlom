"""`engine.pool_stats()` — how full the pool is, from the driver's own counters.

The observer says which statement was slow. This says whether anything was
waiting for a connection while it ran, which is the question that usually comes
next: saturation and a slow database look identical from the outside and are
fixed differently.

Nothing is counted twice — each engine reads the numbers its pool already keeps,
so these tests are as much about the three drivers agreeing on what the fields
mean as about the arithmetic.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from conftest import Author

import rowform


class TestSnapshot:
    async def test_an_idle_pool_reports_everything_idle(self, engine):
        stats = engine.pool_stats()
        assert stats.size == stats.idle
        assert stats.in_use == 0
        assert stats.max_size >= stats.size

    async def test_a_checked_out_connection_is_in_use(self, engine):
        async with engine.transaction():
            stats = engine.pool_stats()
            assert stats.in_use >= 1
            assert stats.idle == stats.size - stats.in_use
        assert engine.pool_stats().in_use == 0

    async def test_it_reflects_growth(self, sqlite_path):
        async with rowform.SqliteEngine(sqlite_path, min_size=1, max_size=4) as db:
            assert db.pool_stats().size == 1
            await asyncio.gather(*(db.fetch_all(sa.select(Author)) for _ in range(10)))
            grown = db.pool_stats()
            assert 1 < grown.size <= 4
            assert grown.max_size == 4

    async def test_it_needs_a_connected_engine(self, sqlite_path):
        db = rowform.SqliteEngine(sqlite_path)
        with pytest.raises(rowform.EngineStateError, match="not connected"):
            db.pool_stats()


class TestWaiters:
    async def test_psycopg_reports_blocked_callers(self, pg_dsn):
        """psycopg is the only one of the three whose pool counts waiters, and
        that is the number that separates "slow query" from "pool too small"."""
        async with rowform.PsycopgEngine(pg_dsn, min_size=1, max_size=1) as db:
            assert db.pool_stats().waiting == 0

            held = asyncio.Event()
            release = asyncio.Event()

            async def hog():
                async with db.transaction():
                    held.set()
                    await release.wait()

            task = asyncio.create_task(hog())
            await held.wait()
            waiter = asyncio.create_task(db.fetch_all(sa.select(Author)))
            await asyncio.sleep(0.2)  # let it block on the pool

            assert db.pool_stats().waiting == 1
            assert db.pool_stats().in_use == 1

            release.set()
            await task
            await waiter
            assert db.pool_stats().waiting == 0

    async def test_the_others_say_none_rather_than_zero(self, sqlite_engine):
        """A zero would be a claim the pool cannot back up."""
        assert sqlite_engine.pool_stats().waiting is None

    async def test_asyncpg_says_none_too(self, pg_dsn):
        async with rowform.AsyncpgEngine(pg_dsn) as db:
            assert db.pool_stats().waiting is None
