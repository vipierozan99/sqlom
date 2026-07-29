"""DML executed against real PostgreSQL, on both engines.

Every write goes to its own scratch table, never the seeded fixtures, so a failure
here cannot corrupt what the read tests assert.
"""

import pytest

from rowform import (
    Column,
    Delete,
    Insert,
    ModelMeta,
    Query,
    Update,
    count,
    row_number,
    sum_,
)

pytestmark = pytest.mark.postgres

TABLE = "t_dml"


class Row(metaclass=ModelMeta):
    __tablename__ = TABLE

    id = Column(int)
    grp = Column(int)
    score = Column(int)
    label = Column(str)


@pytest.fixture(params=["asyncpg", "psycopg"])
async def engine(request, pg_schema):
    from rowform import DatabaseEngine, PsycopgEngine

    if request.param == "asyncpg":
        eng = DatabaseEngine(dsn=pg_schema, min_size=1, max_size=4)
    else:
        eng = PsycopgEngine(pg_schema, min_size=1, max_size=4)
    await eng.connect()
    async with eng.acquire() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await conn.execute(
            f"CREATE TABLE {TABLE} (id int PRIMARY KEY, grp int, score int, label text)"
        )
    try:
        yield eng
    finally:
        async with eng.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        await eng.close()


async def seed(engine, rows):
    await engine.execute(Insert(Row).values(rows))


BASE = [
    {"id": 1, "grp": 1, "score": 10, "label": "a"},
    {"id": 2, "grp": 1, "score": 20, "label": "b"},
    {"id": 3, "grp": 2, "score": 5, "label": "c"},
]


class TestInsert:
    async def test_single_row(self, engine):
        await engine.execute(Insert(Row).values(id=1, grp=1, score=10, label="a"))
        assert await engine.fetch_all(Query(count(Row))) == [(1,)]

    async def test_bulk_in_one_statement(self, engine):
        await seed(engine, BASE)
        assert await engine.fetch_all(Query(count(Row))) == [(3,)]

    async def test_returning_hydrates_scalars(self, engine):
        ids = await engine.fetch_all(Insert(Row).values(BASE).returning(Row.id))
        assert ids == [(1,), (2,), (3,)]

    async def test_returning_hydrates_models(self, engine):
        rows = await engine.fetch_all(
            Insert(Row).values(BASE).returning(Row)
        )
        assert all(isinstance(row, Row) for row in rows)
        assert [row.label for row in rows] == ["a", "b", "c"]

    async def test_returning_an_expression(self, engine):
        rows = await engine.fetch_all(
            Insert(Row).values(id=1, score=10).returning(Row.id, Row.score * 2)
        )
        assert rows == [(1, 20)]

    async def test_execute_reports_what_the_driver_reports(self, engine):
        result = await engine.execute(Insert(Row).values(BASE))
        # asyncpg gives a status tag, psycopg a rowcount; both are the driver's
        # own answer rather than something normalised across them.
        assert result in ("INSERT 0 3", 3)


class TestUpdate:
    async def test_set_and_where(self, engine):
        await seed(engine, BASE)
        await engine.execute(Update(Row).set(label="z").where(Row.id == 1))
        rows = await engine.fetch_all(Query(Row.label).where(Row.id == 1))
        assert rows == [("z",)]

    async def test_expression_assignment(self, engine):
        await seed(engine, BASE)
        await engine.execute(Update(Row).set(score=Row.score + 5).where(Row.grp == 1))
        rows = await engine.fetch_all(
            Query(Row.id, Row.score).where(Row.grp == 1).order_by(Row.id)
        )
        assert rows == [(1, 15), (2, 25)]

    async def test_returning(self, engine):
        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Update(Row).set(score=0).where(Row.grp == 1).returning(Row.id, Row.score)
        )
        assert sorted(rows) == [(1, 0), (2, 0)]

    async def test_no_match_returns_nothing(self, engine):
        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Update(Row).set(score=0).where(Row.id == 999).returning(Row.id)
        )
        assert rows == []


