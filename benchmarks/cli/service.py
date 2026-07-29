"""`bench service run` (PLAN.md §9): stand up the FastAPI app in the
foreground — for manual `curl`, or as the target of an external profiler
attach in phase 5's `bench profile`.
"""

from __future__ import annotations

import asyncio

import typer

from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.harness import registry
from benchmarks.harness import seed as seed_module
from benchmarks.service.launch import launch

app = typer.Typer(help="Run the benchmarked FastAPI service in the foreground.")


@app.command()
def run(
    backend: str = typer.Option(
        "sqlite", help="'sqlite' or 'postgres' — routes are one per registered contender "
        "for this backend/shape; see `bench contenders list --backend ... --shape ...`"
    ),
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
    dsn: str | None = typer.Option(None, help="postgres DSN (required for --backend postgres)"),
    rows: int = typer.Option(200_000, help="rows to seed (sqlite only; postgres must already be seeded)"),
    limit: int = typer.Option(1000, help="rows per request"),
    workers: int = typer.Option(1),
    port: int = typer.Option(8000),
    cores: str | None = typer.Option(
        None, help="comma-separated logical CPUs, one per worker (round-robin)"
    ),
    loop: str = typer.Option("uvloop"),
) -> None:
    """Blocks until Ctrl+C, tearing down every worker (and the ephemeral
    sqlite db, if used) on exit."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")
    if backend not in ("sqlite", "postgres"):
        raise typer.BadParameter("--backend must be 'sqlite' or 'postgres'")
    if backend == "postgres" and not dsn:
        raise typer.BadParameter("--dsn is required for --backend postgres")

    async def go() -> None:
        db = None
        if backend == "sqlite":
            db = EphemeralSqlite.create(shape, rows)
            handle = db.path
        else:
            handle = dsn or ""

        core_list = [int(c) for c in cores.split(",")] if cores else []
        workers_list = await launch(
            "benchmarks.service.app:app", base_port=port, workers=workers,
            cores=core_list, backend=backend, shape=shape, handle=handle,
            limit=limit, loop=loop,
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
            if db is not None:
                db.close()

    asyncio.run(go())
