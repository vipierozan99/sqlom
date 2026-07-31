"""What actually differs between drivers, once SQLAlchemy owns the pool.

Each of these used to be an `Engine` subclass that also opened a pool, checked
connections out of it, and ran its own BEGIN. None of that is here any more: the
connection arrives from `AsyncEngine`, and `conn.begin()`/`begin_nested()` open
the transaction. What is left is the part no two drivers agree on —

* how to run a string and get rows plus a result description back,
* how to stream a result incrementally,
* whether there is a COPY path or a pipeline mode at all.

The dialect comes from the wrapping engine rather than from a class attribute,
so it is one SQLAlchemy has already run `initialize()` against — it knows the
server version and the default schema, where a freshly constructed dialect does
not.

Nothing here imports a driver. The pool code did (`asyncpg.create_pool`,
`aiosqlite.connect`, `psycopg_pool`); these methods only call methods on a
connection somebody else opened, which is why the asyncpg driver no longer needs
to be exported lazily to keep `import rowform` free of it.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event

from .errors import ConfigurationError, UnsupportedError
from .query import CoreQuery

# Cursor names are per *session*, so two streams sharing one connection — which is
# exactly what `conn.fetch_iter()` inside another `conn.fetch_iter()` does — must not
# ask for the same name. A fixed one raises `DuplicateCursor: cursor
# "rowform_stream" already exists` on the second.
_STREAM_NAMES = itertools.count()


class Driver(ABC):
    """One driver's execution primitives. Held by an `Engine`, never subclassed
    per database — `driver_for()` picks the one the dialect names."""

    def __init__(self, dialect: Any):
        self.dialect = dialect

    def configure(self, engine: Any) -> None:
        """Per-driver setup on the `AsyncEngine` being wrapped. Default: none.

        This is the seam the asyncpg JSON codec registration used to need — that
        one is gone, because SQLAlchemy's dialect does it in its own `on_connect`
        now that SQLAlchemy is what opens the connection.
        """

    async def on_cancelled(self, conn: Any) -> None:
        """Called while unwinding a `CancelledError`, before the connection goes
        back to the pool. Default: nothing to do.

        Measured across all three drivers and all three read paths before any of
        this existed: asyncpg and psycopg were already correct, because both
        cancel server-side and hand back a clean connection. sqlite was not.
        """

    @abstractmethod
    async def fetch(
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
    def stream(
        self, conn: Any, sql: str, params: Any, chunk: int, query: CoreQuery[Any]
    ) -> AsyncIterator[tuple[Any, Any]]:
        """Yield `(rows, description)` per chunk, incrementally from the server.

        Same `description` contract as `fetch`, but supplied on every chunk
        because the first one is where the hydrator gets built. Each driver's own
        incremental primitive differs — `fetchmany` on a sqlite cursor, a portal
        on asyncpg, a `DECLARE`d cursor on psycopg — and so does what it can
        stream, which is why the statement is passed in.
        """

    @abstractmethod
    async def execute(self, conn: Any, sql: str, params: Any) -> Any: ...

    @abstractmethod
    async def execute_many(self, conn: Any, sql: str, params: Sequence[Any]) -> Any: ...

    async def copy_in(
        self, conn: Any, table: sa.Table, columns: Sequence[str], records: Sequence[tuple]
    ) -> int:
        """Per-driver COPY. The default is a refusal, since only the postgres
        drivers have one."""
        raise UnsupportedError(
            f"{self.dialect.driver} has no COPY path — that is a postgres feature. "
            f"Use execute_many() instead."
        )

    def pipeline(self, conn: Any) -> Any:
        """Per-driver pipeline mode. Only psycopg has one."""
        raise UnsupportedError(
            f"{self.dialect.driver} has no pipeline mode. psycopg3 is the only "
            f"driver here that implements one; asyncpg has no such API, and "
            f"sqlite is a local file with no round trip to hide."
        )


class SqliteDriver(Driver):
    """aiosqlite.

    **sqlite is where bypassing SQLAlchemy's `Row` is most dangerous**, because
    it stores `Date`/`DateTime`/`Time` as strings and booleans as integers, and
    returns them that way. Nothing here special-cases that: `compile.py` asks
    each column's type for its `result_processor` and gets sqlite's own
    `str_to_datetime` and friends, the same functions `Row` would have run.
    """

    def configure(self, engine: Any) -> None:
        """Take pysqlite's implicit transaction handling out of the way.

        **Savepoints are silently broken without this.** pysqlite opens a
        transaction of its own before DML but not before a `SAVEPOINT`, so the
        savepoint SQLAlchemy issues for `begin_nested()` lands *outside* the
        transaction the following INSERT opens — and rolling the outer block back
        then leaves the inner block's rows behind. Measured before this existed:
        a released savepoint survived its outer rollback.

        This is SQLAlchemy's own documented recipe for pysqlite ("Serializable
        isolation / Savepoints / Transactional DDL"), and it restores exactly what
        rowform's own sqlite pool used to do by opening connections with
        `isolation_level=None`. Registered on the engine rather than asked of the
        caller, because the failure it prevents is silent.
        """
        sync = engine.sync_engine
        if getattr(sync, "_rowform_sqlite_configured", False):
            return
        sync._rowform_sqlite_configured = True

        @event.listens_for(sync, "connect")
        def _no_implicit_begin(dbapi_connection: Any, _record: Any) -> None:
            dbapi_connection.isolation_level = None

        @event.listens_for(sync, "begin")
        def _explicit_begin(conn: Any) -> None:
            conn.exec_driver_sql("BEGIN")

    async def on_cancelled(self, conn: Any) -> None:
        """Abort the abandoned statement rather than leaving it to run.

        aiosqlite runs each statement in a worker thread, and cancelling the task
        awaiting it does not stop that thread. `interrupt()` reaches the sqlite3
        connection directly instead of queueing behind the abandoned work, never
        suspends — so awaiting it while unwinding a cancellation is safe — and is
        a no-op when nothing is running.
        """
        await conn.interrupt()

    async def fetch(self, conn, sql, params, describe):
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        # sqlite3 reports no type codes at all — `description[i][1]` is always
        # None — which is exactly what SQLAlchemy passes its own processors here.
        return rows, cursor.description if describe else None

    async def stream(self, conn, sql, params, chunk, query):
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

    async def execute(self, conn, sql, params):
        cursor = await conn.execute(sql, params or ())
        return cursor.rowcount

    async def execute_many(self, conn, sql, params):
        cursor = await conn.executemany(sql, params)
        return cursor.rowcount


class AsyncpgDriver(Driver):
    """asyncpg — the fastest backend.

    The JSON/JSONB codec setup this file used to carry is gone: SQLAlchemy's
    dialect registers those in its own `on_connect`, and SQLAlchemy is what opens
    the connection now. Running on a raw pool, nothing had done it, and JSON
    columns came back as text while the processor declined to convert them
    — a bug this cost once, when rowform pooled its own connections and JSON
    columns came back as unparsed text.
    """

    async def fetch(self, conn, sql, params, describe):
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

    async def stream(self, conn, sql, params, chunk, query):
        """A portal over the prepared statement, which asyncpg will only open
        inside a transaction — so one is opened here rather than made the caller's
        problem. Inside a transaction it nests as a savepoint, which is harmless.
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

    async def copy_in(self, conn, table, columns, records):
        """asyncpg's own COPY, over the binary protocol.

        It encodes each value with the same codec a parameterised query would
        use, so the bind-processed values `copy_in` hands over are exactly what
        an INSERT of the same rows would have sent.
        """
        # `table.schema` straight through, including None: asyncpg then leaves the
        # name unqualified and postgres resolves it through `search_path`, which is
        # what psycopg's `format_table` does for the same table. Defaulting to
        # "public" instead would send the two drivers to different tables under a
        # non-default search_path.
        await conn.copy_records_to_table(
            table.name,
            records=records,
            columns=list(columns),
            schema_name=table.schema,
        )
        return len(records)

    async def execute(self, conn, sql, params):
        """asyncpg returns its own status tag, e.g. "INSERT 0 3" — the driver's
        report of what happened, not a normalised count, because normalising it
        would hide the difference between "0 rows matched" and "the statement did
        nothing"."""
        return await conn.execute(sql, *(params or ()))

    async def execute_many(self, conn, sql, params):
        return await conn.executemany(sql, params)


