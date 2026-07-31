"""`bench service run`: stand up the FastAPI worker
(`service/app.py`, a plain hand-written app — see its module docstring) in
the foreground, for manual `curl` or as the target of an external profiler
attach.

No `--shape`/`--backend`/`--dsn`/`--limit` options: `service/app.py` is a
fixed, sqlite-only set of routes covering both shapes unconditionally (it
isn't generated per backend/shape the way an earlier revision was), and
`limit` is a per-request query parameter, not a startup config value — see
`bench contenders list` / `bench load cases` for the set of routes this
serves.
"""

from __future__ import annotations

import asyncio

import typer

from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.service.launch import launch

app = typer.Typer(help="Run the benchmarked FastAPI service in the foreground.")


@app.command()
def run(
    rows: int = typer.Option(200_000, help="rows to seed, per shape"),
    workers: int = typer.Option(1),
    port: int = typer.Option(8000),
    cores: str | None = typer.Option(
        None, help="comma-separated logical CPUs, one per worker (round-robin)"
    ),
    loop: str = typer.Option("uvloop"),
) -> None:
    """Blocks until Ctrl+C, tearing down every worker and the ephemeral
    sqlite db on exit."""

    async def go() -> None:
        db = EphemeralSqlite.create_all_shapes(rows)
        core_list = [int(c) for c in cores.split(",")] if cores else []
        workers_list = await launch(
            "benchmarks.service.app:app", base_port=port, workers=workers,
            cores=core_list, env={"BENCH_HANDLE": db.path}, loop=loop,
        )
        typer.echo(f"up: {len(workers_list)} worker(s) on ports {[w.port for w in workers_list]}")
        typer.echo(f"affinity: {[(w.port, w.cores) for w in workers_list]}")
        try:
            await asyncio.gather(*[w.proc.wait() for w in workers_list])
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for worker in workers_list:
                await worker.stop()
            db.close()

    asyncio.run(go())
