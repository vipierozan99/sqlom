"""Shared fixtures.

Two tiers, deliberately:

* Everything that can be tested without a server is tested without one — SQL
  generation, codegen, validation — so the suite runs anywhere.
* Engine and transaction behaviour needs real PostgreSQL. Those tests skip with a
  clear reason rather than failing, and `--pg-required` turns the skip into a
  failure for CI where a server is expected.

Async tests use pytest-asyncio in `asyncio_mode = auto` (see pytest.ini), so an
`async def test_*` is collected with no decorator. The loop scope is per-function
on purpose: both engines hold a pool bound to the loop that opened it, so sharing
one loop across tests would let a closed pool from one test be reached by another.
"""

import os
import socket
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlom import Column, ModelMeta, model  # noqa: E402

PG_DSN = os.environ.get(
    "SQLOM_TEST_DSN",
    "postgresql://postgres:postgres@127.0.0.1:5432/sqlom_bench?sslmode=disable",
)


def pytest_addoption(parser):
    parser.addoption(
        "--pg-required", action="store_true",
        help="fail instead of skipping when PostgreSQL is unreachable",
    )


# --------------------------------------------------------------------------
# Models. Deliberately not the benchmark models: these have a foreign key and a
# nullable column so joins and NULL handling can be exercised, and keeping them
# separate means a change made for a test cannot move a published number.
# --------------------------------------------------------------------------


class Author(metaclass=ModelMeta):
    __tablename__ = "t_authors"

    id = Column(int)
    name = Column(str)
    active = Column(bool)


class Book(metaclass=ModelMeta):
    __tablename__ = "t_books"

    id = Column(int)
    author_id = Column(int)
    title = Column(str)


class Tag(metaclass=ModelMeta):
    __tablename__ = "t_tags"

    id = Column(int)
    book_id = Column(int)
    label = Column(str)


@model
class AuthorDC:
    __tablename__ = "t_authors"

    id: int
    name: str
    active: bool


DDL = [
    "CREATE TABLE t_authors (id INTEGER PRIMARY KEY, name TEXT, active INTEGER)",
    "CREATE TABLE t_books (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT)",
    "CREATE TABLE t_tags (id INTEGER PRIMARY KEY, book_id INTEGER, label TEXT)",
]

AUTHORS = [
    (1, "ada", 1),
    (2, "brian", 1),
    (3, "carol", 0),   # inactive
    (4, "dan", 1),     # no books -> exercises the outer join
]
BOOKS = [
    (10, 1, "structures"),
    (11, 1, "algorithms"),
    (12, 2, "compilers"),
    (13, 3, "typography"),
]
TAGS = [
    (100, 10, "classic"),
    (101, 12, "classic"),
]


@pytest.fixture(scope="session")
def sqlite_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("sqlom") / "test.sqlite3"
    conn = sqlite3.connect(path)
    for statement in DDL:
        conn.execute(statement)
    conn.executemany("INSERT INTO t_authors VALUES (?, ?, ?)", AUTHORS)
    conn.executemany("INSERT INTO t_books VALUES (?, ?, ?)", BOOKS)
    conn.executemany("INSERT INTO t_tags VALUES (?, ?, ?)", TAGS)
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def db(sqlite_path):
    """A sqlite connection to the seeded schema."""
    conn = sqlite3.connect(sqlite_path)
    yield conn
    conn.close()


@pytest.fixture
def run_query(db):
    """Execute a Query against sqlite and hydrate it, the way an engine would.

    sqlom has no sqlite engine class — the benchmarks drive `to_sql()` plus a
    compiled hydrator directly — so this mirrors exactly that, which means these
    tests exercise the real generated code rather than a test-only path.
    """
    from sqlom import SQLITE_CONVERTERS, compile_batch_hydrator, compile_join_hydrator

    def _run(query):
        sql, params = query.to_sql(placeholder="?")
        rows = db.execute(sql, params).fetchall()
        # Mirrors the engines' dispatch exactly: key shape decides.
        if isinstance(query._hydration_key, tuple):
            hydrate = compile_join_hydrator(
                query.hydration_spec(), SQLITE_CONVERTERS,
                wrap=query.is_multi_entity,
            )
        else:
            hydrate = compile_batch_hydrator(query.model, SQLITE_CONVERTERS)
        return hydrate(rows)

    return _run


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


@pytest.fixture(scope="session")
def pg_schema(pg_dsn):
    """Create and seed the test tables on PostgreSQL, then drop them.

    Uses its own `t_`-prefixed tables so it cannot disturb the 200k-row `users`
    table the benchmarks measure against.
    """
    import asyncio

    import asyncpg

    ddl = [
        "DROP TABLE IF EXISTS t_tags, t_books, t_authors",
        "CREATE TABLE t_authors (id int PRIMARY KEY, name text, active boolean)",
        "CREATE TABLE t_books (id int PRIMARY KEY, author_id int, title text)",
        "CREATE TABLE t_tags (id int PRIMARY KEY, book_id int, label text)",
    ]

    async def setup():
        conn = await asyncpg.connect(pg_dsn)
        try:
            for statement in ddl:
                await conn.execute(statement)
            await conn.executemany(
                "INSERT INTO t_authors VALUES ($1, $2, $3)",
                [(i, n, bool(a)) for i, n, a in AUTHORS],
            )
            await conn.executemany("INSERT INTO t_books VALUES ($1, $2, $3)", BOOKS)
            await conn.executemany("INSERT INTO t_tags VALUES ($1, $2, $3)", TAGS)
        finally:
            await conn.close()

    async def teardown():
        conn = await asyncpg.connect(pg_dsn)
        try:
            await conn.execute("DROP TABLE IF EXISTS t_tags, t_books, t_authors")
        finally:
            await conn.close()

    asyncio.run(setup())
    yield pg_dsn
    asyncio.run(teardown())


# --------------------------------------------------------------------------
# Multi-dialect test helper
# --------------------------------------------------------------------------


def assert_dialect_sql(built, *, sqlite=None, postgres=None, params=None):
    """Assert something with `.to_sql(placeholder=, dialect=)` (a `Query`, a
    `CompoundSelect`, an Insert/Update/Delete) renders as expected under one
    or both dialects — pass whichever of `sqlite=`/`postgres=` you want
    checked. `params`, if given, is asserted identically for every dialect
    checked: sqlom's dialects only ever differ in keyword spelling, never in
    which values get bound or in what order.
    """
    from sqlom import POSTGRES, SQLITE

    if sqlite is not None:
        sql, bound = built.to_sql(dialect=SQLITE)
        assert sql == sqlite
        if params is not None:
            assert bound == params
    if postgres is not None:
        sql, bound = built.to_sql(dialect=POSTGRES)
        assert sql == postgres
        if params is not None:
            assert bound == params
