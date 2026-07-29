"""Upserts, CTEs and multi-table DML against real PostgreSQL, on both engines.

The sqlite tests prove the SQL is well-formed and the semantics are what was meant.
These prove Postgres agrees — which is not a formality for this batch:

* `ON CONFLICT ON CONSTRAINT` exists only in Postgres, so it can only be tested here.
* `DELETE ... USING` likewise.
* A `WITH` clause in front of `INSERT`/`UPDATE`/`DELETE` is accepted by both, but the
  data-modifying-CTE form (`WITH moved AS (DELETE ... RETURNING ...)`) is Postgres-only
  and worth pinning down as a *documented* limitation rather than a surprise.
* Every statement here goes through `to_sql()` with `$n` placeholders and then the
  real driver, so a parameter numbered out of order fails loudly instead of quietly
  binding the wrong value.
"""

import pytest

from rowform import (
    Column,
    Delete,
    Insert,
    Query,
    Update,
    count,
    excluded,
    model,
    recursive_cte,
    sum_,
)

pytestmark = pytest.mark.postgres

MAIN = "t_adv_main"
OTHER = "t_adv_other"


@model
class Main:
    __tablename__ = MAIN

    id: Column[int] = Column(int)
    tag: Column[str] = Column(str)
    score: Column[int] = Column(int)


@model
class Other:
    __tablename__ = OTHER

    id: Column[int] = Column(int)
    main_id: Column[int] = Column(int)
    name: Column[str] = Column(str)
    keep: Column[bool] = Column(bool)


@pytest.fixture(params=["asyncpg", "psycopg"])
async def engine(request, pg_schema):
    from rowform import DatabaseEngine, PsycopgEngine

    if request.param == "asyncpg":
        eng = DatabaseEngine(dsn=pg_schema, min_size=1, max_size=4)
    else:
        eng = PsycopgEngine(pg_schema, min_size=1, max_size=4)
    await eng.connect()
    async with eng.acquire() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {OTHER}, {MAIN}")
        # `tag` gets its own unique index so a conflict target other than the
        # primary key can be exercised, including by constraint name.
        await conn.execute(
            f"CREATE TABLE {MAIN} (id int PRIMARY KEY, tag text, score int, "
            f"CONSTRAINT {MAIN}_tag_key UNIQUE (tag))"
        )
        await conn.execute(
            f"CREATE TABLE {OTHER} (id int PRIMARY KEY, main_id int, name text, "
            f"keep boolean)"
        )
    try:
        yield eng
    finally:
        async with eng.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {OTHER}, {MAIN}")
        await eng.close()


MAIN_ROWS = [
    {"id": 1, "tag": "a", "score": 10},
    {"id": 2, "tag": "b", "score": 20},
    {"id": 3, "tag": "c", "score": 30},
]
OTHER_ROWS = [
    {"id": 10, "main_id": 1, "name": "one", "keep": True},
    {"id": 11, "main_id": 2, "name": "two", "keep": False},
    {"id": 12, "main_id": 3, "name": "three", "keep": False},
]


async def seed(engine):
    await engine.execute(Insert(Main).values(MAIN_ROWS))
    await engine.execute(Insert(Other).values(OTHER_ROWS))


async def main_rows(engine):
    return await engine.fetch_all(
        Query(Main.id, Main.tag, Main.score).order_by(Main.id)
    )


