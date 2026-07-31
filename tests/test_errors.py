"""Every deliberate raise site raises a `RowformError`, and still raises the
builtin it used to.

Two assertions per site, and the second is the load-bearing one: the taxonomy was
added *underneath* the builtins (`rowform/errors.py`), so code written against
`except ValueError` before it existed keeps working. A subclass that quietly
dropped its builtin base would pass the first assertion and break every caller.

The declaration errors are covered by `tests/test_model.py`, which already
asserts `TypeError` at each of them; here they are re-asserted as
`DeclarationError` so the mapping is pinned in one place.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import Author, sqlite_url
from sqlalchemy.dialects.sqlite import aiosqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped

import rowform
from rowform import mapped_column


def test_every_error_is_catchable_as_one_base():
    """The point of the hierarchy: one `except` for anything rowform rejects."""
    for cls in (
        rowform.DeclarationError,
        rowform.ConfigurationError,
        rowform.UnsupportedError,
        rowform.StatementError,
        rowform.PlanError,
        rowform.EngineStateError,
    ):
        assert issubclass(cls, rowform.RowformError)


@pytest.mark.parametrize(
    ("cls", "legacy"),
    [
        (rowform.DeclarationError, TypeError),
        (rowform.ConfigurationError, TypeError),
        (rowform.ConfigurationError, ValueError),
        (rowform.UnsupportedError, NotImplementedError),
        (rowform.StatementError, ValueError),
        (rowform.PlanError, ValueError),
        (rowform.EngineStateError, RuntimeError),
    ],
)
def test_each_error_keeps_the_builtin_it_replaced(cls, legacy):
    assert issubclass(cls, legacy)


# --------------------------------------------------------------------------
# Declaration
# --------------------------------------------------------------------------


class TestDeclarationError:
    def test_unmappable_annotation(self):
        class Base(rowform.Base):
            metadata = sa.MetaData()

        with pytest.raises(rowform.DeclarationError, match="no SQLAlchemy type registered"):

            class Bad(Base):
                __tablename__ = "err_unmappable"

                id: Mapped[int] = mapped_column(primary_key=True)
                nope: Mapped[complex]

    def test_an_alias_of_something_that_is_not_the_model(self):
        """`alias(of=...)` refuses a from clause whose columns are not exactly the
        model's, and that refusal is part of the same hierarchy."""
        class Base(rowform.Base):
            metadata = sa.MetaData()

        class Row(Base):
            __tablename__ = "err_alias"

            id: Mapped[int] = mapped_column(primary_key=True)
            name: Mapped[str]

        wider = sa.select(Row, sa.literal(1).label("extra")).subquery()
        with pytest.raises(rowform.DeclarationError, match="exactly that model's columns"):
            rowform.alias(Row, of=wider)

        with pytest.raises(rowform.DeclarationError, match="A Select becomes one with"):
            rowform.alias(Row, of=sa.select(Row))

    def test_selecting_an_abstract_class(self):
        class Base(rowform.Base):
            metadata = sa.MetaData()

        class Mixin(Base):
            name: Mapped[str]

        with pytest.raises(rowform.DeclarationError, match="abstract"):
            sa.select(Mixin)


# --------------------------------------------------------------------------
# Configuration and unsupported options
# --------------------------------------------------------------------------


class TestConfigurationError:
    def test_something_that_is_not_an_async_engine(self, sqlite_path):
        with pytest.raises(rowform.ConfigurationError, match="AsyncEngine"):
            rowform.Engine(sqlite_path)  # pyright: ignore[reportArgumentType]

    def test_an_unsupported_driver(self):
        """A dialect rowform has no execution primitives for. Named rather than
        failing later on a connection whose methods are not the expected ones."""
        engine = create_async_engine("sqlite+aiosqlite://")
        engine.dialect.driver = "pysqlite-but-imaginary"
        with pytest.raises(rowform.ConfigurationError, match="no rowform driver"):
            rowform.Engine(engine)

    def test_an_impossible_cache_size(self, sqlite_path):
        with pytest.raises(rowform.ConfigurationError, match="cache_size"):
            rowform.Engine(create_async_engine(sqlite_url(sqlite_path)), cache_size=0)


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


class TestStatementError:
    async def test_execute_now_accepts_a_statement_that_returns_rows(self, engine):
        """It used to refuse them, because it could only report a rowcount. On
        the compatibility track it returns a `Result` like SQLAlchemy's, so the
        rows are there to be taken."""
        result = await engine.execute(sa.select(Author))
        assert len(result.scalars().all()) == 4

    async def test_fetch_all_refuses_a_statement_that_returns_none(self, engine):
        statement = sa.insert(Author.__table__).values(id=9001, name="ada", active=True)
        with pytest.raises(rowform.StatementError, match="produces no rows"):
            await engine.fetch_all(statement)

    async def test_a_write_result_refuses_to_be_read(self, engine):
        """The inverse guard, and now SQLAlchemy's own error: a statement with no
        result set gives a closed `Result`, so asking for its rows raises rather
        than returning [] and reading as "nothing matched"."""
        result = await engine.execute(
            sa.insert(Author.__table__).values(id=9002, name="z", active=True)
        )
        assert result.rowcount == 1
        with pytest.raises(sa.exc.ResourceClosedError):
            result.all()


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


class TestPlanError:
    def test_a_statement_selecting_nothing(self):
        with pytest.raises(rowform.PlanError, match="at least one column"):
            rowform.plan(sa.select())

    def test_a_result_whose_width_disagrees_with_the_plan(self):
        """The mis-assignment guard: two planned columns, one described, so
        hydrating would write the wrong field."""
        statement = sa.select(Author.id, Author.name)
        dialect = aiosqlite.dialect()
        with pytest.raises(rowform.PlanError, match="refusing to hydrate"):
            rowform.compile_hydrator(rowform.plan(statement), dialect, [None])


# --------------------------------------------------------------------------
# Engine state
# --------------------------------------------------------------------------


class TestEngineStateError:
    async def test_engine_read_inside_a_transaction(self, engine):
        async with engine.begin():
            with pytest.raises(rowform.EngineStateError, match="different pooled connection"):
                await engine.fetch_all(sa.select(Author))


# --------------------------------------------------------------------------
# What is *not* wrapped
# --------------------------------------------------------------------------


async def test_driver_errors_are_not_rewritten(engine):
    """A constraint violation is the driver's own exception, and stays that way:
    wrapping it would hide which server refused what (`rowform/errors.py`)."""
    await engine.execute(
        sa.insert(Author.__table__).values(id=9100, name="ada", active=True)
    )
    with pytest.raises(Exception) as caught:
        await engine.execute(
            sa.insert(Author.__table__).values(id=9100, name="grace", active=True)
        )
    assert not isinstance(caught.value, rowform.RowformError)
