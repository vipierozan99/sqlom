"""`copy_in` loads the same rows `execute_many` would.

COPY bypasses the statement path, and with it the bind processors that turn a
`Decimal`, `datetime`, `Enum`, `UUID` or `dict` into what the driver sends. That
is the same class of mistake as docs/METHODOLOGY.md correction 11, in the other
direction: values that look plausible and are not what came in.

So `execute_many` is the oracle. Both paths load the same rows into the same
table, and what comes back has to match field for field and type for type — over
the `Wide` model, which exists precisely because it holds one column per type
whose driver representation differs from its Python one.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import WIDE_ROW, Author, Wide, engine_at, pg_url, seed

import rowform


def wide_rows(count: int, start: int) -> list[dict]:
    return [{**WIDE_ROW, "id": start + i} for i in range(count)]


class TestItMatchesExecuteMany:
    async def test_every_type_round_trips_identically(self, pg_engine):
        """The load-bearing test: two paths, one oracle, all types."""
        await pg_engine.execute(sa.delete(Wide.__table__))
        await pg_engine.execute_many(sa.insert(Wide.__table__), wide_rows(3, 100))
        via_insert = await pg_engine.fetch_all(sa.select(Wide).order_by(Wide.id))

        await pg_engine.execute(sa.delete(Wide.__table__))
        copied = await pg_engine.copy_in(Wide.__table__, wide_rows(3, 100))
        via_copy = await pg_engine.fetch_all(sa.select(Wide).order_by(Wide.id))

        assert copied == 3
        assert len(via_copy) == len(via_insert) == 3
        for got, want in zip(via_copy, via_insert, strict=True):
            for column in Wide.__table__.columns:
                mine, theirs = getattr(got, column.key), getattr(want, column.key)
                assert mine == theirs, f"{column.key}: {mine!r} != {theirs!r}"
                assert type(mine) is type(theirs), f"{column.key}: {type(mine)}"

    async def test_a_column_subset_leaves_the_rest_to_the_server(self, pg_engine):
        """`Wide.note` is the nullable one; omitting it must land NULL rather
        than shifting every value one column to the left."""
        named = [c.key for c in Wide.__table__.columns if c.key != "note"]
        rows = [{k: v for k, v in row.items() if k != "note"} for row in wide_rows(2, 200)]

        await pg_engine.execute(sa.delete(Wide.__table__))
        await pg_engine.copy_in(Wide.__table__, rows, columns=named)

        loaded = await pg_engine.fetch_all(sa.select(Wide).order_by(Wide.id))
        assert [w.id for w in loaded] == [200, 201]
        assert all(w.note is None for w in loaded)
        assert all(w.text == WIDE_ROW["text"] for w in loaded)
        assert all(w.amount == WIDE_ROW["amount"] for w in loaded)

    async def test_no_rows_is_a_no_op(self, pg_engine):
        assert await pg_engine.copy_in(Wide.__table__, []) == 0

    async def test_it_reports_to_the_observer(self, pg_engine):
        seen: list[tuple[str, float, int | None]] = []
        pg_engine.observer = lambda *call: seen.append(call)
        await pg_engine.execute(sa.delete(Wide.__table__))
        await pg_engine.copy_in(Wide.__table__, wide_rows(2, 300))
        pg_engine.observer = None
        copies = [call for call in seen if call[0].startswith("COPY")]
        assert len(copies) == 1
        assert copies[0][2] == 2


class TestWhatItRefuses:
    async def test_sqlite_says_what_to_use_instead(self, sqlite_engine):
        with pytest.raises(rowform.UnsupportedError, match="execute_many"):
            await sqlite_engine.copy_in(Author.__table__, [{"id": 1, "name": "a", "active": True}])

    async def test_a_missing_column_in_a_row_is_loud(self, pg_engine):
        with pytest.raises(KeyError):
            await pg_engine.copy_in(Author.__table__, [{"id": 1}], columns=["id", "name"])

    async def test_it_is_refused_inside_a_transaction(self, pg_engine):
        """It would take a different pooled connection and commit on its own, so a
        rollback of the surrounding block would leave the loaded rows behind —
        the same reason `fetch_all` is refused there."""
        async with pg_engine.begin():
            with pytest.raises(rowform.EngineStateError, match="copy_in"):
                await pg_engine.copy_in(Wide.__table__, wide_rows(1, 400))


class TestSchemaQualification:
    """A table with no explicit schema must resolve through `search_path`, and both
    postgres engines must agree about where the rows went.

    asyncpg qualifies the target itself from `schema_name`; psycopg gets a name
    quoted by SQLAlchemy's preparer. Defaulting the asyncpg side to `"public"`
    would send the two to different tables whenever `search_path` says otherwise —
    invisible until someone runs with a per-tenant search_path.
    """

    @pytest.fixture
    async def two_schemas(self, pg_dsn):
        """The same table name in `tenant_a` and in `public`, both empty.

        Which one a copy lands in is then a question with a wrong answer, which is
        what makes the search_path behaviour testable at all.
        """
        async with engine_at(pg_url(pg_dsn)) as db, db.acquire() as conn:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS tenant_a")
            for schema in ("tenant_a", "public"):
                await conn.execute(f"DROP TABLE IF EXISTS {schema}.copy_target")
                await conn.execute(
                    f"CREATE TABLE {schema}.copy_target (id int primary key, name text)"
                )
        yield
        async with engine_at(pg_url(pg_dsn)) as db, db.acquire() as conn:
            for schema in ("tenant_a", "public"):
                await conn.execute(f"DROP TABLE IF EXISTS {schema}.copy_target")

    @staticmethod
    def _unqualified() -> sa.Table:
        return sa.Table(
            "copy_target",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String),
        )

    @staticmethod
    def _tenant_scoped(pg_dsn: str, driver: str):
        """An engine whose connections resolve unqualified names to `tenant_a`."""
        connect_args = (
            {"server_settings": {"search_path": "tenant_a"}}
            if driver == "asyncpg"
            else {"options": "-c search_path=tenant_a"}
        )
        return engine_at(pg_url(pg_dsn, driver), connect_args=connect_args)

    @pytest.mark.parametrize("driver", ["asyncpg", "psycopg"])
    async def test_an_unqualified_table_follows_search_path(
        self, pg_dsn, two_schemas, driver
    ):
        """The regression this guards: qualifying the target as "public" whenever
        the table declares no schema sends asyncpg somewhere psycopg would not go,
        and nothing says so until a tenant's rows appear in the wrong schema."""
        table = self._unqualified()
        async with self._tenant_scoped(pg_dsn, driver) as db:
            await db.copy_in(table, [{"id": 1, "name": driver}])

        async with engine_at(pg_url(pg_dsn)) as check, check.acquire() as conn:
            tenant = await conn.fetch("SELECT name FROM tenant_a.copy_target")
            public = await conn.fetch("SELECT name FROM public.copy_target")
        assert [r["name"] for r in tenant] == [driver], "did not follow search_path"
        assert public == [], "landed in public despite the search_path"

    @pytest.mark.parametrize("driver", ["asyncpg", "psycopg"])
    async def test_an_explicit_schema_is_honoured(self, pg_dsn, two_schemas, driver):
        table = sa.Table(
            "copy_target",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String),
            schema="tenant_a",
        )
        async with engine_at(pg_url(pg_dsn, driver)) as db:
            await db.copy_in(table, [{"id": 2, "name": driver}])
            landed = await db.fetch_all(sa.select(table.c.name))
            assert landed == [driver]


class TestBothPostgresDrivers:
    """asyncpg copies over the binary protocol and psycopg over `FROM STDIN`, so
    each needs its own round trip."""

    @pytest.mark.parametrize("engine_factory", ["asyncpg", "psycopg"])
    async def test_each_driver_loads_the_same_values(self, pg_dsn, engine_factory):
        async with engine_at(pg_url(pg_dsn, engine_factory)) as db:
            await seed(db)
            await db.execute(sa.delete(Wide.__table__))
            await db.execute_many(sa.insert(Wide.__table__), wide_rows(2, 500))
            expected = await db.fetch_all(sa.select(Wide).order_by(Wide.id))

            await db.execute(sa.delete(Wide.__table__))
            await db.copy_in(Wide.__table__, wide_rows(2, 500))
            got = await db.fetch_all(sa.select(Wide).order_by(Wide.id))

            for mine, theirs in zip(got, expected, strict=True):
                for column in Wide.__table__.columns:
                    assert getattr(mine, column.key) == getattr(theirs, column.key), column.key