class PsycopgDriver(Driver):
    """psycopg3 — the one supported driver whose paramstyle is not positional, so
    `CoreQuery.bind()` hands it a dict where the others get a tuple. That branch
    is decided by the dialect, not by this class."""

    async def fetch(self, conn, sql, params, describe):
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return rows, cursor.description if describe else None

    async def stream(self, conn, sql, params, chunk, query):
        """A named cursor, which is psycopg's server-side one: `DECLARE` on the
        server, `FETCH` per chunk. The unnamed cursor would also chunk, but only
        after the driver had already read every row into the client, which is the
        memory this method exists to avoid.

        The cost is that postgres will not `DECLARE` a cursor for
        `INSERT ... RETURNING` — it is a syntax error there — so that case is
        refused up front instead of surfacing as one. asyncpg streams it through
        a portal, and `fetch_all` works on either.
        """
        if not query.is_select:
            raise UnsupportedError(
                "psycopg streams through a server-side cursor, and postgres will "
                "only DECLARE one for a SELECT — not for a write with RETURNING. "
                "Use fetch_all() for this statement, or the asyncpg driver, which "
                "streams it through a portal."
            )
        async with conn.cursor(name=f"rowform_stream_{next(_STREAM_NAMES)}") as cursor:
            await cursor.execute(sql, params)
            description = cursor.description
            while True:
                rows = await cursor.fetchmany(chunk)
                if not rows:
                    return
                yield rows, description

    async def copy_in(self, conn, table, columns, records):
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

    async def execute(self, conn, sql, params):
        # psycopg binds a sequence or mapping, never varargs; None means "no
        # parameters", which matters because passing an empty one makes psycopg
        # use the extended protocol and reject multi-statement strings.
        cursor = await conn.execute(sql, params or None)
        return cursor.rowcount

    async def execute_many(self, conn, sql, params):
        async with conn.cursor() as cursor:
            await cursor.executemany(sql, params)
            return cursor.rowcount

    def pipeline(self, conn: Any) -> Any:
        """psycopg's pipeline mode: statements go out without waiting for each
        result, and the server's replies are collected on exit.

        Needs libpq 14+, so it is checked rather than assumed — an older libpq
        would otherwise fail somewhere less obvious.
        """
        import psycopg

        supported = getattr(getattr(psycopg, "capabilities", None), "has_pipeline", None)
        available = supported() if supported is not None else psycopg.Pipeline.is_supported()
        if not available:
            raise UnsupportedError(
                "this psycopg build has no pipeline mode; it needs libpq 14 or newer"
            )
        return conn.pipeline()


#: Keyed by `dialect.driver`, which is the name after the `+` in the URL.
DRIVERS: dict[str, type[Driver]] = {
    "aiosqlite": SqliteDriver,
    "asyncpg": AsyncpgDriver,
    "psycopg": PsycopgDriver,
}


def driver_for(dialect: Any) -> Driver:
    """The execution primitives for whatever `create_async_engine()` was pointed at.

    Refuses a sync driver rather than failing later on a coroutine that is not
    one: rowform executes on the driver connection directly, so there has to be
    one to await.
    """
    try:
        return DRIVERS[dialect.driver](dialect)
    except KeyError:
        raise ConfigurationError(
            f"no rowform driver for {dialect.name}+{dialect.driver}; "
            f"supported: {', '.join(sorted(DRIVERS))}. rowform runs statements on "
            f"the driver connection itself, so the engine must be an async one "
            f"(create_async_engine, not create_engine)."
        ) from None