class TestOnConflict:
    async def test_do_nothing_skips_the_duplicate(self, engine):
        await seed(engine)
        await engine.execute(
            Insert(Main).values(id=1, tag="zz", score=999)
                        .on_conflict_do_nothing(Main.id)
        )
        assert await main_rows(engine) == [(1, "a", 10), (2, "b", 20), (3, "c", 30)]

    async def test_do_nothing_untargeted(self, engine):
        await seed(engine)
        # Conflicts on `tag`, not on the primary key, and is still swallowed.
        await engine.execute(
            Insert(Main).values(id=99, tag="a", score=1).on_conflict_do_nothing()
        )
        assert await engine.fetch_all(Query(count(Main))) == [(3,)]

    async def test_do_nothing_by_constraint_name(self, engine):
        await seed(engine)
        await engine.execute(
            Insert(Main).values(id=99, tag="a", score=1)
                        .on_conflict_do_nothing(constraint=f"{MAIN}_tag_key")
        )
        assert await engine.fetch_all(Query(count(Main))) == [(3,)]

    async def test_do_nothing_returning_is_empty_on_conflict(self, engine):
        await seed(engine)
        rows = await engine.fetch_all(
            Insert(Main).values(id=1, tag="zz", score=1)
                        .on_conflict_do_nothing(Main.id).returning(Main.id)
        )
        assert rows == []

    async def test_do_nothing_returning_reports_a_real_insert(self, engine):
        await seed(engine)
        rows = await engine.fetch_all(
            Insert(Main).values(id=4, tag="d", score=40)
                        .on_conflict_do_nothing(Main.id).returning(Main.id)
        )
        assert rows == [(4,)]

    async def test_upsert_overwrites(self, engine):
        await seed(engine)
        await engine.execute(
            Insert(Main).values(id=1, tag="a", score=111)
                        .on_conflict_do_update(Main.id,
                                               set_={"score": excluded(Main.score)})
        )
        assert await main_rows(engine) == [(1, "a", 111), (2, "b", 20), (3, "c", 30)]

    async def test_upsert_accumulates_from_the_stored_row(self, engine):
        await seed(engine)
        statement = (Insert(Main).values(id=1, tag="a", score=5)
                     .on_conflict_do_update(
                         Main.id,
                         set_={"score": Main.score + excluded(Main.score)}))
        await engine.execute(statement)
        await engine.execute(statement)
        # 10 + 5 + 5: the bare column really is the stored value, twice over.
        assert await engine.fetch_all(Query(Main.score).where(Main.id == 1)) == [(20,)]

    async def test_upsert_by_constraint_name(self, engine):
        await seed(engine)
        await engine.execute(
            Insert(Main).values(id=99, tag="a", score=7)
                        .on_conflict_do_update(constraint=f"{MAIN}_tag_key",
                                               set_={"score": excluded(Main.score)})
        )
        assert await engine.fetch_all(Query(Main.score).where(Main.tag == "a")) == [(7,)]

    async def test_conditional_upsert_keeps_the_maximum(self, engine):
        await seed(engine)

        def keep_max(score):
            return (Insert(Main).values(id=1, tag="a", score=score)
                    .on_conflict_do_update(
                        Main.id,
                        set_={"score": excluded(Main.score)},
                        where=Main.score < excluded(Main.score)))

        await engine.execute(keep_max(50))
        await engine.execute(keep_max(5))
        assert await engine.fetch_all(Query(Main.score).where(Main.id == 1)) == [(50,)]

    async def test_bulk_upsert_mixes_insert_and_update(self, engine):
        await seed(engine)
        await engine.execute(
            Insert(Main).values([
                {"id": 1, "tag": "a", "score": 100},
                {"id": 4, "tag": "d", "score": 400},
            ]).on_conflict_do_update(Main.id, set_={"score": excluded(Main.score)})
        )
        assert await main_rows(engine) == [
            (1, "a", 100), (2, "b", 20), (3, "c", 30), (4, "d", 400)
        ]

    async def test_upsert_returning_hydrates_a_model(self, engine):
        await seed(engine)
        rows = await engine.fetch_all(
            Insert(Main).values(id=1, tag="a", score=77)
                        .on_conflict_do_update(Main.id,
                                               set_={"score": excluded(Main.score)})
                        .returning(Main)
        )
        assert all(isinstance(row, Main) for row in rows)
        assert [(row.id, row.score) for row in rows] == [(1, 77)]

    async def test_without_on_conflict_the_violation_surfaces(self, engine):
        await seed(engine)
        with pytest.raises(Exception, match="duplicate key|unique"):
            await engine.execute(Insert(Main).values(id=1, tag="zz", score=1))


