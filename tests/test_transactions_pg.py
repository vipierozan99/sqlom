"""Transaction semantics on both engines.

Replaces the hand-rolled `benchmarks/verify_transactions.py`: the same properties,
asserted by pytest instead of a script that printed PASS/FAIL and needed reading.
"""

import pytest

from rowform import DatabaseEngine, PsycopgEngine, Query, active_transaction
from tests.conftest import Author

pytestmark = pytest.mark.postgres

PROBE = "t_tx_probe"


class Boom(Exception):
    """Raised deliberately, to check rollback happens on any exception."""


@pytest.fixture(params=["asyncpg", "psycopg"])
async def engine(request, pg_schema):
    if request.param == "asyncpg":
        eng = DatabaseEngine(dsn=pg_schema, min_size=1, max_size=4)
        insert = f"INSERT INTO {PROBE} VALUES ($1, $2)"
    else:
        eng = PsycopgEngine(pg_schema, min_size=1, max_size=4)
        insert = f"INSERT INTO {PROBE} VALUES (%s, %s)"
    await eng.connect()
    eng._test_insert = insert
    async with eng.acquire() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {PROBE}")
        await conn.execute(f"CREATE TABLE {PROBE} (id int primary key, n int)")
    try:
        yield eng
    finally:
        async with eng.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {PROBE}")
        await eng.close()


async def count(engine):
    """Row count on a *fresh* pooled connection — what another request would see."""
    async with engine.acquire() as conn:
        if hasattr(conn, "fetchval"):
            return await conn.fetchval(f"SELECT count(*) FROM {PROBE}")
        cur = await conn.execute(f"SELECT count(*) FROM {PROBE}")
        return (await cur.fetchone())[0]


