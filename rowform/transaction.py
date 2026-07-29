"""Transactions: several statements on one connection, committed together.

`engine.fetch_all()` takes a pooled connection per call and gives it straight back,
which is right for a one-shot read and useless for anything atomic — two calls run
on two connections, so they cannot see each other's uncommitted work and cannot
roll back together. `engine.transaction()` pins one connection for the block:

    async with db.transaction() as tx:
        await tx.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2",
                         100, payer)
        await tx.execute("UPDATE accounts SET balance = balance + $1 WHERE id = $2",
                         100, payee)
        rows = await tx.fetch_all(Query(Account).where(Account.id == payer))

Commits on clean exit, rolls back on any exception. `tx.fetch_all` and
`tx.fetch_json` take the same `Query` objects and reuse the same compiled hydrators
as the engine, so reads inside a transaction cost what reads outside it cost.

Nesting gives savepoints, on both drivers:

    async with db.transaction() as tx:
        await tx.execute(...)                 # kept even if the inner block fails
        try:
            async with tx.transaction() as sp:
                await sp.execute(...)         # rolled back to the savepoint
        except SomethingExpected:
            pass

**Why a transaction marks the connection dirty.** `DatabaseEngine`'s conditional
reset rests on an invariant: `fetch_all`/`fetch_json` emit only plain parameterised
SELECTs, which cannot leave session state behind, so the pool's `RESET ALL` round
trip is skippable. A transaction breaks that — the block can run `SET`, `LISTEN`,
`CREATE TEMP TABLE`, take advisory locks, anything. So `transaction()` goes through
`acquire()`, which marks the connection dirty and makes its release pay the full
reset. Transactions are correct first and fast second; §12 of docs/BENCHMARKS.md
measures what that reset costs.

**Calling `engine.fetch_all()` while inside `engine.transaction()` is an error.**
It would silently take a *different* connection from the pool, so the read would not
see the transaction's uncommitted writes and would not be rolled back with it —
a bug that produces plausible results. The engine raises instead. Use `tx.fetch_all`.
The check is scoped to the same engine, so a second engine remains usable inside a
block.
"""

from __future__ import annotations

import contextvars
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, TypeVar, Union, overload

from .dialects import POSTGRES
from .expr import bind_params, has_deferred_params
from .query import CompoundSelect, Query

if TYPE_CHECKING:
    from .dml import _Statement

R = TypeVar("R")
_Select = Union["Query[R]", "CompoundSelect[R]"]

# Holds the innermost active Transaction for the current task. contextvars, not an
# instance attribute: one engine serves many concurrent tasks, and each needs its
# own answer. Reading it costs ~30 ns, which is why the guard on the hot path is
# affordable at all.
_ACTIVE: contextvars.ContextVar[Transaction | None] = contextvars.ContextVar(
    "rowform_active_transaction", default=None
)


def active_transaction() -> Transaction | None:
    """The innermost `Transaction` running in this task, or None."""
    return _ACTIVE.get()


class Transaction:
    """One connection, held for the life of the block. Driver-specific subclasses
    supply the three primitives; everything else is shared."""

    __slots__ = ("_engine", "connection", "_token", "_depth")

    # Supplied by the driver-specific subclasses; declared here because the shared
    # read methods below use them.
    _placeholder: str
    _dialect: str

    def __init__(self, engine, connection, depth=0):
        self._engine = engine
        self.connection = connection
        self._depth = depth
        self._token = None

    # --- context bookkeeping ------------------------------------------------
    # Entered by the engine's asynccontextmanager, not by `async with tx`, so
    # these are internal.

    def _enter(self):
        self._token = _ACTIVE.set(self)

    def _exit(self):
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None

    # --- driver primitives --------------------------------------------------

    async def _fetch_rows(self, sql, params):
        raise NotImplementedError

    async def _fetch_value(self, sql, params):
        raise NotImplementedError

    async def _execute_raw(self, sql, *args):
        """Run a raw SQL string on this transaction's connection.

        Positional `args` are bound as parameters — pass them rather than
        interpolating into `sql`.
        """
        raise NotImplementedError

    def transaction(
        self, **kwargs: Any
    ) -> AbstractAsyncContextManager[Transaction]:
        """A nested block, implemented as a savepoint."""
        raise NotImplementedError

    # --- the unified entry point, shared -------------------------------------

    @overload
    async def execute(self, target: str, *args: Any) -> Any: ...

    @overload
    async def execute(self, target: _Select[R], **overrides: Any) -> list[R]: ...

    @overload
    async def execute(self, target: _Statement, **overrides: Any) -> Any: ...

    async def execute(self, target: Any, *args: Any, **overrides: Any) -> Any:
        """The same single entry point `engine.execute()` provides, on this
        transaction's connection: a `select()`/`Query`/`CompoundSelect` (or a
        RETURNING Insert/Update/Delete) hydrates and returns its rows — same
        as `fetch_all()`. Calling this on an Insert/Update/Delete *with*
        `returning()` still raises, asking you to use `fetch_all()` instead so
        you get the rows back rather than silently discarding them.

        A bare SQL string with positional `args` runs exactly as it always
        has (`tx.execute("UPDATE ... WHERE id = $1", payer)`) — `**overrides`
        is ignored in that form, since there is no `Query`/`Statement` to
        resolve `bindparam()`s against.
        """
        if isinstance(target, str):
            return await self._execute_raw(target, *args)
        if isinstance(target, (Query, CompoundSelect)):
            return await self.fetch_all(target, **overrides)
        if getattr(target, "returns_rows", False):
            raise ValueError(
                "this statement has RETURNING, so it produces rows — use "
                "fetch_all() to get them"
            )
        sql, params = target.to_sql(placeholder=self._placeholder, dialect=POSTGRES)
        if has_deferred_params(params):
            params = bind_params(params, **overrides)
        return await self._execute_raw(sql, *params)

    # --- the read API, shared -----------------------------------------------

    @overload
    async def fetch_all(self, query: _Select[R], **overrides: Any) -> list[R]: ...

    @overload
    async def fetch_all(self, query: _Statement, **overrides: Any) -> list[Any]: ...

    async def fetch_all(self, query: Any, **overrides: Any) -> Any:
        """Hydrated model instances, read inside this transaction.

        `**overrides` supplies (or replaces) any `bindparam()` values the
        query was built with — see `bind_params()` — the same as
        `engine.fetch_all()`, so a query using one still works unchanged
        inside a transaction block.
        """
        sql, params = query.to_sql(placeholder=self._placeholder, dialect=POSTGRES)
        if has_deferred_params(params):
            params = bind_params(params, **overrides)
        rows = await self._fetch_rows(sql, params)
        return self._engine._hydrator_for(query)(rows)

    async def fetch_json(self, query: Query[Any]) -> bytes:
        """JSON bytes built by the database, read inside this transaction."""
        from .query import json_bytes

        sql, params = query.to_json_sql(dialect=self._dialect)
        return json_bytes(await self._fetch_value(sql, params))

    @property
    def depth(self) -> int:
        """0 for the outermost transaction, 1+ for savepoints."""
        return self._depth

    def __repr__(self):
        kind = "transaction" if not self._depth else f"savepoint depth={self._depth}"
        return f"<{type(self).__name__} {kind}>"
