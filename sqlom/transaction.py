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

import contextvars

# Holds the innermost active Transaction for the current task. contextvars, not an
# instance attribute: one engine serves many concurrent tasks, and each needs its
# own answer. Reading it costs ~30 ns, which is why the guard on the hot path is
# affordable at all.
_ACTIVE = contextvars.ContextVar("sqlom_active_transaction", default=None)


def active_transaction():
    """The innermost `Transaction` running in this task, or None."""
    return _ACTIVE.get()


class Transaction:
    """One connection, held for the life of the block. Driver-specific subclasses
    supply the three primitives; everything else is shared."""

    __slots__ = ("_engine", "connection", "_token", "_depth")

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

    async def execute(self, sql, *args):
        """Run a statement on this transaction's connection.

        Positional `args` are bound as parameters — pass them rather than
        interpolating into `sql`.
        """
        raise NotImplementedError

    def transaction(self, **kwargs):
        """A nested block, implemented as a savepoint."""
        raise NotImplementedError

    # --- the read API, shared -----------------------------------------------

    async def fetch_all(self, query):
        """Hydrated model instances, read inside this transaction."""
        sql, params = query.to_sql(placeholder=self._placeholder)
        rows = await self._fetch_rows(sql, params)
        return self._engine._hydrator_for(query.model)(rows)

    async def fetch_json(self, query):
        """JSON bytes built by the database, read inside this transaction."""
        from .query import json_bytes

        sql, params = query.to_json_sql(dialect=self._dialect)
        return json_bytes(await self._fetch_value(sql, params))

    @property
    def depth(self):
        """0 for the outermost transaction, 1+ for savepoints."""
        return self._depth

    def __repr__(self):
        kind = "transaction" if not self._depth else f"savepoint depth={self._depth}"
        return f"<{type(self).__name__} {kind}>"
