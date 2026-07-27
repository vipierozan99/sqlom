"""Engine behaviour against real PostgreSQL, on both drivers.

Parameterised over both engines so a feature cannot be quietly asyncpg-only —
`PsycopgEngine` was added later and started out missing `acquire()` entirely.
"""

import pytest

from sqlom import DatabaseEngine, PsycopgEngine, Query
from tests.conftest import Author, Book, Tag

pytestmark = pytest.mark.postgres


@pytest.fixture(params=["asyncpg", "psycopg"])
async def engine(request, pg_schema):
    if request.param == "asyncpg":
        eng = DatabaseEngine(dsn=pg_schema, min_size=1, max_size=4)
    else:
        eng = PsycopgEngine(pg_schema, min_size=1, max_size=4)
    await eng.connect()
    try:
        yield eng
    finally:
        await eng.close()


class TestLifecycle:
    async def test_connect_is_idempotent(self, engine):
        # A second connect() that made a new pool would leak the first one.
        assert await engine.connect() is await engine.connect()

    async def test_close_clears_the_pool_and_is_repeatable(self, pg_schema):
        eng = PsycopgEngine(pg_schema, min_size=1, max_size=2)
        await eng.connect()
        await eng.close()
        await eng.close()
        assert eng.pool is None

    async def test_use_before_connect_raises(self, pg_schema):
        eng = DatabaseEngine(dsn=pg_schema)
        with pytest.raises(RuntimeError, match="not connected"):
            await eng.fetch_all(Query(Author).limit(1))

    async def test_use_after_close_raises(self, pg_schema):
        eng = PsycopgEngine(pg_schema, min_size=1, max_size=2)
        await eng.connect()
        await eng.close()
        with pytest.raises(RuntimeError, match="not connected"):
            await eng.fetch_all(Query(Author).limit(1))


class TestFetchAll:
    async def test_returns_hydrated_instances(self, engine):
        rows = await engine.fetch_all(Query(Author).order_by("id"))
        assert [a.name for a in rows] == ["ada", "brian", "carol", "dan"]
        assert all(isinstance(a, Author) for a in rows)

    async def test_booleans_are_real_bools(self, engine):
        rows = await engine.fetch_all(Query(Author).order_by("id"))
        assert rows[0].active is True and rows[2].active is False

    async def test_where_and_limit(self, engine):
        rows = await engine.fetch_all(
            Query(Author).where(Author.active == True).order_by("id").limit(2)  # noqa: E712
        )
        assert [a.name for a in rows] == ["ada", "brian"]

    async def test_is_null_predicate(self, engine):
        rows = await engine.fetch_all(Query(Author).where(Author.name == None))  # noqa: E711
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
    async def test_returns_bytes_not_a_parsed_object(self, engine):
        # psycopg decodes json natively, so without a ::text cast this would come
        # back as a Python list and silently defeat the whole point.
        payload = await engine.fetch_json(Query(Author).order_by("id").limit(2))
        assert isinstance(payload, bytes)
        assert payload.startswith(b"[")

    async def test_content_matches_fetch_all(self, engine):
        import orjson

        from sqlom import compile_json_default

        query = Query(Author).order_by("id")
        objects = await engine.fetch_all(query)
        expected = orjson.loads(orjson.dumps(objects, default=compile_json_default(Author)))
        assert orjson.loads(await engine.fetch_json(query)) == expected

    async def test_empty_result_is_an_empty_array(self, engine):
        payload = await engine.fetch_json(Query(Author).where(Author.id > 10_000))
        assert payload.strip() in (b"[]", b"null") or payload == b"[]"


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

    async def test_model_plus_column(self, engine):
        rows = await engine.fetch_all(
            Query(Author, Book.title)
            .join(Book, Book.author_id == Author.id)
            .order_by(Book.id)
            .limit(1)
        )
        assert rows[0][0].name == "ada" and rows[0][1] == "structures"

    async def test_filtering_join_returns_instances(self, engine):
        rows = await engine.fetch_all(
            Query(Author)
            .join(Book, Book.author_id == Author.id)
            .where(Book.title == "typography")
        )
        assert len(rows) == 1 and isinstance(rows[0], Author)

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
            # asyncpg and psycopg differ here on purpose — this is the escape
            # hatch, so it hands back the driver's own connection.
            if hasattr(conn, "fetchval"):
                assert await conn.fetchval("SELECT 1") == 1
            else:
                cur = await conn.execute("SELECT 1")
                assert (await cur.fetchone())[0] == 1


