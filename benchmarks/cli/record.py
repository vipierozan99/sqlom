"""`bench record <run_id> --note ...` (PLAN.md D15, phase-6 gate): commits a
recorded run's artifacts to a dated branch `bench/<date>-<slug>-<sha>`, and
appends one index row to `docs/RUNS.md` on the branch this command was run
from. `results/runs/` itself stays gitignored on main (D15) — a run's
artifacts only live in git history on its own dated branch, and `docs/RUNS.md`
is the pointer to it. Nothing here republishes into `docs/BENCHMARKS.md`
(PLAN.md D5); this command never touches that file.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import typer

app = typer.Typer(help="Commit a recorded run's artifacts to a dated branch.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
RUNS_DOC = Path(__file__).resolve().parent.parent.parent / "docs" / "RUNS.md"

_INDEX_HEADER = (
    "# Recorded runs\n\n"
    "Dated runs from `bench record` (PLAN.md D15). Each row's branch holds the "
    "full `results/runs/<run_id>/` artifact — `results/runs/` itself is "
    "gitignored on main, so this table is how a run is found later. "
    "`bench verify <run_id>` reproduces the command that produced a row (not "
    "its numbers — PLAN.md D3). Nothing here is republished into "
    "`docs/BENCHMARKS.md` (PLAN.md D5).\n\n"
    "| date | run_id | suite | quotable | branch | note |\n"
    "|---|---|---|---|---|---|\n"
)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "run"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@app.command()
def run(run_id: str, note: str = typer.Option("", "--note", help="one-line note for the docs/RUNS.md row")) -> None:
    run_dir = RESULTS_DIR / "runs" / run_id
    run_json = run_dir / "run.json"
    if not run_json.exists():
        raise typer.BadParameter(f"no run.json at {run_json}")
    recorded = json.loads(run_json.read_text())

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    slug = _slugify(recorded["suite"])
    branch = f"bench/{date}-{slug}-{run_id[-7:]}"
    original_branch = _git("rev-parse", "--abbrev-ref", "HEAD")

    _git("checkout", "-b", branch)
    try:
        subprocess.run(["git", "add", "-f", str(run_dir)], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"bench: record {run_id}\n\n{note}".strip()], check=True,
        )
    finally:
        _git("checkout", original_branch)

    _append_index_row(recorded, branch, note)
    typer.echo(f"committed {run_dir} to branch {branch!r}")
    typer.echo(f"appended a row to {RUNS_DOC}")
    typer.echo("branch created locally only — push it yourself if you want it remote")


def _append_index_row(recorded: dict, branch: str, note: str) -> None:
    RUNS_DOC.parent.mkdir(parents=True, exist_ok=True)
    if not RUNS_DOC.exists():
        RUNS_DOC.write_text(_INDEX_HEADER)
    row = (
        f"| {datetime.now(UTC):%Y-%m-%d} | `{recorded['run_id']}` | {recorded['suite']} | "
        f"{recorded['quotable']} | `{branch}` | {note} |\n"
    )
    with RUNS_DOC.open("a") as fh:
        fh.write(row)
