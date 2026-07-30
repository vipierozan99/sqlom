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

from .engine import Engine, Observer
from .errors import ConfigurationError, UnsupportedError
from .transaction import Transaction


def _reject_unsupported(kwargs: dict[str, Any]) -> None:
    unsupported = {k: v for k, v in kwargs.items() if v}
    if unsupported:
        raise UnsupportedError(
            f"sqlite has no session-level isolation levels and no read-only/"
            f"deferrable transactions — its model is WAL plus BEGIN DEFERRED/"
            f"IMMEDIATE/EXCLUSIVE. Accepting {sorted(unsupported)} as no-ops would "
            f"let a caller believe they took effect; open a plain transaction() "
            f"instead."
        )


class _SqlitePool:
    """`min_size` connections opened up front, growing to `max_size` on demand.

    The same two knobs asyncpg's pool and `psycopg_pool` take, so the three
    engines size alike. They used to be accepted and then collapsed to
    `max(min_size, max_size)`, which opened `min_size` connections eagerly when
    it was the larger of the two and made `max_size` not a maximum.
    """

    def __init__(self, path: str, min_size: int, max_size: int):
        self._path = path
        self._min = min_size
        self._max = max_size
        self._count = 0
        self._all: list[Any] = []
        self._idle: asyncio.Queue[Any] = asyncio.Queue()

    async def open(self) -> None:
        # A failure part-way through leaves this pool object unreferenced —
        # `_open_pool` never returns, so `engine.pool` is never assigned and
        # nothing can close what was already opened. Close it here instead, or a
        # retried `connect()` accumulates file handles.
        try:
            for _ in range(self._min):
                self._count += 1
                self._idle.put_nowait(await self._connect())
        except BaseException:
            await self.close()
            raise

    async def _connect(self) -> Any:
        import aiosqlite

        conn = await aiosqlite.connect(self._path, isolation_level=None)
        # Registered in `_all` only once it is fully set up, so a PRAGMA that
        # raises has to close the connection itself — nothing else knows about it
        # yet.
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            await conn.close()
            raise
        self._all.append(conn)
        return conn

    async def close(self) -> None:
        while not self._idle.empty():
            self._idle.get_nowait()
        conns, self._all = self._all, []
        self._count = 0
        for conn in conns:
            await conn.close()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        # Below the ceiling, open rather than queue: a waiter blocking on an idle
        # queue while the pool is still allowed to grow is waiting for a
        # connection that need not exist yet. The counter is bumped *before* the
        # await, so two tasks arriving together cannot both decide there is room
        # and overshoot `max_size`.
        if self._idle.empty() and self._count < self._max:
            self._count += 1
            try:
                conn = await self._connect()
            except BaseException:
                self._count -= 1
                raise
        else:
            conn = await self._idle.get()
        try:
            yield conn
        finally:
            self._idle.put_nowait(conn)


class SqliteEngine(Engine):
    """See module docstring."""

    dialect = _aiosqlite.dialect()

    def __init__(
        self,
        path: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        observer: Observer | None = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise ConfigurationError(f"unexpected keyword arguments: {sorted(kwargs)}")
        if min_size < 0 or max_size < 1 or max_size < min_size:
            raise ConfigurationError(
                f"pool sizes must satisfy 0 <= min_size <= max_size and max_size >= 1; "
                f"got min_size={min_size}, max_size={max_size}"
            )
        super().__init__(path, observer=observer)
        self.path = path
        self._min_size = min_size
        self._max_size = max_size

    async def _open_pool(self) -> Any:
        pool = _SqlitePool(self.path, self._min_size, self._max_size)
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

    async def _stream(self, conn, sql, params, chunk, query):
        """sqlite3's own cursor is already incremental, so `fetchmany` is the whole
        implementation — and unlike postgres it will stream anything that returns
        rows, `INSERT ... RETURNING` included."""
        cursor = await conn.execute(sql, params)
        try:
            description = cursor.description
            while True:
                rows = await cursor.fetchmany(chunk)
                if not rows:
                    return
                yield rows, description
        finally:
            await cursor.close()

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
