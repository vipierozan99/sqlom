"""Run/Cell/Trial result schema + writer (PLAN.md §6).

One JSON per run at `results/runs/<run_id>/run.json`, plus an append-only
`results/runs/index.jsonl`. `schema_version`/`invocation`/`git.sha` exist so a
run can be trusted or challenged later — `results/README.md` records three
older JSON sweeps deleted precisely because the then-current code could no
longer reproduce them (PLAN.md §6).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.harness.stats import median, spread_pct

SCHEMA_VERSION = 1


@dataclass(slots=True)
class Trial:
    trial: int
    metrics: dict[str, Any]  # rps/mean_ms/p50_ms/... — open on purpose, tiers differ


@dataclass(slots=True)
class Cell:
    contender: str
    shipped: bool
    params: dict[str, Any]
    trials: list[Trial]
    summary: dict[str, Any] = field(default_factory=dict)

    def summarize(self, keys: list[str]) -> None:
        """Fill `summary` with median/min/max/spread_pct per metric key
        (PLAN.md §4: "medians + spread"). Call after all trials are in."""
        summary = {}
        for key in keys:
            values = [t.metrics[key] for t in self.trials if key in t.metrics]
            summary[key] = {
                "median": median(values),
                "min": min(values) if values else float("nan"),
                "max": max(values) if values else float("nan"),
                "spread_pct": spread_pct(values),
            }
        self.summary = summary


@dataclass(slots=True)
class Run:
    run_id: str
    suite: str
    started_at: str
    finished_at: str
    invocation: dict[str, Any]
    git: dict[str, Any]
    env: dict[str, Any]
    plan: dict[str, Any]
    config: dict[str, Any]
    equivalence: dict[str, Any]
    cells: list[Cell]
    ratios: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: str = ""
    schema_version: int = SCHEMA_VERSION

    @property
    def quotable(self) -> bool:
        """False if the tree was dirty, isolation wasn't one-contender-per-
        process for a multi-cell run, the equivalence gate was skipped or
        failed, or any audit gate tripped (PLAN.md §6) — makes "the combined
        suite is for a quick side-by-side, never for publication" mechanical."""
        if self.git.get("dirty"):
            return False
        if not self.equivalence.get("enforced", False):
            return False
        if self.equivalence.get("self_consistent") is False:
            return False
        isolation = self.plan.get("isolation")
        if len(self.cells) > 1 and isolation != "one_contender_per_process":
            return False
        return not self.warnings

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["quotable"] = self.quotable
        return payload


def make_run_id(suite: str, git_sha: str | None, timestamp: str) -> str:
    """`<timestamp>_<suite>_<short-sha>` — sortable, greppable, matches the
    PLAN.md §6 example (`2026-07-29T11-40-00Z_pg-load_a1b2c3d`)."""
    short = (git_sha or "nogit")[:7]
    return f"{timestamp}_{suite}_{short}"


def write(run: Run, base_dir: Path) -> Path:
    """Write `<base_dir>/runs/<run_id>/run.json` and append one line to
    `<base_dir>/runs/index.jsonl`. `base_dir` is the `results/` directory,
    passed explicitly rather than hardcoded so tests write somewhere disposable."""
    run_dir = base_dir / "runs" / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = run.to_dict()
    (run_dir / "run.json").write_text(json.dumps(payload, indent=2, default=str))

    index_line = {
        "run_id": run.run_id,
        "suite": run.suite,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "quotable": payload["quotable"],
        "git_sha": run.git.get("sha"),
    }
    with (base_dir / "runs" / "index.jsonl").open("a") as fh:
        fh.write(json.dumps(index_line) + "\n")
    return run_dir / "run.json"
