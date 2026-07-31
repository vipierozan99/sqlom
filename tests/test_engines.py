"""Engine behaviour, on both backends.

Every test here runs twice — once on aiosqlite, once on asyncpg — because the
two differ in exactly the place the design is most exposed. sqlite hands back
strings for temporal types and integers for booleans; postgres decodes natively.
A result asserted on only one of them has not been tested.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import Author, Base, Book, Tag

import rowform as rf


class TestLifecycle:
    async def test_connect_is_idempotent(self, engine):
        assert await engine.connect() is engine.pool

    async def test_close_clears_the_pool_and_is_repeatable(self, engine):
        await engine.close()
        assert engine.pool is None
        await engine.close()

    async def test_use_before_connect_raises(self, sqlite_path):
        db = rf.SqliteEngine(sqlite_path)
        with pytest.raises(RuntimeError, match="not connected"):
            await db.fetch_all(sa.select(Author))

    async def test_use_after_close_raises(self, engine):
        await engine.close()
        with pytest.raises(RuntimeError, match="not connected"):
            await engine.fetch_all(sa.select(Author))

    async def test_async_context_manager(self, sqlite_path):
        async with rf.SqliteEngine(sqlite_path) as db:
            assert db.pool is not None
        assert db.pool is None

    def test_an_unknown_kwarg_is_refused(self, sqlite_path):
        with pytest.raises(TypeError, match="unexpected keyword"):
            rf.SqliteEngine(sqlite_path, conditional_reset=True)


class TestFetchAll:
    async def test_returns_hydrated_instances(self, engine):
        authors = await engine.fetch_all(sa.select(Author).order_by(Author.id))
        assert [a.name for a in authors] == ["ada", "brian", "carol", "dan"]
        assert all(isinstance(a, Author) for a in authors)

    async def test_booleans_are_real_bools_on_both_backends(self, engine):
        authors = await engine.fetch_all(sa.select(Author).order_by(Author.id))
        assert [a.active for a in authors] == [True, True, False, True]
        assert all(isinstance(a.active, bool) for a in authors)

    async def test_where_and_limit(self, engine):
        rows = await engine.fetch_all(
            sa.select(Author).where(Author.active.is_(True)).order_by(Author.id).limit(2)
        )
        assert [a.id for a in rows] == [1, 2]

    async def test_limit_binds_from_positiontup_not_from_the_caller(self, engine):
        """Core supplies parameters nobody asked for — on sqlite a bare `.limit()`
        emits `LIMIT ? OFFSET ?` with an OFFSET of 0 — so the positional tuple has
        to be built from `positiontup` rather than from what was passed."""
        query = engine.prepare(sa.select(Author).order_by(Author.id).limit(2))
        _, params = query.bind()
        assert len(params) == len(query._keys)
        if "OFFSET" in query.sql.upper():
            assert params == (2, 0)
        assert [a.id for a in await engine.fetch_all(query)] == [1, 2]

    async def test_empty_result(self, engine):
        assert await engine.fetch_all(sa.select(Author).where(Author.id == 999)) == []

    async def test_in_expansion(self, engine):
        """`in_` compiles to one POSTCOMPILE placeholder and is only expanded at
        bind time, which rewrites the SQL string per call."""
        query = engine.prepare(sa.select(Author).where(Author.id.in_([1, 3])))
        rows = await engine.fetch_all(query)
        assert sorted(a.id for a in rows) == [1, 3]

    async def test_bindparam_is_reusable_across_calls(self, engine):
        query = engine.prepare(
            sa.select(Author).where(Author.id > sa.bindparam("floor")).order_by(Author.id)
        )
        assert [a.id for a in await engine.fetch_all(query, floor=2)] == [3, 4]
        assert [a.id for a in await engine.fetch_all(query, floor=0)] == [1, 2, 3, 4]

    async def test_a_statement_is_compiled_once_per_shape(self, engine):
        await engine.fetch_all(sa.select(Author).where(Author.id == 1))
        before = len(engine._queries)
        await engine.fetch_all(sa.select(Author).where(Author.id == 2))
        assert len(engine._queries) == before, "same shape, different literal"

        await engine.fetch_all(sa.select(Author.name))
        assert len(engine._queries) == before + 1

    async def test_a_cached_statement_uses_the_callers_literals(self, engine):
        """The structural cache key ignores literal values, so `where(id == 2)`
        reuses the query compiled from `where(id == 1)`. Without carrying the
        caller's own bind values along, it would silently answer the first
        query's question."""
        first = await engine.fetch_all(sa.select(Author).where(Author.id == 1))
        second = await engine.fetch_all(sa.select(Author).where(Author.id == 2))
        assert [a.id for a in first] == [1]
        assert [a.id for a in second] == [2]

    async def test_a_cached_insert_uses_the_callers_values(self, engine):
        await engine.execute(sa.insert(Author.__table__).values(id=60, name="p", active=True))
        await engine.execute(sa.insert(Author.__table__).values(id=61, name="q", active=True))
        rows = await engine.fetch_all(sa.select(Author).where(Author.id > 59).order_by(Author.id))
        assert [(a.id, a.name) for a in rows] == [(60, "p"), (61, "q")]

    async def test_the_hydrator_is_built_once_and_reused(self, engine):
        query = engine.prepare(sa.select(Author))
        assert query._hydrate is None
        await engine.fetch_all(query)
        first = query._hydrate
        assert first is not None
        await engine.fetch_all(query)
        assert query._hydrate is first

    async def test_fetch_one(self, engine):
        author = await engine.fetch_one(sa.select(Author).order_by(Author.id))
        assert author.name == "ada"
        assert await engine.fetch_one(sa.select(Author).where(Author.id == 999)) is None

    async def test_fetch_value(self, engine):
        assert await engine.fetch_value(sa.select(sa.func.count()).select_from(Author)) == 4
        assert await engine.fetch_value(sa.select(Author.name).where(Author.id == 1)) == "ada"

    async def test_a_write_without_returning_is_refused(self, engine):
        with pytest.raises(ValueError, match="produces no rows"):
            await engine.fetch_all(sa.delete(Tag.__table__))


