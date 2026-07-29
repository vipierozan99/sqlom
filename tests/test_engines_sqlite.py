"""Engine behaviour against sqlite, mirroring test_engines_pg.py's shape.

Kept as its own file rather than folded into the pg suite: sqlite lacks
`FOR UPDATE`/`FOR SHARE`, its RIGHT/FULL join support and locking semantics
differ, and `TestNewQueryFeaturesOnPostgres` in the pg file documents exactly
why those stay Postgres-only. What's shared (Author/Book/Tag models, the
lifecycle/fetch_all/fetch_json/joins/acquire shape) is ported here instead of
duplicated from scratch.
"""

import asyncio

import pytest

from rowform import Query, SqliteEngine
from tests.conftest import Author, Book, Tag


@pytest.fixture
async def engine(sqlite_path):
    eng = SqliteEngine(sqlite_path, min_size=1, max_size=4)
    await eng.connect()
    try:
        yield eng
    finally:
        await eng.close()


class TestLifecycle:
    async def test_connect_is_idempotent(self, engine):
        # A second connect() that made a new pool would leak the first one.
        assert await engine.connect() is await engine.connect()

    async def test_close_clears_the_pool_and_is_repeatable(self, sqlite_path):
        eng = SqliteEngine(sqlite_path, min_size=1, max_size=2)
        await eng.connect()
        await eng.close()
        await eng.close()
        assert eng.pool is None

    async def test_use_before_connect_raises(self, sqlite_path):
        eng = SqliteEngine(sqlite_path)
        with pytest.raises(RuntimeError, match="not connected"):
            await eng.fetch_all(Query(Author).limit(1))

    async def test_use_after_close_raises(self, sqlite_path):
        eng = SqliteEngine(sqlite_path, min_size=1, max_size=2)
        await eng.connect()
        await eng.close()
        with pytest.raises(RuntimeError, match="not connected"):
            await eng.fetch_all(Query(Author).limit(1))

    def test_conditional_reset_kwarg_raises(self, sqlite_path):
        # No sqlite equivalent of asyncpg's pool-reset concept — reject the
        # kwarg outright rather than silently no-op it.
        with pytest.raises(NotImplementedError, match="conditional_reset"):
            SqliteEngine(sqlite_path, conditional_reset=True)

    def test_unknown_kwarg_raises(self, sqlite_path):
        with pytest.raises(TypeError, match="unexpected keyword"):
            SqliteEngine(sqlite_path, bogus=1)


class TestFetchAll:
    async def test_returns_hydrated_instances(self, engine):
        rows = await engine.fetch_all(Query(Author).order_by("id"))
        assert [a.name for a in rows] == ["ada", "brian", "carol", "dan"]
        assert all(isinstance(a, Author) for a in rows)

    async def test_booleans_are_real_bools(self, engine):
        # sqlite has no boolean type — stored as 0/1 INTEGER, coerced by
        # SQLITE_CONVERTERS.
        rows = await engine.fetch_all(Query(Author).order_by("id"))
        assert rows[0].active is True and rows[2].active is False

    async def test_where_and_limit(self, engine):
        rows = await engine.fetch_all(
            Query(Author).where(Author.active == True).order_by("id").limit(2)
        )
        assert [a.name for a in rows] == ["ada", "brian"]

    async def test_is_null_predicate(self, engine):
        rows = await engine.fetch_all(Query(Author).where(Author.name == None))
        assert rows == []

    async def test_empty_result(self, engine):
        rows = await engine.fetch_all(Query(Author).where(Author.id > 10_000))
        assert rows == []

    async def test_limit_zero(self, engine):
        assert await engine.fetch_all(Query(Author).limit(0)) == []

    async def test_the_hydrator_is_compiled_once_per_shape(self, engine):
        query = Query(Author).limit(1)
        await engine.fetch_all(query)
        first = engine._hydrator_for(query)
        await engine.fetch_all(query)
        assert engine._hydrator_for(query) is first


class TestFetchJson:
    async def test_returns_bytes(self, engine):
        payload = await engine.fetch_json(Query(Author).order_by("id").limit(2))
        assert isinstance(payload, bytes)
        assert payload.startswith(b"[")

    async def test_content_matches_fetch_all(self, engine):
        import orjson

        from rowform import compile_json_default

        query = Query(Author).order_by("id")
        objects = await engine.fetch_all(query)
        expected = orjson.loads(orjson.dumps(objects, default=compile_json_default(Author)))
        assert orjson.loads(await engine.fetch_json(query)) == expected

    async def test_empty_result_is_an_empty_array(self, engine):
        payload = await engine.fetch_json(Query(Author).where(Author.id > 10_000))
        assert payload.strip() in (b"[]", b"null")


