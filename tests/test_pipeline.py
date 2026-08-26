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
from sqlalchemy.ext.asyncio import async_sessionmaker

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


class TestOnSomebodyElsesConnection:
    """A pipeline on a connection SQLAlchemy owns — `docs/PLAN_SQLA_API.md` §5.5,
    which named this as the likeliest place for the two to confuse each other and
    left it untested. Pipeline mode is set on the psycopg connection itself, and
    `bind=` means the transaction around it is SQLAlchemy's, so the question is
    whether pipelined statements land in *that* transaction and unwind with it.

    They do, on all four counts below. Nothing here needed a code change.
    """

    async def test_pipelined_writes_are_in_the_callers_transaction(self, psycopg_engine):
        sa_engine = psycopg_engine.sa_engine
        async with sa_engine.connect() as their, their.begin():
            async with psycopg_engine.connect(bind=their) as conn, conn.pipeline():
                await conn.execute(
                    sa.update(Author.__table__).where(Author.id == 1).values(name="piped")
                )
            # SQLAlchemy's own cursor, on the same connection, after the pipeline
            # synchronised: it must see the write as its own uncommitted state.
            seen = await their.scalar(sa.text("SELECT name FROM t_authors WHERE id = 1"))
            assert seen == "piped"
        assert await psycopg_engine.fetch_one(sa.select(Author.name).where(Author.id == 1)) == (
            "piped"
        )

    async def test_they_roll_back_with_the_callers_block(self, psycopg_engine):
        sa_engine = psycopg_engine.sa_engine
        async with sa_engine.connect() as their, their.begin():
            async with psycopg_engine.connect(bind=their) as conn, conn.pipeline():
                await conn.execute(sa.update(Author.__table__).values(name="clobbered"))
            await their.rollback()
        names = [a.name for a in await psycopg_engine.fetch_all(sa.select(Author))]
        assert "clobbered" not in names

    async def test_it_works_inside_an_async_session(self, psycopg_engine):
        """The adoption shape: an application's `AsyncSession`, with rowform
        pipelining a batch of updates inside the session's transaction."""
        session_factory = async_sessionmaker(psycopg_engine.sa_engine)
        async with (
            session_factory() as session,
            session.begin(),
            psycopg_engine.connect(bind=session) as conn,
            conn.pipeline(),
        ):
            for author_id in (1, 2, 3):
                await conn.execute(
                    sa.update(Author.__table__)
                    .where(Author.id == author_id)
                    .values(name=f"sess-{author_id}")
                )
        rows = await psycopg_engine.fetch_all(
            sa.select(Author).where(Author.id.in_([1, 2, 3])).order_by(Author.id)
        )
        assert [a.name for a in rows] == ["sess-1", "sess-2", "sess-3"]

    async def test_the_error_arrives_when_the_block_closes(self, psycopg_engine):
        """The documented consequence, asserted rather than described: the failing
        statement returns without raising, and the error comes at the synchronise.
        A caller who wraps the `await` instead of the block catches nothing."""
        sa_engine = psycopg_engine.sa_engine
        returned = False
        with pytest.raises(Exception) as caught:
            async with sa_engine.connect() as their, their.begin():
                async with psycopg_engine.connect(bind=their) as conn, conn.pipeline():
                    await conn.execute(
                        sa.insert(Author.__table__).values(id=1, name="dupe", active=True)
                    )
                    returned = True
        assert returned, "the statement raised at the call site, not at the synchronise"
        assert not isinstance(caught.value, rowform.RowformError)
