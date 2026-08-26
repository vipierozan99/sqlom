"""Server errors arrive as each driver's own exception, never `sa.exc.IntegrityError`.

Characterisation: an `except sa.exc.IntegrityError` stops catching a write once it
moves to rowform, and this file is what changes if rowform ever wraps them.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from conftest import Author

#: `(module, class)` each driver raises, keyed by `dialect.driver`.
UNIQUE_VIOLATION = {
    "aiosqlite": ("sqlite3", "IntegrityError"),
    "asyncpg": ("asyncpg.exceptions", "UniqueViolationError"),
    "psycopg": ("psycopg.errors", "UniqueViolation"),
}

UNDEFINED_COLUMN = {
    "aiosqlite": ("sqlite3", "OperationalError"),
    "asyncpg": ("asyncpg.exceptions", "UndefinedColumnError"),
    "psycopg": ("psycopg.errors", "UndefinedColumn"),
}


def duplicate() -> Any:
    return sa.insert(Author.__table__).values(id=1, name="already-taken", active=True)


def assert_raised_by_the_driver(caught, expected: tuple[str, str]) -> None:
    error = caught.value
    assert (type(error).__module__, type(error).__name__) == expected
    assert not isinstance(error, sa.exc.SQLAlchemyError), "driver errors are wrapped now"


class TestUniqueViolation:
    async def test_inside_a_scope_it_is_the_drivers_own(self, engine):
        with pytest.raises(Exception) as caught:
            async with engine.begin() as conn:
                await conn.execute(duplicate())
        assert_raised_by_the_driver(caught, UNIQUE_VIOLATION[engine.dialect.driver])

    async def test_a_one_shot_raises_the_same_way(self, engine):
        """Write one-shots go through `sa_engine.begin()`, which could have wrapped it."""
        with pytest.raises(Exception) as caught:
            await engine.execute(duplicate())
        assert_raised_by_the_driver(caught, UNIQUE_VIOLATION[engine.dialect.driver])

    async def test_sqlalchemy_wraps_the_very_same_statement(self, engine):
        """What an application's existing `except` clause is written against."""
        with pytest.raises(sa.exc.IntegrityError):
            async with engine.sa_engine.begin() as sa_conn:
                await sa_conn.execute(duplicate())


class TestMalformedStatement:
    async def test_a_read_of_a_column_that_does_not_exist(self, engine):
        missing = sa.select(sa.column("nope")).select_from(sa.table("t_authors"))
        with pytest.raises(Exception) as caught:
            await engine.fetch_all(missing)
        assert_raised_by_the_driver(caught, UNDEFINED_COLUMN[engine.dialect.driver])

    async def test_the_engine_is_still_usable_afterwards(self, engine):
        """An aborted statement must not leave a poisoned connection in the pool."""
        for _ in range(3):
            with pytest.raises(Exception):  # noqa: B017
                await engine.execute(sa.text("SELECT no_such_column FROM t_authors"))
        assert len(await engine.fetch_all(sa.select(Author))) == 4
