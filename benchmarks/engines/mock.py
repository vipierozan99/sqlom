"""`MockEngine`: the mapper's floor, with zero driver cost (PLAN.md D8/D9, §7).

`DatabaseEngine.fetch_all` (`rowform/engine.py`) touches the driver in exactly
one place:

    async with self._require_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)

`MockEngine` overrides **only `_require_pool()`**, so SQL generation, param
binding, hydrator selection and hydration all run byte-for-byte identical to
production — including the per-request `to_sql()` call that exposed a real 4%
self-inflicted regression in the shipped engine.

Rows are precomputed plain tuples (rowform is always positional), so this
measures the mapper with *no* driver term. Its absolutes are therefore not
comparable to sqlite/Postgres numbers — it is a mapper instrument only.

(D9) `mock_sqlalchemy_engine()` is the equivalent seam for SQLAlchemy: it
fakes aiosqlite instead of asyncpg, one layer further down than
`MockEngine` — SQL compilation, Core result processing and ORM hydration
all run for real; only the SQLite call is canned. It must sit at the driver
seam rather than `engine.connect()`/`execute()` because ORM hydration needs
a genuine `CursorResult` (row identity, type processors, entity loading),
not a hand-rolled stand-in. Pairing it against rowform's mock would still
put different work inside the two timed regions (correction 6), so each is
its own mapper-floor instrument, not a cross-mapper comparison.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from rowform.engine import DatabaseEngine


class _FakeConnection:
    """`fetch()` yields the same precomputed rows regardless of `sql`/`params`
    — MockEngine measures one query shape's hydration cost, not query
    correctness (that's what the real-engine tests are for)."""

    def __init__(self, rows: list[tuple[Any, ...]]):
        self._rows = rows

    async def fetch(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        return self._rows

    async def fetchval(self, sql: str, *params: Any) -> Any:
        return None

    async def execute(self, sql: str, *params: Any) -> str:
        return "MOCK 0"


class _FakeAcquire:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakePool:
    def __init__(self, rows: list[tuple[Any, ...]]):
        self._conn = _FakeConnection(rows)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _MockAioCursor:
    """Stands in for aiosqlite's cursor at the DBAPI seam the `sqlite+aiosqlite`
    dialect talks to (`sqlalchemy.dialects.sqlite.aiosqlite`) — everything
    above this (statement compilation, param binding, Core result
    processing, ORM hydration) runs for real; only the actual SQLite call is
    canned, the same floor `_FakeConnection.fetch()` gives rowform.

    One exception: `SQLiteDialect.initialize()` runs `PRAGMA read_uncommitted`
    against the first real connection to detect the isolation level, before
    any application query — canned rows there would hand back the wrong
    shape and crash dialect bootstrap (`assert False, "Unknown isolation
    level %s"`), so that one dialect-internal query is answered for real
    (`0` = SQLite's actual default, "SERIALIZABLE") instead of canned.
    """

    def __init__(self, columns: Sequence[str], rows: list[tuple[Any, ...]]):
        self._columns = columns
        self._rows = rows
        self.description: list[tuple[Any, ...]] | None = None
        self._result_rows: list[tuple[Any, ...]] = []
        self.lastrowid = -1
        self.rowcount = -1

    async def execute(self, operation: str, parameters: Any = None) -> None:
        if operation == "PRAGMA read_uncommitted":
            self.description = [("read_uncommitted", None, None, None, None, None, None)]
            self._result_rows = [(0,)]
        else:
            self.description = [(name, None, None, None, None, None, None) for name in self._columns]
            self._result_rows = self._rows

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result_rows

    async def close(self) -> None:
        return None


class _MockAioConnection:
    """Stands in for aiosqlite's `Connection`. `isolation_level = None` marks
    it autocommit at the DBAPI level (the pysqlite dialect's
    `detect_autocommit_setting`), which is why sqlite's transaction emulation
    calls `execute()` (raw `"BEGIN"`/`"COMMIT"`) straight on the connection
    instead of through a cursor — both `execute()` and `create_function()`
    (the `regexp`/`floor` UDFs the dialect registers `on_connect`) are
    accepted here and ignored."""

    isolation_level: str | None = None

    def __init__(self, columns: Sequence[str], rows: list[tuple[Any, ...]]):
        self._columns = columns
        self._rows = rows

    async def cursor(self) -> _MockAioCursor:
        return _MockAioCursor(self._columns, self._rows)

    async def create_function(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


def mock_sqlalchemy_engine(columns: Sequence[str], rows: list[tuple[Any, ...]]) -> AsyncEngine:
    """A real `AsyncEngine` — not a lookalike — whose driver is faked at the
    aiosqlite seam via `connect_args={"async_creator_fn": ...}`
    (`AsyncAdapt_aiosqlite_dbapi.connect()` special-cases this key, see that
    module). SQL compilation, Core result processing and ORM hydration all
    run byte-for-byte identical to production; only the actual SQLite call
    is canned — the SQLAlchemy-side counterpart to `MockEngine` above.

    `columns` must match `rows`' column order: there's no query to
    introspect at this seam, only opaque compiled SQL text and params, which
    are ignored.
    """

    def async_creator_fn(*args: Any, **kwargs: Any) -> Any:
        async def _connect() -> _MockAioConnection:
            return _MockAioConnection(columns, rows)

        return _connect()

    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"async_creator_fn": async_creator_fn},
    )


class MockEngine(DatabaseEngine):
    """A `DatabaseEngine` whose pool is fake — see module docstring.

    `rows` must already be shaped the way asyncpg would hand them back (real
    Python `bool`s, not sqlite's 0/1) — `MockEngine` inherits `DatabaseEngine`'s
    `ASYNCPG_CONVERTERS`, which do no int->bool coercion, unlike the sqlite
    engine's converters.

    Never call `connect()`/`close()` on this: they are inherited unmodified
    and would try to open a real asyncpg pool against `dsn`. `_require_pool()`
    is the only override, and it works whether or not `connect()` ever runs.
    """

    def __init__(self, rows: list[tuple[Any, ...]]):
        super().__init__(dsn="mock://unused")
        self._mock_pool = _FakePool(rows)
        self._rows = rows

    def _require_pool(self) -> _FakePool:
        return self._mock_pool
