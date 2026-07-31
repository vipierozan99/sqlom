"""One connection, two ways to read it.

    async with db.begin() as conn:
        # SQLAlchemy's names, SQLAlchemy's semantics, to the letter
        users = (await conn.execute(sa.select(User))).scalars().all()
        for row in await conn.execute(sa.select(User.name, User.id)):
            row.name, row[1]

        # rowform's names, rowform's rows: no Result, no Row, no wrap
        users = await conn.fetch_all(sa.select(User))

The two tracks are told apart by *name*, never by a subtle difference in what a
method returns. Anything spelled the way SQLAlchemy spells it behaves the way
SQLAlchemy behaves, because `execute()` hands its rows to SQLAlchemy's own
`Result` (`result.py`). Anything spelled `fetch_*` is rowform's, hands back plain
hydrated objects, and pays for no row machinery at all.

That is what makes ported code safe: a `row[0]` that meant something under
SQLAlchemy still means it here, rather than silently indexing the string a
single-column select would otherwise have handed back.

**Transactions are SQLAlchemy's.** `conn.begin()` and `conn.begin_nested()` return
its `AsyncTransaction` unchanged — rowform does not wrap it, so `commit()`,
`rollback()`, `is_active` and `is_nested` are the real ones.

**Autobegin.** The first statement on a `Connection` opens a transaction if one is
not already open, as `AsyncConnection` does — so two reads in one scope share a
snapshot, and a write is committed by `commit()` rather than discarded by the
pool's rollback on release. `Engine`'s one-shot `fetch_*` are outside that rule
and say so: they are rowform's own API, not a scope.
"""

from __future__ import annotations

import contextvars
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import TYPE_CHECKING, Any, TypeVar, overload

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncResult, AsyncScalarResult

from . import result as _result
from .query import CoreQuery

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.engine import Result, ScalarResult
    from sqlalchemy.ext.asyncio import AsyncConnection

# One type variable per selected entity — the same per-arity overloads the engine
# carries, because the hot track's exact typing is half of why it exists.
R = TypeVar("R")
R2 = TypeVar("R2")
R3 = TypeVar("R3")
R4 = TypeVar("R4")

# Holds the innermost active Connection for the current task. contextvars, not an
# instance attribute: one engine serves many concurrent tasks, and each needs its
# own answer. Reading it costs ~30 ns, which is why the guard on the hot path is
# affordable at all.
_ACTIVE: contextvars.ContextVar[Connection | None] = contextvars.ContextVar(
    "rowform_active_connection", default=None
)


def active_connection() -> Connection | None:
    """The innermost `Connection` scope open in this task, or None.

    Only scopes rowform opened register here. One bound to a caller's connection
    with `connect(bind=...)` does not: that transaction's lifetime is theirs, and
    claiming it would make the engine's one-shots refuse to run for the rest of
    the task.
    """
    return _ACTIVE.get()


