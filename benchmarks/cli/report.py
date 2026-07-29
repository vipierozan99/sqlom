"""`bench report` (PLAN.md §9): summarise a recorded run from its `run.json`,
or list every recorded run — without re-running anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(help="Summarise recorded runs.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _load(run_id: str) -> dict:
    path = RESULTS_DIR / "runs" / run_id / "run.json"
    if not path.exists():
        raise typer.BadParameter(f"no run.json at {path}")
    return json.loads(path.read_text())


@app.command()
def show(run_id: str) -> None:
    """Print one run's summary: suite, quotable status, equivalence, and
    every cell's recorded metrics."""
    run = _load(run_id)
    typer.echo(f"run {run['run_id']}  suite={run['suite']}  quotable={run['quotable']}")
    typer.echo(f"started {run['started_at']}  finished {run['finished_at']}")
    typer.echo(f"git sha {run['git'].get('sha')}  dirty={run['git'].get('dirty')}")
    eq = run["equivalence"]
    typer.echo(
        f"equivalence: enforced={eq.get('enforced')} self_consistent={eq.get('self_consistent')} "
        f"payload_bytes={eq.get('payload_bytes')}"
    )
    if run["warnings"]:
        typer.echo("warnings:")
        for warning in run["warnings"]:
            typer.echo(f"  - {warning}")
    typer.echo("\ncells:")
    for cell in run["cells"]:
        for trial in cell["trials"]:
            metrics = ", ".join(
                f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                for k, v in trial["metrics"].items()
            )
            typer.echo(f"  {cell['contender']:<38} {metrics}")


@app.command("list")
def list_runs() -> None:
    """List every recorded run_id from `results/runs/index.jsonl`, most
    recent first."""
    index_path = RESULTS_DIR / "runs" / "index.jsonl"
    if not index_path.exists():
        typer.echo("no runs recorded yet")
        return
    for line in reversed(index_path.read_text().splitlines()):
        entry = json.loads(line)
        typer.echo(f"{entry['run_id']}  suite={entry['suite']}  quotable={entry['quotable']}")
