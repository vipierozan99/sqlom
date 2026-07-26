"""psycopg3-backed engine, so sqlom and SQLAlchemy can be compared on one driver.

The asyncpg engine in `engine.py` is the faster backend, but SQLAlchemy cannot
use it and psycopg3 at the same time, and comparing two mappers across two
drivers confounds the mapper with the driver. This engine exists so both sides
can run on `postgresql+psycopg` / `psycopg_pool` with each library's **default**
pool behaviour — no `reset=` overrides, no AUTOCOMMIT, nothing tuned.

That default is not free, and it is the same cost SQLAlchemy pays: psycopg3
connections are transactional unless told otherwise, so a pooled request is
`BEGIN` … `SELECT` … `COMMIT`. Keeping it means the comparison measures the
mapper rather than two different transaction policies.

`fetch_all` returns hydrated model instances; rows arrive as plain tuples from
psycopg3, which suits the positional hydrator directly.
"""

from .compile import PSYCOPG_CONVERTERS, compile_batch_hydrator


class PsycopgEngine:
    def __init__(self, conninfo: str, **pool_kwargs):
        self.conninfo = conninfo
        self.pool = None
        self._pool_kwargs = pool_kwargs
        self._hydrators = {}

    async def connect(self):
        from psycopg_pool import AsyncConnectionPool

        # open=False then open() explicitly: constructing an open pool from a
        # running loop is deprecated in psycopg_pool 3.2+.
        self.pool = AsyncConnectionPool(self.conninfo, open=False, **self._pool_kwargs)
        await self.pool.open(wait=True)

    async def close(self):
        if self.pool is not None:
            await self.pool.close()

    def _hydrator_for(self, model):
        hydrator = self._hydrators.get(model)
        if hydrator is None:
            hydrator = compile_batch_hydrator(model, PSYCOPG_CONVERTERS)
            self._hydrators[model] = hydrator
        return hydrator

    async def fetch_all(self, query):
        sql, params = query.to_sql(placeholder="%s")
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        return self._hydrator_for(query.model)(rows)

    async def fetch_json(self, query):
        """Result set as JSON bytes built by Postgres, no per-row Python objects."""
        sql, params = query.to_json_sql(dialect="psycopg")
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
        payload = row[0]
        return payload.encode() if isinstance(payload, str) else payload
