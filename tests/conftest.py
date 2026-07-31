"""Shared fixtures.

Two tiers, deliberately:

* Everything that can be tested without a server is tested without one —
  declaration, statement planning, codegen — so the suite runs anywhere.
* Engine and transaction behaviour runs against **both** sqlite and PostgreSQL
  from one parametrised `engine` fixture, because the two differ in exactly the
  place this library is most exposed: sqlite stores temporal types as strings and
  booleans as integers, postgres does not. A test that passes on only one of them
  has not tested the interesting half. PostgreSQL skips with a clear reason when
  unreachable; `--pg-required` turns that skip into a failure.

The schema is created by `engine.create_all(Base.metadata)` rather than by
hand-written `CREATE TABLE` strings. That is the first dividend of letting
SQLAlchemy own the schema, and it is also a test: if the declaration layer built
the wrong table, every fixture below fails.

Async tests use pytest-asyncio in `asyncio_mode = auto` (see pytest.ini), so an
`async def test_*` is collected with no decorator. The loop scope is per-function
on purpose: engines hold a pool bound to the loop that opened it, so sharing one
loop across tests would let a closed pool from one test be reached by another.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import os
import socket
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Mapped

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rowform as rf

PG_DSN = os.environ.get(
    "ROWFORM_TEST_DSN",
    "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable",
)


def pytest_addoption(parser):
    parser.addoption(
        "--pg-required",
        action="store_true",
        help="fail instead of skipping when PostgreSQL is unreachable",
    )


# --------------------------------------------------------------------------
# Models. Deliberately not the benchmark models: these have a foreign key and a
# nullable column so joins and NULL handling can be exercised, and keeping them
# separate means a change made for a test cannot move a published number.
# --------------------------------------------------------------------------


class Base(rf.Base):
    metadata = sa.MetaData()


class Author(Base):
    __tablename__ = "t_authors"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    name: Mapped[str]
    active: Mapped[bool]


class Book(Base):
    __tablename__ = "t_books"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    author_id: Mapped[int] = rf.mapped_column(sa.ForeignKey("t_authors.id"))
    title: Mapped[str]


class Tag(Base):
    __tablename__ = "t_tags"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    book_id: Mapped[int] = rf.mapped_column(sa.ForeignKey("t_books.id"))
    label: Mapped[str]


class Colour(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Wide(Base):
    """Every type whose driver representation differs from its Python one.

    This is the shape docs/METHODOLOGY.md correction 11 asks for: on sqlite, 8 of
    these come back as something other than what they went in as unless the right
    processor runs. `int/str/str/bool` — the old benchmark shape — is the one
    layout where that hazard is invisible.
    """

    __tablename__ = "t_wide"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    text: Mapped[str]
    flag: Mapped[bool]
    when: Mapped[dt.datetime]
    day: Mapped[dt.date]
    clock: Mapped[dt.time]
    amount: Mapped[decimal.Decimal] = rf.mapped_column(sa.Numeric(12, 3))
    ratio: Mapped[float]
    colour: Mapped[Colour]
    uid: Mapped[uuid.UUID]
    payload: Mapped[dict]
    blob: Mapped[bytes]
    note: Mapped[str | None]


AUTHORS = [
    {"id": 1, "name": "ada", "active": True},
    {"id": 2, "name": "brian", "active": True},
    {"id": 3, "name": "carol", "active": False},
    {"id": 4, "name": "dan", "active": True},  # no books -> exercises the outer join
]
BOOKS = [
    {"id": 10, "author_id": 1, "title": "structures"},
    {"id": 11, "author_id": 1, "title": "algorithms"},
    {"id": 12, "author_id": 2, "title": "compilers"},
    {"id": 13, "author_id": 3, "title": "typography"},
]
TAGS = [
    {"id": 100, "book_id": 10, "label": "classic"},
    {"id": 101, "book_id": 12, "label": "classic"},
]

WIDE_ROW = {
    "id": 1,
    "text": "hello",
    "flag": True,
    "when": dt.datetime(2024, 3, 1, 12, 30, 45, 123456),
    "day": dt.date(2024, 3, 1),
    "clock": dt.time(12, 30, 45),
    "amount": decimal.Decimal("19.990"),
    "ratio": 1.5,
    "colour": Colour.RED,
    "uid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "payload": {"a": [1, 2], "b": "x"},
    "blob": b"\x00\x01binary",
    "note": None,
}


async def seed(engine):
    """A clean schema and a known set of rows, per test.

    Drop-then-create rather than delete-the-rows: it keeps tests independent of
    each other's DDL (one of them drops everything on purpose), and it exercises
    the `create_all` path on every single test rather than once.
    """
    await engine.drop_all(Base.metadata)
    await engine.create_all(Base.metadata)
    await engine.execute_many(sa.insert(Author.__table__), AUTHORS)
    await engine.execute_many(sa.insert(Book.__table__), BOOKS)
    await engine.execute_many(sa.insert(Tag.__table__), TAGS)
    await engine.execute_many(sa.insert(Wide.__table__), [WIDE_ROW])


# --------------------------------------------------------------------------
# sqlite
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sqlite_path(tmp_path_factory):
    return str(tmp_path_factory.mktemp("rowform") / "test.sqlite3")


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------


def _pg_reachable():
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def pg_dsn(request):
    if not _pg_reachable():
        message = f"PostgreSQL not reachable for {PG_DSN}"
        if request.config.getoption("--pg-required"):
            pytest.fail(message)
        pytest.skip(message)
    return PG_DSN


# --------------------------------------------------------------------------
# The parametrised engine both halves of the suite run against
# --------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "postgres"])
async def engine(request):
    """A connected, seeded engine — once per backend.

    Parametrised rather than duplicated so a behaviour asserted here is asserted
    on a driver that decodes types natively *and* on one that does not.
    """
    if request.param == "sqlite":
        path = request.getfixturevalue("sqlite_path")
        db = rf.SqliteEngine(path)
    else:
        dsn = request.getfixturevalue("pg_dsn")
        db = rf.AsyncpgEngine(dsn)

    await db.connect()
    try:
        await seed(db)
        yield db
    finally:
        await db.close()


@pytest.fixture
async def sqlite_engine(sqlite_path):
    db = rf.SqliteEngine(sqlite_path)
    await db.connect()
    try:
        await seed(db)
        yield db
    finally:
        await db.close()
