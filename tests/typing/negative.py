"""Mistakes that must be type errors. Checked by basedpyright; never run.

Every line below carries a `# pyright: ignore[...]`. That is what makes this a
*test* rather than a comment: the checker runs with
`reportUnnecessaryTypeIgnoreComment = "error"`, so if any of these stops being an
error, the now-unnecessary suppression fails the run.

A checker reporting nothing on `positive.py` proves the good cases work. Only
this file proves the bad cases are caught.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped

import rowform as rf


class Base(rf.Base):
    metadata = sa.MetaData()


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    name: Mapped[str]
    active: Mapped[bool]


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    title: Mapped[str]


engine = rf.Engine(create_async_engine("sqlite+aiosqlite:///app.db"))


# --- construction ----------------------------------------------------------
Author(id="one", name="ada", active=True)  # pyright: ignore[reportArgumentType]
Author(id=1, name=2, active=True)  # pyright: ignore[reportArgumentType]
Author(id=1, name="ada")  # pyright: ignore[reportCallIssue]
Author(id=1, name="ada", active=True, nope=1)  # pyright: ignore[reportCallIssue]

author = Author(id=1, name="ada", active=True)

# --- instance attributes ---------------------------------------------------
author.id = "one"  # pyright: ignore[reportAttributeAccessIssue]
author.missing  # pyright: ignore[reportAttributeAccessIssue]

reveal: int = author.name  # pyright: ignore[reportAssignmentType]
also: str = author.id  # pyright: ignore[reportAssignmentType]

# --- class-level expressions ----------------------------------------------
_ = Author.missing  # pyright: ignore[reportAttributeAccessIssue]


# --- an alias exposes the model's fields, and only those -------------------
other = rf.alias(Author, "a2")

_ = other.missing  # pyright: ignore[reportAttributeAccessIssue]


# --- result types are not interchangeable ----------------------------------
async def reads() -> None:
    # One selected model is a list of models, not a list of tuples.
    rows: list[tuple[Author]] = await engine.fetch_all(  # pyright: ignore[reportAssignmentType]
        sa.select(Author)
    )

    # ...and two selected models are tuples, not bare models.
    pairs: list[Author] = await engine.fetch_all(  # pyright: ignore[reportAssignmentType]
        sa.select(Author, Book)
    )

    # The row type follows the statement, so the wrong model is caught.
    books: list[Book] = await engine.fetch_all(  # pyright: ignore[reportAssignmentType]
        sa.select(Author)
    )

    # A scalar select is a list of that scalar, not of the model.
    names: list[Author] = await engine.fetch_all(  # pyright: ignore[reportAssignmentType]
        sa.select(Author.name)
    )

    # fetch_one can be None.
    one: Author = await engine.fetch_one(  # pyright: ignore[reportAssignmentType]
        sa.select(Author)
    )

    # An alias carries the model it aliases, so a self-join is pairs of Author.
    selves: list[tuple[Author, Book]] = await engine.fetch_all(  # pyright: ignore[reportAssignmentType]
        sa.select(Author, other)
    )

    # A hoisted query is typed by what it was prepared from.
    hoisted = engine.prepare(sa.select(Book))
    wrong: list[Author] = await engine.fetch_all(  # pyright: ignore[reportAssignmentType]
        hoisted
    )


async def streams() -> None:
    # fetch_iter follows the same rule, so the loop variable is not interchangeable
    # either — and it is an async iterator, never awaited.
    async for author in engine.fetch_iter(sa.select(Author)):
        book: Book = author  # pyright: ignore[reportAssignmentType]

    async for pair in engine.fetch_iter(sa.select(Author, Book)):
        only: Author = pair  # pyright: ignore[reportAssignmentType]

    rows = await engine.fetch_iter(sa.select(Author))  # pyright: ignore[reportGeneralTypeIssues]


# --- declaration ------------------------------------------------------------
# `frozen` is declared on the Base as well: a checker treats every model under a
# Base as sharing its dataclass configuration, and refuses a frozen class
# inheriting from a non-frozen one. Stdlib dataclasses say the same thing.
class FrozenBase(rf.Base, frozen=True):
    metadata = sa.MetaData()


class Frozen(FrozenBase, frozen=True):
    __tablename__ = "frozen"

    id: Mapped[int] = rf.mapped_column(primary_key=True)


Frozen(id=1).id = 2  # pyright: ignore[reportAttributeAccessIssue]


class Timestamped(Base):
    created: Mapped[dt.datetime]


class Review(Timestamped, kw_only=True):
    __tablename__ = "reviews"

    id: Mapped[int] = rf.mapped_column(primary_key=True)


# kw_only means positional construction is refused, and the inherited field is
# still required.
Review(dt.datetime(2024, 1, 1), 1)  # pyright: ignore[reportCallIssue]
Review(id=1)  # pyright: ignore[reportCallIssue]
