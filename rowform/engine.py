"""What is left of an engine once SQLAlchemy owns both the SQL and the pool: a
compiled-statement cache, an execute, and the compiled hydrator.

Engines used to generate SQL. Then they stopped, and `CoreQuery` held the
compiled string and the parameter recipe. Now they no longer pool either —
`rf.Engine` wraps a SQLAlchemy `AsyncEngine` and takes its connections from it:

    sa_engine = create_async_engine("postgresql+asyncpg://localhost/app")
    db = rf.Engine(sa_engine)

    users = await db.fetch_all(sa.select(User))

**Why give up rowform's own pool.** It was measurably cheaper — ~0.09 ms per
checkout against SQLAlchemy's ~0.40 ms on the same box, and that gap is real and
fixed (`docs/PLAN_SQLA_API.md` §2). It is paid per *checkout*, not per row and
not per statement: with the connection in hand, executing on a SQLAlchemy-pooled
connection costs what executing on rowform's own did, to within the noise. What
it buys is the thing an own pool structurally cannot: rowform reads that run
*inside somebody else's transaction*, so an application can adopt this one query
at a time without giving up its engine, its sessions or its migrations
(`CLAUDE.md`, goal 2).

rowform never opens or disposes the `AsyncEngine`. That stays the caller's, which
is the whole point of handing one in.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any, TypeVar, overload

import sqlalchemy as sa
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .connection import _ACTIVE, Connection
from .drivers import Driver, driver_for
from .errors import (
    ConfigurationError,
    EngineStateError,
    StatementError,
)
from .query import CoreQuery

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


class Engine:
    """rowform's row layer over a SQLAlchemy `AsyncEngine`. See module docstring."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        observer: Observer | None = None,
        cache_size: int | None = DEFAULT_CACHE_SIZE,
    ):
        if cache_size is not None and cache_size < 1:
            raise ConfigurationError(
                f"cache_size must be at least 1, or None for no limit; got {cache_size}"
            )
        if not isinstance(engine, AsyncEngine):
            raise ConfigurationError(
                f"rf.Engine wraps a SQLAlchemy AsyncEngine, got {type(engine).__name__}. "
                f"Build one with create_async_engine(url) and hand it here; rowform "
                f"does not open connections of its own."
            )
        #: The wrapped engine. rowform reads its dialect and takes connections
        #: from it, and never opens or disposes it.
        self.sa_engine = engine
        self.driver: Driver = driver_for(engine.dialect)
        self.driver.configure(engine)
        #: Called after every statement with `(sql, seconds, rows)` — the hook for
        #: slow-query logs, per-request counters or a tracing span. Reassignable at
        #: any time; `None` disables it, which is one attribute load and a branch
        #: per statement and nothing at all per row. Exceptions raised inside it
        #: are not caught: it runs on the caller's path, so it must be cheap and
        #: must not throw.
        self.observer = observer
        self._cache_size = cache_size
        self._queries: OrderedDict[Any, CoreQuery[Any]] = OrderedDict()

    @property
    def dialect(self) -> Any:
        """The dialect statements compile for, and whose type `result_processor`s
        decode rows. It is the engine's own — one SQLAlchemy has run
        `initialize()` against, so it knows the server version, where a freshly
        constructed dialect does not."""
        return self.sa_engine.dialect

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.sa_engine.url!r}>"

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

        Takes a connection from the pool for this one statement and does not open
        a transaction. To run several statements together — or inside anyone
        else's transaction — use `connect()` or `begin()`.
        """
        self._reject_if_in_transaction("fetch_all")
        query, extracted = self._require_rows(statement)
        rows, hydrate = await self._run(query, params, self._connection, extracted)
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

            async for user in db.fetch_iter(sa.select(User), chunk=500):
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
        server's, not this library's: psycopg uses a server-side cursor, which
        postgres cannot `DECLARE` for `INSERT ... RETURNING` — it raises
        `UnsupportedError` saying so. asyncpg streams the same statement through a
        portal, and sqlite streams anything.
        """
        self._reject_if_in_transaction("fetch_iter")
        return self._iterate(statement, chunk, params, self._connection)

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
            async for rows, description in self.driver.stream(conn, sql, bound, chunk, query):
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

    async def execute(self, statement: Any, parameters: Any = None, **params: Any) -> Any:
        """Run a statement in a scope of its own and return a SQLAlchemy `Result`.

        The compatibility track's one-shot: equivalent to opening `connect()`,
        executing, and closing. `parameters` is a dict, or a list of dicts for an
        executemany, exactly as `AsyncConnection.execute` takes it; `**params` is
        rowform's extension and merges into it.

        A statement that returns rows runs without committing; one that does not
        is committed, because a write on a connection the pool resets would
        otherwise be discarded on two of the three drivers.
        """
        query, _ = self._query_for(statement)
        async with self._scope(commit=not query.returns_rows) as conn:
            return await conn.execute(statement, parameters, **params)

    async def scalar(self, statement: Any, parameters: Any = None, **params: Any) -> Any:
        """`execute(...).scalar()`, in a scope of its own."""
        return (await self.execute(statement, parameters, **params)).scalar()

    async def scalars(self, statement: Any, parameters: Any = None, **params: Any) -> Any:
        """`execute(...).scalars()`, in a scope of its own. The rows are already
        buffered, so the `ScalarResult` outlives the connection."""
        return (await self.execute(statement, parameters, **params)).scalars()

    async def execute_many(self, statement: Any, params: Sequence[dict[str, Any]]) -> Any:
        """One compiled statement, many parameter sets, one driver round trip.

        rowform's own: returns the driver's report rather than a `Result`. The
        SQLAlchemy spelling of the same thing is `execute(stmt, [ ... ])`.
        """
        async with self._scope(commit=True) as conn:
            return await conn.execute_many(statement, params)

    async def copy_in(
        self,
        table: sa.Table,
        rows: Sequence[dict[str, Any]],
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """Bulk-load rows through the server's COPY path. Returns how many.

            await db.copy_in(User.__table__, [{"id": 1, "name": "ada"}, ...])

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

        Refused inside `transaction()`, as the reads are: it would take a
        different pooled connection and commit on its own, so a rollback of the
        surrounding block would leave the loaded rows behind.
        """
        self._reject_if_in_transaction("copy_in")
        async with self._checkout(commit=True) as (_, conn):
            return await self._copy_in(conn, table, rows, columns)

    async def _copy_in(
        self,
        conn: Any,
        table: sa.Table,
        rows: Sequence[dict[str, Any]],
        columns: Sequence[str] | None,
    ) -> int:
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
        copied = await self.driver.copy_in(conn, table, [c.name for c in selected], records)
        self._observe(label, start, copied)
        return copied

    # --- schema -------------------------------------------------------------

    async def create_all(self, metadata: sa.MetaData) -> None:
        """Create every table in `metadata`, in dependency order.

        The whole reason for this design: the model declaration *is* the table
        declaration, so tests and fixtures stop hand-writing DDL strings.

        SQLAlchemy's own `SchemaGenerator` through `run_sync`, so this gets
        dependency ordering, indexes, and the `CREATE TYPE` a postgres enum
        column needs before its table. `checkfirst=False` — this is bootstrap,
        not schema management; for an existing database point Alembic at the same
        `metadata`, which is the whole point of building a real `MetaData`.
        """
        async with self.sa_engine.begin() as conn:
            await conn.run_sync(metadata.create_all, checkfirst=False)

    async def drop_all(self, metadata: sa.MetaData, *, ignore_missing: bool = True) -> None:
        """Drop every table in `metadata`, dependants first.

        `ignore_missing` becomes SQLAlchemy's `checkfirst`, which asks the
        catalogue what exists rather than dropping blind and swallowing the
        error — so this stays usable as a test reset without knowing what state
        the database was left in.
        """
        async with self.sa_engine.begin() as conn:
            await conn.run_sync(metadata.drop_all, checkfirst=ignore_missing)

    # --- connections and transactions ---------------------------------------

    @asynccontextmanager
    async def _checkout(self, *, commit: bool = False) -> AsyncIterator[tuple[Any, Any]]:
        """One pooled checkout, as `(sqlalchemy_connection, driver_connection)`.

        `driver_connection` is the real `asyncpg.Connection` /
        `aiosqlite.Connection` / `psycopg.AsyncConnection` under SQLAlchemy's
        adapter, so statements run on it are awaited directly rather than through
        `greenlet_spawn` — measured at ~0.17 ms per statement cheaper than going
        through the adapter's DBAPI shim (`docs/PLAN_SQLA_API.md` §2c).

        **`commit` is not a nicety.** Without it a one-shot write is *silently
        discarded* on two of the three drivers: `connect()` hands back a
        connection the pool resets with a rollback on release, and a statement
        run straight on the driver connection sits inside whatever transaction
        that driver opened for it — pysqlite's implicit BEGIN, psycopg's
        transactional connection. Only asyncpg is autocommit, so only asyncpg
        would have committed. `begin()` makes all three agree, on the safe answer.

        Cancellation is the other thing this handles, and the driver connection is
        resolved *before* the yield so that path needs no await of its own while
        unwinding.
        """
        cm = self.sa_engine.begin() if commit else self.sa_engine.connect()
        async with cm as conn:
            driver_conn = (await conn.get_raw_connection()).driver_connection
            try:
                yield conn, driver_conn
            except asyncio.CancelledError:
                # The statement may still be running with nobody waiting for it,
                # and SQLAlchemy's pool will hand this connection to the next
                # borrower regardless. Measured on aiosqlite, which runs each
                # statement in a worker thread the cancelled task cannot stop:
                # without this the next borrower queues behind abandoned work,
                # which looks exactly like a leaked connection.
                await self.driver.on_cancelled(driver_conn)
                raise

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """The read seam: a checked-out driver connection, nothing committed.

        Every read goes through here, which is what makes a mock engine possible:
        override this and nothing else changes.
        """
        async with self._checkout() as (_, driver_conn):
            yield driver_conn

    @asynccontextmanager
    async def _write_connection(self) -> AsyncIterator[Any]:
        """`_connection()`, committed on the way out. See `_checkout`."""
        async with self._checkout(commit=True) as (_, driver_conn):
            yield driver_conn

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        """Raw driver connection, for anything this engine does not model."""
        async with self._connection() as conn:
            yield conn

    @asynccontextmanager
    async def _scope(self, *, commit: bool) -> AsyncIterator[Connection]:
        """One checkout as a `Connection`, for the engine's own one-shots.

        Not `connect()`: these do not autobegin for a read, which is what keeps
        the shorthand a shorthand rather than a transaction (a `begin`/`commit`
        pair measured at +31% on a single 1000-row read).
        """
        async with self._checkout(commit=commit) as (sa_conn, driver_conn):
            yield Connection(self, sa_conn, driver_conn, owns=False)

    @asynccontextmanager
    async def connect(self, bind: Any = None, **execution_options: Any) -> AsyncIterator[Connection]:
        """A connection scope — `AsyncEngine.connect()`, with rowform's two tracks
        on it.

            async with db.connect() as conn:
                users = (await conn.execute(sa.select(User))).scalars().all()
                await conn.execute(sa.insert(User.__table__).values(name="ada"))
                await conn.commit()

        Commit-as-you-go, as SQLAlchemy has it: the first statement begins a
        transaction and leaving the block without `commit()` rolls it back. Use
        `begin()` for the begin-once form.

        `bind=` runs on a connection somebody else owns — an `AsyncConnection` or
        an `AsyncSession`. Statements then see that transaction's uncommitted
        writes and roll back with it, and rowform neither begins nor ends
        anything: the caller's block is the scope.

            async with Session() as session, session.begin():
                session.add(AuditRow(...))
                async with db.connect(bind=session) as conn:
                    hot = await conn.fetch_all(sa.select(User))

        `execution_options` reach `AsyncConnection.execution_options()`, so
        isolation is spelled the way SQLAlchemy spells it —
        `isolation_level="SERIALIZABLE"`, `postgresql_readonly=True`.
        """
        if bind is not None:
            if execution_options:
                raise ConfigurationError(
                    "execution_options cannot be set on a connection rowform did not "
                    "open; configure them where the connection was opened"
                )
            sa_conn = await self._resolve(bind)
            yield Connection(self, sa_conn, await self._driver_connection(sa_conn), owns=False)
            return
        async with self._checkout() as (sa_conn, driver_conn):
            if execution_options:
                await sa_conn.execution_options(**execution_options)
            conn = Connection(self, sa_conn, driver_conn)
            conn._enter()
            try:
                yield conn
            finally:
                conn._exit()

    @asynccontextmanager
    async def begin(self, **execution_options: Any) -> AsyncIterator[Connection]:
        """A connection scope with a transaction already open — `AsyncEngine.begin()`.

        Commits on clean exit, rolls back on any exception, and nests as
        savepoints through `conn.begin_nested()`. All three are SQLAlchemy's, on
        every driver.
        """
        async with self._checkout() as (sa_conn, driver_conn):
            if execution_options:
                await sa_conn.execution_options(**execution_options)
            async with sa_conn.begin():
                conn = Connection(self, sa_conn, driver_conn)
                conn._enter()
                try:
                    yield conn
                finally:
                    conn._exit()

    async def _resolve(self, target: Any) -> AsyncConnection:
        """The `AsyncConnection` behind an `AsyncConnection` or an `AsyncSession`."""
        if isinstance(target, AsyncConnection):
            conn = target
        else:
            connection = getattr(target, "connection", None)
            if connection is None:
                raise ConfigurationError(
                    f"bind= takes an AsyncConnection or an AsyncSession, got "
                    f"{type(target).__name__}"
                )
            conn = await connection()
        driver = conn.engine.dialect.driver
        if driver != self.dialect.driver:
            raise ConfigurationError(
                f"this engine compiles for {self.dialect.driver} and that connection "
                f"is {driver}; the compiled SQL would use the wrong paramstyle. Use "
                f"an rf.Engine wrapping that connection's own engine."
            )
        return conn

    @staticmethod
    async def _driver_connection(conn: AsyncConnection) -> Any:
        return (await conn.get_raw_connection()).driver_connection

    def _reject_if_in_transaction(self, method: str) -> None:
        """Inside `connect()` or `begin()` this method would take a *different*
        pooled connection, so it would not see the scope's uncommitted writes and
        would not roll back with it. Fail loudly rather than return plausible
        wrong results."""
        active = _ACTIVE.get()
        if active is not None and active._engine is self:
            raise EngineStateError(
                f"engine.{method}() was called inside connect()/begin(); it would run "
                f"on a different pooled connection and miss the scope's uncommitted "
                f"state. Use conn.{method}() instead."
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
            rows, description = await self.driver.fetch(conn, sql, bound, hydrate is None)
        if hydrate is None:
            hydrate = query.hydrator(self.dialect, description)
        self._observe(sql, start, len(rows))
        return rows, hydrate

    def _chunks(self, query: CoreQuery[Any], params: dict[str, Any], extracted: Any,
                default_chunk: int, acquire: Any) -> Any:
        """A factory of async chunk iterators for `Connection.stream()`.

        Called with the size SQLAlchemy asks for — `result.partitions(50)` fetches
        fifty at a time — falling back to the `chunk=` the caller set.
        """

        async def chunks(size: int | None) -> AsyncIterator[list[Any]]:
            wanted = size or default_chunk
            if wanted < 1:
                raise ConfigurationError(f"chunk must be at least 1, got {wanted}")
            sql, bound = query.bind(params, extracted)
            start = perf_counter() if self.observer is not None else 0.0
            total = 0
            async with acquire() as conn:
                async for rows, description in self.driver.stream(
                    conn, sql, bound, wanted, query
                ):
                    hydrate = query._hydrate
                    if hydrate is None:
                        hydrate = query.hydrator(self.dialect, description)
                    total += len(rows)
                    yield hydrate(rows)
            self._observe(sql, start, total)

        return chunks

    def _observe(self, sql: str, start: float, rows: int | None) -> None:
        """Hand one completed statement to the `observer`, if there is one.

        Timing covers the driver round trip, not hydration: hydration is the part
        this library controls and benchmarks, while the round trip is what a
        slow-query log is actually about.
        """
        observer = self.observer
        if observer is not None:
            observer(sql, perf_counter() - start, rows)
