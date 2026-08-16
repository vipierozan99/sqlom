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
import time
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


#: Where this locust process writes its measured window's wall-clock
#: timestamps (test_start on line 1, test_stop appended on line 2) — the
#: orchestrator reads them, because locust runs as two subprocesses per level
#: (warmup, then measured) whose interpreter startup and ramp are invisible
#: from outside: every "sample mid-run" or "divide CPU by the window" guess
#: made from the parent used to be against a timeline that didn't exist.
WINDOW_FILE = os.environ.get("LOCUST_WINDOW_FILE", "")


@events.test_start.add_listener
def _announce(environment, **_):
    if WINDOW_FILE:
        Path(WINDOW_FILE).write_text(f"{time.time()}\n")
    print(
        f"locust  expect={EXPECT or 'unchecked'} bytes  "
        f"users={environment.runner.target_user_count if environment.runner else '?'}"
    )


@events.test_stop.add_listener
def _warn_on_failures(environment, **_):
    if WINDOW_FILE:
        with open(WINDOW_FILE, "a") as fh:
            fh.write(f"{time.time()}\n")
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
    #: Wall-clock bounds of the measured window, reported by the locust
    #: process itself (test_start/test_stop) — nan if it never reported.
    window_start: float = float("nan")
    window_end: float = float("nan")


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


def _env_for(route: str | None, expect_bytes: int, limit: int) -> dict[str, str]:
    # BENCH_* deliberately passes through: the locustfile never reads it, and
    # the worker got its own copy at launch.
    env = {**os.environ, "LOCUST_EXPECT": str(expect_bytes), "LOCUST_LIMIT": str(limit)}
    if route is not None:
        env["LOCUST_ROUTE"] = route
    return env


def _cmd(locustfile: str, host: str, users: int, seconds: float, prefix: str) -> list[str]:
    return [
        "locust", "-f", locustfile, "--headless", "--host", host,
        "-u", str(users), "-r", str(users), "-t", f"{seconds:.0f}s",
        "--only-summary", "--csv", prefix,
    ]


async def _communicate(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """`proc.communicate()`, but the subprocess dies with the awaiting task —
    a cancelled level (Ctrl-C, a sibling worker's failure inside a gather)
    must not leave a locust generating load until its `-t` expires."""
    try:
        return await proc.communicate()
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise


async def warm(
    *, host: str, locustfile: str, users: int, duration: float, route: str | None = None,
    expect_bytes: int = 0, limit: int = 1000,
) -> None:
    """One throwaway locust pass to warm the server side (lazily-opened pools,
    statement caches); locust has no native "warmup then reset" flag for
    headless runs. Its own subprocess, so its TCP connections do NOT carry
    over — the measured pass still reconnects inside its own window. A
    separate function rather than a `run()` flag so callers can bracket CPU
    accounting around the measured pass alone.

    A warmup in which nothing succeeded warmed nothing — the measured pass
    would silently measure a cold (or broken) server — so that raises.
    """
    if duration < 1:
        raise ValueError(f"locust -t has 1-second resolution: warmup duration={duration}")
    with tempfile.TemporaryDirectory(prefix="rowform-bench-locust-warm-") as tmpdir:
        prefix = str(Path(tmpdir) / "warmup")
        proc = await asyncio.create_subprocess_exec(
            *_cmd(locustfile, host, users, duration, prefix),
            env=_env_for(route, expect_bytes, limit),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await _communicate(proc)
        row = _aggregated_row(prefix)
        completed = int(row["Request Count"]) if row else 0
        failures = int(row["Failure Count"]) if row else 0
        if completed == 0 or failures == completed:
            raise RuntimeError(
                f"locust warmup pass completed {completed} requests with "
                f"{failures} failures — the server was never warmed: "
                f"{stderr.decode()[-2000:]}"
            )
        if failures:
            print(f"WARNING: {failures} failed requests during locust warmup")


def _window_bounds(window_file: str) -> tuple[float, float]:
    try:
        lines = Path(window_file).read_text().split()
        return float(lines[0]), float(lines[1])
    except (OSError, IndexError, ValueError):
        return float("nan"), float("nan")


async def run(
    *, host: str, locustfile: str, users: int, duration: float, route: str | None = None,
    expect_bytes: int = 0, limit: int = 1000, window_file: str | None = None,
    on_spawn: Callable[[int], None] | None = None,
) -> LocustResult:
    """Drive `locustfile` (a `CaseUser` subclass) against `host` for
    `duration` seconds at `users` concurrent users, headless — parses the CSV
    summary the same way `bench_locust.sh` did. `route`, if given, is exported
    as `LOCUST_ROUTE` for `load/locustfile.py` to hit. Call `warm()` first if
    the server should not be measured cold.

    Failed requests are returned in `LocustResult.failures`, not raised:
    locust exits non-zero whenever any request failed but still writes full
    stats, and "the level had failures" is the caller's verdict to render (it
    fails the audit), not a crash. Only a run that produced no stats at all —
    bad locustfile, unreachable host — raises.

    `on_spawn`, if given, is called once with the locust subprocess's pid —
    the caller's hook for tracking every process it spawns, e.g. into a
    `ProcessMonitor`. `window_file`, if given, is where the subprocess writes
    its window timestamps — pass a path you can watch to align samples with
    the actual measured window while the run is still in flight.
    """
    if duration < 1:
        # `-t` takes whole seconds; f"{0.4:.0f}s" is "0s", which locust reads
        # as "run until stopped".
        raise ValueError(f"locust -t has 1-second resolution: duration={duration}")
    with tempfile.TemporaryDirectory(prefix="rowform-bench-locust-") as tmpdir:
        prefix = str(Path(tmpdir) / "run")
        if window_file is None:
            window_file = str(Path(tmpdir) / "window")
        env = _env_for(route, expect_bytes, limit)
        env["LOCUST_WINDOW_FILE"] = window_file
        proc = await asyncio.create_subprocess_exec(
            *_cmd(locustfile, host, users, duration, prefix),
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        if on_spawn is not None:
            on_spawn(proc.pid)
        _stdout, stderr = await _communicate(proc)
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
        window_start, window_end = _window_bounds(window_file)
        return LocustResult(
            users=users,
            completed=completed, failures=int(row["Failure Count"]),
            rps=float(row["Requests/s"]), mean_ms=_ms(row["Average Response Time"]),
            p50_ms=_ms(row["50%"]), p95_ms=_ms(row["95%"]), p99_ms=_ms(row["99%"]),
            window_start=window_start, window_end=window_end,
        )