class TestJoins:
    async def test_inner_join_two_models(self, engine):
        rows = await engine.fetch_all(
            Query(Author, Book).join(Book, Book.author_id == Author.id).order_by(Book.id)
        )
        assert [(a.name, b.title) for a, b in rows] == [
            ("ada", "structures"),
            ("ada", "algorithms"),
            ("brian", "compilers"),
            ("carol", "typography"),
        ]

    async def test_outer_join_yields_none_for_no_match(self, engine):
        rows = await engine.fetch_all(
            Query(Author, Book)
            .outer_join(Book, Book.author_id == Author.id)
            .where(Author.name == "dan")
        )
        assert len(rows) == 1
        author, book = rows[0]
        assert author.name == "dan" and book is None

    async def test_three_way_join(self, engine):
        rows = await engine.fetch_all(
            Query(Author, Book, Tag)
            .join(Book, Book.author_id == Author.id)
            .join(Tag, Tag.book_id == Book.id)
            .order_by(Tag.id)
        )
        assert [(a.name, t.label) for a, _, t in rows] == [
            ("ada", "classic"), ("brian", "classic")
        ]

    async def test_join_and_plain_query_get_different_hydrators(self, engine):
        plain = Query(Author).limit(1)
        joined = Query(Author, Book).join(Book, Book.author_id == Author.id).limit(1)
        await engine.fetch_all(plain)
        await engine.fetch_all(joined)
        assert engine._hydrator_for(plain) is not engine._hydrator_for(joined)


class TestAcquire:
    async def test_yields_a_usable_raw_connection(self, engine):
        async with engine.acquire() as conn:
            cur = await conn.execute("SELECT 1")
            assert (await cur.fetchone())[0] == 1


class TestTransaction:
    async def test_commits_on_clean_exit(self, sqlite_path):
        eng = SqliteEngine(sqlite_path, min_size=1, max_size=1)
        await eng.connect()
        try:
            async with eng.transaction() as tx:
                await tx.execute("INSERT INTO t_authors VALUES (999, 'temp', 1)")
            rows = await eng.fetch_all(Query(Author).where(Author.id == 999))
            assert len(rows) == 1
        finally:
            async with eng.acquire() as conn:
                await conn.execute("DELETE FROM t_authors WHERE id = 999")
            await eng.close()

    async def test_rolls_back_on_exception(self, engine):
        with pytest.raises(ValueError):
            async with engine.transaction() as tx:
                await tx.execute("INSERT INTO t_authors VALUES (998, 'temp', 1)")
                raise ValueError("boom")
        rows = await engine.fetch_all(Query(Author).where(Author.id == 998))
        assert rows == []

    async def test_nested_transaction_is_a_savepoint_and_rolls_back_alone(self, engine):
        async with engine.transaction() as tx:
            await tx.execute("INSERT INTO t_authors VALUES (997, 'outer', 1)")
            with pytest.raises(ValueError):
                async with tx.transaction() as nested:
                    await nested.execute("INSERT INTO t_authors VALUES (996, 'inner', 1)")
                    raise ValueError("boom")
            rows = await tx.fetch_all(Query(Author).where(Author.id.in_([996, 997])))
            assert [a.id for a in rows] == [997]
        try:
            rows = await engine.fetch_all(Query(Author).where(Author.id == 997))
            assert len(rows) == 1
        finally:
            async with engine.acquire() as conn:
                await conn.execute("DELETE FROM t_authors WHERE id IN (996, 997)")

    async def test_isolation_kwarg_raises(self, engine):
        with pytest.raises(NotImplementedError, match="isolation"):
            async with engine.transaction(isolation="serializable"):
                pass

    async def test_readonly_kwarg_raises(self, engine):
        with pytest.raises(NotImplementedError, match="isolation"):
            async with engine.transaction(readonly=True):
                pass


class TestWalAndConcurrency:
    async def test_pool_connections_are_in_wal_mode(self, engine):
        for conn in engine._require_pool()._all:
            cur = await conn.execute("PRAGMA journal_mode")
            mode = (await cur.fetchone())[0]
            assert mode.lower() == "wal"

    async def test_pool_size_matches_max_size(self, sqlite_path):
        eng = SqliteEngine(sqlite_path, min_size=1, max_size=3)
        await eng.connect()
        try:
            assert len(eng._require_pool()._all) == 3
        finally:
            await eng.close()

    async def test_concurrent_reads_do_not_serialize_through_one_connection(self, sqlite_path):
        eng = SqliteEngine(sqlite_path, min_size=1, max_size=4)
        await eng.connect()
        try:
            results = await asyncio.gather(
                *[eng.fetch_all(Query(Author).order_by("id")) for _ in range(8)]
            )
            assert all(len(r) == 4 for r in results)
        finally:
            await eng.close()
