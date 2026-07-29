"""Bring up whatever backend a load-test/profile case needs — an ephemeral
sqlite file, or an ephemeral postgres container — already seeded for
`shape`, and hand back the env var(s) `service/app.py` reads plus a teardown
callable. `bench load run` and `bench profile load` both call this instead of
each independently duplicating "start postgres, seed it, remember to stop
it".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from benchmarks.backends.postgres import EphemeralPostgres
from benchmarks.backends.sqlite import EphemeralSqlite

Teardown = Callable[[], Awaitable[None]]


async def provision(
    backend: str, shape: str, rows: int, *, pg_port: int = 5432, pg_cores: str | None = None,
) -> tuple[dict[str, str], Teardown]:
    """Returns `(env, teardown)` — `env` is what `launch()` should pass to the
    worker subprocess, `teardown` tears the backend back down (call it from a
    `finally`, always, win or lose)."""
    if backend == "sqlite":
        db = EphemeralSqlite.create(shape, rows)

        async def teardown() -> None:
            db.close()

        return {"BENCH_HANDLE": db.path}, teardown

    if backend == "postgres":
        instance = await EphemeralPostgres.start(port=pg_port, cpuset=pg_cores)
        await instance.seed(shape, rows)

        async def teardown() -> None:
            await instance.stop()

        return {"BENCH_PG_DSN": instance.dsn}, teardown

    raise ValueError(f"no provisioner for backend {backend!r}")
