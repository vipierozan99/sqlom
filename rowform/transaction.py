"""Transactions: several statements on one connection, committed together.

`engine.fetch_all()` takes a pooled connection per call and gives it straight
back, which is right for a one-shot read and useless for anything atomic — two
calls run on two connections, so they cannot see each other's uncommitted work
and cannot roll back together. `engine.transaction()` pins one connection for the
block:

    async with db.transaction() as tx:
        await tx.execute(sa.update(Account).where(Account.id == payer)
                         .values(balance=Account.balance - 100))
        await tx.execute(sa.update(Account).where(Account.id == payee)
                         .values(balance=Account.balance + 100))
        rows = await tx.fetch_all(sa.select(Account).where(Account.id == payer))

Commits on clean exit, rolls back on any exception. `tx.fetch_all` takes the same
statements and reuses the same compiled queries and hydrators as the engine, so
reads inside a transaction cost what reads outside cost.

Nesting gives savepoints, on every driver:

    async with db.transaction() as tx:
        await tx.execute(...)                 # kept even if the inner block fails
        try:
            async with tx.transaction() as sp:
                await sp.execute(...)         # rolled back to the savepoint
        except SomethingExpected:
            pass

**Calling `engine.fetch_all()` while inside `engine.transaction()` is an error.**
It would silently take a *different* connection from the pool, so the read would
not see the transaction's uncommitted writes and would not be rolled back with it
— a bug that produces plausible results. The engine raises instead; use
`tx.fetch_all`. The check is scoped to the same engine, so a second engine
remains usable inside a block.
"""

from __future__ import annotations

import contextvars
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import Any, TypeVar

from .errors import StatementError

R = TypeVar("R")

# Holds the innermost active Transaction for the current task. contextvars, not
# an instance attribute: one engine serves many concurrent tasks, and each needs
# its own answer. Reading it costs ~30 ns, which is why the guard on the hot path
# is affordable at all.
_ACTIVE: contextvars.ContextVar[Transaction | None] = contextvars.ContextVar(
    "rowform_active_transaction", default=None
)


def active_transaction() -> Transaction | None:
    """The innermost `Transaction` running in this task, or None."""
    return _ACTIVE.get()


class Transaction:
    """One connection, held for the life of the block.

    Driver differences live in the engine, not here: this runs every statement
    through the same `_fetch`/`_execute` hooks the engine uses, against its own
    pinned connection. Only how a block is *opened* differs, and that is the
    engine's `_block()`.
    """

    __slots__ = ("_depth", "_engine", "_token", "connection")

    def __init__(self, engine: Any, connection: Any, depth: int = 0):
        self._engine = engine
        self.connection = connection
        self._depth = depth
        self._token: Any = None

    # --- context bookkeeping -------------------------------------------------
    # Entered by the engine's block context manager rather than by
    # `async with tx`, so these are internal.

    def _enter(self) -> None:
        self._token = _ACTIVE.set(self)

    def _exit(self) -> None:
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None

    def transaction(self, **kwargs: Any) -> AbstractAsyncContextManager[Transaction]:
        """A nested block, implemented as a savepoint."""
        return self._engine._block(self.connection, self._depth + 1, kwargs)

    # --- the same API the engine exposes, on this connection ------------------

    async def fetch_all(self, statement: Any, **params: Any) -> Any:
        """Hydrated rows, read inside this transaction."""
        query, extracted = self._engine._require_rows(statement)
        rows, hydrate = await self._engine._run(query, params, self._pinned, extracted)
        return hydrate(rows)

    def pipeline(self) -> AbstractAsyncContextManager[Any]:
        """Send statements without waiting for each result in turn.

            async with engine.transaction() as tx, tx.pipeline():
                for row in rows:
                    await tx.execute(update, **row)

        Only worth it when the round trip is the cost. Measured over 200 updates:
        on loopback it is slightly *slower* than issuing them one by one (56 ms
        against 44 ms, the batching being pure overhead when latency is nil), and
        at 1 ms of network latency it is **13.5x** faster (42 ms against 564 ms).

        It lives on the transaction because a pipeline is a property of one
        connection, and a transaction is how a connection gets pinned.

        Two consequences, both inherent: a statement's result is not available
        while the pipeline is open — psycopg reports a rowcount of -1 — and an
        error raises when the pipeline synchronises rather than at the statement
        that caused it.

        psycopg only. `AsyncpgEngine` and `SqliteEngine` raise `UnsupportedError`
        rather than accepting the block and doing nothing.
        """
        return self._engine._pipeline(self.connection)

    def fetch_iter(self, statement: Any, *, chunk: int = 1000, **params: Any) -> Any:
        """`Engine.fetch_iter`, on this block's connection.

        Worth preferring over `engine.fetch_iter` when the stream is long: the
        cursor and everything read through it then sit inside one transaction the
        caller controls, rather than one opened per stream.
        """
        return self._engine._iterate(statement, chunk, params, self._pinned)

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

    async def execute(self, statement: Any, **params: Any) -> Any:
        """Run a statement that produces no rows. A raw SQL string is accepted
        too, for the DDL and session state a statement object cannot express."""
        engine = self._engine
        if isinstance(statement, str):
            start = perf_counter() if engine.observer is not None else 0.0
            result = await engine._execute(self.connection, statement, None)
            engine._observe(statement, start, None)
            return result
        query, extracted = engine._query_for(statement)
        if query.returns_rows:
            raise StatementError(
                "this statement produces rows — use fetch_all() to get them, "
                "rather than execute(), which would discard them"
            )
        sql, bound = query.bind(params, extracted)
        start = perf_counter() if engine.observer is not None else 0.0
        result = await engine._execute(self.connection, sql, bound)
        engine._observe(sql, start, None)
        return result

    async def execute_many(self, statement: Any, params: Sequence[dict[str, Any]]) -> Any:
        engine = self._engine
        query, extracted = engine._query_for(statement)
        shaped = [query.bind(each, extracted) for each in params]
        if not shaped:
            return None
        sql = shaped[0][0]
        start = perf_counter() if engine.observer is not None else 0.0
        result = await engine._execute_many(
            self.connection, sql, [bound for _, bound in shaped]
        )
        engine._observe(sql, start, None)
        return result

    def _pinned(self) -> AbstractAsyncContextManager[Any]:
        """Stands in for the engine's pool checkout, handing back this block's
        already-held connection so `_run` is shared verbatim."""
        return _Held(self.connection)

    @property
    def depth(self) -> int:
        """0 for the outermost transaction, 1+ for savepoints."""
        return self._depth

    def __repr__(self) -> str:
        kind = "transaction" if not self._depth else f"savepoint depth={self._depth}"
        return f"<{type(self).__name__} {kind}>"


class _Held:
    __slots__ = ("connection",)

    def __init__(self, connection: Any):
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *exc: object) -> None:
        return None
