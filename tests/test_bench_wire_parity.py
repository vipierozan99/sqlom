"""What the benchmark contenders actually send, per postgres cell.

The equivalence gate compares the bytes a contender returns. It cannot see how
they were obtained, and correction 15 is what that blind spot costs: two
`floor: on SQLAlchemy (dict)` arms shipped wrapping their read in
`async with sa_conn.begin()`, which marks a transaction in Python and sends
nothing, because SQLAlchemy emits `BEGIN` lazily with the first statement it
routes itself — and those floors deliberately await the driver connection
directly. Both returned byte-identical payloads while running two round trips
lighter than every contender they bounded, and the published pool decomposition
was built on the gap.

It was caught by hand, twice, with `log_statement=all` on the bench container.
This pins it instead. Counting happens at the driver: `asyncpg.Transaction`
issues `BEGIN`/`COMMIT`/`ROLLBACK` through `Connection.execute`, and
SQLAlchemy's asyncpg adapter drives that same `Transaction`, so one spy on
`Connection.execute` observes every path into a transaction without needing
server privileges or a readable log — which is what makes this runnable in CI
against the service container rather than only on the bench box.

**postgres only, deliberately.** On sqlite a `SELECT` opens no wire transaction
for *anyone* — pysqlite implicitly begins before DML only — so the floors and the
contenders agree there by construction, which `contenders.py`'s module docstring
records with the measurement behind it. There is no asymmetry to pin.
"""

from __future__ import annotations

from typing import Any

import pytest

import benchmarks.micro.contenders  # noqa: F401  — importing registers every contender
from benchmarks.backends import postgres as pg_backend
from benchmarks.harness import registry

#: Enough rows to make the reads real, few enough to seed per test. The counts
#: asserted below do not depend on how many rows come back.
ROWS = 200
LIMIT = 50

#: Iterations inside the measured window. Two, so a contender that opens one
#: transaction for the whole run instead of one per read is a failure rather
#: than a coincidence.
ITERATIONS = 2

#: Warm-up calls before counting starts. A SQLAlchemy engine establishes its
#: first connection lazily and that handshake emits its own `BEGIN`/`ROLLBACK`
#: pair, which would otherwise land in the window and be indistinguishable from
#: the contender's own.
WARMUP = 3

#: The one contender registered *without* a transaction, because pricing that
#: is the point of the row (METHODOLOGY, "the two tracks"). Anything else
#: sending no transaction is correction 15 happening again.
NO_TRANSACTION = {"rowform (no transaction)"}


