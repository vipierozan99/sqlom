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
from sqlalchemy import Select

from .query import CoreQuery
from .transaction import _ACTIVE, Transaction

# One type variable per selected entity. The overloads below are written out per
# arity rather than with a variadic, because that is exactly the information a
# checker has: `Select` is parameterised by a tuple of its selected types.
R = TypeVar("R")
R2 = TypeVar("R2")
R3 = TypeVar("R3")
R4 = TypeVar("R4")


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

    @overload
    def prepare(self, statement: Select[tuple[R]]) -> CoreQuery[R]: ...

    @overload
    def prepare(self, statement: Select[tuple[R, R2]]) -> CoreQuery[tuple[R, R2]]: ...

    @overload
    def prepare(self, statement: Select[tuple[R, R2, R3]]) -> CoreQuery[tuple[R, R2, R3]]: ...

    @overload
    def prepare(
        self, statement: Select[tuple[R, R2, R3, R4]]
    ) -> CoreQuery[tuple[R, R2, R3, R4]]: ...

    @overload
    def prepare(self, statement: Any) -> CoreQuery[Any]: ...

    def prepare(self, statement: Any) -> Any:
        """Compile a statement for this engine's dialect, once.

        Hoist this out of the request when you can. `fetch_all` will do it for
        you and cache the result, but that costs a structural cache-key
        computation per call that a hoisted `CoreQuery` does not.
        """
        return CoreQuery(statement, self.dialect)

    def _query_for(self, statement: Any) -> tuple[CoreQuery[Any], Any]:
        """The compiled query, plus this statement's own literal values.

        SQLAlchemy's structural cache key deliberately ignores literals, so two
        statements built the same way from different values share one entry —
        that is what makes compiling once worthwhile. The consequence is that the
        cached compiled object holds the *first* statement's literals, so the
        caller's have to travel separately as `CacheKey.bindparams`. Returning
        the two together is what stops that being forgettable.

        `.key` rather than the `CacheKey` itself, whose `__hash__` deliberately
        returns None — only the structural tuple inside it is hashable.
        """
        if isinstance(statement, CoreQuery):
            return statement, None
        cache_key = statement._generate_cache_key()
        query = self._queries.get(cache_key.key)
        if query is None:
            query = self._queries[cache_key.key] = self.prepare(statement)
        return query, cache_key.bindparams

    # --- reads --------------------------------------------------------------

    @overload
    async def fetch_all(self, statement: CoreQuery[R], **params: Any) -> list[R]: ...

    @overload
    async def fetch_all(self, statement: Select[tuple[R]], **params: Any) -> list[R]: ...

    @overload
    async def fetch_all(
        self, statement: Select[tuple[R, R2]], **params: Any
    ) -> list[tuple[R, R2]]: ...

    @overload
    async def fetch_all(
        self, statement: Select[tuple[R, R2, R3]], **params: Any
    ) -> list[tuple[R, R2, R3]]: ...

    @overload
    async def fetch_all(
        self, statement: Select[tuple[R, R2, R3, R4]], **params: Any
    ) -> list[tuple[R, R2, R3, R4]]: ...

    @overload
    async def fetch_all(self, statement: Any, **params: Any) -> list[Any]: ...

    async def fetch_all(self, statement: Any, **params: Any) -> Any:
        """Hydrated rows. `**params` supplies the statement's `bindparam()` values.

        What each row *is* comes from the statement, not the model. One selected
        entity yields that entity — `select(User)` gives `User`s and
        `select(User.name)` gives `str`s; two or more yield a tuple, so
        `select(User, Post)` gives `(User, Post)` and `select(User.name, User.id)`
        gives `(str, int)` (`planner.py`).

        The overloads above mirror that rule exactly, which is why it is stated in
        terms of arity: a checker can tell `Select[Tuple[User]]` from
        `Select[Tuple[User, Post]]`, but not `Select[Tuple[User]]` from
        `Select[Tuple[str]]`. Past four selected entities the row degrades to
        `list[Any]`.
        """
        self._reject_if_in_transaction("fetch_all")
        query, extracted = self._require_rows(statement)
        rows, hydrate = await self._run(query, params, self._acquire, extracted)
        return hydrate(rows)

    @overload
    async def fetch_one(self, statement: CoreQuery[R], **params: Any) -> R | None: ...

    @overload
    async def fetch_one(self, statement: Select[tuple[R]], **params: Any) -> R | None: ...

    @overload
    async def fetch_one(self, statement: Any, **params: Any) -> Any: ...

    async def fetch_one(self, statement: Any, **params: Any) -> Any:
        """The first row, or None."""
        rows = await self.fetch_all(statement, **params)
        return rows[0] if rows else None

    async def fetch_value(self, statement: Any, **params: Any) -> Any:
        """The first column of the first row, or None.

        Distinct from `fetch_one` only for a multi-entity statement, since a
        single selected entity already arrives unwrapped.
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
        query, extracted = self._query_for(statement)
        if query.returns_rows:
            raise ValueError(
                "this statement produces rows — use fetch_all() to get them, "
                "rather than execute(), which would discard them"
            )
        sql, bound = query.bind(params, extracted)
        async with self._acquire() as conn:
            return await self._execute(conn, sql, bound)

    async def execute_many(self, statement: Any, params: Sequence[dict[str, Any]]) -> Any:
        """One compiled statement, many parameter sets, one driver round trip."""
        query, extracted = self._query_for(statement)
        shaped = [query.bind(each, extracted) for each in params]
        if not shaped:
            return None
        # One compiled statement, so every row shares its SQL; an expanding
        # statement would not, and executemany cannot express that anyway.
        sql = shaped[0][0]
        async with self._acquire() as conn:
            return await self._execute_many(conn, sql, [bound for _, bound in shaped])

    # --- schema -------------------------------------------------------------

    async def create_all(self, metadata: sa.MetaData) -> None:
        """Create every table in `metadata`, in dependency order.

        The whole reason for this design: the model declaration *is* the table
        declaration, so tests and fixtures stop hand-writing DDL strings.

        This is bootstrap, not schema management — it assumes nothing exists yet
        and has no `checkfirst`, because answering "does this exist?" needs a
        catalogue query per dialect. For an existing database, point Alembic at
        the same `metadata`; that is the whole point of building a real
        `MetaData` in the first place.
        """
        for statement in self._ddl(metadata, drop=False):
            await self._execute_ddl(statement)

    async def drop_all(self, metadata: sa.MetaData, *, ignore_missing: bool = True) -> None:
        """Drop every table in `metadata`, dependants first.

        `ignore_missing` skips over anything that is not there, so this is usable
        as a test reset without knowing what state the database was left in.
        """
        for statement in self._ddl(metadata, drop=True):
            try:
                await self._execute_ddl(statement)
            except Exception:
                if not ignore_missing:
                    raise

    def _ddl(self, metadata: sa.MetaData, *, drop: bool) -> list[str]:
        """The exact DDL SQLAlchemy itself would emit, without a connection.

        `create_mock_engine` runs the real `SchemaGenerator`, so this gets
        dependency ordering, indexes, and the `CREATE TYPE` that a postgres enum
        column needs before its table — all things a hand-rolled loop over
        `sorted_tables` silently omits.
        """
        statements: list[str] = []

        def collect(element: Any, *_: Any, **__: Any) -> None:
            statements.append(str(element.compile(dialect=self.dialect)).strip())

        mock = sa.create_mock_engine(f"{self.dialect.name}+{self.dialect.driver}://", collect)
        if drop:
            metadata.drop_all(mock, checkfirst=False)
        else:
            metadata.create_all(mock, checkfirst=False)
        return statements

    async def _execute_ddl(self, statement: str) -> None:
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

    def _require_rows(self, statement: Any) -> tuple[CoreQuery[Any], Any]:
        query, extracted = self._query_for(statement)
        if not query.returns_rows:
            raise ValueError(
                "this statement produces no rows; hydrating it would return [] and "
                "look like 'nothing matched'. Use execute() for it, or add "
                "returning(...)."
            )
        return query, extracted

    async def _run(
        self, query: CoreQuery[Any], params: dict[str, Any], acquire: Any, extracted: Any = None
    ) -> Any:
        """Execute and return `(rows, hydrator)`.

        The driver is asked to describe its result only while the hydrator is
        still unbuilt — once per statement, not once per request — because the
        per-column `result_processor` needs the DBAPI type codes and postgres
        `Numeric` raises without them.
        """
        sql, bound = query.bind(params, extracted)
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