class TestUpdateFrom:
    async def test_copies_across_the_join(self, engine):
        await seed(engine)
        await engine.execute(
            Update(Main).set(tag=Other.name).from_(Other)
                        .where(Other.main_id == Main.id)
        )
        assert await main_rows(engine) == [
            (1, "one", 10), (2, "two", 20), (3, "three", 30)
        ]

    async def test_the_condition_restricts_the_rows(self, engine):
        await seed(engine)
        changed = await engine.execute(
            Update(Main).set(score=0).from_(Other)
                        .where(Other.main_id == Main.id, Other.keep == True)
        )
        assert changed in ("UPDATE 1", 1)
        assert await main_rows(engine) == [(1, "a", 0), (2, "b", 20), (3, "c", 30)]

    async def test_returning_reaches_both_tables(self, engine):
        await seed(engine)
        rows = await engine.fetch_all(
            Update(Main).set(score=Main.score + 1).from_(Other)
                        .where(Other.main_id == Main.id, Other.keep == True)
                        .returning(Main.id, Other.name)
        )
        assert rows == [(1, "one")]

    async def test_expression_reading_both_tables(self, engine):
        await seed(engine)
        await engine.execute(
            Update(Main).set(tag=Main.tag.concat(Other.name)).from_(Other)
                        .where(Other.main_id == Main.id)
        )
        assert [row[1] for row in await main_rows(engine)] == [
            "aone", "btwo", "cthree"
        ]


class TestDeleteUsing:
    async def test_deletes_by_a_condition_on_the_other_table(self, engine):
        await seed(engine)
        await engine.execute(
            Delete(Main).using(Other)
                        .where(Other.main_id == Main.id, Other.keep == False)
        )
        assert await main_rows(engine) == [(1, "a", 10)]

    async def test_returning(self, engine):
        await seed(engine)
        gone = await engine.fetch_all(
            Delete(Main).using(Other)
                        .where(Other.main_id == Main.id, Other.keep == False)
                        .returning(Main.id)
        )
        assert sorted(gone) == [(2,), (3,)]