class _ExecuteSpy:
    """Records every statement asyncpg sends through `Connection.execute`.

    That is transaction control and `Connection.reset()`, not the reads —
    statements go out via `prepare`/`fetch`. Exactly the traffic in question.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.recording = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncpg

        original = asyncpg.Connection.execute

        async def spy(conn: Any, query: str, *args: Any, **kwargs: Any) -> Any:
            if self.recording:
                self.statements.append(query)
            return await original(conn, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, "execute", spy)

    def count(self, keyword: str) -> int:
        return sum(1 for s in self.statements if s.lstrip().upper().startswith(keyword))


async def _run(spec: Any, dsn: str, spy: _ExecuteSpy) -> dict[str, int]:
    """Warm a contender, then count what `ITERATIONS` reads put on the wire."""
    target, teardown = await spec.factory(registry.ContenderInit(handle=dsn, limit=LIMIT))
    try:
        for _ in range(WARMUP):
            await target()
        spy.statements.clear()
        spy.recording = True
        try:
            for _ in range(ITERATIONS):
                await target()
        finally:
            spy.recording = False
    finally:
        await teardown()
    return {kw: spy.count(kw) for kw in ("BEGIN", "COMMIT")} | {
        "reset": sum(1 for s in spy.statements if "pg_advisory_unlock_all" in s)
    }


@pytest.fixture
async def seeded_shape(pg_dsn, request):
    """The benchmark tables for one shape, on whatever postgres is reachable.

    Uses the suite's own seeder against an *attached* server, so this works
    against CI's service container as well as a `bench db up` box.
    """
    shape = request.param
    await pg_backend.attach(pg_dsn).seed(shape, ROWS)
    return shape


@pytest.mark.parametrize("seeded_shape", ["flat", "join"], indirect=True)
async def test_every_contender_in_a_cell_opens_one_transaction_per_read(
    seeded_shape, pg_dsn, monkeypatch
):
    """Transaction parity across the cell — the invariant the floors broke.

    Not "each contender opens a transaction" but "they all open the *same*
    number", because a floor is only a bound on the thing above it if both do
    the same work around the read.
    """
    spy = _ExecuteSpy()
    spy.install(monkeypatch)

    counts = {}
    for spec in registry.select(backend="postgres", shape=seeded_shape):
        counts[spec.name] = await _run(spec, pg_dsn, spy)

    assert counts, f"no postgres contenders registered for {seeded_shape!r}"

    expected = {
        name: (0 if name in NO_TRANSACTION else ITERATIONS) for name in counts
    }
    actual = {name: c["BEGIN"] for name, c in counts.items()}
    assert actual == expected, (
        "these contenders did not send one BEGIN per read; a floor that sends "
        "fewer is not a floor (correction 15)"
    )

    unbalanced = {n: c for n, c in counts.items() if c["BEGIN"] != c["COMMIT"]}
    assert not unbalanced, f"opened transactions without committing them: {unbalanced}"


@pytest.mark.parametrize("seeded_shape", ["flat"], indirect=True)
async def test_the_reset_rung_is_the_only_floor_without_asyncpg_s_reset(
    seeded_shape, pg_dsn, monkeypatch
):
    """The pool ladder's middle rung means what METHODOLOGY says it means.

    `floor: hand-rolled (no pool reset)` exists to price one thing:
    `asyncpg.Pool.release()` -> `Connection.reset()`, a `RESET ALL`-family round
    trip per request. If a future asyncpg default, or an edit to either floor,
    made the pair stop differing in exactly that, the published 0.0791 ms would
    quietly become a measurement of nothing.
    """
    spy = _ExecuteSpy()
    spy.install(monkeypatch)

    shipped = await _run(registry.get("postgres-flat-floor-hand-rolled-dict"), pg_dsn, spy)
    no_reset = await _run(
        registry.get("postgres-flat-floor-hand-rolled-no-pool-reset"), pg_dsn, spy
    )

    assert shipped["reset"] == ITERATIONS, (
        "asyncpg.Pool no longer resets on release, so the reset rung prices nothing"
    )
    assert no_reset["reset"] == 0, "create_pool(reset=...) no longer suppresses the reset"


class _TransactionStatusSpy:
    """What state psycopg's connection was in when each read went out.

    The asyncpg spy above counts statements because asyncpg's transaction control
    passes through `Connection.execute`. psycopg's does not — `BEGIN` goes out
    through libpq as its own command — so counting would see nothing and report
    every psycopg contender as transactionless.

    What *is* observable, and is what correction 15 was actually about, is whether
    the read ran inside a transaction at all: after a statement,
    `info.transaction_status` is `INTRANS` when one is open and `IDLE` when the
    connection is in autocommit. Recorded per `AsyncCursor.execute`, which every
    path here goes through — rowform's driver, SQLAlchemy's adapter, and a floor
    awaiting the connection directly.
    """

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.recording = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psycopg

        original = psycopg.AsyncCursor.execute

        async def spy(cursor: Any, *args: Any, **kwargs: Any) -> Any:
            result = await original(cursor, *args, **kwargs)
            if self.recording:
                self.statuses.append(cursor.connection.info.transaction_status.name)
            return result

        monkeypatch.setattr(psycopg.AsyncCursor, "execute", spy)


async def _statuses(spec: Any, dsn: str, spy: _TransactionStatusSpy) -> set[str]:
    target, teardown = await spec.factory(registry.ContenderInit(handle=dsn, limit=LIMIT))
    try:
        for _ in range(WARMUP):
            await target()
        spy.statuses.clear()
        spy.recording = True
        try:
            for _ in range(ITERATIONS):
                await target()
        finally:
            spy.recording = False
    finally:
        await teardown()
    return set(spy.statuses)


@pytest.mark.parametrize("seeded_shape", ["flat", "join", "wide"], indirect=True)
async def test_every_psycopg_contender_reads_inside_a_transaction(
    seeded_shape, pg_dsn, monkeypatch
):
    """Transaction parity for the psycopg cell — the same invariant as the
    asyncpg test above, in the terms psycopg makes observable."""
    spy = _TransactionStatusSpy()
    spy.install(monkeypatch)

    seen = {}
    for spec in registry.select(backend="postgres-psycopg", shape=seeded_shape):
        seen[spec.name] = await _statuses(spec, pg_dsn, spy)

    assert seen, f"no psycopg contenders registered for {seeded_shape!r}"
    assert all(statuses == {"INTRANS"} for statuses in seen.values()), (
        f"these psycopg contenders ran a read outside a transaction: "
        f"{ {name: s for name, s in seen.items() if s != {'INTRANS'}} }"
    )
