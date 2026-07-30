"""psycopg3-backed engine, so rowform and SQLAlchemy can be compared on one driver.

`AsyncpgEngine` is the faster backend, but SQLAlchemy cannot use asyncpg and
psycopg3 at the same time, and comparing two row layers across two drivers
confounds the row layer with the driver. This engine exists so both sides can run
on `postgresql+psycopg` / `psycopg_pool` with each library's **default** pool
behaviour — no `reset=` overrides, no AUTOCOMMIT, nothing tuned.

That default is not free, and it is the same cost SQLAlchemy pays: psycopg3
connections are transactional unless told otherwise, so a pooled request is
`BEGIN` … `SELECT` … `COMMIT`. Keeping it means the comparison measures the row
layer rather than two different transaction policies.

This is also the one supported driver whose paramstyle is *not* positional —
psycopg uses `pyformat`, so `CoreQuery.bind()` hands it a dict where the others
get a tuple. That branch is decided by the dialect, not by this module.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.dialects.postgresql import psycopg as _psycopg

from .engine import Engine, PoolStats
from .errors import ConfigurationError, UnsupportedError
from .transaction import Transaction

# Cursor names are per *session*, so two streams sharing one connection — which is
# exactly what `tx.fetch_iter()` inside another `tx.fetch_iter()` does — must not
# ask for the same name. A fixed one raises `DuplicateCursor: cursor
# "rowform_stream" already exists` on the second.
_STREAM_NAMES = itertools.count()


class PsycopgEngine(Engine):
    """See module docstring."""

    dialect = _psycopg.dialect()

    async def _open_pool(self) -> Any:
        from psycopg_pool import AsyncConnectionPool

        # open=False then open() explicitly: constructing an open pool from a
        # running loop is deprecated in psycopg_pool 3.2+.
        pool = AsyncConnectionPool(self.dsn, open=False, **self._pool_kwargs)
        await pool.open(wait=True)
        return pool

    def _pool_stats(self, pool: Any) -> PoolStats:
        # The one pool of the three that counts blocked callers, which is the
        # number that distinguishes "the database is slow" from "the pool is too
        # small".
        stats = pool.get_stats()
        return PoolStats(
            size=stats.get("pool_size", 0),
            idle=stats.get("pool_available", 0),
            max_size=pool.max_size,
            waiting=stats.get("requests_waiting", 0),
        )

    async def _close_pool(self, pool: Any) -> None:
        await pool.close()

    def _acquire(self) -> Any:
        return self._require_pool().connection()

    async def _fetch(self, conn, sql, params, describe):
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return rows, cursor.description if describe else None

    async def _stream(self, conn, sql, params, chunk, query):
        """A named cursor, which is psycopg's server-side one: `DECLARE` on the
        server, `FETCH` per chunk. The unnamed cursor would also chunk, but only
        after the driver had already read every row into the client, which is the
        memory this method exists to avoid.

        The cost is that postgres will not `DECLARE` a cursor for
        `INSERT ... RETURNING` — it is a syntax error there — so that case is
        refused up front instead of surfacing as one. `AsyncpgEngine` streams it
        through a portal, and `fetch_all` works on either.
        """
        if not query.is_select:
            raise UnsupportedError(
                "PsycopgEngine.fetch_iter streams through a server-side cursor, and "
                "postgres will only DECLARE one for a SELECT — not for a write with "
                "RETURNING. Use fetch_all() for this statement, or AsyncpgEngine, "
                "which streams it through a portal."
            )
        async with conn.cursor(name=f"rowform_stream_{next(_STREAM_NAMES)}") as cursor:
            await cursor.execute(sql, params)
            description = cursor.description
            while True:
                rows = await cursor.fetchmany(chunk)
                if not rows:
                    return
                yield rows, description

    async def _copy_in(self, conn, table, columns, records):
        """`COPY ... FROM STDIN`, a row at a time into psycopg's writer.

        Identifiers are quoted by SQLAlchemy's own preparer rather than by hand:
        a table or column needing quotes is exactly the case a hand-rolled f-string
        gets wrong.
        """
        preparer = self.dialect.identifier_preparer
        target = preparer.format_table(table)
        names = ", ".join(preparer.quote(name) for name in columns)
        async with conn.cursor() as cursor, cursor.copy(f"COPY {target} ({names}) FROM STDIN") as copy:
            for record in records:
                await copy.write_row(record)
        return len(records)

    async def _execute(self, conn, sql, params):
        # psycopg binds a sequence or mapping, never varargs; None means "no
        # parameters", which matters because passing an empty one makes psycopg
        # use the extended protocol and reject multi-statement strings.
        cursor = await conn.execute(sql, params or None)
        return cursor.rowcount

    async def _execute_many(self, conn, sql, params):
        async with conn.cursor() as cursor:
            await cursor.executemany(sql, params)
            return cursor.rowcount

    def _block(self, conn: Any, depth: int, kwargs: dict[str, Any]) -> Any:
        return _psycopg_block(self, conn, depth, kwargs)

    @asynccontextmanager
    async def transaction(self, **kwargs: Any) -> AsyncIterator[Transaction]:
        """Several statements on one connection, committed together.

        Where the two postgres drivers genuinely differ: asyncpg puts
        `isolation`/`readonly`/`deferrable` on the `BEGIN` itself, so they expire
        with the transaction. psycopg puts them on the **connection**, and
        `psycopg_pool` does *not* restore them on release — its reset rolls back
        an open transaction and nothing more. So a `readonly=True` block would
        hand a permanently read-only connection back to the pool and the next
        borrower's first write would fail somewhere unrelated. This restores
        whatever was set before, which is what makes the two engines behave the
        same way.

        The setters are awaited because the corresponding properties are
        read-only on an `AsyncConnection`; assignment raises rather than silently
        doing nothing.
        """
        isolation = kwargs.pop("isolation", None)
        readonly = kwargs.pop("readonly", None)
        deferrable = kwargs.pop("deferrable", None)
        if kwargs:
            raise ConfigurationError(f"unexpected keyword arguments: {sorted(kwargs)}")

        async with self._acquire() as conn:
            level = None
            if isolation is not None:
                from psycopg import IsolationLevel

                try:
                    level = IsolationLevel[isolation.upper()]
                except KeyError:
                    raise ConfigurationError(
                        f"unknown isolation level {isolation!r}; expected one of "
                        f"{', '.join(lvl.name.lower() for lvl in IsolationLevel)}"
                    ) from None

            previous = (conn.isolation_level, conn.read_only, conn.deferrable)
            try:
                if level is not None:
                    await conn.set_isolation_level(level)
                if readonly is not None:
                    await conn.set_read_only(readonly)
                if deferrable is not None:
                    await conn.set_deferrable(deferrable)
                async with _psycopg_block(self, conn, 0, {}) as tx:
                    yield tx
            finally:
                # Restore before the connection goes back to the pool. Must run
                # outside the transaction block, since psycopg refuses these
                # changes while a transaction is open.
                if (conn.isolation_level, conn.read_only, conn.deferrable) != previous:
                    await conn.set_isolation_level(previous[0])
                    await conn.set_read_only(previous[1])
                    await conn.set_deferrable(previous[2])


@asynccontextmanager
async def _psycopg_block(
    engine: PsycopgEngine, conn: Any, depth: int, kwargs: dict[str, Any]
) -> AsyncIterator[Transaction]:
    """psycopg3 connections are transactional already — `pool.connection()`
    commits on clean exit. `conn.transaction()` is still used explicitly so the
    block's boundaries are ours rather than the pool's, and so nesting produces
    real savepoints."""
    tx = Transaction(engine, conn, depth)
    async with conn.transaction(**kwargs):
        tx._enter()
        try:
            yield tx
        finally:
            tx._exit()
