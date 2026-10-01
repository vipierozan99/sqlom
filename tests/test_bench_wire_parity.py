"""What the postgres contenders actually send, per cell.

The equivalence gate compares the bytes a contender returns and cannot see how
they were obtained: a floor that skips `BEGIN`/`COMMIT` returns the same bytes
two round trips lighter than everything it bounds, and that happened twice. This
pins transaction parity instead. Counting happens at the driver — asyncpg's
`Transaction` and SQLAlchemy's asyncpg adapter both issue `BEGIN`/`COMMIT`
through `Connection.execute` — so one spy sees every path without server logs.

postgres only: on sqlite the floors spell the same literal `BEGIN` rowform sends,
and stock SQLAlchemy sends none before a SELECT, so there is no single parity to
pin there.
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

#: The one contender registered without a transaction, because pricing that is
#: the point of the row. Anything else sending none is a broken floor.
NO_TRANSACTION = {"rowform (one-shot)"}


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
    return {kw: spy.count(kw) for kw in ("BEGIN", "COMMIT")}


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
        "fewer is not a floor"
    )

    unbalanced = {n: c for n, c in counts.items() if c["BEGIN"] != c["COMMIT"]}
    assert not unbalanced, f"opened transactions without committing them: {unbalanced}"