class TestStatementMatrix:
    """The planner matrix, end to end against real rows."""

    async def test_select_model(self, engine):
        rows = await engine.fetch_all(sa.select(Author).order_by(Author.id))
        assert isinstance(rows[0], Author)

    async def test_select_two_columns_is_a_tuple(self, engine):
        rows = await engine.fetch_all(sa.select(Author.id, Author.name).order_by(Author.id))
        assert rows[0] == (1, "ada")

    async def test_select_columns_reversed(self, engine):
        rows = await engine.fetch_all(sa.select(Author.name, Author.id).order_by(Author.id))
        assert rows[0] == ("ada", 1)
        assert isinstance(rows[0][0], str) and isinstance(rows[0][1], int)

    async def test_inner_join_two_models(self, engine):
        rows = await engine.fetch_all(sa.select(Author, Book).join(Book).order_by(Book.id))
        assert len(rows) == 4
        author, book = rows[0]
        assert (author.name, book.title) == ("ada", "structures")

    async def test_model_plus_column(self, engine):
        rows = await engine.fetch_all(
            sa.select(Author, Book.title).join(Book).order_by(Book.id)
        )
        assert isinstance(rows[0][0], Author)
        assert rows[0][1] == "structures"

    async def test_outer_join_yields_none_for_no_match(self, engine):
        rows = await engine.fetch_all(
            sa.select(Author, Book).outerjoin(Book).order_by(Author.id, Book.id)
        )
        by_author = {a.name: b for a, b in rows}
        assert by_author["dan"] is None
        assert by_author["carol"].title == "typography"

    async def test_three_way_join(self, engine):
        rows = await engine.fetch_all(
            sa.select(Author, Book, Tag).select_from(Author).join(Book).join(Tag)
            .order_by(Tag.id)
        )
        assert [(a.name, b.title, t.label) for a, b, t in rows] == [
            ("ada", "structures", "classic"),
            ("brian", "compilers", "classic"),
        ]

    async def test_aggregate(self, engine):
        assert await engine.fetch_all(sa.select(sa.func.count()).select_from(Author)) == [4]

    async def test_one_selected_column_is_unwrapped(self, engine):
        """One entity is one entity, model or scalar — the rule `fetch_all`'s
        overloads are written against."""
        names = await engine.fetch_all(sa.select(Author.name).order_by(Author.id))
        assert names == ["ada", "brian", "carol", "dan"]

    async def test_self_join_through_an_alias(self, engine):
        other = sa.alias(Author.__table__, "a2")
        rows = await engine.fetch_all(
            sa.select(Author, other)
            .join(other, Author.id < other.c.id)
            .order_by(Author.id, other.c.id)
        )
        first, second = rows[0]
        assert isinstance(first, Author) and isinstance(second, Author)
        assert first.id < second.id

    async def test_self_join_through_rowform_alias(self, engine):
        other = rf.alias(Author, "a2")
        rows = await engine.fetch_all(
            sa.select(Author, other)
            .join(other, Author.id < other.id)
            .where(other.active)
            .order_by(Author.id, other.id)
        )
        first, second = rows[0]
        assert isinstance(first, Author) and isinstance(second, Author)
        assert first.id < second.id and second.active is True

    async def test_a_cte_hydrates_as_a_model(self, engine):
        active = rf.alias(Author, of=sa.select(Author).where(Author.active).cte("active"))
        rows = await engine.fetch_all(sa.select(active).order_by(active.id))
        assert [a.name for a in rows] == ["ada", "brian", "dan"]
        assert all(isinstance(a, Author) for a in rows)

    async def test_a_subquery_joins_back_against_its_table(self, engine):
        newest = rf.alias(
            Author, of=sa.select(Author).order_by(Author.id.desc()).limit(2).subquery()
        )
        rows = await engine.fetch_all(
            sa.select(Book, newest).join(newest, Book.author_id == newest.id).order_by(Book.id)
        )
        assert [(b.title, a.name) for b, a in rows] == [("typography", "carol")]

    async def test_group_by_and_having(self, engine):
        rows = await engine.fetch_all(
            sa.select(Author.name, sa.func.count(Book.id))
            .join(Book)
            .group_by(Author.name)
            .having(sa.func.count(Book.id) > 1)
        )
        assert rows == [("ada", 2)]

    async def test_a_subquery_of_scalars(self, engine):
        prolific = (
            sa.select(Book.author_id)
            .group_by(Book.author_id)
            .having(sa.func.count(Book.id) > 1)
            .scalar_subquery()
        )
        rows = await engine.fetch_all(sa.select(Author).where(Author.id.in_(prolific)))
        assert [a.name for a in rows] == ["ada"]