class TestDelete:
    async def test_with_where(self, engine):
        await seed(engine, BASE)
        await engine.execute(Delete(Row).where(Row.grp == 1))
        assert await engine.fetch_all(Query(count(Row))) == [(1,)]

    async def test_returning(self, engine):
        await seed(engine, BASE)
        gone = await engine.fetch_all(Delete(Row).where(Row.grp == 2).returning(Row))
        assert [row.label for row in gone] == ["c"]

    async def test_all_rows(self, engine):
        await seed(engine, BASE)
        await engine.execute(Delete(Row).all_rows())
        assert await engine.fetch_all(Query(count(Row))) == [(0,)]


class TestTheEngineBoundary:
    async def test_execute_refuses_a_statement_with_returning(self, engine):
        with pytest.raises(ValueError, match="use fetch_all"):
            await engine.execute(Insert(Row).values(id=1).returning(Row.id))

    async def test_fetch_all_refuses_a_statement_without_returning(self, engine):
        # Hydrating it would give [] and read as "nothing matched".
        with pytest.raises(ValueError, match="no returning"):
            await engine.fetch_all(Insert(Row).values(id=1))

    async def test_writes_are_visible_immediately_outside_a_transaction(self, engine):
        # A lone statement runs in autocommit on both pools, which is worth
        # asserting because it is the reason writes need grouping to be atomic.
        await engine.execute(Insert(Row).values(id=1, score=1))
        assert await engine.fetch_all(Query(count(Row))) == [(1,)]


class TestInTransactions:
    async def test_rollback_undoes_a_write(self, engine):
        class Boom(Exception):
            pass

        await seed(engine, BASE)
        with pytest.raises(Boom):
            async with engine.transaction() as tx:
                sql, params = Insert(Row).values(id=9, score=1).to_sql(
                    placeholder=tx._placeholder)
                await tx.execute(sql, *params)
                raise Boom
        assert await engine.fetch_all(Query(count(Row))) == [(3,)]

    async def test_commit_keeps_it(self, engine):
        await seed(engine, BASE)
        async with engine.transaction() as tx:
            sql, params = Insert(Row).values(id=9, score=1).to_sql(
                placeholder=tx._placeholder)
            await tx.execute(sql, *params)
        assert await engine.fetch_all(Query(count(Row))) == [(4,)]


class TestNewExpressionsAgainstTheServer:
    """The expression features, executed rather than only rendered — Postgres is
    stricter than sqlite about several of these."""

    async def test_window_over_partitions(self, engine):
        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Query(Row.grp, Row.id,
                  row_number().over(partition_by=Row.grp, order_by=(Row.score, "DESC")))
            .order_by(Row.grp, Row.id)
        )
        assert rows == [(1, 1, 2), (1, 2, 1), (2, 3, 1)]

    async def test_windowed_aggregate(self, engine):
        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Query(Row.id, sum_(Row.score).over(partition_by=Row.grp)).order_by(Row.id)
        )
        assert rows == [(1, 30), (2, 30), (3, 5)]

    async def test_case_and_arithmetic(self, engine):
        from rowform import case, func

        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Query(Row.id,
                  case((Row.score > 15, "hi"), else_="lo"),
                  Row.score * 2,
                  func.upper(Row.label))
            .order_by(Row.id)
        )
        assert rows == [(1, "lo", 20, "A"), (2, "hi", 40, "B"), (3, "lo", 10, "C")]

    async def test_set_operation(self, engine):
        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Query(Row.grp).where(Row.score > 15)
            .union(Query(Row.grp).where(Row.score < 8))
            .order_by("grp")
        )
        assert rows == [(1,), (2,)]

    async def test_concat_uses_the_portable_operator(self, engine):
        # `+` on text is an error in Postgres; `||` is what concat renders.
        await seed(engine, BASE)
        rows = await engine.fetch_all(
            Query(Row.label.concat("!")).where(Row.id == 1)
        )
        assert rows == [("a!",)]