class TestAtomicity:
    async def test_commit_persists_every_statement(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(engine._test_insert, 1, 10)
            await tx.execute(engine._test_insert, 2, 20)
        assert await count(engine) == 2

    async def test_exception_rolls_the_whole_block_back(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(engine._test_insert, 1, 10)
        with pytest.raises(Boom):
            async with engine.transaction() as tx:
                await tx.execute(engine._test_insert, 2, 20)
                raise Boom
        assert await count(engine) == 1

    async def test_the_exception_is_not_swallowed(self, engine):
        with pytest.raises(Boom):
            async with engine.transaction():
                raise Boom


class TestVisibility:
    async def test_reads_its_own_uncommitted_writes(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(engine._test_insert, 1, 10)
            rows = await tx._fetch_rows(f"SELECT count(*) FROM {PROBE}", ())
            assert rows[0][0] == 1

    async def test_invisible_to_other_connections_until_commit(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(engine._test_insert, 1, 10)
            assert await count(engine) == 0
        assert await count(engine) == 1


class TestQueriesInsideTransactions:
    async def test_fetch_all_hydrates_models(self, engine):
        async with engine.transaction() as tx:
            rows = await tx.fetch_all(Query(Author).order_by("id").limit(2))
        assert [a.name for a in rows] == ["ada", "brian"]
        assert all(isinstance(a, Author) for a in rows)

    async def test_fetch_json_returns_bytes(self, engine):
        async with engine.transaction() as tx:
            payload = await tx.fetch_json(Query(Author).order_by("id").limit(2))
        assert isinstance(payload, bytes) and payload.startswith(b"[")

    async def test_joins_work_inside_a_transaction(self, engine):
        from tests.conftest import Book

        async with engine.transaction() as tx:
            rows = await tx.fetch_all(
                Query(Author, Book)
                .join(Book, Book.author_id == Author.id)
                .order_by(Book.id)
                .limit(1)
            )
        assert rows[0][0].name == "ada" and rows[0][1].title == "structures"

    async def test_the_engine_and_transaction_share_compiled_hydrators(self, engine):
        query = Query(Author).limit(1)
        await engine.fetch_all(query)
        before = engine._hydrator_for(query)
        async with engine.transaction() as tx:
            await tx.fetch_all(query)
        assert engine._hydrator_for(query) is before


class TestSavepoints:
    async def test_nested_block_reports_depth(self, engine):
        async with engine.transaction() as tx:
            assert tx.depth == 0
            async with tx.transaction() as sp:
                assert sp.depth == 1

    async def test_inner_failure_keeps_the_outer_work(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(engine._test_insert, 1, 10)
            with pytest.raises(Boom):
                async with tx.transaction() as sp:
                    await sp.execute(engine._test_insert, 2, 20)
                    raise Boom
        assert await count(engine) == 1

    async def test_inner_success_is_kept(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(engine._test_insert, 1, 10)
            async with tx.transaction() as sp:
                await sp.execute(engine._test_insert, 2, 20)
        assert await count(engine) == 2

    async def test_outer_failure_discards_a_committed_savepoint(self, engine):
        with pytest.raises(Boom):
            async with engine.transaction() as tx:
                async with tx.transaction() as sp:
                    await sp.execute(engine._test_insert, 1, 10)
                raise Boom
        assert await count(engine) == 0


class TestTheFootgunGuard:
    """engine.fetch_all() inside a transaction would take a *different* pooled
    connection, so it would miss uncommitted writes and not roll back with the
    block — a bug that returns plausible data."""

    async def test_engine_fetch_all_inside_a_transaction_raises(self, engine):
        async with engine.transaction():
            with pytest.raises(RuntimeError, match="inside engine.transaction"):
                await engine.fetch_all(Query(Author).limit(1))

    async def test_engine_fetch_json_inside_a_transaction_raises(self, engine):
        async with engine.transaction():
            with pytest.raises(RuntimeError, match="inside engine.transaction"):
                await engine.fetch_json(Query(Author).limit(1))

    async def test_the_engine_works_again_after_the_block(self, engine):
        async with engine.transaction():
            pass
        assert len(await engine.fetch_all(Query(Author).limit(2))) == 2

    async def test_guard_is_scoped_to_the_same_engine(self, engine, pg_schema):
        other = PsycopgEngine(pg_schema, min_size=1, max_size=2)
        await other.connect()
        try:
            async with engine.transaction():
                # A second engine is a separate pool; using it is legitimate.
                assert len(await other.fetch_all(Query(Author).limit(1))) == 1
        finally:
            await other.close()

    async def test_active_transaction_tracks_the_innermost_block(self, engine):
        assert active_transaction() is None
        async with engine.transaction() as tx:
            assert active_transaction() is tx
            async with tx.transaction() as sp:
                assert active_transaction() is sp
            assert active_transaction() is tx
        assert active_transaction() is None


class TestIsolation:
    async def test_serializable_is_accepted(self, engine):
        async with engine.transaction(isolation="serializable") as tx:
            await tx.execute(engine._test_insert, 1, 10)
        assert await count(engine) == 1

    async def test_repeatable_read_is_accepted(self, engine):
        async with engine.transaction(isolation="repeatable_read") as tx:
            await tx.fetch_all(Query(Author).limit(1))

    async def test_unknown_isolation_name_is_rejected(self, engine):
        with pytest.raises((ValueError, KeyError)):
            async with engine.transaction(isolation="nonsense"):
                pass

    async def test_readonly_transaction_refuses_a_write(self, engine):
        with pytest.raises(Exception) as excinfo:
            async with engine.transaction(readonly=True) as tx:
                await tx.execute(engine._test_insert, 1, 10)
        assert "read-only" in str(excinfo.value).lower()

    async def test_readonly_does_not_leak_to_the_next_borrower(self, engine):
        """asyncpg puts READ ONLY on the BEGIN, so it expires with the
        transaction. psycopg puts it on the *connection* and its pool does not
        restore it on release, so without an explicit restore a readonly block
        would hand back a permanently read-only connection and some later,
        unrelated write would fail. Pool of 1 would be ideal here; the assertion
        below instead retries enough times to hit the same connection."""
        async with engine.transaction(readonly=True) as tx:
            await tx.fetch_all(Query(Author).limit(1))

        for _ in range(8):
            async with engine.transaction() as tx:
                await tx.execute(f"DELETE FROM {PROBE} WHERE id = -1")
        assert await count(engine) == 0

    async def test_deferrable_is_accepted(self, engine):
        # Only meaningful for SERIALIZABLE READ ONLY, but both engines accept
        # the kwarg regardless — it is a no-op outside that combination.
        async with engine.transaction(
            isolation="serializable", readonly=True, deferrable=True
        ) as tx:
            await tx.fetch_all(Query(Author).limit(1))

    async def test_deferrable_does_not_leak_to_the_next_borrower(self, engine):
        # Same leak psycopg_pool has for readonly/isolation (see
        # test_readonly_does_not_leak_to_the_next_borrower): deferrable lives on
        # the connection, not the BEGIN, so it has to be restored on release too.
        async with engine.transaction(
            isolation="serializable", readonly=True, deferrable=True
        ) as tx:
            await tx.fetch_all(Query(Author).limit(1))

        for _ in range(8):
            async with engine.transaction() as tx:
                await tx.execute(f"DELETE FROM {PROBE} WHERE id = -1")
        assert await count(engine) == 0

    async def test_isolation_level_does_not_leak_either(self, engine):
        async with engine.transaction(isolation="serializable") as tx:
            await tx.fetch_all(Query(Author).limit(1))

        # A serializable level left on a pooled connection would change the
        # semantics of every later request that borrowed it, silently.
        for _ in range(8):
            async with engine.acquire() as conn:
                if hasattr(conn, "fetchval"):
                    level = await conn.fetchval("SHOW transaction_isolation")
                else:
                    cur = await conn.execute("SHOW transaction_isolation")
                    level = (await cur.fetchone())[0]
                assert level != "serializable"


class TestSessionResetInvariant:
    """The conditional reset assumes fetch_all cannot dirty a connection. A
    transaction can, so it must mark the connection dirty — otherwise a `SET`
    inside a block leaks to whoever borrows that connection next."""

    async def test_transaction_triggers_the_sql_reset(self, pg_schema):
        eng = DatabaseEngine(dsn=pg_schema, conditional_reset=True,
                             min_size=1, max_size=1)
        await eng.connect()
        try:
            before = eng.reset_count
            async with eng.transaction() as tx:
                await tx.execute("SET statement_timeout = '7s'")
            assert eng.reset_count > before
            # Pool of 1, so the next borrow is guaranteed to be that connection.
            async with eng.acquire() as conn:
                assert await conn.fetchval("SHOW statement_timeout") != "7s"
        finally:
            await eng.close()

    async def test_plain_reads_still_skip_the_reset(self, pg_schema):
        eng = DatabaseEngine(dsn=pg_schema, conditional_reset=True,
                             min_size=1, max_size=1)
        await eng.connect()
        try:
            before = eng.reset_count
            for _ in range(5):
                await eng.fetch_all(Query(Author).limit(1))
            assert eng.reset_count == before
        finally:
            await eng.close()
