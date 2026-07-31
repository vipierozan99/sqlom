"""`MockEngine`: the row layer's floor, with zero driver cost.

Every rowform engine touches its driver through exactly one hook:

    async def _fetch(self, conn, sql, params, describe) -> (rows, description)

`MockEngine` overrides only that, so statement compilation, parameter binding,
hydrator planning and hydration all run byte-for-byte identical to production —
including the per-request cache-key lookup, which is the kind of self-inflicted
cost this instrument exists to expose (it caught a real 4% regression in the
shipped engine under the previous design).

Rows are precomputed plain tuples. Its absolutes are therefore not comparable to
sqlite/Postgres numbers — it is a row-layer instrument only.

`mock_sqlalchemy_engine()` is the equivalent seam for SQLAlchemy: it fakes
aiosqlite one layer further down. SQL compilation, Core result processing and ORM
hydration all run for real; only the SQLite call is canned. It must sit at the
driver seam rather than at `engine.connect()`/`execute()` because ORM hydration
needs a genuine `CursorResult` (row identity, type processors, entity loading),
not a hand-rolled stand-in. Pairing it against rowform's mock would still put
different work inside the two timed regions (correction 6), so each is its own
row-layer floor, not a cross-mapper comparison.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import rowform as rf


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


class MockEngine(rf.SqliteEngine):
    """A `SqliteEngine` whose driver call is canned — see module docstring.

    Subclasses the sqlite engine rather than a postgres one so the *processors*
    are sqlite's: rows arrive as 0/1 for booleans and strings for temporal types,
    exactly as the real driver hands them over, and the hydrator does the same
    work it would in production. A postgres-flavoured mock would silently skip
    every conversion and measure a row layer that never runs.

    `connect()`/`close()` are inherited and harmless — nothing opens a file,
    because `_fetch` never reaches a connection.
    """

    def __init__(self, rows: list[tuple[Any, ...]], columns: Sequence[str] = ()):
        super().__init__(":memory:")
        self._rows = rows
        self._description = [(name, None) for name in columns]

    def _acquire(self) -> Any:
        return _NoConnection()

    async def _fetch(self, conn, sql, params, describe):
        return self._rows, self._description if describe else None


class _NoConnection:
    """There is no connection to check out, and `_fetch` never looks at one."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None
