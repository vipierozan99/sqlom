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

**Nothing is wrapped on the way in.** The hydrator's output goes to the `Result`
as it is, and `source_supports_scalars` tells SQLAlchemy whether each item is a
whole row (two or more selected entities, already a tuple) or one scalar to be
wrapped *if and when* somebody asks for rows. So the cost is paid per accessor
rather than per execute, measured at 1000 rows:

    .scalars().all()   0.0049 ms   — no Row is built at all
    .all()             0.168 ms    — one Row per row, on demand
    .mappings().all()  0.471 ms

An earlier arrangement wrapped every arity-1 row in a 1-tuple here and let
`ScalarResult` undo it with `itemgetter(0)`, which made `.scalars().all()` cost
0.210 ms — *more* than `.all()`. Handing the scalars over directly is both less
code and 43x cheaper on the most idiomatic call there is.

`SimpleResultMetaData` and the `source_supports_scalars` flag are
SQLAlchemy-internal, like the compiler surface the rest of this library reads.
Note the flag is spelled `_source_supports_scalars` on `IteratorResult` and
`source_supports_scalars` on `ChunkedIteratorResult` — upstream's inconsistency,
not a typo here. The metadata is built once per compiled statement and cached on
the `CoreQuery`, which is what SQLAlchemy itself does with `CursorResultMetaData`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
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

    def __init__(
        self,
        metadata: Any,
        iterator: Iterator[Any],
        rowcount: int = -1,
        *,
        _source_supports_scalars: bool = False,
    ):
        super().__init__(metadata, iterator, _source_supports_scalars=_source_supports_scalars)
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
    return _Result(
        metadata, iter(hydrated), len(hydrated), _source_supports_scalars=not plan.wrap
    )


def no_rows(report: Any) -> _NoRows:
    return _NoRows(rowcount_of(report))


def chunked_result(
    metadata: Any,
    make_chunks: Callable[[int | None], AsyncIterator[list[Any]]],
    *,
    scalars: bool = False,
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

    return ChunkedIteratorResult(metadata, sync_chunks, source_supports_scalars=scalars)
