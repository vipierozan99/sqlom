"""A query cancelled mid-flight must leave the pool usable.

This is not a rare case: under any web framework a client that disconnects
cancels the handler's task, and the handler may well be awaiting a query. What
happens next was previously undefined — the library caught `CancelledError`
nowhere.

Measured before anything was written, on all three engines and all three paths
(a plain read, a stream, and a read inside a transaction): **asyncpg and psycopg
were already correct**, because both drivers cancel server-side and hand back a
clean connection. **sqlite stalled the pool every time.** aiosqlite runs each
statement in a worker thread, and cancelling the awaiting task does not stop that
thread, so the connection went back to the pool with the abandoned statement
still running — the next borrower queued behind work nobody wanted, which looks
exactly like a leak. `_SqlitePool.acquire` now interrupts it.

The statements here are slow enough to still be running when cancelled and no
slower, so the suite does not pay for this.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import Author, seed, sqlite_db

# A statement that is still running a quarter of a second in. sqlite has no
# sleep, so a recursive CTE counts instead; `.columns()` makes it something the
# planner will accept as returning rows.
SLOW_SQLITE = sa.text(
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 300000000)"
    " SELECT count(*) AS n FROM c"
).columns(sa.column("n"))
SLOW_PG = sa.select(sa.func.pg_sleep(30))


def slow_for(engine) -> Any:
    return SLOW_SQLITE if engine.dialect.name == "sqlite" else SLOW_PG


async def cancel_after(coro_factory, delay: float = 0.25) -> None:
    """Start `coro_factory()`, cancel it once it is genuinely in flight."""
    task = asyncio.create_task(coro_factory())
    await asyncio.sleep(delay)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def assert_usable(engine, expected: list[str]) -> None:
    """The pool hands back a working connection, promptly and with the right rows.

    The timeout is the assertion: without it a stalled pool hangs the suite
    instead of failing it.
    """
    try:
        rows = await asyncio.wait_for(
            engine.fetch_all(sa.select(Author).order_by(Author.id)), timeout=10
        )
    except TimeoutError:
        # Only reached when the fix under test is absent. The abandoned CTE is
        # still running, so fixture teardown would queue `close()` behind it and
        # the failure would arrive slowly; interrupt first so it arrives now.
        if engine.dialect.name == "sqlite":
            for conn in engine._require_pool()._all:
                await conn.interrupt()
        raise
    assert [a.name for a in rows] == expected


@pytest.fixture
async def names(engine):
    return [a.name for a in await engine.fetch_all(sa.select(Author).order_by(Author.id))]


class TestThePoolSurvives:
    async def test_a_cancelled_read(self, engine, names):
        await cancel_after(lambda: engine.fetch_value(slow_for(engine)))
        await assert_usable(engine, names)

    async def test_a_cancelled_stream(self, engine, names):
        """The connection is held across the consumer's awaits, so cancellation
        can land while nothing is executing on it."""

        async def consume():
            async for _row in engine.fetch_iter(sa.select(Author), chunk=1):
                await asyncio.sleep(30)

        await cancel_after(consume)
        await assert_usable(engine, names)

    async def test_a_cancellation_inside_a_transaction(self, engine, names):
        async def in_transaction():
            async with engine.begin() as conn:
                await conn.fetch_value(slow_for(engine))

        await cancel_after(in_transaction)
        await assert_usable(engine, names)

    async def test_repeated_cancellations_do_not_drain_a_small_pool(self, sqlite_path):
        """The failing shape: with two connections, three abandoned statements
        used to be enough to stall everything that came after."""
        async with sqlite_db(sqlite_path, pool_size=1, max_overflow=1) as db:
            await seed(db)
            expected = [a.name for a in await db.fetch_all(sa.select(Author).order_by(Author.id))]
            for _ in range(3):
                await cancel_after(lambda: db.fetch_value(SLOW_SQLITE))
                await assert_usable(db, expected)


class TestTimeouts:
    async def test_asyncio_timeout_is_the_timeout_mechanism(self, engine, names):
        """There is no `timeout=` argument: `asyncio.timeout()` composes with the
        cancellation handling above, so it needs no help from the library."""
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.25):
                await engine.fetch_value(slow_for(engine))

        await assert_usable(engine, names)
