"""asyncpg-backed engine — the fastest backend.

**Conditional session reset** (`conditional_reset=True`, the default)

asyncpg's pool runs `SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *;
RESET ALL;` on every release, as its own server round trip — so a pooled request
costs two round trips, one of which is cleanup. Measured, that cleanup is ~20-30%
of throughput (docs/FINDINGS.md, "the pool sends a second query").

The usual advice is to pass `reset=` a no-op, but that changes behaviour: session
state then leaks between requests. This engine takes a third route — run the
reset only when the connection *could* have been dirtied.

The invariant that makes it sound: `fetch_all` executes only statements compiled
by SQLAlchemy Core and run as parameterised queries. Two limits, stated plainly:

* The guarantee holds only if *all* access goes through this engine. Code that
  reaches into `engine.pool` and runs its own SQL is invisible here.
* A caller can now hand `fetch_all` any statement SQLAlchemy can build, including
  `select(func.set_config(...))`. That is a wider surface than the old in-house
  query builder offered, so the invariant rests on convention rather than on the
  builder being unable to express such a thing. Use `acquire()` for anything that
  touches session state, and it will be reset.

Anything through `acquire()` or `transaction()` marks the connection dirty, so
the full reset runs on its release. Pass `conditional_reset=False` for asyncpg's
unmodified behaviour.
"""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.dialects.postgresql import asyncpg as _asyncpg

from .engine import Engine
from .transaction import Transaction


class _DriverConnection:
    """What the dialect's codec setup expects to be handed: something with a
    `._connection`. rowform holds the driver connection directly."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any):
        self._connection = connection


class AsyncpgEngine(Engine):
    """See module docstring."""

    dialect = _asyncpg.dialect()

    def __init__(self, dsn: str, *, conditional_reset: bool = True, **pool_kwargs: Any):
        super().__init__(dsn, **pool_kwargs)
        self.conditional_reset = conditional_reset
        # Connections needing a SQL reset on release. A WeakSet of the real
        # Connection objects: they are hashable by identity and weakref-able, so
        # this neither keeps closed connections alive nor risks the id() reuse a
        # set of integers would.
        self._dirty: weakref.WeakSet[Any] = weakref.WeakSet()
        self.reset_count = 0

    async def _open_pool(self) -> Any:
        import asyncpg

        kwargs = dict(self._pool_kwargs)
        if self.conditional_reset:
            kwargs["reset"] = self._reset_if_dirty
        kwargs.setdefault("init", self._configure_connection)
        return await asyncpg.create_pool(self.dsn, **kwargs)

    async def _configure_connection(self, conn: Any) -> None:
        """Install the type codecs the dialect assumes are there.

        asyncpg does not decode `json`/`jsonb` on its own, so SQLAlchemy's
        dialect registers codecs in its `on_connect` and its `JSON.result_processor`
        then returns None — "the driver already did it". Running on a raw pool,
        nothing had done it, and JSON columns came back as text while the
        processor declined to convert them.

        These are the dialect's own coroutines rather than a reimplementation:
        if SQLAlchemy changes what its processors expect, this changes with them.
        They read the driver connection off `._connection`, which is the one
        attribute of its connection wrapper they touch.
        """
        shim = _DriverConnection(conn)
        await self.dialect.setup_asyncpg_json_codec(shim)
        await self.dialect.setup_asyncpg_jsonb_codec(shim)
        if self.dialect._native_inet_types is False:
            await self.dialect._disable_asyncpg_inet_codecs(shim)

    async def _close_pool(self, pool: Any) -> None:
        await pool.close()
        self._dirty.clear()

    def _acquire(self) -> Any:
        return self._require_pool().acquire()

    @staticmethod
    def _real(conn: Any) -> Any:
        """`pool.acquire()` yields a PoolConnectionProxy while the `reset=` hook
        receives the underlying Connection, so normalise before comparing — the
        two are different objects and mixing them silently defeats the whole
        mechanism."""
        return getattr(conn, "_con", conn)

    async def _reset_if_dirty(self, conn: Any) -> None:
        """Pool `reset=` hook: issue the SQL reset only for dirtied connections."""
        real = self._real(conn)
        if real not in self._dirty:
            return
        self._dirty.discard(real)
        reset_query = conn.get_reset_query()
        if reset_query:
            self.reset_count += 1
            await conn.execute(reset_query)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Raw connection access. Marks the connection dirty, so it gets the full
        session reset on release — use this for anything that is not a compiled
        statement, including `SET`, `LISTEN` and DDL."""
        async with self._acquire() as conn:
            self._dirty.add(self._real(conn))
            yield conn

    async def _fetch(self, conn, sql, params, describe):
        if not describe:
            return await conn.fetch(sql, *params), None
        # asyncpg has no cursor and so no `cursor.description`; the column type
        # OIDs live on the prepared statement, and the hydrator needs them
        # because postgres `Numeric.result_processor` raises without a type code.
        # asyncpg caches prepared statements, so this costs nothing after the
        # first call — and `describe` is only true on the first call anyway.
        prepared = await conn.prepare(sql)
        rows = await prepared.fetch(*params)
        description = [(a.name, a.type.oid) for a in prepared.get_attributes()]
        return rows, description

    async def _stream(self, conn, sql, params, chunk, query):
        """A portal over the prepared statement, which asyncpg will only open
        inside a transaction — so one is opened here rather than made the caller's
        problem. Inside `transaction()` it nests as a savepoint, which is
        harmless.

        The connection is *not* marked dirty. Only a compiled statement ran, and
        the portal dies with the transaction on either exit path — including a
        consumer that abandons the loop — so there is no session state left for
        `RESET ALL` to clean up.
        """
        async with conn.transaction():
            prepared = await conn.prepare(sql)
            description = [(a.name, a.type.oid) for a in prepared.get_attributes()]
            cursor = await prepared.cursor(*params)
            while True:
                rows = await cursor.fetch(chunk)
                if not rows:
                    return
                yield rows, description

    async def _execute(self, conn, sql, params):
        """asyncpg returns its own status tag, e.g. "INSERT 0 3" — the driver's
        report of what happened, not a normalised count, because normalising it
        would hide the difference between "0 rows matched" and "the statement did
        nothing"."""
        return await conn.execute(sql, *(params or ()))

    async def _execute_many(self, conn, sql, params):
        return await conn.executemany(sql, params)

    def _block(self, conn: Any, depth: int, kwargs: dict[str, Any]) -> Any:
        return _asyncpg_block(self, conn, depth, kwargs)


@asynccontextmanager
async def _asyncpg_block(
    engine: AsyncpgEngine, conn: Any, depth: int, kwargs: dict[str, Any]
) -> AsyncIterator[Transaction]:
    """asyncpg's `pool.acquire()` is autocommit, so the BEGIN is explicit; a
    nested `conn.transaction()` becomes a SAVEPOINT on its own.

    `isolation` accepts asyncpg's names ("read_committed", "repeatable_read",
    "serializable"); `deferrable` only applies to a serializable read-only
    transaction. They ride on the BEGIN itself, so they expire with it.
    """
    engine._dirty.add(engine._real(conn))
    tx = Transaction(engine, conn, depth)
    async with conn.transaction(**kwargs):
        tx._enter()
        try:
            yield tx
        finally:
            tx._exit()
