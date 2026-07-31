"""`conn.pipeline()`: statements go out without waiting for each result.

Worth having only where the round trip is the cost, and the measurement says so
plainly. Over 200 updates: on loopback, pipelining is *slower* than issuing them
one at a time (56 ms against 44 ms — with no latency to hide, the batching is
pure overhead), and through a proxy adding 1 ms each way it is 13.5x faster
(42 ms against 564 ms).

So these tests assert correctness, not speed: the same rows must end up in the
same state, errors must still arrive, and the engines without a pipeline must say
so rather than accept the block and do nothing.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import Author, engine_at, pg_url

import rowform


@pytest.fixture
async def psycopg_engine(pg_dsn):
    from conftest import seed

    async with engine_at(pg_url(pg_dsn, "psycopg")) as db:
        await seed(db)
        yield db


class TestItWorks:
    async def test_every_statement_takes_effect(self, psycopg_engine):
        update = sa.update(Author.__table__).where(Author.id == sa.bindparam("i")).values(
            name=sa.bindparam("n")
        )
        async with psycopg_engine.begin() as conn, conn.pipeline():
            for i in (1, 2, 3):
                await conn.execute(update, i=i, n=f"piped-{i}")

        rows = await psycopg_engine.fetch_all(
            sa.select(Author).where(Author.id.in_([1, 2, 3])).order_by(Author.id)
        )
        assert [a.name for a in rows] == ["piped-1", "piped-2", "piped-3"]

    async def test_it_rolls_back_with_its_transaction(self, psycopg_engine):
        before = [
            a.name
            for a in await psycopg_engine.fetch_all(sa.select(Author).order_by(Author.id))
        ]

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with psycopg_engine.begin() as conn, conn.pipeline():
                await conn.execute(
                    sa.update(Author.__table__).values(name="clobbered")
                )
                raise Boom

        after = [
            a.name
            for a in await psycopg_engine.fetch_all(sa.select(Author).order_by(Author.id))
        ]
        assert after == before

    async def test_a_failing_statement_still_raises(self, psycopg_engine):
        """The error surfaces when the pipeline synchronises rather than at the
        statement — but it does surface."""
        with pytest.raises(Exception) as caught:
            async with psycopg_engine.begin() as conn, conn.pipeline():
                await conn.execute(
                    sa.insert(Author.__table__).values(id=1, name="dupe", active=True)
                )
        assert not isinstance(caught.value, rowform.RowformError)  # the driver's own

    async def test_reads_still_work_inside_one(self, psycopg_engine):
        async with psycopg_engine.begin() as conn, conn.pipeline():
            rows = await conn.fetch_all(sa.select(Author).order_by(Author.id))
        assert rows


class TestWhereThereIsNone:
    async def test_sqlite_refuses(self, sqlite_engine):
        async with sqlite_engine.begin() as conn:
            with pytest.raises(rowform.UnsupportedError, match="no pipeline mode"):
                conn.pipeline()

    async def test_asyncpg_refuses(self, pg_engine):
        async with pg_engine.begin() as conn:
            with pytest.raises(rowform.UnsupportedError, match="no pipeline mode"):
                conn.pipeline()
