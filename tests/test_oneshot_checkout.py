"""The one-shot reads check out from the pool directly, and a dead connection is
retired rather than handed to the next caller.

`Engine.fetch_all` needs a pooled connection and nothing else, and it used to
take one through `sa_engine.connect()`: a `Connection`, an `AsyncConnection`,
three greenlet crossings and the `engine_connect` event per read. On a one-row
read that costs as much again as the pool checkout itself (`engine.py`,
`_direct_connection`). `Pool.connect()` keeps the pool and skips the rest.

What it skips is the gate: anything that listens on `engine_connect` —
`execution_options(isolation_level=...)` above all — turns the direct checkout
off, per call, so a caller's engine configuration is never silently bypassed.

The third thing here is older than the optimisation and was found while
building it: rowform runs statements on the driver connection, so SQLAlchemy
never sees a driver exception and never runs `is_disconnect`. A dead connection
went back into the pool. Both checkouts now ask the dialect, and invalidate.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import AUTHORS, Author, engine_at, pg_url, seed, sqlite_url
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import rowform as rf

NAMES = sorted(a["name"] for a in AUTHORS)
BY_NAME = sa.select(Author).order_by(Author.name)


@pytest.fixture(params=["sqlite", "asyncpg", "psycopg"])
def url(request):
    """One URL per driver, for tests that build their engine by hand."""
    if request.param == "sqlite":
        return sqlite_url(request.getfixturevalue("sqlite_path"))
    return pg_url(request.getfixturevalue("pg_dsn"), request.param)


@pytest.fixture
def sa_calls(monkeypatch):
    """Every `AsyncEngine.connect()` a read went through. The ordinary checkout
    always makes one (`AsyncEngine.begin()` opens with it too); the direct one
    never does."""
    calls: list[str] = []
    original = AsyncEngine.connect

    def record(self, *args, **kwargs):
        calls.append("connect")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "connect", record)
    return calls


class TestTheDirectCheckout:
    async def test_a_one_shot_select_builds_no_sqlalchemy_connection(self, engine, sa_calls):
        rows = await engine.fetch_all(BY_NAME)
        one = await engine.fetch_one(BY_NAME)
        assert [r.name for r in rows] == NAMES
        assert one is not None and one.name == NAMES[0]
        assert sa_calls == []

    async def test_everything_else_still_takes_the_ordinary_one(self, engine, sa_calls):
        # A stream: psycopg's server-side cursor needs the transaction.
        assert [r.name async for r in engine.fetch_iter(BY_NAME)] == NAMES
        assert sa_calls == ["connect"]
        # A write with RETURNING: it has to commit.
        del sa_calls[:]
        inserted = await engine.fetch_all(
            sa.insert(Author).values(id=99, name="zed", active=True).returning(Author)
        )
        assert [r.name for r in inserted] == ["zed"]
        assert sa_calls == ["connect"]
        # The compatibility track is SQLAlchemy's to the letter, scope included.
        del sa_calls[:]
        assert (await engine.execute(BY_NAME)).scalars().first().name == NAMES[0]
        assert sa_calls == ["connect"]

    async def test_the_pool_still_sees_the_checkout(self, engine):
        """`pool_pre_ping`, `pool_recycle` and the pool events all live behind
        `checkout`; the direct path keeps that half and skips only the engine's."""
        checkouts: list[object] = []
        event.listen(engine.sa_engine.sync_engine.pool, "checkout", lambda *a: checkouts.append(a))
        await engine.fetch_all(BY_NAME)
        assert len(checkouts) == 1

    async def test_on_sqlite_the_select_is_the_only_driver_round_trip(
        self, sqlite_engine, monkeypatch
    ):
        """The wire-level form of the claim, in the style of
        `test_transactions.py::TestSqliteBeginCost`: the statement and its
        `fetchall` are the only hops to the worker thread before the connection
        goes back — no `BEGIN`, no `cursor()`/`close()` dance. The one hop after
        is SQLAlchemy's reset-on-return (`pool_reset_on_return`), which is the
        caller's engine setting and not rowform's to skip."""
        import aiosqlite

        hops: list[str] = []
        original = aiosqlite.Connection._execute

        async def record(self, fn, *args, **kwargs):
            sql = next((a for a in args if isinstance(a, str)), "")
            hops.append(f"{getattr(fn, '__name__', '?')} {sql}".strip())
            return await original(self, fn, *args, **kwargs)

        monkeypatch.setattr(aiosqlite.Connection, "_execute", record)
        await sqlite_engine.fetch_all(BY_NAME)
        assert hops[0].startswith("execute SELECT") and hops[1] == "fetchall", hops
        assert hops[2:] in ([], ["rollback"]), hops


