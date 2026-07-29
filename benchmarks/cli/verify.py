"""`bench verify <run_id>` (PLAN.md §9, phase-6 gate): "one command reproduces
any recorded run" — re-executes the exact argv stored in that run's
`invocation.argv` as a fresh subprocess, and confirms it exits clean.

Deliberately not a numeric reproduction: PLAN.md D3 (no baseline capture, no
old-vs-new agreement gate) means no figure from a rerun may be claimed to
match a recorded one. This verifies the command still *runs*, on the current
code, end to end — that `schema_version`/`invocation`/`git.sha` are enough to
make a run reproducible, not that its numbers repeat exactly.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Reproduce a recorded run from its stored invocation.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


@app.command()
def run(run_id: str) -> None:
    path = RESULTS_DIR / "runs" / run_id / "run.json"
    if not path.exists():
        raise typer.BadParameter(f"no run.json at {path}")
    recorded = json.loads(path.read_text())
    argv = recorded["invocation"]["argv"]
    # argv[0] is the recorded process's own entry-point path
    # ("…/benchmarks/__main__.py" under `python -m benchmarks`); re-run
    # through `-m benchmarks` instead of that literal path, which may not
    # exist at the same location on whatever machine `verify` runs on.
    cmd = [sys.executable, "-m", "benchmarks", *argv[1:]]
    typer.echo(f"reproducing {run_id}:\n  {' '.join(cmd)}")

    async def go() -> int:
        proc = await asyncio.create_subprocess_exec(*cmd)
        return await proc.wait()

    exit_code = asyncio.run(go())
    if exit_code != 0:
        typer.echo(f"\nreproduction FAILED: exit {exit_code}")
        raise typer.Exit(1)
    typer.echo("\nreproduction succeeded — the recorded invocation still runs clean on this code")
