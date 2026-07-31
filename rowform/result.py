"""The compatibility track: rowform's rows inside SQLAlchemy's own `Result`.

Two ways to read, told apart by name rather than by semantics:

    users = await conn.fetch_all(sa.select(User))          # list[User] — the hot path
    users = (await conn.execute(sa.select(User))).scalars().all()   # SQLAlchemy, exactly

The second is not an imitation. rowform hydrates the rows, wraps a single selected
entity in the 1-tuple SQLAlchemy would have produced, and hands the list to
SQLAlchemy's own `IteratorResult` — so `.scalars()`, `.tuples()`, `.mappings()`,
`.unique()`, `.partitions()`, `Row` attribute access, `NoResultFound` and
everything added upstream later are the real implementations, not reimplementations
that could drift.

**What that costs**, measured at 1000 rows: the 1-tuple wrap 0.042 ms (arity 1
only — at two or more selected entities the hydrator already produces tuples), and
`Row` construction 0.09–0.15 ms on top. `.scalars().all()` is *more* expensive than
`.all()`, not less (0.187 vs 0.091 at arity 2): `ScalarResult` builds the rows and
then extracts, so there is no consuming a `Result` without paying for its rows.
That is exactly why `fetch_all()` exists beside it.

`SimpleResultMetaData` is SQLAlchemy-internal, like the compiler surface the rest
of this library reads. It is built once per compiled statement and cached on the
`CoreQuery`, which is what SQLAlchemy itself does with `CursorResultMetaData`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

from sqlalchemy.engine.result import (
    ChunkedIteratorResult,
    IteratorResult,
    SimpleResultMetaData,
)
from sqlalchemy.util import await_only

from .planner import Plan


def keys_for(plan: Plan) -> list[str]:
    """Column labels for one statement's rows — what `row.name`, `.keys()` and
    `.mappings()` expose.

    A model entity is keyed by its class name and a scalar by its column key,
    which is what the ORM does for `select(User)` and `select(User.name)`
    respectively. Duplicates are possible (`select(User.id, Post.id)`) and are
    left alone: SQLAlchemy already reports ambiguous keys as an error on access,
    which is better than a name this library invented.
    """
    keys: list[str] = []
    for entity in plan.entities:
        if entity[0] == "model":
            keys.append(entity[1].__name__)
        else:
            column = entity[1]
            keys.append(getattr(column, "key", None) or getattr(column, "name", None) or str(column))
    return keys


def rows_for(plan: Plan, hydrated: list[Any]) -> Sequence[Any]:
    """Hydrated rows in the shape a `Result` expects — a tuple per row.

    `plan.wrap` is already true for two or more selected entities, and the
    generated hydrator emits tuples in that case, so this is free there and costs
    one tuple per row only for the single-entity shape rowform otherwise unwraps.
    """
    if plan.wrap:
        return hydrated
    return [(row,) for row in hydrated]


def rowcount_of(report: Any) -> int:
    """The driver's report of a write, as an int.

    sqlite and psycopg return a rowcount; asyncpg returns a status tag such as
    `"INSERT 0 3"`, which SQLAlchemy's own asyncpg dialect parses the same way.
    `-1` for anything unparseable, matching the DBAPI convention for "not known".
    """
    if isinstance(report, int):
        return report
    if isinstance(report, str):
        tail = report.rsplit(" ", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return -1


class _Result(IteratorResult):
    """`IteratorResult` plus the two attributes a `CursorResult` carries and it
    does not: `rowcount`, for a write, and `returns_rows`."""

    def __init__(self, metadata: Any, iterator: Iterator[Any], rowcount: int = -1):
        super().__init__(metadata, iterator)
        self._rowform_rowcount = rowcount

    @property
    def rowcount(self) -> int:
        return self._rowform_rowcount

    @property
    def returns_rows(self) -> bool:
        return True


class _NoRows(_Result):
    """What a statement with no result set returns.

    Closed on construction, so every row accessor raises `ResourceClosedError` —
    which is what SQLAlchemy raises for `insert(...)` without RETURNING, rather
    than the empty list that would read as "nothing matched".
    """

    def __init__(self, rowcount: int = -1):
        super().__init__(SimpleResultMetaData([]), iter(()), rowcount)
        self.close()

    @property
    def returns_rows(self) -> bool:
        return False


def result_for(plan: Plan, metadata: Any, hydrated: list[Any]) -> _Result:
    rows = rows_for(plan, hydrated)
    return _Result(metadata, iter(rows), len(rows))


def no_rows(report: Any) -> _NoRows:
    return _NoRows(rowcount_of(report))


def chunked_result(
    metadata: Any, make_chunks: Callable[[int | None], AsyncIterator[list[Any]]]
) -> ChunkedIteratorResult:
    """A streaming `Result` fed by an async generator.

    `AsyncResult` runs the underlying sync `Result` inside `greenlet_spawn`, which
    is what makes `await_only` legal here: the chunk function looks synchronous to
    SQLAlchemy and suspends on the driver underneath. It is the same bridge the
    async dialects themselves are built on.

    `make_chunks` is called with the size SQLAlchemy asks for, so
    `result.partitions(50)` fetches fifty at a time from the server rather than
    re-slicing whatever the stream happened to yield.
    """

    def sync_chunks(size: int | None) -> Iterator[list[Any]]:
        iterator = make_chunks(size)
        while True:
            try:
                yield await_only(iterator.__anext__())
            except StopAsyncIteration:
                return

    return ChunkedIteratorResult(metadata, sync_chunks)