class TestCtes:
    async def test_cte_joined_to_a_table(self, engine):
        await seed(engine)
        totals = (Query(Other.main_id, count(Other.id).label("n"))
                  .group_by(Other.main_id).cte("totals"))
        rows = await engine.fetch_all(
            Query(Main.id, totals.n).join(totals, totals.main_id == Main.id)
                                    .order_by(Main.id)
        )
        assert rows == [(1, 1), (2, 1), (3, 1)]

    async def test_cte_hydrates_a_model(self, engine):
        await seed(engine)
        keepers = Query(Other.main_id).where(Other.keep == True).cte("keepers")
        rows = await engine.fetch_all(
            Query(Main).join(keepers, keepers.main_id == Main.id)
        )
        assert [(row.id, row.tag) for row in rows] == [(1, "a")]

    async def test_nested_ctes(self, engine):
        await seed(engine)
        inner = (Query(Other.main_id, count(Other.id).label("n"))
                 .group_by(Other.main_id).cte("inner_totals"))
        outer = Query(inner.main_id).where(inner.n >= 1).cte("outer_ids")
        rows = await engine.fetch_all(Query(outer.main_id).order_by("main_id"))
        assert rows == [(1,), (2,), (3,)]

    async def test_recursive_cte_generates_a_series(self, engine):
        await seed(engine)
        series = recursive_cte(
            "series",
            Query(Main.id).where(Main.id == 1),
            lambda cte: Query((cte.id + 1).label("id")).where(cte.id < 5),
        )
        rows = await engine.fetch_all(Query(series.id).order_by("id"))
        assert rows == [(1,), (2,), (3,), (4,), (5,)]

    async def test_recursive_cte_walks_a_chain(self, engine):
        # Rewire main_id into a linked list: 12 -> 11 -> 10, with 10 pointing at 1,
        # which is not an id in this table and so ends the walk. The chain has to be
        # acyclic — UNION ALL does not de-duplicate, so a cycle here is an infinite
        # loop inside Postgres rather than a failing assertion. (Learned the hard
        # way: the first version of this test pointed 12 at itself.)
        await seed(engine)
        await engine.execute(
            Update(Other).set(main_id=Other.id - 1).where(Other.id > 10)
        )
        chain = recursive_cte(
            "chain",
            Query(Other.id, Other.main_id).where(Other.id == 12),
            lambda cte: (Query(Other.id, Other.main_id)
                         .join(cte, Other.id == cte.main_id)),
        )
        rows = await engine.fetch_all(Query(chain.id).order_by("id"))
        assert rows == [(10,), (11,), (12,)]

    async def test_union_terminates_a_cyclic_recursive_cte(self, engine):
        # The counterpart: point a row at itself, and `union_all=False` still
        # terminates because UNION de-duplicates. This is the reason the flag
        # exists, and the reason the docstring says so.
        await seed(engine)
        await engine.execute(
            Update(Other).set(main_id=Other.id).where(Other.id == 12)
        )
        cyclic = recursive_cte(
            "cyclic",
            Query(Other.id, Other.main_id).where(Other.id == 12),
            lambda cte: (Query(Other.id, Other.main_id)
                         .join(cte, Other.id == cte.main_id)),
            union_all=False,
        )
        rows = await engine.fetch_all(Query(cyclic.id))
        assert rows == [(12,)]

    async def test_cte_in_a_compound_select(self, engine):
        await seed(engine)
        high = Query(Main.id).where(Main.score > 25).cte("high")
        rows = await engine.fetch_all(
            Query(high.id).union(Query(Main.id).where(Main.score < 15))
                          .order_by("id")
        )
        assert rows == [(1,), (3,)]

    async def test_cte_feeding_a_delete(self, engine):
        await seed(engine)
        doomed = Query(Other.main_id).where(Other.keep == False).cte("doomed")
        await engine.execute(
            Delete(Main).where(Main.id.in_(Query(doomed.main_id)))
        )
        assert await main_rows(engine) == [(1, "a", 10)]

    async def test_cte_feeding_an_update(self, engine):
        await seed(engine)
        keepers = Query(Other.main_id).where(Other.keep == True).cte("keepers")
        await engine.execute(
            Update(Main).set(score=0).where(Main.id.in_(Query(keepers.main_id)))
        )
        assert await main_rows(engine) == [(1, "a", 0), (2, "b", 20), (3, "c", 30)]

    async def test_cte_with_parameters_numbers_them_first(self, engine):
        # The WITH clause renders before the outer WHERE, so its parameter must be
        # $1. A driver binding these in the wrong order gives the wrong rows rather
        # than an error, which is why this asserts data and not SQL.
        await seed(engine)
        body = (Query(Main.id, Main.score).where(Main.score > 15).cte("high"))
        _sql, params = Query(body.id).where(body.score < 25).to_sql(placeholder="$")
        assert params == (15, 25)
        assert await engine.fetch_all(Query(body.id).where(body.score < 25)) == [(2,)]

    async def test_aggregate_over_a_cte(self, engine):
        await seed(engine)
        totals = (Query(Other.main_id, count(Other.id).label("n"))
                  .group_by(Other.main_id).cte("totals"))
        rows = await engine.fetch_all(Query(sum_(totals.n).label("all")))
        assert rows == [(3,)]


class TestKnownLimits:
    async def test_a_data_modifying_cte_is_not_supported(self, engine):
        # `WITH moved AS (DELETE ... RETURNING *) INSERT INTO ... SELECT * FROM moved`
        # is a real Postgres feature, and `cte()` cannot express it: it takes a
        # select, and there is no INSERT ... SELECT builder to consume it. Recorded
        # as a limit rather than left to be discovered.
        await seed(engine)
        with pytest.raises(AttributeError):
            Delete(Main).where(Main.id == 1).returning(Main).cte("moved")
