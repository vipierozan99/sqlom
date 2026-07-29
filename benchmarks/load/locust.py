"""Locust load shape (ported from `benchmarks/locustfile.py`) plus a `run()`
wrapper that drives it via the CLI and parses its CSV output — the
independent second opinion PLAN.md D-equivalent invariants call for: every
end-to-end figure from a single hand-rolled generator (`httpload.py`) is one
bug away from being wrong in the same direction on every published number.

Design choices, made so the two generators measure the *same* thing:

* **`FastHttpUser`, not `HttpUser`.** The default user wraps `requests`,
  costing ~1ms of client CPU per request — on one pinned core that makes the
  *client* the bottleneck well below the server's throughput.
* **`wait_time = constant(0)`.** No think time, so N users means N requests
  in flight at all times — the same closed-loop model as
  `httpload.run(connections=N)`. Any non-zero wait time would make the two
  incomparable.
* **Response validation.** Every response is checked for the expected byte
  length, so a run that silently 500s cannot be reported as throughput.

This module is loaded two ways: `locust -f benchmarks/load/locust.py` (locust
imports it for the `BenchUser` class only) and `import
benchmarks.load.locust` from `load/audit.py`/`cli/load.py` (for `run()`).
"""

from __future__ import annotations

import asyncio
import csv
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from locust import FastHttpUser, constant, events, task

PATH = os.environ.get("LOCUST_PATH", "/rowform")
EXPECT = int(os.environ.get("LOCUST_EXPECT", "0"))


class BenchUser(FastHttpUser):
    wait_time = constant(0)
    # geventhttpclient defaults to a 60s network timeout; a stalled request
    # should surface as a failure inside the run, not hang past the deadline.
    network_timeout = 30.0
    connection_timeout = 10.0

    @task
    def get(self):
        with self.client.get(PATH, name=PATH, catch_response=True) as r:
            if EXPECT and len(r.content) != EXPECT:
                r.failure(f"expected {EXPECT} bytes, got {len(r.content)}")


@events.test_start.add_listener
def _announce(environment, **_):
    print(
        f"locust -> {PATH}  expect={EXPECT or 'unchecked'} bytes  "
        f"users={environment.runner.target_user_count if environment.runner else '?'}"
    )


@events.test_stop.add_listener
def _warn_on_failures(environment, **_):
    stats = environment.stats.total
    if stats.num_failures:
        print(
            f"WARNING: {stats.num_failures} failed requests — throughput figures "
            f"from this run are not usable"
        )


@dataclass(slots=True)
class LocustResult:
    path: str
    users: int
    completed: int
    failures: int
    rps: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


async def run(
    *, host: str, path: str, users: int, duration: float, expect_bytes: int = 0,
    warmup: float = 5.0, on_spawn: Callable[[int], None] | None = None,
) -> LocustResult:
    """Drive `BenchUser` against `path` for `duration` seconds at `users`
    concurrent users, headless, via the `locust` CLI — parses the CSV summary
    the same way `bench_locust.sh` did.

    `on_spawn`, if given, is called once with the *measured* locust
    subprocess's pid (not the discarded warmup process's) — the caller's hook
    for tracking every process it spawns, e.g. into a `ProcessMonitor`."""
    module_path = str(Path(__file__).resolve())
    with tempfile.TemporaryDirectory(prefix="rowform-bench-locust-") as tmpdir:
        prefix = str(Path(tmpdir) / "run")
        env = {
            **os.environ, "LOCUST_PATH": path, "LOCUST_EXPECT": str(expect_bytes),
        }
        cmd = [
            "locust", "-f", module_path, "--headless", "--host", host,
            "-u", str(users), "-r", str(users), "-t", f"{duration:.0f}s",
            "--only-summary", "--csv", prefix,
        ]
        if warmup:
            # A short unmeasured ramp so connection setup isn't charged to the
            # measured window; locust has no native "warmup then reset" flag
            # for headless runs, so this runs a short throwaway pass first.
            warm_cmd = cmd.copy()
            warm_cmd[warm_cmd.index("-t") + 1] = f"{warmup:.0f}s"
            warm_prefix = str(Path(tmpdir) / "warmup")
            warm_cmd[warm_cmd.index("--csv") + 1] = warm_prefix
            proc = await asyncio.create_subprocess_exec(*warm_cmd, env=env,
                                                          stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()

        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        if on_spawn is not None:
            on_spawn(proc.pid)
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"locust failed: {stderr.decode()[-2000:]}")

        # The subprocess has already exited, so this is a one-shot read of a
        # small file with nothing else in the loop to block — not worth a
        # thread hop.
        with open(f"{prefix}_stats.csv") as fh:  # noqa: ASYNC230
            row = next(r for r in csv.DictReader(fh) if r["Name"] == "Aggregated")
        return LocustResult(
            path=path, users=users,
            completed=int(row["Request Count"]), failures=int(row["Failure Count"]),
            rps=float(row["Requests/s"]), mean_ms=float(row["Average Response Time"]),
            p50_ms=float(row["50%"]), p95_ms=float(row["95%"]), p99_ms=float(row["99%"]),
        )