class TestNewQueryFeaturesOnPostgres:
    """The features added after the first join release, exercised against a real
    server — sqlite is permissive about some of this and Postgres is not."""

    async def test_self_join_via_alias(self, engine):
        from sqlom import Alias

        other = Alias(Book, "other")
        rows = await engine.fetch_all(
            Query(Book, other)
            .join(other, Book.author_id == other.author_id)
            .where(Book.id < other.id)
            .order_by(Book.id)
        )
        assert [(a.title, b.title) for a, b in rows] == [("structures", "algorithms")]

    async def test_right_join_nulls_the_primary_entity(self, engine):
        async with engine.acquire() as conn:
            await conn.execute("INSERT INTO t_books VALUES (99, 999, 'orphan')")
        try:
            rows = await engine.fetch_all(
                Query(Author, Book)
                .right_join(Book, Book.author_id == Author.id)
                .where(Book.title == "orphan")
            )
            assert len(rows) == 1 and rows[0][0] is None
            assert rows[0][1].title == "orphan"
        finally:
            async with engine.acquire() as conn:
                await conn.execute("DELETE FROM t_books WHERE id = 99")

    async def test_full_join(self, engine):
        rows = await engine.fetch_all(
            Query(Author, Book).full_join(Book, Book.author_id == Author.id)
        )
        assert any(book is None for _, book in rows)

    async def test_or_and_not(self, engine):
        from sqlom import and_, or_

        rows = await engine.fetch_all(
            Query(Author)
            .where(or_(and_(Author.active == True, Author.id == 1),  # noqa: E712
                       ~(Author.name != "carol")))
            .order_by("id")
        )
        assert [a.name for a in rows] == ["ada", "carol"]

    async def test_in_and_not_in(self, engine):
        rows = await engine.fetch_all(
            Query(Author).where(Author.id.in_([1, 3])).where(Author.id.not_in([3]))
        )
        assert [a.name for a in rows] == ["ada"]

    async def test_empty_in_is_valid_sql_on_postgres(self, engine):
        # `IN ()` is a syntax error here, which is why it renders as FALSE.
        assert await engine.fetch_all(Query(Author).where(Author.id.in_([]))) == []

    async def test_in_subquery(self, engine):
        rows = await engine.fetch_all(
            Query(Author).where(
                Author.id.in_(Query(Book.author_id).where(Book.title == "compilers"))
            )
        )
        assert [a.name for a in rows] == ["brian"]

    async def test_correlated_exists(self, engine):
        from sqlom import exists

        rows = await engine.fetch_all(
            Query(Author)
            .where(~exists(Query(Book.id).correlate(Author)
                           .where(Book.author_id == Author.id)))
        )
        assert [a.name for a in rows] == ["dan"]

    async def test_group_by_with_having(self, engine):
        from sqlom import count

        rows = await engine.fetch_all(
            Query(Book.author_id, count())
            .group_by(Book.author_id)
            .having(count() > 1)
        )
        assert rows == [(1, 2)]

    async def test_aggregates(self, engine):
        from sqlom import count, max_, min_

        rows = await engine.fetch_all(
            Query(count(), min_(Book.id), max_(Book.id))
        )
        assert rows == [(4, 10, 13)]

    async def test_derived_table(self, engine):
        from sqlom import count

        sub = (Query(Book.author_id, count().label("n"))
               .group_by(Book.author_id)
               .subquery("s"))
        rows = await engine.fetch_all(
            Query(Author, sub.n).join(sub, sub.author_id == Author.id).where(sub.n > 1)
        )
        assert len(rows) == 1
        author, n = rows[0]
        assert author.name == "ada" and n == 2

    async def test_distinct_and_offset(self, engine):
        rows = await engine.fetch_all(
            Query(Book.author_id).distinct().order_by(Book.author_id).limit(2).offset(1)
        )
        assert rows == [(2,), (3,)]

    async def test_single_column_select_is_not_nested(self, engine):
        rows = await engine.fetch_all(Query(Author.name).order_by(Author.name).limit(2))
        assert rows == [("ada",), ("brian",)]
