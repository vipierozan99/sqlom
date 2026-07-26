import itertools
import re

from .column import hydrate


class DatabaseEngine:
    """asyncpg-backed engine, as described in the README. Requires a real
    Postgres server; see benchmarks/bench_sqlite.py for a driver-agnostic
    stand-in used to benchmark the hydration path without one."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None

    async def connect(self):
        import asyncpg

        self._pool = await asyncpg.create_pool(self.dsn)

    async def fetch_all(self, query):
        sql, params = query.to_sql(placeholder="$")
        # asyncpg wants $1, $2, ... — to_sql() only emits a bare "$" per
        # placeholder, so number them left-to-right here.
        counter = itertools.count(1)
        numbered = re.sub(r"\$", lambda _: f"${next(counter)}", sql)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(numbered, *params)
        return [hydrate(query.model, row) for row in rows]
