"""aiosqlite-backed engine — the third backend, full parity with asyncpg/psycopg.

Exists so the benchmark suite's service tier (PLAN.md D7) exercises the library's
own code path rather than a harness-owned stand-in — the same rule that caught a
real 4% regression in the shipped asyncpg engine (see `benchmarks/engines/mock.py`).

aiosqlite wraps stdlib `sqlite3` in a background thread per connection and ships no
pool of its own, and a single connection would serialise the whole service
benchmark under concurrency, so this file provides one: a fixed-size queue of
connections, each opened with WAL + `synchronous=NORMAL` so concurrent readers
don't serialise behind each other, and `isolation_level=None` so sqlite3's own
implicit "open a transaction before the first DML" never fires — this engine's
BEGIN/SAVEPOINT SQL is the only thing that opens one.

sqlite has no server-side session state to reset (`conditional_reset`, an
asyncpg-pool concept, has no target here) and no real isolation levels (WAL plus
BEGIN DEFERRED/IMMEDIATE/EXCLUSIVE is the whole model) — both raise
`NotImplementedError` rather than silently no-op, so a caller cannot believe
either took effect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, TypeVar, Union, overload

from .compile import SQLITE_CONVERTERS, compile_batch_hydrator, compile_join_hydrator
from .dialects import SQLITE
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


def _reject_unsupported(isolation, readonly, deferrable):
    if isolation is not None or readonly or deferrable:
        raise NotImplementedError(
            "sqlite has no session-level isolation levels and no read-only/"
            "deferrable transactions — its model is WAL plus BEGIN DEFERRED/"
            "IMMEDIATE/EXCLUSIVE. Accepting isolation=/readonly=/deferrable= as "
            "no-ops would let a caller believe they took effect; open a plain "
            "transaction() instead."
        )


class _SqlitePool:
    """Fixed-size pool of aiosqlite connections. aiosqlite ships no pool of its
    own, and `min_size`/`max_size` are accepted by `SqliteEngine` only for call-site
    parity with the other two engines — there is no elastic growth to configure."""

    def __init__(self, path: str, size: int):
        self._path = path
        self._size = size
        self._all: list[Any] = []
        self._idle: asyncio.Queue[Any] = asyncio.Queue()

    async def open(self):
        import aiosqlite

        for _ in range(self._size):
            conn = await aiosqlite.connect(self._path, isolation_level=None)
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            self._all.append(conn)
            self._idle.put_nowait(conn)

    async def close(self):
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


@asynccontextmanager
async def _sqlite_block(engine, conn, depth):
    tx = SqliteTransaction(engine, conn, depth)
    savepoint = f"rowform_sp_{depth}"
    if depth == 0:
        await conn.execute("BEGIN")
    else:
        await conn.execute(f"SAVEPOINT {savepoint}")
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
        if depth == 0:
            await conn.execute("COMMIT")
        else:
            await conn.execute(f"RELEASE {savepoint}")
    finally:
        tx._exit()


class SqliteTransaction(Transaction):
    """sqlite has no `conn.transaction()` context manager, unlike asyncpg/psycopg —
    `_sqlite_block` issues the BEGIN/SAVEPOINT SQL directly."""

    __slots__ = ()
    _placeholder = "?"
    _dialect = "sqlite"

    async def _fetch_rows(self, sql, params):
        cur = await self.connection.execute(sql, params)
        return await cur.fetchall()

    async def _fetch_value(self, sql, params):
        cur = await self.connection.execute(sql, params)
        row = await cur.fetchone()
        return row[0] if row else None

    async def _execute_raw(self, sql, *args):
        cur = await self.connection.execute(sql, args)
        return cur.rowcount

    def transaction(
        self, *, isolation: str | None = None, readonly: bool = False,
        deferrable: bool = False,
    ) -> AbstractAsyncContextManager[Transaction]:
        """Nested block -> a SAVEPOINT. `isolation`/`readonly`/`deferrable` are
        accepted for signature parity with the other engines' `transaction()`; see
        `_reject_unsupported`."""
        _reject_unsupported(isolation, readonly, deferrable)
        return _sqlite_block(self._engine, self.connection, self._depth + 1)


class SqliteEngine:
    """aiosqlite-backed engine — see module docstring."""

    def __init__(self, path: str, *, min_size: int = 1, max_size: int = 5, **kwargs):
        if "conditional_reset" in kwargs:
            raise NotImplementedError(
                "conditional_reset is an asyncpg pool-reset concept with no sqlite "
                "equivalent — sqlite connections have no server-side session state "
                "to reset. Remove the argument rather than pass it as a no-op."
            )
        if kwargs:
            raise TypeError(f"unexpected keyword arguments: {sorted(kwargs)}")
        self.path = path
        self.pool: _SqlitePool | None = None
        self._pool_size = max(min_size, max_size)
        self._hydrators: dict[Any, Callable[[Any], Any]] = {}

    async def connect(self):
        """Open the pool. Idempotent — a second call would otherwise leak the
        first pool, leaving its connections open with nothing referencing them."""
        if self.pool is not None:
            return self.pool
        pool = _SqlitePool(self.path, self._pool_size)
        await pool.open()
        self.pool = pool
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
        request. See `DatabaseEngine._hydrator_for` (`rowform/engine.py`) for why
        the dispatch is on the key's own shape rather than a second predicate."""
        key = query._hydration_key
        hydrator = self._hydrators.get(key)
        if hydrator is None:
            if isinstance(key, tuple):
                hydrator = compile_join_hydrator(
                    query.hydration_spec(), SQLITE_CONVERTERS, wrap=query.is_multi_entity
                )
            else:
                hydrator = compile_batch_hydrator(query.model, SQLITE_CONVERTERS)
            self._hydrators[key] = hydrator
        return hydrator

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Raw connection access, for anything that is not a `Query`."""
        async with self._require_pool().acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(
        self, *, isolation: str | None = None, readonly: bool = False,
        deferrable: bool = False,
    ) -> AsyncIterator[SqliteTransaction]:
        """Several statements on one connection, committed together. See
        `SqliteTransaction.transaction()` for why `isolation`/`readonly`/
        `deferrable` raise rather than no-op."""
        _reject_unsupported(isolation, readonly, deferrable)
        async with self._require_pool().acquire() as conn, _sqlite_block(self, conn, 0) as tx:
            yield tx

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
        query was built with — see `bind_params()`."""
        self._reject_if_in_transaction("fetch_all")
        _require_rows(query)
        sql, params = query.to_sql(placeholder="?", dialect=SQLITE)
        if has_deferred_params(params):
            params = bind_params(params, **overrides)
        async with self._require_pool().acquire() as conn:
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
        RETURNING runs and returns its rowcount — same convention as
        `PsycopgEngine.execute()`.

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
        sql, params = statement.to_sql(placeholder="?", dialect=SQLITE)
        if has_deferred_params(params):
            params = bind_params(params, **overrides)
        async with self._require_pool().acquire() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def fetch_json(self, query: Query[Any]) -> bytes:
        """Result set as JSON bytes built by sqlite itself (`json_group_array` /
        `json_object`), no per-row Python objects."""
        self._reject_if_in_transaction("fetch_json")
        sql, params = query.to_json_sql(dialect="sqlite")
        async with self._require_pool().acquire() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
        return _json_bytes(row[0] if row else None)
