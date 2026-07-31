"""`bench env [check]`."""

from __future__ import annotations

import json

import typer

from benchmarks.harness import env as env_module

app = typer.Typer(help="Show or audit the machine/software environment.", no_args_is_help=False)


def _print(snapshot: dict) -> None:
    typer.echo(json.dumps(snapshot, indent=2, default=str))


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """`bench env` with no subcommand prints the full block."""
    if ctx.invoked_subcommand is None:
        _print(env_module.capture())


@app.command()
def check() -> None:
    """Print the env block, then warn on boost/dirty tree/high loadavg —
    exits non-zero if anything is flagged, for use in a pre-flight script."""
    snapshot = env_module.capture()
    _print(snapshot)
    warnings = env_module.warnings_for(snapshot)
    if warnings:
        typer.echo("\nwarnings:")
        for warning in warnings:
            typer.echo(f"  - {warning}")
        raise typer.Exit(1)
    typer.echo("\nno warnings")
