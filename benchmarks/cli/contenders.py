"""`bench contenders list` (PLAN.md §5): the contender registry, made
inspectable on its own instead of only discoverable by reading
`contenders/*.py` — the source every other command's `--shape`/`--backend`/
`--only` filters draw from, and of the slugs `bench load run --case` takes.
"""

from __future__ import annotations

import json

import typer

import benchmarks.contenders  # noqa: F401 -- registration side-effects
from benchmarks.harness import registry
from benchmarks.harness import seed as seed_module

app = typer.Typer(help="Inspect the contender registry.")


@app.command("list")
def list_contenders(
    shape: str | None = typer.Option(None, help="filter by shape, e.g. 'flat' or 'join'"),
    backend: str | None = typer.Option(None, help="filter by backend, e.g. 'sqlite' or 'mock'"),
    only: str | None = typer.Option(None, help="substring match on name"),
    as_json: bool = typer.Option(
        False, "--json", help="machine-readable output, one JSON array — for scripts"
    ),
) -> None:
    """Print every registered contender: slug, name, backend, shape, shipped,
    tags, description. `slug` is the value `bench load run --case` takes."""
    specs = registry.select(backend=backend, shape=shape, only=only)

    if as_json:
        typer.echo(json.dumps(
            [
                {
                    "slug": s.slug, "name": s.name, "description": s.description,
                    "backend": s.backend, "shape": s.shape, "shipped": s.shipped,
                    "tags": list(s.tags),
                }
                for s in specs
            ],
            indent=2,
        ))
        return

    if not specs:
        typer.echo("no contenders match")
        raise typer.Exit(1)

    slug_width = max(len(s.slug) for s in specs)
    for spec in specs:
        tags = f"  tags={','.join(spec.tags)}" if spec.tags else ""
        typer.echo(
            f"{spec.slug:<{slug_width}}  shipped={spec.shipped!s:<5}{tags}\n"
            f"{'':<{slug_width}}  {spec.description}"
        )


@app.command()
def shapes() -> None:
    """List the known shapes ('flat'/'join') — the valid values for every
    other command's `--shape`."""
    for shape in seed_module.SHAPES:
        typer.echo(shape)
