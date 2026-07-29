"""`CaseUser`, the shared locust base every file under `benchmarks/loadtests/`
subclasses, plus `run()`, which drives one such file via the `locust` CLI and
parses its CSV summary.

Design choices:

* **`FastHttpUser`, not `HttpUser`.** The default user wraps `requests`,
  costing ~1ms of client CPU per request — on one pinned core that makes the
  *client* the bottleneck well below the server's throughput.
* **`wait_time = constant(0)`.** No think time, so N users means N requests
  in flight at all times — a closed loop.
* **Response validation.** Every response is checked for the expected byte
  length (via `LOCUST_EXPECT`, set per run since it varies with `--limit`),
  so a run that silently 500s cannot be reported as throughput.

Each case file sets its own `path` class attribute (the route to hit) and is
loaded via `locust -f <that file's path>` — no `LOCUST_PATH` env var
indirection for *which* route, since that's now a property of the file
itself, not a runtime parameter (PLAN.md-successor decision: "each contender
is a locust file").
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

EXPECT = int(os.environ.get("LOCUST_EXPECT", "0"))
LIMIT = int(os.environ.get("LOCUST_LIMIT", "1000"))


class CaseUser(FastHttpUser):
    """Base class for every `benchmarks/loadtests/*.py` file — each
    subclasses this and sets `path`.

    `abstract = True` is load-bearing, not decorative: without it, locust
    auto-spawns every concrete `User` subclass it finds in a locustfile,
    *including this base class itself* once a case file imports it — so a
    run against e.g. `sqlite-flat-rowform` would actually split its requested
    concurrency between the real route and this class's default `/noop`
    path, roughly in half, silently. `abstract = True` tells locust this
    class is not runnable on its own, only its subclasses are.
    """

    abstract = True
    wait_time = constant(0)
    # geventhttpclient defaults to a 60s network timeout; a stalled request
    # should surface as a failure inside the run, not hang past the deadline.
    network_timeout = 30.0
    connection_timeout = 10.0
    path: str = "/noop"

    @task
    def hit(self):
        # `limit` is a query param on every request (LOCUST_LIMIT, set per
        # run since it varies with `--limit`) — the worker (service/app.py)
        # reads it per request, not once at startup.
        url = f"{self.path}?limit={LIMIT}"
        with self.client.get(url, name=self.path, catch_response=True) as r:
            if EXPECT and len(r.content) != EXPECT:
                r.failure(f"expected {EXPECT} bytes, got {len(r.content)}")


@events.test_start.add_listener
def _announce(environment, **_):
    print(
        f"locust  expect={EXPECT or 'unchecked'} bytes  "
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
    users: int
    completed: int
    failures: int
    rps: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


async def run(
    *, host: str, locustfile: str, users: int, duration: float, expect_bytes: int = 0,
    limit: int = 1000, warmup: float = 5.0, on_spawn: Callable[[int], None] | None = None,
) -> LocustResult:
    """Drive `locustfile` (a `CaseUser` subclass) against `host` for
    `duration` seconds at `users` concurrent users, headless — parses the CSV
    summary the same way `bench_locust.sh` did.

    `on_spawn`, if given, is called once with the *measured* locust
    subprocess's pid (not the discarded warmup process's) — the caller's hook
    for tracking every process it spawns, e.g. into a `ProcessMonitor`.
    """
    with tempfile.TemporaryDirectory(prefix="rowform-bench-locust-") as tmpdir:
        prefix = str(Path(tmpdir) / "run")
        env = {**os.environ, "LOCUST_EXPECT": str(expect_bytes), "LOCUST_LIMIT": str(limit)}
        cmd = [
            "locust", "-f", locustfile, "--headless", "--host", host,
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
            users=users,
            completed=int(row["Request Count"]), failures=int(row["Failure Count"]),
            rps=float(row["Requests/s"]), mean_ms=float(row["Average Response Time"]),
            p50_ms=float(row["50%"]), p95_ms=float(row["95%"]), p99_ms=float(row["99%"]),
        )
