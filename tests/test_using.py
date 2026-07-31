"""`Engine.using()` — rowform's reads on a connection somebody else owns.

This is the payoff for giving up rowform's own pool (`docs/PLAN_SQLA_API.md`), and
the thing an own pool structurally could not do: an application keeps its
`AsyncEngine`, its `AsyncSession` and its migrations, and adopts rowform one query
at a time. If these pass, `CLAUDE.md`'s goal 2 holds; if they fail, the checkout
cost bought nothing.

The claim under test is not "it runs" but "it is *the same transaction*": it sees
uncommitted writes, and it is rolled back with them.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import Author, sqlite_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rowform as rf


@pytest.fixture
async def pair(sqlite_path, tmp_path):
    """One `AsyncEngine`, wrapped — the shape a migrating application is in."""
    sa_engine = create_async_engine(sqlite_url(str(tmp_path / "using.sqlite3")))
    db = rf.Engine(sa_engine)
    from conftest import Base

    await db.create_all(Base.metadata)
    await db.execute(sa.insert(Author.__table__).values(id=1, name="ada", active=True))
    try:
        yield db, sa_engine
    finally:
        await sa_engine.dispose()


class TestOnAConnection:
    async def test_it_reads(self, pair):
        db, sa_engine = pair
        async with sa_engine.connect() as conn:
            rows = await (await db.using(conn)).fetch_all(sa.select(Author))
        assert [a.name for a in rows] == ["ada"]

    async def test_it_sees_uncommitted_writes(self, pair):
        """SQLAlchemy writes, rowform reads, one transaction. Two connections
        could not do this, which is the whole point."""
        db, sa_engine = pair
        async with sa_engine.connect() as conn, conn.begin():
            await conn.execute(sa.insert(Author.__table__).values(id=2, name="bo", active=True))
            rows = await (await db.using(conn)).fetch_all(sa.select(Author))
            assert sorted(a.name for a in rows) == ["ada", "bo"]

    async def test_its_writes_roll_back_with_the_block(self, pair):
        db, sa_engine = pair

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with sa_engine.connect() as conn, conn.begin():
                tx = await db.using(conn)
                await tx.execute(
                    sa.insert(Author.__table__).values(id=3, name="cy", active=True)
                )
                assert len(await tx.fetch_all(sa.select(Author))) == 2
                raise Boom
        assert [a.name for a in await db.fetch_all(sa.select(Author))] == ["ada"]


class TestOnASession:
    async def test_it_joins_the_sessions_transaction(self, pair):
        db, sa_engine = pair
        Session = async_sessionmaker(sa_engine)
        async with Session() as session, session.begin():
            await session.execute(
                sa.insert(Author.__table__).values(id=4, name="di", active=True)
            )
            rows = await (await db.using(session)).fetch_all(sa.select(Author))
            assert sorted(a.name for a in rows) == ["ada", "di"]

    async def test_a_session_rollback_takes_rowforms_reads_with_it(self, pair):
        db, sa_engine = pair
        Session = async_sessionmaker(sa_engine)
        async with Session() as session:
            await session.execute(
                sa.insert(Author.__table__).values(id=5, name="eve", active=True)
            )
            assert len(await (await db.using(session)).fetch_all(sa.select(Author))) == 2
            await session.rollback()
        assert [a.name for a in await db.fetch_all(sa.select(Author))] == ["ada"]


class TestItRefusesWhatItCannotDo:
    async def test_something_that_is_neither(self, pair):
        db, _ = pair
        with pytest.raises(rf.ConfigurationError, match="AsyncConnection or an AsyncSession"):
            await db.using(object())

    async def test_a_connection_from_another_driver(self, pg_dsn, pair):
        """The compiled SQL carries one driver's paramstyle. Running it on
        another's would bind wrongly rather than fail cleanly, so it is refused
        by name. Needs a real postgres, because the check is on a live
        connection's dialect — it skips with the rest of the postgres suite."""
        from conftest import pg_url

        db, _ = pair
        other = create_async_engine(pg_url(pg_dsn))
        try:
            async with other.connect() as conn:
                with pytest.raises(rf.ConfigurationError, match="asyncpg"):
                    await db.using(conn)
        finally:
            await other.dispose()

    async def test_it_does_not_claim_the_active_transaction(self, pair):
        """`using()` hands back a block the caller owns and ends, so registering
        it would make `db.fetch_all()` refuse for the rest of the task."""
        db, sa_engine = pair
        async with sa_engine.connect() as conn:
            await db.using(conn)
            assert rf.active_transaction() is None
            assert await db.fetch_all(sa.select(Author))
