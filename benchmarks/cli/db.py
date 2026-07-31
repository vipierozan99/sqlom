"""`bench db up|down|status|dsn|seed`.

State (dsn/container id) persists to a small JSON file between invocations —
each `bench db ...` is a fresh process, so `up` and a later `seed` can't share
memory. The file lives under `results/runs/`, which is gitignored.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from benchmarks.backends.postgres import EphemeralPostgres, attach
from benchmarks.harness import registry
from benchmarks.harness import seed as seed_module

app = typer.Typer(help="Provision, seed, and tear down the benchmark Postgres.")

_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "results" / "runs" / ".state" / "db.json"
)


def _load_state() -> dict | None:
    if not _STATE_PATH.exists():
        return None
    return json.loads(_STATE_PATH.read_text())


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2))


def _require_state() -> dict:
    state = _load_state()
    if state is None:
        typer.echo("no database is up — run `bench db up` (or `up --attach DSN`) first", err=True)
        raise typer.Exit(1)
    return state


def _instance_from_state(state: dict) -> EphemeralPostgres:
    return EphemeralPostgres(
        container_id=state["container_id"], dsn=state["dsn"], port=state["port"],
        cpuset=state.get("cpuset"), image=state.get("image", ""),
    )


@app.command("up")
def up(
    cores: str | None = typer.Option(None, help="cpuset for the container, e.g. '6,7'"),
    version: str = typer.Option("16", help="postgres major version (image tag)"),
    ssl: bool = typer.Option(False, help="require SSL on the connection"),
    port: int = typer.Option(5432, help="host port; host networking binds it directly"),
    attach_dsn: str | None = typer.Option(
        None, "--attach", help="use an already-running server instead of docker"
    ),
) -> None:
    """Start an ephemeral Postgres (docker, --network host, --cpuset-cpus), or
    attach to an existing one with --attach."""
    if _load_state() is not None:
        typer.echo("a database is already up — run `bench db down` first", err=True)
        raise typer.Exit(1)

    instance = (
        attach(attach_dsn) if attach_dsn
        else asyncio.run(EphemeralPostgres.start(port=port, cpuset=cores, version=version, ssl=ssl))
    )
    _save_state({
        "dsn": instance.dsn, "container_id": instance.container_id,
        "port": instance.port, "cpuset": instance.cpuset, "image": instance.image,
    })
    typer.echo(f"up: {instance.dsn}")
    if instance.container_id:
        typer.echo(
            f"container {instance.container_id[:12]}  "
            f"cpuset (from docker inspect): {instance.cpuset_from_outside() or '(none)'}"
        )


@app.command()
def down() -> None:
    """Tear down the ephemeral container (a no-op for an attached server)."""
    state = _require_state()
    asyncio.run(_instance_from_state(state).stop())
    _STATE_PATH.unlink(missing_ok=True)
    typer.echo("down")


@app.command()
def status() -> None:
    """Show the current database's dsn/container, or say there is none."""
    state = _load_state()
    if state is None:
        typer.echo("no database is up")
        raise typer.Exit(1)
    typer.echo(json.dumps(state, indent=2))


@app.command()
def dsn() -> None:
    """Print just the DSN, for piping into another tool."""
    typer.echo(_require_state()["dsn"])


@app.command()
def seed(
    rows: int = typer.Option(200_000, help="rows for the shape's primary table"),
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
) -> None:
    """Drop, recreate, and seed `shape`'s tables on the current database."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")
    state = _require_state()
    total = asyncio.run(_instance_from_state(state).seed(shape, rows))
    typer.echo(f"seeded {total} rows for shape={shape!r}")
