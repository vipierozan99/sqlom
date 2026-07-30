"""Transactions and savepoints, on both backends."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import Author, Book, Tag

import rowform


class Boom(Exception):
    """Raised on purpose, so a rollback test cannot pass by accident."""


def count_authors():
    return sa.select(sa.func.count()).select_from(Author)


class TestAtomicity:
    async def test_commit_persists_every_statement(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(sa.insert(Author.__table__).values(id=50, name="x", active=True))
            await tx.execute(sa.insert(Author.__table__).values(id=51, name="y", active=True))
        assert await engine.fetch_value(count_authors()) == 6

    async def test_an_exception_rolls_the_whole_block_back(self, engine):
        with pytest.raises(Boom):
            async with engine.transaction() as tx:
                await tx.execute(
                    sa.insert(Author.__table__).values(id=50, name="x", active=True)
                )
                raise Boom
        assert await engine.fetch_value(count_authors()) == 4

    async def test_the_exception_is_not_swallowed(self, engine):
        with pytest.raises(Boom):
            async with engine.transaction():
                raise Boom


class TestVisibility:
    async def test_reads_its_own_uncommitted_writes(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(sa.insert(Author.__table__).values(id=50, name="x", active=True))
            assert await tx.fetch_value(count_authors()) == 5

    async def test_invisible_to_other_connections_until_commit(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(sa.insert(Author.__table__).values(id=50, name="x", active=True))
            async with engine.acquire() as conn:
                rows, _ = await engine._fetch(
                    conn, "SELECT count(*) FROM t_authors", (), False
                )
                assert rows[0][0] == 4


class TestReadsInsideTransactions:
    async def test_fetch_all_hydrates_models(self, engine):
        async with engine.transaction() as tx:
            authors = await tx.fetch_all(sa.select(Author).order_by(Author.id))
        assert [a.name for a in authors] == ["ada", "brian", "carol", "dan"]

    async def test_joins_work_inside_a_transaction(self, engine):
        async with engine.transaction() as tx:
            rows = await tx.fetch_all(
                sa.select(Author, Book).outerjoin(Book).order_by(Author.id, Book.id)
            )
        assert {a.name: b for a, b in rows}["dan"] is None

    async def test_fetch_one_and_fetch_value(self, engine):
        async with engine.transaction() as tx:
            assert (await tx.fetch_one(sa.select(Author).order_by(Author.id))).name == "ada"
            assert await tx.fetch_value(count_authors()) == 4

    async def test_the_engine_and_transaction_share_compiled_queries(self, engine):
        statement = sa.select(Author).order_by(Author.id)
        await engine.fetch_all(statement)
        compiled = dict(engine._queries)
        async with engine.transaction() as tx:
            await tx.fetch_all(statement)
        assert engine._queries == compiled, "the transaction recompiled the statement"

    async def test_a_raw_string_runs_on_the_pinned_connection(self, engine):
        async with engine.transaction() as tx:
            await tx.execute("DELETE FROM t_tags")
            assert await tx.fetch_value(sa.select(sa.func.count()).select_from(Tag)) == 0

    async def test_execute_many_inside_a_transaction(self, engine):
        async with engine.transaction() as tx:
            await tx.execute_many(
                sa.insert(Tag.__table__),
                [{"id": 300 + i, "book_id": 10, "label": "l"} for i in range(3)],
            )
        assert await engine.fetch_value(sa.select(sa.func.count()).select_from(Tag)) == 5

    async def test_execute_refuses_a_statement_that_returns_rows(self, engine):
        async with engine.transaction() as tx:
            with pytest.raises(ValueError, match="produces rows"):
                await tx.execute(sa.select(Author))


class TestSavepoints:
    async def test_a_nested_block_reports_its_depth(self, engine):
        async with engine.transaction() as tx:
            assert tx.depth == 0
            async with tx.transaction() as sp:
                assert sp.depth == 1
                assert "savepoint depth=1" in repr(sp)

    async def test_inner_failure_keeps_the_outer_work(self, engine):
        async with engine.transaction() as tx:
            await tx.execute(sa.insert(Author.__table__).values(id=50, name="x", active=True))
            with pytest.raises(Boom):
                async with tx.transaction() as sp:
                    await sp.execute(
                        sa.insert(Author.__table__).values(id=51, name="y", active=True)
                    )
                    raise Boom
            assert await tx.fetch_value(count_authors()) == 5
        assert await engine.fetch_value(count_authors()) == 5

    async def test_inner_success_is_kept(self, engine):
        async with engine.transaction() as tx, tx.transaction() as sp:
            await sp.execute(sa.insert(Author.__table__).values(id=50, name="x", active=True))
        assert await engine.fetch_value(count_authors()) == 5

    async def test_outer_failure_discards_a_released_savepoint(self, engine):
        with pytest.raises(Boom):
            async with engine.transaction() as tx:
                async with tx.transaction() as sp:
                    await sp.execute(
                        sa.insert(Author.__table__).values(id=50, name="x", active=True)
                    )
                raise Boom
        assert await engine.fetch_value(count_authors()) == 4


class TestTheFootgunGuard:
    """`engine.fetch_all()` inside `engine.transaction()` would take a *different*
    pooled connection, miss the uncommitted writes, and not roll back with the
    block — producing plausible wrong results rather than an error."""

    async def test_engine_fetch_all_inside_a_transaction_raises(self, engine):
        async with engine.transaction():
            with pytest.raises(RuntimeError, match="different pooled connection"):
                await engine.fetch_all(sa.select(Author))

    async def test_the_engine_works_again_after_the_block(self, engine):
        async with engine.transaction():
            pass
        assert len(await engine.fetch_all(sa.select(Author))) == 4

    async def test_the_guard_is_scoped_to_the_same_engine(self, engine, sqlite_path):
        other = rowform.SqliteEngine(sqlite_path)
        await other.connect()
        try:
            async with engine.transaction():
                assert await other.fetch_all(sa.select(Author)) is not None
        finally:
            await other.close()

    async def test_active_transaction_tracks_the_innermost_block(self, engine):
        assert rowform.active_transaction() is None
        async with engine.transaction() as tx:
            assert rowform.active_transaction() is tx
            async with tx.transaction() as sp:
                assert rowform.active_transaction() is sp
            assert rowform.active_transaction() is tx
        assert rowform.active_transaction() is None


class TestSqliteRefusesWhatItCannotDo:
    async def test_isolation_levels_raise_rather_than_no_op(self, sqlite_engine):
        with pytest.raises(NotImplementedError, match="no session-level isolation"):
            async with sqlite_engine.transaction(isolation="serializable"):
                pass

    async def test_readonly_raises(self, sqlite_engine):
        with pytest.raises(NotImplementedError):
            async with sqlite_engine.transaction(readonly=True):
                pass

    async def test_an_unset_option_is_not_treated_as_a_request(self, sqlite_engine):
        async with sqlite_engine.transaction(isolation=None, readonly=False) as tx:
            assert tx.depth == 0
