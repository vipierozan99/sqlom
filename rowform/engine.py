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

import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any, TypeVar, overload

import sqlalchemy as sa
from sqlalchemy import Select

from .errors import (
    ConfigurationError,
    EngineStateError,
    StatementError,
    UnsupportedError,
)
from .query import CoreQuery
from .transaction import _ACTIVE, Transaction

_LOG = logging.getLogger("rowform")


def _one_row(statement: Any) -> Any:
    """`statement`, narrowed to a single row where that is safe to do.

    `fetch_one` and `fetch_value` read the whole result and threw away everything
    after the first row, so "get me this user" transferred and hydrated the entire
    table. Adding the LIMIT is the fix, but only for a `Select` that sets none of
    its own:

    * a caller's `.limit()` may be a bind parameter, and replacing it would leave
      their value with nothing to bind to;
    * a `CoreQuery` is already compiled, so there is no statement left to narrow —
      hoist it with `.limit(1)` already applied if you want that.

    An OFFSET without a LIMIT is narrowed too: the first row of *that* statement
    is still what the caller asked for.

    `_limit_clause` is SQLAlchemy-private, like the rest of the compiler surface
    this library reads (`docs/PLAN_CORE_COMPILER.md`); there is no public way to
    ask a Select whether it is limited.
    """
    if isinstance(statement, Select) and statement._limit_clause is None:
        return statement.limit(1)
    return statement

#: What an `observer` is handed after every statement: the SQL as executed, how
#: long the round trip took in seconds, and how many rows came back — `None` for a
#: statement that returns none, where the driver's own report is the useful number
#: and `execute()` already returns it.
Observer = Callable[[str, float, "int | None"], None]

@dataclass(frozen=True, slots=True)
class PoolStats:
    """A snapshot of one engine's pool.

    The `observer` answers "which statement was slow"; this answers the question
    that usually comes next, which is whether anything was waiting for a
    connection at the time. Saturation looks like slow queries from the outside,
    and the two are fixed differently.

    `waiting` is `None` where the driver does not report it — asyncpg's pool
    keeps no waiter count, and rowform's sqlite pool queues on an
    `asyncio.Queue` whose waiters it does not track. A zero would be a claim;
    `None` is the truth.
    """

    #: Connections that exist, idle or not.
    size: int
    #: Connections available to be checked out right now.
    idle: int
    #: The ceiling `size` will not pass.
    max_size: int
    #: Callers currently blocked waiting for one, where the driver reports it.
    waiting: int | None = None

    @property
    def in_use(self) -> int:
        return self.size - self.idle


