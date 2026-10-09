"""One engine, many tasks at once — the shape every service using this runs in.

Nothing else in the suite starts a second task, so every property here was
unasserted: the whole suite could pass on a library whose statement cache
compiled twice under load, whose scope registration leaked from one request into
the next, or whose failures kept their checkout. Those are the three failure modes
that only appear concurrently, and each has its class below.

Two things are deliberately *not* tested on sqlite. Concurrent writers, because a
file database serialises them and a lock timeout would read as a rowform bug; and
the uncommitted-write isolation of two open scopes, for the same reason. Both run
on postgres, where they are real.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import AUTHORS, Author, Book, sqlite_db

import rowform as rf

NAMES = [a["name"] for a in AUTHORS]
BY_ID = sa.select(Author).order_by(Author.id)


async def gather(factory, n: int = 24) -> list[Any]:
    """`n` copies of one coroutine, in flight together.

    24 against a default pool of 5+10: more tasks than connections on purpose, so
    the queue is exercised rather than just the checkout.
    """
    return await asyncio.gather(*(factory() for _ in range(n)))


class TestManyReadsAtOnce:
    async def test_the_same_statement_from_every_task(self, engine):
        """The plain case, and the one that would catch a hydrator or compiled
        statement being mutated per call rather than reused read-only."""
        results = await gather(lambda: engine.fetch_all(BY_ID))
        assert all([a.name for a in rows] == NAMES for rows in results)

    async def test_different_shapes_in_flight_together_do_not_cross(self, engine):
        """Three statements whose plans differ — entity, scalar, arity two —
        interleaved. A cache keyed loosely enough to confuse two of them hands
        back the wrong hydrator, and the wrong hydrator on the right rows is
        exactly the silent corruption `planner.py` is written against.
        """
        models = sa.select(Author).order_by(Author.id)
        scalars = sa.select(Author.name).order_by(Author.id)
        pairs = sa.select(Author, Book).join(Book).order_by(Author.id, Book.id)

        async def read(statement):
            return await engine.fetch_all(statement)

        batches = await asyncio.gather(
            *(read(s) for _ in range(8) for s in (models, scalars, pairs))
        )
        for rows in batches[0::3]:
            assert all(isinstance(row, Author) for row in rows)
        for rows in batches[1::3]:
            assert rows == NAMES
        for rows in batches[2::3]:
            assert all(isinstance(a, Author) and isinstance(b, Book) for a, b in rows)

    async def test_the_cache_ends_with_one_entry_per_statement(self, sqlite_path):
        """`_query_for` is synchronous from lookup to insert, so concurrent
        callers cannot both miss and both compile. Asserted as a count because
        that is what a future `await` slipped into the middle would change.
        """
        async with sqlite_db(sqlite_path) as db:
            await asyncio.gather(
                *(db.fetch_all(BY_ID) for _ in range(12)),
                *(db.fetch_all(sa.select(Author.name)) for _ in range(12)),
            )
            assert db.cached_statements == 2


class TestScopesAreTaskLocal:
    async def test_a_sibling_task_neither_sees_the_scope_nor_is_refused_by_it(self, engine):
        """`_ACTIVE` is a `ContextVar`, so a scope belongs to the task that opened
        it. If it were engine state instead, one request holding a transaction
        would make every other request's one-shot raise `EngineStateError` — the
        guard turning into a global lock on the API it protects.
        """
        opened, release = asyncio.Event(), asyncio.Event()

        async def holder():
            async with engine.begin() as conn:
                assert rf.active_connection() is conn
                opened.set()
                await release.wait()

        task = asyncio.create_task(holder())
        try:
            await opened.wait()
            assert rf.active_connection() is None
            rows = await engine.fetch_all(BY_ID)
            assert [a.name for a in rows] == NAMES
        finally:
            release.set()
            await task

    async def test_a_task_started_inside_a_scope_inherits_it(self, engine):
        """The other half of the same semantics, and the half that has to refuse:
        a task created inside a scope gets a copy of the context, so it sees the
        scope — and a one-shot there really would take a second connection and
        miss the transaction's writes. Refusing is right; it is written down here
        because it follows from asyncio's context copy rather than from any code
        in this library.
        """
        async with engine.begin() as conn:

            async def child():
                assert rf.active_connection() is conn
                with pytest.raises(rf.EngineStateError, match="different pooled connection"):
                    await engine.fetch_all(BY_ID)
                return await conn.fetch_all(BY_ID)

            rows = await asyncio.create_task(child())
            assert [a.name for a in rows] == NAMES

    async def test_scopes_in_parallel_tasks_are_separate_connections(self, engine):
        """Two scopes at once get two connections, and each one's
        `active_connection()` is its own."""
        seen: list[Any] = []
        both_in = asyncio.Barrier(2)

        async def scope():
            async with engine.begin() as conn:
                await both_in.wait()
                seen.append(rf.active_connection())
                assert rf.active_connection() is conn
                return conn.connection

        first, second = await asyncio.gather(scope(), scope())
        assert first is not second
        assert seen[0] is not seen[1]


class TestConcurrentTransactions:
    """postgres only — see the module docstring."""

    async def test_neither_scope_sees_the_others_uncommitted_write(self, pg_engine):
        inserted = asyncio.Barrier(2)

        async def writer(author_id: int, name: str, other: str):
            async with pg_engine.begin() as conn:
                await conn.execute(
                    sa.insert(Author.__table__).values(id=author_id, name=name, active=True)
                )
                await inserted.wait()
                names = {a.name for a in await conn.fetch_all(sa.select(Author))}
                assert name in names, "a scope cannot see its own write"
                assert other not in names, "a scope saw another transaction's uncommitted row"

        await asyncio.gather(
            writer(91, "ninetyone", "ninetytwo"), writer(92, "ninetytwo", "ninetyone")
        )
        after = {a.name for a in await pg_engine.fetch_all(sa.select(Author))}
        assert {"ninetyone", "ninetytwo"} <= after

    async def test_one_task_rolling_back_leaves_the_other_committed(self, pg_engine):
        wrote = asyncio.Barrier(2)

        async def commits():
            async with pg_engine.begin() as conn:
                await conn.execute(
                    sa.insert(Author.__table__).values(id=93, name="keeper", active=True)
                )
                await wrote.wait()

        async def rolls_back():
            with pytest.raises(RuntimeError):
                async with pg_engine.begin() as conn:
                    await conn.execute(
                        sa.insert(Author.__table__).values(id=94, name="goner", active=True)
                    )
                    await wrote.wait()
                    raise RuntimeError("out")

        await asyncio.gather(commits(), rolls_back())
        names = {a.name for a in await pg_engine.fetch_all(sa.select(Author))}
        assert "keeper" in names
        assert "goner" not in names


class TestEveryCheckoutComesBack:
    """A connection leaked once per request is a pool that dies at whatever depth
    it is configured to, minutes into a load test rather than in a test run.
    `test_cancellation.py` and `test_streaming.py` assert this for their own
    paths, one sequential call at a time; these are the concurrent forms.
    """

    async def test_after_reads_that_succeed(self, engine):
        await gather(lambda: engine.fetch_all(BY_ID))
        assert engine.sa_engine.pool.checkedout() == 0

    async def test_after_reads_that_fail_on_the_server(self, engine):
        """The failure has to happen *after* checkout to test anything, so it is a
        column the server rejects rather than a statement rowform refuses."""
        bad = sa.text("SELECT no_such_column FROM t_authors")
        results = await asyncio.gather(
            *(engine.execute(bad) for _ in range(12)), return_exceptions=True
        )
        assert all(isinstance(r, Exception) for r in results)
        assert engine.sa_engine.pool.checkedout() == 0
        assert [a.name for a in await engine.fetch_all(BY_ID)] == NAMES

    async def test_after_scopes_that_roll_back(self, engine):
        async def failing_scope():
            with pytest.raises(RuntimeError):
                async with engine.begin() as conn:
                    await conn.fetch_all(BY_ID)
                    raise RuntimeError("out")

        await gather(failing_scope, n=12)
        assert engine.sa_engine.pool.checkedout() == 0


class TestConcurrentStreams:
    async def test_interleaved_streams_each_read_their_own_result(self, streamable_engine):
        """`chunk=1` and a yield between chunks, so the three streams are
        genuinely interleaved rather than run one after another: each holds its
        own connection and its own server-side cursor for the whole walk.
        """

        async def walk():
            names = []
            async for author in streamable_engine.fetch_iter(BY_ID, chunk=1):
                names.append(author.name)
                await asyncio.sleep(0)
            return names

        assert await asyncio.gather(walk(), walk(), walk()) == [NAMES, NAMES, NAMES]
        assert streamable_engine.sa_engine.pool.checkedout() == 0

    async def test_streams_abandoned_mid_walk_return_their_connections(self, streamable_engine):
        """Three tasks that stop at the first row while the others keep going.
        Closed explicitly rather than left to GC: the point is that the checkout
        comes back when the generator closes, and waiting for a collection cycle
        to prove it would make the test flaky instead of strict.
        """

        async def first_only():
            stream = streamable_engine.fetch_iter(BY_ID, chunk=1)
            try:
                async for author in stream:
                    return author.name
                return None
            finally:
                await stream.aclose()

        assert await asyncio.gather(*(first_only() for _ in range(3))) == [NAMES[0]] * 3
        assert streamable_engine.sa_engine.pool.checkedout() == 0
