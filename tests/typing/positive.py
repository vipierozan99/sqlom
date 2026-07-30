"""Types that must be inferred exactly. Checked by basedpyright; never run.

`assert_type` compares for exact equality, so a type that has decayed to `Any`
fails here — which is the failure mode worth guarding, since `Any` silently makes
every other assertion pass.

The whole reason the declaration layer is a base class rather than a decorator is
this file: a decorator *factory*, which is what `@model(metadata)` would have to
be, erases every field type to `Any` and each `assert_type` below would fail.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any, assert_type

import sqlalchemy as sa
from sqlalchemy.orm import InstrumentedAttribute, Mapped

import rowform
from rowform import CoreQuery, mapped_column


class Base(rowform.Base):
    metadata = sa.MetaData()


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    active: Mapped[bool]
    born: Mapped[dt.date | None]


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(sa.ForeignKey("authors.id"))
    title: Mapped[str]
    price: Mapped[decimal.Decimal]
    uid: Mapped[uuid.UUID]


author = Author(id=1, name="ada", active=True, born=None)

# --- instance attributes keep their declared types -------------------------
assert_type(author.id, int)
assert_type(author.name, str)
assert_type(author.active, bool)
assert_type(author.born, "dt.date | None")

# --- class attributes are SQL expressions ----------------------------------
# Declared as InstrumentedAttribute, an sa.Column at runtime. Both are
# ColumnOperators, which is why the comparisons below work; the mismatch is an
# inherited type-level fiction from Mapped.__get__'s overloads.
assert_type(Author.id, InstrumentedAttribute[int])
assert_type(Author.name, InstrumentedAttribute[str])

assert_type(Author.id > 100, sa.ColumnElement[bool])
assert_type(Author.name == "ada", sa.ColumnElement[bool])
assert_type(Author.id.in_([1, 2]), sa.BinaryExpression[bool])

# --- statements carry their selected types ---------------------------------
assert_type(sa.select(Author), sa.Select[tuple[Author]])
assert_type(sa.select(Author.name), sa.Select[tuple[str]])
assert_type(sa.select(Author, Book), sa.Select[tuple[Author, Book]])
assert_type(sa.select(Author, Book.title), sa.Select[tuple[Author, str]])
assert_type(sa.select(Author).where(Author.id > 1), sa.Select[tuple[Author]])

# --- an alias types exactly as the model it aliases ------------------------
# The whole reason `alias()` is declared `type[_M]`: an alias class of its own
# could only expose `__getattr__`, and every field below would be `Any`.
manager = rowform.alias(Author, "mgr")

assert_type(manager.id, InstrumentedAttribute[int])
assert_type(manager.born, InstrumentedAttribute["dt.date | None"])
assert_type(manager.id > 100, sa.ColumnElement[bool])
assert_type(sa.select(manager), sa.Select[tuple[Author]])
assert_type(sa.select(Author, manager), sa.Select[tuple[Author, Author]])

# A declared subquery or CTE is the model too — which is the whole point of
# `of=` refusing anything but that model's exact columns.
recent = rowform.alias(Author, of=sa.select(Author).limit(10).subquery())

assert_type(recent.name, InstrumentedAttribute[str])
assert_type(sa.select(recent), sa.Select[tuple[Author]])


# --- prepare() and fetch_all() preserve them -------------------------------
engine = rowform.SqliteEngine("app.db")

assert_type(engine.prepare(sa.select(Author)), CoreQuery[Author])
assert_type(engine.prepare(sa.select(Author.name)), CoreQuery[str])
assert_type(engine.prepare(sa.select(Author, Book)), CoreQuery[tuple[Author, Book]])


async def reads() -> None:
    # One selected entity yields that entity, model or scalar.
    assert_type(await engine.fetch_all(sa.select(Author)), list[Author])
    assert_type(await engine.fetch_all(sa.select(Author.name)), list[str])
    assert_type(await engine.fetch_all(sa.select(Book.price)), list[decimal.Decimal])
    assert_type(await engine.fetch_all(sa.select(Book.uid)), list[uuid.UUID])

    # Two or more yield a tuple, in select order.
    assert_type(await engine.fetch_all(sa.select(Author, Book)), list[tuple[Author, Book]])
    assert_type(
        await engine.fetch_all(sa.select(Author.name, Author.id)), list[tuple[str, int]]
    )
    assert_type(
        await engine.fetch_all(sa.select(Author, Book, Book.title)),
        list[tuple[Author, Book, str]],
    )

    # A hoisted query keeps its row type.
    hoisted = engine.prepare(sa.select(Author).where(Author.id > sa.bindparam("floor")))
    assert_type(await engine.fetch_all(hoisted, floor=3), list[Author])

    assert_type(await engine.fetch_one(sa.select(Author)), "Author | None")
    assert_type(await engine.fetch_one(sa.select(Author.name)), "str | None")

    # A self-join is a list of pairs of the model, with no cast.
    assert_type(
        await engine.fetch_all(sa.select(Author, manager).join(manager, Author.id < manager.id)),
        list[tuple[Author, Author]],
    )
    assert_type(await engine.fetch_all(sa.select(recent)), list[Author])


async def transactions() -> None:
    async with engine.transaction() as tx:
        assert_type(tx.depth, int)
        assert_type(rowform.active_transaction(), "rowform.Transaction | None")


# --- schema surface --------------------------------------------------------
assert_type(Base.metadata, sa.MetaData)
assert_type(Author.__table__, sa.Table)
assert_type(Author.__tablename__, str)
assert_type(Author.__column_order__, tuple[str, ...])


# --- mixins ----------------------------------------------------------------
class Timestamped(Base):
    created: Mapped[dt.datetime]


class Review(Timestamped, kw_only=True):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str]


# A mixin's fields are real constructor parameters, not merely visible
# attributes — the metaclass is shared, so the checker sees one dataclass.
review = Review(created=dt.datetime(2024, 1, 1), id=1, body="good")
assert_type(review.created, dt.datetime)
assert_type(review.body, str)


# --- what is deliberately Any ----------------------------------------------
async def wide() -> None:
    # Past four selected entities the row degrades, rather than growing
    # overloads without end.
    assert_type(
        await engine.fetch_all(
            sa.select(Author.id, Author.name, Author.active, Author.born, Book.title)
        ),
        list[Any],
    )

    # A write's result is the driver's own answer — an int on sqlite and
    # psycopg, a status string on asyncpg — so it is not normalised.
    assert_type(await engine.execute(sa.insert(Author.__table__)), Any)