#: How many compiled statements an engine keeps. Matches SQLAlchemy's own
#: `compiled_cache` default, and for the same reason: an application's statement
#: set is normally small and fixed, but one built from a request — a filter set
#: that varies, an `IN` whose length varies — mints a new cache key every time,
#: and an uncapped dict would hold every one of them for the life of the process.
DEFAULT_CACHE_SIZE = 500

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

    def __init__(
        self,
        dsn: str,
        *,
        observer: Observer | None = None,
        cache_size: int | None = DEFAULT_CACHE_SIZE,
        **pool_kwargs: Any,
    ):
        if cache_size is not None and cache_size < 1:
            raise ConfigurationError(
                f"cache_size must be at least 1, or None for no limit; got {cache_size}"
            )
        self.dsn = dsn
        self.pool: Any = None
        #: Called after every statement with `(sql, seconds, rows)` — the hook for
        #: slow-query logs, per-request counters or a tracing span. Reassignable at
        #: any time; `None` disables it, which is one attribute load and a branch
        #: per statement and nothing at all per row. Exceptions raised inside it
        #: are not caught: it runs on the caller's path, so it must be cheap and
        #: must not throw.
        self.observer = observer
        self._pool_kwargs = pool_kwargs
        self._cache_size = cache_size
        self._queries: OrderedDict[Any, CoreQuery[Any]] = OrderedDict()

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> Any:
        """Open the pool. Idempotent: without the guard a stray second call would
        replace the pool reference and leak the first one, leaving its
        connections open against the server with nothing holding them."""
        if self.pool is None:
            self.pool = await self._open_pool()
            _LOG.debug("pool opened: %s", type(self).__name__)
        return self.pool

    async def close(self) -> None:
        """Close the pool and drop the reference, so a later `connect()` opens a
        fresh one by design rather than by overwriting a closed object. Safe to
        call more than once."""
        pool, self.pool = self.pool, None
        if pool is not None:
            await self._close_pool(pool)
            _LOG.debug("pool closed: %s", type(self).__name__)

    def _require_pool(self) -> Any:
        if self.pool is None:
            raise EngineStateError(
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

        The cache is bounded and least-recently-used. Unbounded, an application
        that builds statements per request holds every one of them forever; a
        plain cap would instead evict whatever happened to be compiled first,
        which for a long-lived service is its startup statements — the hot ones.
        The bookkeeping is one `move_to_end` per cached execute, measured at no
        cost against the flat micro shape.
        """
        if isinstance(statement, CoreQuery):
            return statement, None
        cache_key = statement._generate_cache_key()
        queries = self._queries
        query = queries.get(cache_key.key)
        if query is None:
            query = queries[cache_key.key] = self.prepare(statement)
            if self._cache_size is not None and len(queries) > self._cache_size:
                queries.popitem(last=False)
        else:
            queries.move_to_end(cache_key.key)
        return query, cache_key.bindparams

    @property
    def cached_statements(self) -> int:
        """How many compiled statements are held. Worth watching: an application
        whose statement set is fixed sits at a constant here, and one building
        statements per request pins itself to `cache_size` and recompiles
        forever."""
        return len(self._queries)

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
    def fetch_iter(
        self, statement: CoreQuery[R], *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[R]: ...

    @overload
    def fetch_iter(
        self, statement: Select[tuple[R]], *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[R]: ...

    @overload
    def fetch_iter(
        self, statement: Select[tuple[R, R2]], *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[tuple[R, R2]]: ...

    @overload
    def fetch_iter(
        self, statement: Select[tuple[R, R2, R3]], *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[tuple[R, R2, R3]]: ...

    @overload
    def fetch_iter(
        self, statement: Select[tuple[R, R2, R3, R4]], *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[tuple[R, R2, R3, R4]]: ...

    @overload
    def fetch_iter(
        self, statement: Any, *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[Any]: ...

    def fetch_iter(self, statement: Any, *, chunk: int = 1000, **params: Any) -> Any:
        """The same rows as `fetch_all`, `chunk` at a time, without ever holding
        them all.

            async for user in engine.fetch_iter(sa.select(User), chunk=500):
                await sink.write(user)

        `fetch_all` builds one list, so peak memory is the whole result; an export
        or a backfill over a large table is the case that does not fit. Here each
        chunk is hydrated by the same generated function and handed over row by
        row, so what is live is one chunk, not one result set.

        The connection is held for the whole iteration — that is what makes it a
        cursor rather than repeated `LIMIT`/`OFFSET` queries, and it means a slow
        consumer holds a pooled connection for as long as it takes. Abandoning the
        loop early is safe: leaving the `async for` closes the cursor.

        Not every statement can stream on every driver, and the difference is the
        server's, not this library's: `PsycopgEngine` uses a server-side cursor,
        which postgres cannot `DECLARE` for `INSERT ... RETURNING` — it raises
        `UnsupportedError` saying so. asyncpg streams the same statement through a
        portal, and sqlite streams anything.
        """
        self._reject_if_in_transaction("fetch_iter")
        return self._iterate(statement, chunk, params, self._acquire)

    async def _iterate(
        self, statement: Any, chunk: int, params: dict[str, Any], acquire: Any
    ) -> AsyncIterator[Any]:
        """Shared by `Engine.fetch_iter` and `Transaction.fetch_iter`; the only
        difference is whether the connection comes from the pool or is the
        transaction's own."""
        if chunk < 1:
            raise ConfigurationError(f"chunk must be at least 1, got {chunk}")
        query, extracted = self._require_rows(statement)
        sql, bound = query.bind(params, extracted)
        start = perf_counter() if self.observer is not None else 0.0
        total = 0
        async with acquire() as conn:
            async for rows, description in self._stream(conn, sql, bound, chunk, query):
                hydrate = query._hydrate
                if hydrate is None:
                    hydrate = query.hydrator(self.dialect, description)
                total += len(rows)
                for row in hydrate(rows):
                    yield row
        # One call for the whole stream, with the total row count. Unlike the
        # other paths, this duration includes the consumer's own time between
        # chunks — there is no round trip to time in isolation.
        self._observe(sql, start, total)

    @overload
    async def fetch_one(self, statement: CoreQuery[R], **params: Any) -> R | None: ...

    @overload
    async def fetch_one(self, statement: Select[tuple[R]], **params: Any) -> R | None: ...

    @overload
    async def fetch_one(self, statement: Any, **params: Any) -> Any: ...

    async def fetch_one(self, statement: Any, **params: Any) -> Any:
        """The first row, or None.

        The statement is narrowed to one row where that is safe (`_one_row`), so
        this is a `LIMIT 1` rather than a whole result set with everything after
        the first discarded.
        """
        rows = await self.fetch_all(_one_row(statement), **params)
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
            raise StatementError(
                "this statement produces rows — use fetch_all() to get them, "
                "rather than execute(), which would discard them"
            )
        sql, bound = query.bind(params, extracted)
        start = perf_counter() if self.observer is not None else 0.0
        async with self._acquire() as conn:
            result = await self._execute(conn, sql, bound)
        self._observe(sql, start, None)
        return result

    async def execute_many(self, statement: Any, params: Sequence[dict[str, Any]]) -> Any:
        """One compiled statement, many parameter sets, one driver round trip."""
        query, extracted = self._query_for(statement)
        shaped = [query.bind(each, extracted) for each in params]
        if not shaped:
            return None
        # One compiled statement, so every row shares its SQL; an expanding
        # statement would not, and executemany cannot express that anyway.
        sql = shaped[0][0]
        start = perf_counter() if self.observer is not None else 0.0
        async with self._acquire() as conn:
            result = await self._execute_many(conn, sql, [bound for _, bound in shaped])
        self._observe(sql, start, None)
        return result

    async def copy_in(
        self,
        table: sa.Table,
        rows: Sequence[dict[str, Any]],
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """Bulk-load rows through the server's COPY path. Returns how many.

            await engine.copy_in(User.__table__, [{"id": 1, "name": "ada"}, ...])

        `execute_many` sends one INSERT per row's worth of parameters; COPY sends
        a stream the server parses without planning a statement per row, which is
        the difference between a backfill that takes minutes and one that takes
        seconds. It is a load path, not a write path: no RETURNING, no ON
        CONFLICT, no per-row result.

        `columns` defaults to every column of the table; name a subset to let
        server defaults fill the rest. Every row must carry each named column.

        **Values go through the same bind processors a parameterised INSERT
        uses** (`column.type._cached_bind_processor`), because COPY bypasses the
        statement path where those normally run — and a `Decimal`, `datetime`,
        `Enum` or `dict` that skipped them would land as something the round trip
        does not return unchanged. The tests assert `copy_in` and `execute_many`
        produce identical rows for every type in the type map.

        Refused inside `engine.transaction()`, as the reads are: it would take a
        different pooled connection and commit on its own, so a rollback of the
        surrounding block would leave the loaded rows behind.
        """
        self._reject_if_in_transaction("copy_in")
        if not rows:
            return 0
        names = list(columns) if columns is not None else [c.key for c in table.columns]
        selected = [table.columns[name] for name in names]
        processors = [
            column.type._cached_bind_processor(self.dialect) for column in selected
        ]
        records = [
            tuple(
                processor(row[column.key]) if processor is not None else row[column.key]
                for column, processor in zip(selected, processors, strict=True)
            )
            for row in rows
        ]
        start = perf_counter() if self.observer is not None else 0.0
        label = f"COPY {table.name} ({', '.join(names)})"
        async with self._acquire() as conn:
            copied = await self._copy_in(conn, table, [c.name for c in selected], records)
        self._observe(label, start, copied)
        return copied

    async def _copy_in(
        self, conn: Any, table: sa.Table, columns: Sequence[str], records: Sequence[tuple]
    ) -> int:
        """Per-driver COPY. The default is a refusal, since only the postgres
        engines have one."""
        raise UnsupportedError(
            f"{type(self).__name__} has no COPY path — that is a postgres feature. "
            f"Use execute_many() instead."
        )

    def _pipeline(self, conn: Any) -> Any:
        """Per-driver pipeline mode. Only psycopg has one."""
        raise UnsupportedError(
            f"{type(self).__name__} has no pipeline mode. psycopg3 is the only "
            f"driver here that implements one; asyncpg has no such API, and "
            f"sqlite is a local file with no round trip to hide."
        )

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
            raise EngineStateError(
                f"engine.{method}() was called inside engine.transaction(); it would "
                f"run on a different pooled connection and miss the transaction's "
                f"uncommitted state. Use tx.{method}() instead."
            )

    # --- shared plumbing ----------------------------------------------------

    def _require_rows(self, statement: Any) -> tuple[CoreQuery[Any], Any]:
        query, extracted = self._query_for(statement)
        if not query.returns_rows:
            raise StatementError(
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
        start = perf_counter() if self.observer is not None else 0.0
        async with acquire() as conn:
            rows, description = await self._fetch(conn, sql, bound, hydrate is None)
        if hydrate is None:
            hydrate = query.hydrator(self.dialect, description)
        self._observe(sql, start, len(rows))
        return rows, hydrate

    def _observe(self, sql: str, start: float, rows: int | None) -> None:
        """Hand one completed statement to the `observer`, if there is one.

        Timing covers the driver round trip, not hydration: hydration is the part
        this library controls and benchmarks, while the round trip is what a
        slow-query log is actually about.
        """
        observer = self.observer
        if observer is not None:
            observer(sql, perf_counter() - start, rows)

    # --- driver hooks -------------------------------------------------------

    def pool_stats(self) -> PoolStats:
        """A snapshot of the pool: how many connections exist, how many are free,
        and — where the driver reports it — how many callers are waiting.

        Pair it with the `observer`: a slow statement and a saturated pool look
        the same from the outside and are fixed differently.
        """
        return self._pool_stats(self._require_pool())

    @abstractmethod
    def _pool_stats(self, pool: Any) -> PoolStats:
        """Read the driver's own counters. Every pool here keeps them already, so
        nothing is tracked twice."""

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
    def _stream(
        self, conn: Any, sql: str, params: Any, chunk: int, query: CoreQuery[Any]
    ) -> AsyncIterator[tuple[Any, Any]]:
        """Yield `(rows, description)` per chunk, incrementally from the server.

        Same `description` contract as `_fetch`, but supplied on every chunk
        because the first one is where the hydrator gets built. Each driver's own
        incremental primitive differs — `fetchmany` on a sqlite cursor, a portal
        on asyncpg, a `DECLARE`d cursor on psycopg — and so does what it can
        stream, which is why the statement is passed in.
        """

    @abstractmethod
    async def _execute(self, conn: Any, sql: str, params: Any) -> Any: ...

    @abstractmethod
    async def _execute_many(self, conn: Any, sql: str, params: Sequence[Any]) -> Any: ...

    @abstractmethod
    def _block(self, conn: Any, depth: int, kwargs: dict[str, Any]) -> Any:
        """Async context manager running a transaction (depth 0) or savepoint."""