class Connection:
    """A checked-out connection. See the module docstring for the two tracks."""

    __slots__ = ("_engine", "_owns", "_token", "connection", "sa_connection")

    def __init__(
        self,
        engine: Any,
        sa_connection: AsyncConnection,
        connection: Any,
        *,
        owns: bool = True,
    ):
        self._engine = engine
        #: SQLAlchemy's connection — what owns the transaction.
        self.sa_connection = sa_connection
        #: The driver connection under it — what statements actually run on.
        self.connection = connection
        # False when bound to somebody else's connection: their transaction, so
        # rowform neither opens nor ends one.
        self._owns = owns
        self._token: Any = None

    # --- scope bookkeeping ---------------------------------------------------

    def _enter(self) -> None:
        self._token = _ACTIVE.set(self)

    def _exit(self) -> None:
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None

    async def _autobegin(self) -> None:
        if self._owns and not self.sa_connection.in_transaction():
            await self.sa_connection.begin()

    # --- transactions, unwrapped ---------------------------------------------

    def begin(self) -> Any:
        """SQLAlchemy's `AsyncTransaction`, not a wrapper around one."""
        return self.sa_connection.begin()

    def begin_nested(self) -> Any:
        """A SAVEPOINT, as `AsyncConnection.begin_nested()`."""
        return self.sa_connection.begin_nested()

    async def commit(self) -> None:
        await self.sa_connection.commit()

    async def rollback(self) -> None:
        await self.sa_connection.rollback()

    async def close(self) -> None:
        await self.sa_connection.close()

    async def execution_options(self, **options: Any) -> Connection:
        await self.sa_connection.execution_options(**options)
        return self

    def in_transaction(self) -> bool:
        return self.sa_connection.in_transaction()

    def in_nested_transaction(self) -> bool:
        return self.sa_connection.in_nested_transaction()

    @property
    def closed(self) -> bool:
        return self.sa_connection.closed

    # --- compatibility track -------------------------------------------------

    async def execute(
        self, statement: Any, parameters: Any = None, **params: Any
    ) -> Result[Any]:
        """Run `statement` and return a SQLAlchemy `Result`.

        `parameters` is a dict, or a list of dicts for an executemany — the
        signature `AsyncConnection.execute` has. `**params` is rowform's
        extension, and merges into it.

        A statement with no result set returns a closed `Result`: `.rowcount`
        works, and `.all()` raises `ResourceClosedError`, which is what
        SQLAlchemy raises rather than returning an empty list that reads as
        "nothing matched".
        """
        engine = self._engine
        await self._autobegin()
        if isinstance(parameters, (list, tuple)):
            return _result.no_rows(await self.execute_many(statement, parameters))
        bound = {**(parameters or {}), **params}
        query, extracted = engine._query_for(statement)
        if not query.returns_rows:
            return _result.no_rows(await self._execute(query, bound, extracted))
        rows, hydrate = await engine._run(query, bound, self._pinned, extracted)
        plan = query.entities
        assert plan is not None  # returns_rows guarantees it
        return _result.result_for(plan, query.result_metadata, hydrate(rows))

    async def scalar(self, statement: Any, parameters: Any = None, **params: Any) -> Any:
        return (await self.execute(statement, parameters, **params)).scalar()

    async def scalars(
        self, statement: Any, parameters: Any = None, **params: Any
    ) -> ScalarResult[Any]:
        return (await self.execute(statement, parameters, **params)).scalars()

    async def stream(
        self, statement: Any, parameters: Any = None, *, chunk: int = 1000, **params: Any
    ) -> AsyncResult[Any]:
        """`AsyncResult` over a server-side cursor — `conn.stream()`, with rowform
        hydrating each chunk.

        `chunk` is rowform's extension; SQLAlchemy takes the same idea through
        `execution_options(yield_per=...)`.
        """
        engine = self._engine
        await self._autobegin()
        bound = {**(parameters or {}), **params}
        query, extracted = engine._require_rows(statement)
        chunks = engine._chunks(query, bound, extracted, chunk, self._pinned)
        return AsyncResult(_result.chunked_result(query.result_metadata, chunks))

    async def stream_scalars(
        self, statement: Any, parameters: Any = None, *, chunk: int = 1000, **params: Any
    ) -> AsyncScalarResult[Any]:
        return (await self.stream(statement, parameters, chunk=chunk, **params)).scalars()

    async def exec_driver_sql(self, sql: str, parameters: Any = None) -> Result[Any]:
        """A literal string on the driver, for the DDL and session state a
        statement object cannot express. No compilation, and therefore no rows to
        hydrate — this always reports rather than returns."""
        engine = self._engine
        await self._autobegin()
        start = perf_counter() if engine.observer is not None else 0.0
        report = await engine.driver.execute(self.connection, sql, parameters)
        engine._observe(sql, start, None)
        return _result.no_rows(report)

    # --- hot track -----------------------------------------------------------

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
        """Hydrated rows, with no `Result` and no `Row` between them and you.

        One selected entity yields that entity; two or more yield a tuple
        (`planner.py`). For SQLAlchemy's shape — a `Row` even at arity one — use
        `execute()`.
        """
        engine = self._engine
        await self._autobegin()
        query, extracted = engine._require_rows(statement)
        rows, hydrate = await engine._run(query, params, self._pinned, extracted)
        return hydrate(rows)

    @overload
    async def fetch_one(self, statement: CoreQuery[R], **params: Any) -> R | None: ...

    @overload
    async def fetch_one(self, statement: Select[tuple[R]], **params: Any) -> R | None: ...

    @overload
    async def fetch_one(self, statement: Any, **params: Any) -> Any: ...

    async def fetch_one(self, statement: Any, **params: Any) -> Any:
        """The first row, or None — narrowed to `LIMIT 1` where that is safe, as
        on the engine (`engine._one_row`)."""
        from .engine import _one_row

        rows = await self.fetch_all(_one_row(statement), **params)
        return rows[0] if rows else None

    async def fetch_value(self, statement: Any, **params: Any) -> Any:
        row = await self.fetch_one(statement, **params)
        if row is None:
            return None
        return row[0] if isinstance(row, tuple) else row

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
        self, statement: Any, *, chunk: int = ..., **params: Any
    ) -> AsyncIterator[Any]: ...

    def fetch_iter(self, statement: Any, *, chunk: int = 1000, **params: Any) -> Any:
        """`Engine.fetch_iter` on this connection: the same rows `fetch_all`
        gives, `chunk` at a time, without a `Result`."""
        return self._engine._iterate(statement, chunk, params, self._pinned)

    async def execute_many(self, statement: Any, params: Sequence[dict[str, Any]]) -> Any:
        """One compiled statement, many parameter sets, one round trip. Returns
        the driver's own report; `execute(stmt, [ ... ])` wraps the same thing in
        a `Result`."""
        engine = self._engine
        await self._autobegin()
        query, extracted = engine._query_for(statement)
        shaped = [query.bind(each, extracted) for each in params]
        if not shaped:
            return None
        sql = shaped[0][0]
        start = perf_counter() if engine.observer is not None else 0.0
        report = await engine.driver.execute_many(
            self.connection, sql, [bound for _, bound in shaped]
        )
        engine._observe(sql, start, None)
        return report

    # --- extensions ----------------------------------------------------------

    async def copy_in(
        self,
        table: Any,
        rows: Sequence[dict[str, Any]],
        *,
        columns: Sequence[str] | None = None,
    ) -> int:
        """Bulk-load through the server's COPY path, on this connection."""
        await self._autobegin()
        return await self._engine._copy_in(self.connection, table, rows, columns)

    def pipeline(self) -> AbstractAsyncContextManager[Any]:
        """Send statements without waiting for each result in turn.

            async with db.begin() as conn, conn.pipeline():
                for row in rows:
                    await conn.execute_many(update, [row])

        Only worth it when the round trip is the cost. Measured over 200 updates:
        on loopback it is slightly *slower* than issuing them one by one (56 ms
        against 44 ms, the batching being pure overhead when latency is nil), and
        at 1 ms of network latency it is **13.5x** faster (42 ms against 564 ms).

        It lives here because a pipeline is a property of one connection. Two
        consequences, both inherent: a statement's result is not available while
        the pipeline is open — psycopg reports a rowcount of -1 — and an error
        raises when the pipeline synchronises rather than at the statement that
        caused it.

        psycopg only; the others raise `UnsupportedError` rather than accepting
        the block and doing nothing.
        """
        return self._engine.driver.pipeline(self.connection)

    # --- plumbing ------------------------------------------------------------

    async def _execute(self, query: Any, params: dict[str, Any], extracted: Any) -> Any:
        engine = self._engine
        sql, bound = query.bind(params, extracted)
        start = perf_counter() if engine.observer is not None else 0.0
        report = await engine.driver.execute(self.connection, sql, bound)
        engine._observe(sql, start, None)
        return report

    def _pinned(self) -> AbstractAsyncContextManager[Any]:
        """Stands in for the engine's pool checkout, handing back this scope's
        already-held connection so `_run` is shared verbatim."""
        return _Held(self.connection)

    def __repr__(self) -> str:
        state = "bound" if not self._owns else "open"
        return f"<{type(self).__name__} {state}>"


class _Held:
    __slots__ = ("connection",)

    def __init__(self, connection: Any):
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *exc: object) -> None:
        return None
