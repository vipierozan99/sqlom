"""What is left of an engine once SQLAlchemy compiles the SQL: a pool, an
execute, and the compiled hydrator.

Engines used to generate SQL as well. They no longer do — `CoreQuery` holds the
compiled string and the parameter recipe, and each subclass here supplies only
what genuinely differs between drivers:

* how to open and check out of a pool,
* how to run a string with parameters and get rows plus a result description,
* how a transaction block is opened.

**Why rowform's pool rather than SQLAlchemy's.** Hoisting the connection out of
the timed region and then putting it back costs SQLAlchemy's pool ~0.18 ms per
request against ~0.03–0.08 ms here (docs/PLAN_CORE_COMPILER.md §2g). Core
compiles; this runs it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, TypeVar, overload

import sqlalchemy as sa

from .query import CoreQuery
from .transaction import _ACTIVE, Transaction

R = TypeVar("R")


class Engine(ABC):
    """Shared engine behaviour. See `SqliteEngine`, `AsyncpgEngine`, `PsycopgEngine`."""

    #: The SQLAlchemy dialect statements are compiled for, and whose type
    #: `result_processor`s decode rows. One per driver, since paramstyle and
    #: type handling both come from it.
    dialect: Any

    def __init__(self, dsn: str, **pool_kwargs: Any):
        self.dsn = dsn
        self.pool: Any = None
        self._pool_kwargs = pool_kwargs
        self._queries: dict[Any, CoreQuery[Any]] = {}

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> Any:
        """Open the pool. Idempotent: without the guard a stray second call would
        replace the pool reference and leak the first one, leaving its
        connections open against the server with nothing holding them."""
        if self.pool is None:
            self.pool = await self._open_pool()
        return self.pool

    async def close(self) -> None:
        """Close the pool and drop the reference, so a later `connect()` opens a
        fresh one by design rather than by overwriting a closed object. Safe to
        call more than once."""
        pool, self.pool = self.pool, None
        if pool is not None:
            await self._close_pool(pool)

    def _require_pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError(
                "engine is not connected — await engine.connect() first "
                "(or it has been closed)"
            )
        return self.pool

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- statements ---------------------------------------------------------

    def prepare(self, statement: Any) -> CoreQuery[Any]:
        """Compile a statement for this engine's dialect, once.

        Hoist this out of the request when you can. `fetch_all` will do it for
        you and cache the result, but that costs a structural cache-key
        computation per call that a hoisted `CoreQuery` does not.
        """
        return CoreQuery(statement, self.dialect)

    def _query_for(self, statement: Any) -> CoreQuery[Any]:
        if isinstance(statement, CoreQuery):
            return statement
        # SQLAlchemy's own structural cache key: two statements built the same
        # way from different literal values share one entry, which is the whole
        # point of compiling once. `.key` rather than the `CacheKey` itself,
        # whose `__hash__` deliberately returns None — only the structural tuple
        # inside it is hashable.
        key = statement._generate_cache_key().key
        query = self._queries.get(key)
        if query is None:
            query = self._queries[key] = self.prepare(statement)
        return query

    # --- reads --------------------------------------------------------------

    @overload
    async def fetch_all(self, statement: CoreQuery[R], **params: Any) -> list[R]: ...

    @overload
    async def fetch_all(self, statement: Any, **params: Any) -> list[Any]: ...

    async def fetch_all(self, statement: Any, **params: Any) -> Any:
        """Hydrated rows. `**params` supplies the statement's `bindparam()` values.

        What each row *is* comes from the statement, not the model:
        `select(User)` yields `User`s, `select(User, Post)` yields
        `(User, Post)` tuples, `select(User.name, User.id)` yields `(str, int)`
        — the same shapes SQLAlchemy returns for the same queries (`planner.py`).
        """
        self._reject_if_in_transaction("fetch_all")
        query = self._require_rows(statement)
        rows, hydrate = await self._run(query, params, self._acquire)
        return hydrate(rows)

    async def fetch_one(self, statement: Any, **params: Any) -> Any:
        """The first row, or None."""
        rows = await self.fetch_all(statement, **params)
        return rows[0] if rows else None

    async def fetch_value(self, statement: Any, **params: Any) -> Any:
        """The first column of the first row, or None.

        For `select(func.count()).select_from(User)` and friends, where planning
        a whole entity would be ceremony around one integer.
        """
        row = await self.fetch_one(statement, **params)
        if row is None:
            return None
        return row[0] if isinstance(row, tuple) else row

    # --- writes -------------------------------------------------------------

    async def execute(self, statement: Any, **params: Any) -> Any:
        """Run a statement that produces no rows, and return the driver's own
        report of what happened — a rowcount, or asyncpg's status tag.

        A statement *with* RETURNING raises here rather than silently discarding
        its rows; use `fetch_all()` for those.
        """
        query = self._query_for(statement)
        if query.returns_rows:
            raise ValueError(
                "this statement produces rows — use fetch_all() to get them, "
                "rather than execute(), which would discard them"
            )
        sql, bound = query.bind(params)
        async with self._acquire() as conn:
            return await self._execute(conn, sql, bound)

    async def execute_many(self, statement: Any, params: Sequence[dict[str, Any]]) -> Any:
        """One compiled statement, many parameter sets, one driver round trip."""
        query = self._query_for(statement)
        shaped = [query.bind(each) for each in params]
        if not shaped:
            return None
        # One compiled statement, so every row shares its SQL; an expanding
        # statement would not, and executemany cannot express that anyway.
        sql = shaped[0][0]
        async with self._acquire() as conn:
            return await self._execute_many(conn, sql, [bound for _, bound in shaped])

    # --- schema -------------------------------------------------------------

    async def create_all(self, metadata: sa.MetaData, *, checkfirst: bool = True) -> None:
        """`CREATE TABLE` for every table in `metadata`, in dependency order.

        The whole reason for this design: the model declaration *is* the table
        declaration, so tests and fixtures stop hand-writing DDL strings. For
        anything versioned, point Alembic at the same `metadata` instead.
        """
        for table in metadata.sorted_tables:
            await self._execute_ddl(sa.schema.CreateTable(table, if_not_exists=checkfirst))
            for index in table.indexes:
                await self._execute_ddl(sa.schema.CreateIndex(index, if_not_exists=checkfirst))

    async def drop_all(self, metadata: sa.MetaData, *, checkfirst: bool = True) -> None:
        for table in reversed(metadata.sorted_tables):
            await self._execute_ddl(sa.schema.DropTable(table, if_exists=checkfirst))

    async def _execute_ddl(self, element: Any) -> None:
        statement = str(element.compile(dialect=self.dialect))
        async with self._acquire() as conn:
            await self._execute(conn, statement, None)

    # --- transactions -------------------------------------------------------

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Raw driver connection, for anything this engine does not model."""
        async with self._acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self, **kwargs: Any) -> AsyncIterator[Transaction]:
        """Several statements on one connection, committed together.

        Commits on clean exit, rolls back on any exception. The yielded
        `Transaction` takes the same statements and reuses the same compiled
        queries and hydrators as the engine, so a read inside a transaction
        costs what a read outside it costs.
        """
        async with self._acquire() as conn, self._block(conn, 0, kwargs) as tx:
            yield tx

    def _reject_if_in_transaction(self, method: str) -> None:
        """Inside `engine.transaction()` this method would take a *different*
        pooled connection, so it would not see the transaction's uncommitted
        writes and would not roll back with it. Fail loudly rather than return
        plausible wrong results."""
        active = _ACTIVE.get()
        if active is not None and active._engine is self:
            raise RuntimeError(
                f"engine.{method}() was called inside engine.transaction(); it would "
                f"run on a different pooled connection and miss the transaction's "
                f"uncommitted state. Use tx.{method}() instead."
            )

    # --- shared plumbing ----------------------------------------------------

    def _require_rows(self, statement: Any) -> CoreQuery[Any]:
        query = self._query_for(statement)
        if not query.returns_rows:
            raise ValueError(
                "this statement produces no rows; hydrating it would return [] and "
                "look like 'nothing matched'. Use execute() for it, or add "
                "returning(...)."
            )
        return query

    async def _run(self, query: CoreQuery[Any], params: dict[str, Any], acquire: Any) -> Any:
        """Execute and return `(rows, hydrator)`.

        The driver is asked to describe its result only while the hydrator is
        still unbuilt — once per statement, not once per request — because the
        per-column `result_processor` needs the DBAPI type codes and postgres
        `Numeric` raises without them.
        """
        sql, bound = query.bind(params)
        hydrate = query._hydrate
        async with acquire() as conn:
            rows, description = await self._fetch(conn, sql, bound, hydrate is None)
        if hydrate is None:
            hydrate = query.hydrator(self.dialect, description)
        return rows, hydrate

    # --- driver hooks -------------------------------------------------------

    @abstractmethod
    async def _open_pool(self) -> Any: ...

    @abstractmethod
    async def _close_pool(self, pool: Any) -> None: ...

    @abstractmethod
    def _acquire(self) -> Any:
        """Async context manager yielding a checked-out driver connection."""

    @abstractmethod
    async def _fetch(
        self, conn: Any, sql: str, params: Any, describe: bool
    ) -> tuple[Any, Any]:
        """Run `sql` and return `(rows, description)`.

        `description` is `cursor.description`-shaped — an iterable of tuples
        whose second element is the DBAPI type code — and is only consulted when
        `describe` is true, so a driver that has to do extra work for it (asyncpg
        must prepare the statement to read attribute OIDs) can skip that work on
        every subsequent call.
        """

    @abstractmethod
    async def _execute(self, conn: Any, sql: str, params: Any) -> Any: ...

    @abstractmethod
    async def _execute_many(self, conn: Any, sql: str, params: Sequence[Any]) -> Any: ...

    @abstractmethod
    def _block(self, conn: Any, depth: int, kwargs: dict[str, Any]) -> Any:
        """Async context manager running a transaction (depth 0) or savepoint."""