class TestWrites:
    async def test_insert_and_read_back(self, engine):
        await engine.execute(
            sa.insert(Author.__table__).values(id=90, name="eve", active=True)
        )
        assert (await engine.fetch_one(sa.select(Author).where(Author.id == 90))).name == "eve"

    async def test_insert_many(self, engine):
        await engine.execute_many(
            sa.insert(Tag.__table__),
            [{"id": 200 + i, "book_id": 10, "label": f"l{i}"} for i in range(3)],
        )
        assert await engine.fetch_value(sa.select(sa.func.count()).select_from(Tag)) == 5

    async def test_update(self, engine):
        await engine.execute(
            sa.update(Author.__table__).where(Author.id == 1).values(name="ada l.")
        )
        assert await engine.fetch_value(sa.select(Author.name).where(Author.id == 1)) == "ada l."

    async def test_delete(self, engine):
        await engine.execute(sa.delete(Tag.__table__).where(Tag.id == 100))
        assert await engine.fetch_value(sa.select(sa.func.count()).select_from(Tag)) == 1

    async def test_returning_hydrates(self, engine):
        rows = await engine.fetch_all(
            sa.insert(Author.__table__)
            .values(id=91, name="frank", active=False)
            .returning(Author.__table__)
        )
        assert isinstance(rows[0], Author)
        assert (rows[0].name, rows[0].active) == ("frank", False)

    async def test_execute_refuses_a_statement_that_returns_rows(self, engine):
        with pytest.raises(ValueError, match="produces rows"):
            await engine.execute(sa.select(Author))

    async def test_execute_many_with_no_rows_is_a_no_op(self, engine):
        assert await engine.execute_many(sa.insert(Tag.__table__), []) is None


class TestSchema:
    async def test_drop_all_then_create_all_round_trips(self, engine):
        await engine.drop_all(Base.metadata)
        await engine.create_all(Base.metadata)
        assert await engine.fetch_all(sa.select(Author)) == []

    async def test_drop_all_tolerates_a_missing_schema(self, engine):
        await engine.drop_all(Base.metadata)
        await engine.drop_all(Base.metadata)

    async def test_the_ddl_creates_enum_types_before_their_tables(self, engine):
        """A hand-rolled loop over `sorted_tables` omits `CREATE TYPE`, and the
        table then fails to create on postgres. The DDL comes from SQLAlchemy's
        own SchemaGenerator so it cannot."""
        statements = engine._ddl(Base.metadata, drop=False)
        if engine.dialect.supports_native_enum:
            assert any(s.startswith("CREATE TYPE") for s in statements)
            types = next(i for i, s in enumerate(statements) if s.startswith("CREATE TYPE"))
            wide = next(i for i, s in enumerate(statements) if "CREATE TABLE t_wide" in s)
            assert types < wide


class TestAcquire:
    async def test_yields_a_raw_driver_connection(self, engine):
        async with engine.acquire() as conn:
            assert conn is not None
            assert not isinstance(conn, rf.Engine)