class TestTheGate:
    async def test_engine_execution_options_turn_it_off(self, url, sa_calls):
        """`execution_options(isolation_level=...)` is an `engine_connect`
        listener; a read that skipped it would run at the wrong level."""
        base = create_async_engine(url)
        try:
            db = rf.Engine(base.execution_options(isolation_level="SERIALIZABLE"))
            await seed(db)
            del sa_calls[:]
            assert [r.name for r in await db.fetch_all(BY_NAME)] == NAMES
            assert sa_calls == ["connect"]
        finally:
            await base.dispose()

    async def test_a_listener_registered_after_wrapping_turns_it_off(self, url, sa_calls):
        """The gate is read per call, not at `rf.Engine()`: a caller's own
        `engine_connect` listener runs for one-shots too."""
        async with engine_at(url) as db:
            await seed(db)
            seen: list[object] = []
            event.listen(db.sa_engine.sync_engine, "engine_connect", seen.append)
            del sa_calls[:]
            await db.fetch_all(BY_NAME)
            assert sa_calls == ["connect"] and len(seen) == 1


class TestPsycopgOneShotsSendNoTransaction:
    """psycopg's connection is transactional by default, so before
    `PsycopgDriver.autocommit` a "no transaction" one-shot was `BEGIN`, `SELECT`,
    then the pool's `ROLLBACK` — the same three round trips as a scope. The
    server's own transaction status is the witness: IDLE means no `BEGIN` went
    out, and psycopg's reset skips the `ROLLBACK` when it finds IDLE."""

    @pytest.fixture
    def recording(self):
        from psycopg.pq import TransactionStatus

        class Recording(rf.drivers.PsycopgDriver):
            seen: list[tuple[bool, TransactionStatus]] = []

            async def fetch(self, conn, sql, params, describe):
                out = await super().fetch(conn, sql, params, describe)
                self.seen.append((conn.autocommit, conn.info.transaction_status))
                return out

        return Recording, TransactionStatus

    async def test_the_select_runs_in_autocommit_and_leaves_the_connection_idle(
        self, pg_dsn, recording
    ):
        Recording, TransactionStatus = recording
        async with engine_at(pg_url(pg_dsn, "psycopg"), pool_size=1, max_overflow=0) as db:
            await seed(db)
            db.driver = Recording(db.dialect)
            await db.fetch_all(BY_NAME)
            assert Recording.seen == [(True, TransactionStatus.IDLE)]
            # Restored before the connection went back: the next borrower gets
            # psycopg's default, and a scoped read is a real transaction again.
            async with db.acquire() as conn:
                assert conn.autocommit is False
            async with db.begin() as conn:
                await conn.fetch_all(BY_NAME)
            assert Recording.seen[-1] == (False, TransactionStatus.INTRANS)

    async def test_an_engine_already_in_autocommit_is_left_there(self, pg_dsn, recording):
        Recording, TransactionStatus = recording
        url = pg_url(pg_dsn, "psycopg")
        async with engine_at(url, isolation_level="AUTOCOMMIT", pool_size=1, max_overflow=0) as db:
            await seed(db)
            db.driver = Recording(db.dialect)
            await db.fetch_all(BY_NAME)
            assert Recording.seen[-1] == (True, TransactionStatus.IDLE)
            async with db.acquire() as conn:
                assert conn.autocommit is True


async def _kill(db: rf.Engine, driver_conn) -> None:
    """Close the connection underneath rowform and the pool, the way a server
    restart or a killed backend does, while it sits idle in the pool."""
    if db.dialect.name == "sqlite":
        # The sqlite3 connection belongs to aiosqlite's worker thread, so it is
        # closed there; the aiosqlite wrapper stays "open" and the next statement
        # raises pysqlite's `Cannot operate on a closed database`.
        await driver_conn._execute(driver_conn._connection.close)
    else:
        await driver_conn.close()


async def _read(db: rf.Engine, path: str) -> list[str]:
    if path == "fetch_all":
        return [r.name for r in await db.fetch_all(BY_NAME)]
    if path == "fetch_iter":
        return [r.name async for r in db.fetch_iter(BY_NAME)]
    async with db.begin() as conn:
        return [r.name for r in await conn.fetch_all(BY_NAME)]


class TestADeadConnectionIsRetired:
    """A pool of one, so the second read can only succeed if the first one's
    connection was invalidated rather than returned.

    On sqlite the pool's own reset also fails on a closed database and
    SQLAlchemy invalidates for that reason, so the sqlite leg passes with or
    without rowform's check; the postgres legs are the ones that need it —
    asyncpg's adapter resets with a local no-op and would hand the dead
    connection straight back.
    """

    @pytest.mark.parametrize("path", ["fetch_all", "fetch_iter", "begin"])
    async def test_the_next_read_gets_a_live_connection(self, url, path):
        async with engine_at(url, pool_size=1, max_overflow=0) as db:
            await seed(db)
            async with db.acquire() as victim:
                pass
            await _kill(db, victim)
            invalidated: list[object] = []
            event.listen(db.sa_engine.sync_engine.pool, "invalidate", lambda *a: invalidated.append(a))

            with pytest.raises(Exception):  # noqa: B017 -- the driver's own error, deliberately
                await _read(db, path)
            assert invalidated, "the dead connection went back into the pool"
            assert await _read(db, path) == NAMES
