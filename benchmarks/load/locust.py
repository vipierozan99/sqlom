"""`CaseUser`, the locust user the single case locustfile
(`load/locustfile.py`) subclasses, plus `run()`, which drives it via the
`locust` CLI and parses its CSV summary.

Design choices:

* **`FastHttpUser`, not `HttpUser`.** The default user wraps `requests`,
  costing ~1ms of client CPU per request — on one pinned core that makes the
  *client* the bottleneck well below the server's throughput.
* **`wait_time = constant(0)`.** No think time, so N users means N requests
  in flight at all times — a closed loop.
* **Response validation.** `CaseUser.hit()` can check every response for an
  expected byte length (via `LOCUST_EXPECT`), but no current caller passes
  `expect_bytes` — locust's CSV summary exposes nothing to derive it from, so
  `bench load run` runs with it off (the gap is noted where the levels loop
  lives, `cli/load.py`). HTTP-level failures are still counted and fail the
  level.

The one concrete subclass lives in `load/locustfile.py`; which route it hits
arrives via `LOCUST_ROUTE` (see `run(route=...)`), the same env channel
`LOCUST_LIMIT`/`LOCUST_EXPECT` use — case identity lives in the service app's
route table, not in per-case locustfiles.
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
    """Base class for the case locustfile (`load/locustfile.py`), which
    subclasses this and sets `path`.

    `abstract = True` is load-bearing, not decorative: without it, locust
    auto-spawns every concrete `User` subclass it finds in a locustfile,
    *including this base class itself* once the case file imports it — so a
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


def _aggregated_row(prefix: str) -> dict[str, str] | None:
    """The "Aggregated" row of a locust `--csv` stats file, or None if the
    file or the row is missing (locust died before writing stats)."""
    try:
        with open(f"{prefix}_stats.csv") as fh:
            return next((r for r in csv.DictReader(fh) if r["Name"] == "Aggregated"), None)
    except OSError:
        return None


def _ms(value: str) -> float:
    # locust writes the literal "N/A" into every percentile column of a run
    # that completed zero requests; callers guard the zero case, this guards
    # the parse.
    return float("nan") if value == "N/A" else float(value)


async def run(
    *, host: str, locustfile: str, users: int, duration: float, route: str | None = None,
    expect_bytes: int = 0, limit: int = 1000, warmup: float = 5.0,
    on_spawn: Callable[[int], None] | None = None,
) -> LocustResult:
    """Drive `locustfile` (a `CaseUser` subclass) against `host` for
    `duration` seconds at `users` concurrent users, headless — parses the CSV
    summary the same way `bench_locust.sh` did. `route`, if given, is exported
    as `LOCUST_ROUTE` for `load/locustfile.py` to hit.

    Failed requests are returned in `LocustResult.failures`, not raised:
    locust exits non-zero whenever any request failed but still writes full
    stats, and "the level had failures" is the caller's verdict to render (it
    fails the audit), not a crash. Only a run that produced no stats at all —
    bad locustfile, unreachable host — raises.

    `on_spawn`, if given, is called once with the *measured* locust
    subprocess's pid (not the discarded warmup process's) — the caller's hook
    for tracking every process it spawns, e.g. into a `ProcessMonitor`.
    """
    if duration < 1 or (warmup and warmup < 1):
        # `-t` takes whole seconds; f"{0.4:.0f}s" is "0s", which locust reads
        # as "run until stopped".
        raise ValueError(
            f"locust -t has 1-second resolution: duration={duration}, warmup={warmup}"
        )
    with tempfile.TemporaryDirectory(prefix="rowform-bench-locust-") as tmpdir:
        prefix = str(Path(tmpdir) / "run")
        env = {**os.environ, "LOCUST_EXPECT": str(expect_bytes), "LOCUST_LIMIT": str(limit)}
        if route is not None:
            env["LOCUST_ROUTE"] = route
        cmd = [
            "locust", "-f", locustfile, "--headless", "--host", host,
            "-u", str(users), "-r", str(users), "-t", f"{duration:.0f}s",
            "--only-summary", "--csv", prefix,
        ]
        if warmup:
            # A short throwaway pass to warm the server side (lazily-opened
            # pools, statement caches); locust has no native "warmup then
            # reset" flag for headless runs. It runs as its own subprocess, so
            # its TCP connections do NOT carry over — the measured pass still
            # reconnects inside its own window.
            warm_cmd = cmd.copy()
            warm_cmd[warm_cmd.index("-t") + 1] = f"{warmup:.0f}s"
            warm_prefix = str(Path(tmpdir) / "warmup")
            warm_cmd[warm_cmd.index("--csv") + 1] = warm_prefix
            proc = await asyncio.create_subprocess_exec(*warm_cmd, env=env,
                                                          stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.PIPE)
            _, warm_stderr = await proc.communicate()
            warm_row = _aggregated_row(warm_prefix)
            warm_completed = int(warm_row["Request Count"]) if warm_row else 0
            warm_failures = int(warm_row["Failure Count"]) if warm_row else 0
            if warm_completed == 0 or warm_failures == warm_completed:
                # A warmup in which nothing succeeded warmed nothing — the
                # measured pass would silently measure a cold (or broken)
                # server.
                raise RuntimeError(
                    f"locust warmup pass completed {warm_completed} requests with "
                    f"{warm_failures} failures — the server was never warmed: "
                    f"{warm_stderr.decode()[-2000:]}"
                )
            if warm_failures:
                print(f"WARNING: {warm_failures} failed requests during locust warmup")

        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        if on_spawn is not None:
            on_spawn(proc.pid)
        _stdout, stderr = await proc.communicate()
        row = _aggregated_row(prefix)
        if row is None:
            raise RuntimeError(
                f"locust wrote no stats (exit {proc.returncode}): {stderr.decode()[-2000:]}"
            )
        completed = int(row["Request Count"])
        if completed == 0:
            raise RuntimeError(
                "locust completed 0 requests in the measured window — nothing to "
                "report; increase --duration or check the endpoint"
            )
        return LocustResult(
            users=users,
            completed=completed, failures=int(row["Failure Count"]),
            rps=float(row["Requests/s"]), mean_ms=_ms(row["Average Response Time"]),
            p50_ms=_ms(row["50%"]), p95_ms=_ms(row["95%"]), p99_ms=_ms(row["99%"]),
        )
