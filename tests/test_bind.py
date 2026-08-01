"""`db.connect(bind=...)` — rowform's reads on a connection somebody else owns.

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
from conftest import Author, Base, pg_url, sqlite_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

import rowform as rf


class Boom(Exception):
    pass


class OrmBase(DeclarativeBase):
    pass


class AuthorORM(OrmBase):
    """A stock ORM mapping over the *same* `Table` the rowform model declares —
    the shape of an application migrating one query at a time, and the only way
    to exercise `session.add()`, which rowform models do not support."""

    __table__ = Author.__table__


@pytest.fixture(params=["sqlite", "asyncpg", "psycopg"])
async def pair(request, tmp_path):
    """One `AsyncEngine`, wrapped — the shape a migrating application is in.

    All three drivers, because "the same transaction" is three different
    mechanisms underneath: psycopg's connection is transactional in its own
    right, sqlite gets an explicit BEGIN from `SqliteDriver.configure`, and
    asyncpg has none until `AsyncpgDriver.enter_transaction` puts it in one.
    Asserting the goal on sqlite alone is how the asyncpg case came to be written
    and stay broken — so naming psycopg here and then not running it was the same
    mistake one driver over.
    """
    if request.param == "sqlite":
        url = sqlite_url(str(tmp_path / "bind.sqlite3"))
    else:
        url = pg_url(request.getfixturevalue("pg_dsn"), request.param)
    sa_engine = create_async_engine(url)
    db = rf.Engine(sa_engine)
    await db.drop_all(Base.metadata)
    await db.create_all(Base.metadata)
    await db.execute(sa.insert(Author.__table__).values(id=1, name="ada", active=True))
    try:
        yield db, sa_engine
    finally:
        await sa_engine.dispose()


class TestOnAConnection:
    async def test_it_reads(self, pair):
        db, sa_engine = pair
        async with sa_engine.connect() as their, db.connect(bind=their) as conn:
            assert [a.name for a in await conn.fetch_all(sa.select(Author))] == ["ada"]

    async def test_it_sees_uncommitted_writes(self, pair):
        """SQLAlchemy writes, rowform reads, one transaction. Two connections
        could not do this, which is the whole point."""
        db, sa_engine = pair
        async with sa_engine.connect() as their, their.begin():
            await their.execute(sa.insert(Author.__table__).values(id=2, name="bo", active=True))
            async with db.connect(bind=their) as conn:
                rows = await conn.fetch_all(sa.select(Author))
            assert sorted(a.name for a in rows) == ["ada", "bo"]

    async def test_its_writes_roll_back_with_the_block(self, pair):
        db, sa_engine = pair
        with pytest.raises(Boom):
            async with sa_engine.connect() as their, their.begin():
                async with db.connect(bind=their) as conn:
                    await conn.execute(
                        sa.insert(Author.__table__).values(id=3, name="cy", active=True)
                    )
                    assert len(await conn.fetch_all(sa.select(Author))) == 2
                raise Boom
        assert [a.name for a in await db.fetch_all(sa.select(Author))] == ["ada"]

    async def test_the_compat_track_works_bound_too(self, pair):
        db, sa_engine = pair
        async with sa_engine.connect() as their, db.connect(bind=their) as conn:
            result = await conn.execute(sa.select(Author))
            assert [a.name for a in result.scalars().all()] == ["ada"]

    async def test_it_does_not_end_the_transaction_it_was_handed(self, pair):
        """Leaving the bound block must not commit or roll back: the caller's
        block is the scope, and ending it here would surprise them."""
        db, sa_engine = pair
        async with sa_engine.connect() as their, their.begin():
            async with db.connect(bind=their) as conn:
                await conn.execute(
                    sa.insert(Author.__table__).values(id=7, name="fi", active=True)
                )
            assert their.in_transaction() is True
            assert len(await db.fetch_all(sa.select(Author))) == 1, "committed early"
        assert len(await db.fetch_all(sa.select(Author))) == 2


class TestOnASession:
    async def test_it_joins_the_sessions_transaction(self, pair):
        db, sa_engine = pair
        Session = async_sessionmaker(sa_engine)
        async with Session() as session, session.begin():
            await session.execute(
                sa.insert(Author.__table__).values(id=4, name="di", active=True)
            )
            async with db.connect(bind=session) as conn:
                rows = await conn.fetch_all(sa.select(Author))
            assert sorted(a.name for a in rows) == ["ada", "di"]

    async def test_a_pending_add_is_not_visible_until_it_is_flushed(self, pair):
        """rowform reads the connection under the session, not the session.

        So nothing it does triggers autoflush, and a `session.add()` still
        pending in the identity map is not in the database for rowform to find.
        `Engine.connect` documents the flush as the caller's; this pins both
        halves so the documented rule cannot quietly stop being true.
        """
        db, sa_engine = pair
        Session = async_sessionmaker(sa_engine)
        async with Session() as session, session.begin():
            session.add(AuthorORM(id=6, name="fran", active=True))

            async with db.connect(bind=session) as conn:
                before = await conn.fetch_all(sa.select(Author))
            assert [a.name for a in before] == ["ada"]

            await session.flush()
            async with db.connect(bind=session) as conn:
                after = await conn.fetch_all(sa.select(Author))
            assert sorted(a.name for a in after) == ["ada", "fran"]

    async def test_a_session_rollback_takes_rowforms_reads_with_it(self, pair):
        db, sa_engine = pair
        Session = async_sessionmaker(sa_engine)
        async with Session() as session:
            await session.execute(
                sa.insert(Author.__table__).values(id=5, name="eve", active=True)
            )
            async with db.connect(bind=session) as conn:
                assert len(await conn.fetch_all(sa.select(Author))) == 2
            await session.rollback()
        assert [a.name for a in await db.fetch_all(sa.select(Author))] == ["ada"]


class TestItWillNotEndSomebodyElsesTransaction:
    """`connect(bind=...)` promises the caller's block is the scope. `_owns` kept
    rowform from *starting* a transaction there; these are the three methods that
    could have ended one."""

    @pytest.mark.parametrize("method", ["commit", "rollback", "close"])
    async def test_it_refuses(self, pair, method):
        db, sa_engine = pair
        async with sa_engine.connect() as their, db.connect(bind=their) as conn:
            with pytest.raises(rf.EngineStateError, match=f"conn.{method}"):
                await getattr(conn, method)()

    async def test_the_caller_is_still_usable_afterwards(self, pair):
        """The point of the refusal: before it, `conn.close()` closed the
        connection under the caller and their next statement raised."""
        db, sa_engine = pair
        Session = async_sessionmaker(sa_engine)
        async with Session() as session, session.begin():
            async with db.connect(bind=session) as conn:
                with pytest.raises(rf.EngineStateError):
                    await conn.close()
            await session.execute(
                sa.insert(Author.__table__).values(id=7, name="gil", active=True)
            )
        assert len(await db.fetch_all(sa.select(Author))) == 2


class TestItRefusesWhatItCannotDo:
    async def test_something_that_is_neither(self, pair):
        db, _ = pair
        with pytest.raises(rf.ConfigurationError, match="AsyncConnection or an AsyncSession"):
            async with db.connect(bind=object()):
                pass

    async def test_a_connection_from_another_driver(self, pg_dsn, pair, tmp_path):
        """The compiled SQL carries one driver's paramstyle. Running it on
        another's would bind wrongly rather than fail cleanly, so it is refused
        by name. Needs a real postgres, because the check is on a live
        connection's dialect — it skips with the rest of the postgres suite."""
        db, _ = pair
        other_url = (
            pg_url(pg_dsn)
            if db.dialect.name == "sqlite"
            else sqlite_url(str(tmp_path / "other.sqlite3"))
        )
        other = create_async_engine(other_url)
        try:
            async with other.connect() as their:
                with pytest.raises(rf.ConfigurationError, match="wrong paramstyle"):
                    async with db.connect(bind=their):
                        pass
        finally:
            await other.dispose()

    async def test_it_does_not_claim_the_active_connection(self, pair):
        """A bound scope is one the caller owns and ends, so registering it would
        make the engine's one-shots refuse for the rest of the task."""
        db, sa_engine = pair
        async with sa_engine.connect() as their, db.connect(bind=their):
            assert rf.active_connection() is None
            assert await db.fetch_all(sa.select(Author))
