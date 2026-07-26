"""psycopg3-backed engine, so sqlom and SQLAlchemy can be compared on one driver.

The asyncpg engine in `engine.py` is the faster backend, but SQLAlchemy cannot
use it and psycopg3 at the same time, and comparing two mappers across two
drivers confounds the mapper with the driver. This engine exists so both sides
can run on `postgresql+psycopg` / `psycopg_pool` with each library's **default**
pool behaviour — no `reset=` overrides, no AUTOCOMMIT, nothing tuned.

That default is not free, and it is the same cost SQLAlchemy pays: psycopg3
connections are transactional unless told otherwise, so a pooled request is
`BEGIN` … `SELECT` … `COMMIT`. Keeping it means the comparison measures the
mapper rather than two different transaction policies.

`fetch_all` returns hydrated model instances; rows arrive as plain tuples from
psycopg3, which suits the positional hydrator directly.
"""

from contextlib import asynccontextmanager

from .compile import PSYCOPG_CONVERTERS, compile_batch_hydrator
from .query import json_bytes as _json_bytes
from .transaction import _ACTIVE, Transaction


class PsycopgTransaction(Transaction):
    """psycopg3 connections are transactional already — `pool.connection()`
    commits on clean exit. `conn.transaction()` is still used explicitly so the
    block's boundaries are ours rather than the pool's, and so nesting produces
    real savepoints."""

    __slots__ = ()
    _placeholder = "%s"
    _dialect = "psycopg"

    async def _fetch_rows(self, sql, params):
        cur = await self.connection.execute(sql, params)
        return await cur.fetchall()

    async def _fetch_value(self, sql, params):
        cur = await self.connection.execute(sql, params)
        row = await cur.fetchone()
        return row[0] if row else None

    async def execute(self, sql, *args):
        # psycopg binds a sequence, not varargs; None means "no parameters",
        # which matters because passing () makes psycopg use the extended
        # protocol and reject multi-statement strings.
        return await self.connection.execute(sql, args or None)

    def transaction(self, **kwargs):
        """Nested block — psycopg issues a SAVEPOINT."""
        return _psycopg_block(self._engine, self.connection, self._depth + 1, kwargs)


@asynccontextmanager
async def _psycopg_block(engine, conn, depth, kwargs):
    tx = PsycopgTransaction(engine, conn, depth)
    async with conn.transaction(**kwargs):
        tx._enter()
        try:
            yield tx
        finally:
            tx._exit()


class PsycopgEngine:
    def __init__(self, conninfo: str, **pool_kwargs):
        self.conninfo = conninfo
        self.pool = None
        self._pool_kwargs = pool_kwargs
        self._hydrators = {}

    async def connect(self):
        """Open the pool. Idempotent — a second call would otherwise leak the
        first pool, leaving its connections open with nothing referencing them."""
        if self.pool is not None:
            return self.pool

        from psycopg_pool import AsyncConnectionPool

        # open=False then open() explicitly: constructing an open pool from a
        # running loop is deprecated in psycopg_pool 3.2+.
        self.pool = AsyncConnectionPool(self.conninfo, open=False, **self._pool_kwargs)
        await self.pool.open(wait=True)
        return self.pool

    async def close(self):
        """Close the pool and clear the reference. Safe to call more than once."""
        pool, self.pool = self.pool, None
        if pool is not None:
            await pool.close()

    def _require_pool(self):
        if self.pool is None:
            raise RuntimeError(
                "engine is not connected — await engine.connect() first "
                "(or it has been closed)"
            )
        return self.pool

    def _hydrator_for(self, model):
        hydrator = self._hydrators.get(model)
        if hydrator is None:
            hydrator = compile_batch_hydrator(model, PSYCOPG_CONVERTERS)
            self._hydrators[model] = hydrator
        return hydrator

    @asynccontextmanager
    async def acquire(self):
        """Raw connection access, for anything that is not a `Query`.

        Unlike the asyncpg engine there is no dirty-tracking to do: this engine
        deliberately keeps psycopg_pool's default reset behaviour, so every
        connection is already reset on release.
        """
        async with self._require_pool().connection() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self, *, isolation=None, readonly=None, deferrable=None):
        """Several statements on one connection, committed together.

        `isolation` accepts the same names as the asyncpg engine
        ("read_committed", "repeatable_read", "serializable") and is translated
        to psycopg's `IsolationLevel`. psycopg sets these on the *connection*
        rather than per transaction, so they are applied before the block opens
        — and the pool resets them on release, so they do not leak.

        Note the setters are awaited: on an `AsyncConnection` the corresponding
        properties are read-only, and assigning to them raises rather than
        silently doing nothing.
        """
        async with self._require_pool().connection() as conn:
            if isolation is not None:
                from psycopg import IsolationLevel

                try:
                    level = IsolationLevel[isolation.upper()]
                except KeyError:
                    raise ValueError(
                        f"unknown isolation level {isolation!r}; expected one of "
                        f"{', '.join(lvl.name.lower() for lvl in IsolationLevel)}"
                    ) from None
                await conn.set_isolation_level(level)
            if readonly is not None:
                await conn.set_read_only(readonly)
            if deferrable is not None:
                await conn.set_deferrable(deferrable)
            async with _psycopg_block(self, conn, 0, {}) as tx:
                yield tx

    def _reject_if_in_transaction(self, method):
        active = _ACTIVE.get()
        if active is not None and active._engine is self:
            raise RuntimeError(
                f"engine.{method}() was called inside engine.transaction(); it would "
                f"run on a different pooled connection and miss the transaction's "
                f"uncommitted state. Use tx.{method}() instead."
            )

    async def fetch_all(self, query):
        self._reject_if_in_transaction("fetch_all")
        sql, params = query.to_sql(placeholder="%s")
        async with self._require_pool().connection() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        return self._hydrator_for(query.model)(rows)

    async def fetch_json(self, query):
        """Result set as JSON bytes built by Postgres, no per-row Python objects.

        The `psycopg` dialect casts the aggregate to text precisely so this stays
        true: psycopg registers a json/jsonb loader, so without the cast the
        driver would parse the array into Python lists and dicts — the opposite
        of what this method is for.
        """
        self._reject_if_in_transaction("fetch_json")
        sql, params = query.to_json_sql(dialect="psycopg")
        async with self._require_pool().connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
        return _json_bytes(row[0] if row else None)
