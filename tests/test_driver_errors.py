"""What a *server* error looks like coming out of rowform, per driver.

Deliberate, and documented (`docs/GUIDE.md`, "Handling errors"): statements run on
the driver connection, so a unique violation arrives as asyncpg's or psycopg's or
pysqlite's own exception rather than `sqlalchemy.exc.IntegrityError`. Renaming it
would hide which server refused what.

Untested, though, which made it the one part of the compatibility story that
nothing would notice changing — and it is the seam an adopting application feels
first, because `except sa.exc.IntegrityError` around a write stops catching when
that write moves to rowform. So these tests pin the exact classes a caller has to
write in an `except`, alongside what SQLAlchemy raises for the same statement.

**They are characterisation tests.** If rowform ever wraps driver exceptions, this
file is what has to change, on purpose — that is the notice such a change needs,
and the reason the two sides are asserted next to each other rather than only one.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from conftest import Author

#: `(module, class)` of what each driver raises for one condition, keyed by
#: `dialect.driver`. Three spellings of "that key already exists" — which is the
#: cost being written down here, not an implementation detail.
UNIQUE_VIOLATION = {
    "aiosqlite": ("sqlite3", "IntegrityError"),
    "asyncpg": ("asyncpg.exceptions", "UniqueViolationError"),
    "psycopg": ("psycopg.errors", "UniqueViolation"),
}

#: The same, for a column the server does not have. pysqlite reports the whole
#: class of malformed statement as one error; both postgres drivers name it.
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
    assert not isinstance(error, sa.exc.SQLAlchemyError), (
        "a SQLAlchemy exception here means driver errors are being wrapped now — "
        "which is a change worth making, and this file is where it is recorded"
    )


class TestUniqueViolation:
    async def test_inside_a_scope_it_is_the_drivers_own(self, engine):
        with pytest.raises(Exception) as caught:
            async with engine.begin() as conn:
                await conn.execute(duplicate())
        assert_raised_by_the_driver(caught, UNIQUE_VIOLATION[engine.dialect.driver])

    async def test_a_one_shot_raises_the_same_way(self, engine):
        """A different checkout (`_write_connection`), so worth its own case: the
        write one-shots run through `sa_engine.begin()` and could plausibly pick
        up SQLAlchemy's wrapping on the way. They do not."""
        with pytest.raises(Exception) as caught:
            await engine.execute(duplicate())
        assert_raised_by_the_driver(caught, UNIQUE_VIOLATION[engine.dialect.driver])

    async def test_sqlalchemy_wraps_the_very_same_statement(self, engine):
        """The other half of the seam, on the same engine and the same row: what
        an application's existing `except` clause is written against today."""
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
        """The error is the driver's, but the checkout is rowform's: an aborted
        statement must not leave the pool holding a poisoned connection. psycopg
        is the one that would show it, its connection being transactional in its
        own right."""
        for _ in range(3):
            with pytest.raises(Exception):  # noqa: B017
                await engine.execute(sa.text("SELECT no_such_column FROM t_authors"))
        assert len(await engine.fetch_all(sa.select(Author))) == 4
