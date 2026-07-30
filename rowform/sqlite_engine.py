"""aiosqlite-backed engine.

aiosqlite wraps stdlib `sqlite3` in a background thread per connection and ships
no pool of its own, and a single connection would serialise any concurrent
workload, so this file provides one: a fixed-size queue of connections, each
opened with WAL + `synchronous=NORMAL` so concurrent readers don't serialise
behind each other, and `isolation_level=None` so sqlite3's own implicit "open a
transaction before the first DML" never fires — this engine's BEGIN/SAVEPOINT SQL
is the only thing that opens one.

sqlite has no real isolation levels (WAL plus BEGIN DEFERRED/IMMEDIATE/EXCLUSIVE
is the whole model), so `isolation`/`readonly`/`deferrable` raise rather than
silently no-op; a caller cannot be left believing they took effect.

**sqlite is where bypassing SQLAlchemy's `Row` is most dangerous**, because it
stores `Date`/`DateTime`/`Time` as strings and booleans as integers, and returns
them that way. Nothing here special-cases that: `compile.py` asks each column's
type for its `result_processor` and gets sqlite's own `str_to_datetime` and
friends, the same functions `Row` would have run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.dialects.sqlite import aiosqlite as _aiosqlite

from .engine import Engine
from .transaction import Transaction


def _reject_unsupported(kwargs: dict[str, Any]) -> None:
    unsupported = {k: v for k, v in kwargs.items() if v}
    if unsupported:
        raise NotImplementedError(
            f"sqlite has no session-level isolation levels and no read-only/"
            f"deferrable transactions — its model is WAL plus BEGIN DEFERRED/"
            f"IMMEDIATE/EXCLUSIVE. Accepting {sorted(unsupported)} as no-ops would "
            f"let a caller believe they took effect; open a plain transaction() "
            f"instead."
        )


class _SqlitePool:
    """Fixed-size pool of aiosqlite connections. `min_size`/`max_size` are
    accepted by `SqliteEngine` only for call-site parity with the other engines —
    there is no elastic growth to configure."""

    def __init__(self, path: str, size: int):
        self._path = path
        self._size = size
        self._all: list[Any] = []
        self._idle: asyncio.Queue[Any] = asyncio.Queue()

    async def open(self) -> None:
        import aiosqlite

        for _ in range(self._size):
            conn = await aiosqlite.connect(self._path, isolation_level=None)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            self._all.append(conn)
            self._idle.put_nowait(conn)

    async def close(self) -> None:
        while not self._idle.empty():
            self._idle.get_nowait()
        conns, self._all = self._all, []
        for conn in conns:
            await conn.close()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        conn = await self._idle.get()
        try:
            yield conn
        finally:
            self._idle.put_nowait(conn)


class SqliteEngine(Engine):
    """See module docstring."""

    dialect = _aiosqlite.dialect()

    def __init__(self, path: str, *, min_size: int = 1, max_size: int = 5, **kwargs: Any):
        if kwargs:
            raise TypeError(f"unexpected keyword arguments: {sorted(kwargs)}")
        super().__init__(path)
        self.path = path
        self._pool_size = max(min_size, max_size)

    async def _open_pool(self) -> Any:
        pool = _SqlitePool(self.path, self._pool_size)
        await pool.open()
        return pool

    async def _close_pool(self, pool: Any) -> None:
        await pool.close()

    def _acquire(self) -> Any:
        return self._require_pool().acquire()

    async def _fetch(self, conn, sql, params, describe):
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        # sqlite3 reports no type codes at all — `description[i][1]` is always
        # None — which is exactly what SQLAlchemy passes its own processors here.
        return rows, cursor.description if describe else None

    async def _execute(self, conn, sql, params):
        cursor = await conn.execute(sql, params or ())
        return cursor.rowcount

    async def _execute_many(self, conn, sql, params):
        cursor = await conn.executemany(sql, params)
        return cursor.rowcount

    def _block(self, conn: Any, depth: int, kwargs: dict[str, Any]) -> Any:
        _reject_unsupported(kwargs)
        return _sqlite_block(self, conn, depth)


@asynccontextmanager
async def _sqlite_block(engine: SqliteEngine, conn: Any, depth: int) -> AsyncIterator[Transaction]:
    """sqlite has no `conn.transaction()` context manager, unlike asyncpg and
    psycopg, so the BEGIN/SAVEPOINT SQL is issued directly."""
    tx = Transaction(engine, conn, depth)
    savepoint = f"rowform_sp_{depth}"
    await conn.execute("BEGIN" if depth == 0 else f"SAVEPOINT {savepoint}")
    tx._enter()
    try:
        yield tx
    except BaseException:
        if depth == 0:
            await conn.execute("ROLLBACK")
        else:
            await conn.execute(f"ROLLBACK TO {savepoint}")
            await conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        await conn.execute("COMMIT" if depth == 0 else f"RELEASE {savepoint}")
    finally:
        tx._exit()
