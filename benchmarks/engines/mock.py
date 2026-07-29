"""`MockEngine`: the mapper's floor, with zero driver cost (PLAN.md D8/D9, §7).

`DatabaseEngine.fetch_all` (`rowform/engine.py`) touches the driver in exactly
one place:

    async with self._require_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)

`MockEngine` overrides **only `_require_pool()`**, so SQL generation, param
binding, hydrator selection and hydration all run byte-for-byte identical to
production — including the per-request `to_sql()` call that exposed a real 4%
self-inflicted regression in the shipped engine.

Rows are precomputed plain tuples (rowform is always positional), so this
measures the mapper with *no* driver term. Its absolutes are therefore not
comparable to sqlite/Postgres numbers — it is a mapper instrument only, and
(D9) never paired against SQLAlchemy: there is no result-mocking seam on the
other side, so pairing them would put different work inside the two timed
regions, correction 6 exactly.
"""

from __future__ import annotations

from typing import Any

from rowform.engine import DatabaseEngine


class _FakeConnection:
    """`fetch()` yields the same precomputed rows regardless of `sql`/`params`
    — MockEngine measures one query shape's hydration cost, not query
    correctness (that's what the real-engine tests are for)."""

    def __init__(self, rows: list[tuple[Any, ...]]):
        self._rows = rows

    async def fetch(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        return self._rows

    async def fetchval(self, sql: str, *params: Any) -> Any:
        return None

    async def execute(self, sql: str, *params: Any) -> str:
        return "MOCK 0"


class _FakeAcquire:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakePool:
    def __init__(self, rows: list[tuple[Any, ...]]):
        self._conn = _FakeConnection(rows)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class MockEngine(DatabaseEngine):
    """A `DatabaseEngine` whose pool is fake — see module docstring.

    `rows` must already be shaped the way asyncpg would hand them back (real
    Python `bool`s, not sqlite's 0/1) — `MockEngine` inherits `DatabaseEngine`'s
    `ASYNCPG_CONVERTERS`, which do no int->bool coercion, unlike the sqlite
    engine's converters.

    Never call `connect()`/`close()` on this: they are inherited unmodified
    and would try to open a real asyncpg pool against `dsn`. `_require_pool()`
    is the only override, and it works whether or not `connect()` ever runs.
    """

    def __init__(self, rows: list[tuple[Any, ...]]):
        super().__init__(dsn="mock://unused")
        self._mock_pool = _FakePool(rows)

    def _require_pool(self) -> _FakePool:
        return self._mock_pool
