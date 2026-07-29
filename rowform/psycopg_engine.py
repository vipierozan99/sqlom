"""psycopg3-backed engine, so rowform and SQLAlchemy can be compared on one driver.

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

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, TypeVar, Union, overload

from .compile import (
    PSYCOPG_CONVERTERS,
    compile_batch_hydrator,
    compile_join_hydrator,
)
from .dialects import POSTGRES
from .dml import _Statement
from .expr import bind_params, has_deferred_params
from .query import CompoundSelect, Query
from .query import json_bytes as _json_bytes
from .transaction import _ACTIVE, Transaction

R = TypeVar("R")

# Anything the engine can hydrate rows from.
_Select = Union[Query[R], "CompoundSelect[R]"]


def _require_rows(statement: Any) -> None:
    """A write with no RETURNING produces no rows; hydrating it would return [] and
    look like "nothing matched" rather than "you asked the wrong way"."""
    if hasattr(statement, "returns_rows") and not statement.returns_rows:
        raise ValueError(
            f"{type(statement).__name__} has no returning(); it produces no rows. "
            f"Use engine.execute() for it, or add returning(...)."
        )


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

    async def _execute_raw(self, sql, *args):
        # psycopg binds a sequence, not varargs; None means "no parameters",
        # which matters because passing () makes psycopg use the extended
        # protocol and reject multi-statement strings.
        return await self.connection.execute(sql, args or None)

    def transaction(
        self, **kwargs: Any
    ) -> AbstractAsyncContextManager[Transaction]:
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
        self._hydrators: dict[Any, Callable[[Any], Any]] = {}

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

    def _hydrator_for(self, query):
        """Compiled once per query *shape*, then reused for every row of every
        request. The key is the model class itself for a plain single-model
        select, so this stays the same single dict lookup it always was; joined
        and multi-entity selects key on a tuple describing their entities."""
        key = query._hydration_key
        hydrator = self._hydrators.get(key)
        if hydrator is None:
            # Dispatch on the key's own shape rather than on a second predicate:
            # a plain model key means the fast single-model hydrator, a tuple key
            # means the general one. Deciding this two different ways is how a
            # RIGHT-joined single-entity query once got the fast hydrator and
            # returned an object with every field None.
            if isinstance(key, tuple):
                hydrator = compile_join_hydrator(
                    query.hydration_spec(), PSYCOPG_CONVERTERS, wrap=query.is_multi_entity
                )
            else:
                hydrator = compile_batch_hydrator(query.model, PSYCOPG_CONVERTERS)
            self._hydrators[key] = hydrator
        return hydrator

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Raw connection access, for anything that is not a `Query`.

        Unlike the asyncpg engine there is no dirty-tracking to do: this engine
        deliberately keeps psycopg_pool's default reset behaviour, so every
        connection is already reset on release.
        """
        async with self._require_pool().connection() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(
        self, *, isolation: str | None = None, readonly: bool | None = None,
        deferrable: bool | None = None,
    ) -> AsyncIterator[PsycopgTransaction]:
        """Several statements on one connection, committed together.

        `isolation` accepts the same names as the asyncpg engine
        ("read_committed", "repeatable_read", "serializable") and is translated
        to psycopg's `IsolationLevel`.

        Where the two drivers genuinely differ: asyncpg puts these on the
        `BEGIN` itself, so they expire with the transaction. psycopg puts them on
        the **connection**, and `psycopg_pool` does *not* restore them on release
        — its reset rolls back an open transaction and nothing more. So a
        `readonly=True` block would hand a permanently read-only connection back
        to the pool and the next borrower's first write would fail somewhere
        unrelated. This restores whatever was set before, which is what makes the
        two engines behave the same way.

        The setters are awaited because the corresponding properties are
        read-only on an `AsyncConnection`; assignment raises rather than silently
        doing nothing.
        """
        async with self._require_pool().connection() as conn:
            level = None
            if isolation is not None:
                from psycopg import IsolationLevel

                try:
                    level = IsolationLevel[isolation.upper()]
                except KeyError:
                    raise ValueError(
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
                current = (conn.isolation_level, conn.read_only, conn.deferrable)
                if current != previous:
                    await conn.set_isolation_level(previous[0])
                    await conn.set_read_only(previous[1])
                    await conn.set_deferrable(previous[2])

    def _reject_if_in_transaction(self, method):
        active = _ACTIVE.get()
        if active is not None and active._engine is self:
            raise RuntimeError(
                f"engine.{method}() was called inside engine.transaction(); it would "
                f"run on a different pooled connection and miss the transaction's "
                f"uncommitted state. Use tx.{method}() instead."
            )

    @overload
    async def fetch_all(self, query: _Select[R], **overrides: Any) -> list[R]: ...

    @overload
    async def fetch_all(self, query: _Statement, **overrides: Any) -> list[Any]: ...

    async def fetch_all(self, query: Any, **overrides: Any) -> Any:
        """`**overrides` supplies (or replaces) any `bindparam()` values the
        query was built with — see `bind_params()`. Checked once via
        `has_deferred_params()`, so a query with none pays nothing beyond
        that."""
        self._reject_if_in_transaction("fetch_all")
        _require_rows(query)
        sql, params = query.to_sql(placeholder="%s", dialect=POSTGRES)
        if has_deferred_params(params):
            params = bind_params(params, **overrides)
        async with self._require_pool().connection() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        return self._hydrator_for(query)(rows)

    @overload
    async def execute(self, statement: _Select[R], **overrides: Any) -> list[R]: ...

    @overload
    async def execute(self, statement: _Statement, **overrides: Any) -> int: ...

    async def execute(self, statement: Any, **overrides: Any) -> Any:
        """The SQLAlchemy-style single entry point: `execute(select(...))` (or
        any other `Query`/`CompoundSelect`) hydrates and returns its rows,
        exactly as `fetch_all()` does. An Insert/Update/Delete with no
        RETURNING runs and returns the rowcount instead — psycopg reports an
        integer here where asyncpg reports a status string; both are the
        driver's own answer rather than a normalisation across them.

        Calling this on an Insert/Update/Delete *with* `returning()` still
        raises, asking you to use `fetch_all()` so you get the rows back
        rather than silently discarding them.

        `**overrides` — see `fetch_all()`.
        """
        if isinstance(statement, (Query, CompoundSelect)):
            return await self.fetch_all(statement, **overrides)
        if getattr(statement, "returns_rows", False):
            raise ValueError(
                "this statement has RETURNING, so it produces rows — use "
                "fetch_all() to get them"
            )
        sql, params = statement.to_sql(placeholder="%s", dialect=POSTGRES)
        if has_deferred_params(params):
            params = bind_params(params, **overrides)
        async with self._require_pool().connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def fetch_json(self, query: Query[Any]) -> bytes:
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
