import itertools
import re

from .compile import ASYNCPG_CONVERTERS, compile_batch_hydrator


class DatabaseEngine:
    """asyncpg-backed engine, as described in the README. Requires a real
    Postgres server; see benchmarks/bench_sqlite.py for a driver-agnostic
    stand-in used to benchmark the hydration path without one."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None
        self._hydrators = {}

    async def connect(self):
        import asyncpg

        self._pool = await asyncpg.create_pool(self.dsn)

    def _hydrator_for(self, model):
        # Compiled once per model, then reused for every row of every query.
        hydrator = self._hydrators.get(model)
        if hydrator is None:
            hydrator = compile_batch_hydrator(model, ASYNCPG_CONVERTERS)
            self._hydrators[model] = hydrator
        return hydrator

    @staticmethod
    def _number_placeholders(sql):
        # asyncpg wants $1, $2, ... — to_sql() only emits a bare "$" per
        # placeholder, so number them left-to-right here.
        counter = itertools.count(1)
        return re.sub(r"\$", lambda _: f"${next(counter)}", sql)

    async def fetch_all(self, query):
        sql, params = query.to_sql(placeholder="$")
        numbered = self._number_placeholders(sql)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(numbered, *params)
        return self._hydrator_for(query.model)(rows)

    async def fetch_json(self, query):
        """Return the result set as JSON bytes built by Postgres itself.

        No per-row Python objects are created — the database does the row
        shaping and JSON encoding, and the result goes straight into a
        response body.
        """
        sql, params = query.to_json_sql(dialect="postgres")
        numbered = self._number_placeholders(sql)
        async with self._pool.acquire() as conn:
            payload = await conn.fetchval(numbered, *params)
        return payload.encode() if isinstance(payload, str) else payload
