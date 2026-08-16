"""`bench load run`.

Provisions whatever backend `--case` needs (an ephemeral sqlite file, or an
ephemeral postgres container — `benchmarks/backends/provision.py`) plus
`--workers` FastAPI worker(s), sweeps `--levels` of locust concurrency against
one `--case`, and audits every level (sockets, Little's Law, generator
saturation) plus the sweep as a whole (scaling knee, `/noop` headroom) —
self-contained the same way the old `bench_locust.sh` was, minus the shell
script. Locust is the only generator (no more httpload<->locust cross-check:
there is only one generator left to cross-check against itself). The postgres
container is always torn down in a `finally`, win or lose — never left
running after a `bench load run` exits.

`--case` is a contender slug (`bench contenders list`): `harness/registry.py`
says which shape/backend to provision, `load/registry.py` (derived from
`service/app.py`'s own route table) says which route locust drives traffic
at. Before any level runs, the case's response is fetched once and
byte-compared against the rowform reference route for the same
backend/shape — the HTTP counterpart of `bench micro`'s equivalence gate —
and its byte length is then enforced on *every* response via
`LOCUST_EXPECT`, so a run that silently returns the wrong payload cannot be
reported as throughput.

`--workers` (how many uvicorn processes are spawned) and `--levels` (how many
locust users are kept in flight) are deliberately separate knobs: each worker
is its own process on its own port, so testing N workers under load means
splitting the requested concurrency across N ports and summing the result —
`_split_across_workers`/`_aggregate_locust` below.

Every process this module spawns (each uvicorn worker, each `locust`
subprocess) is tracked in a `ProcessMonitor`, which samples every tracked
pid's CPU utilization once a second, silently, and prints only the average at
the end. Per level, the server's own CPU utilization over the interval
bracketing that level's measured window (`cpuacct.CpuAccountant`, summed
across every worker pid; see `_measure_level`) is also printed live in the
sweep table, next to the existing client-side
`generator_util` figure — if `--name` is given, the full time series plus the
run's config/results, including a machine + git snapshot (`harness/env.py`),
are also written to `results/runs/loadtests/<name>-<case>.json`. uvicorn's own
stdout/stderr is discarded (`launch(..., quiet=True)`) so it doesn't interleave
with the run's own progress output.
"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import typer

import benchmarks.micro.contenders  # noqa: F401 -- @contender registration side-effects
from benchmarks.backends.provision import Teardown, provision
from benchmarks.harness import cpuacct, registry
from benchmarks.harness import env as env_module
from benchmarks.harness.monitor import ProcessMonitor
from benchmarks.harness.registry import ContenderSpec
from benchmarks.load import audit as audit_module
from benchmarks.load import locust as locust_module
from benchmarks.load import registry as load_registry
from benchmarks.load.locust import LocustResult
from benchmarks.service.launch import ServiceWorker, launch

app = typer.Typer(help="Load-test one case with locust, auditing every level.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
DEFAULT_WARMUP_S = 2.0


async def _provision(
    spec: ContenderSpec,
    rows: int,
    port: int,
    server_cores: list[int],
    workers: int,
    pg_port: int,
) -> tuple[Teardown, list[ServiceWorker]]:
    env, teardown = await provision(spec.backend, spec.shape, rows, pg_port=pg_port)
    try:
        workers_list = await launch(
            "benchmarks.service.app:app",
            base_port=port,
            workers=workers,
            cores=server_cores,
            env=env,
            quiet=True,
        )
    except BaseException:
        # The caller's try/finally hasn't begun yet — a failed launch must tear
        # the backend down here or the postgres container outlives the run.
        await teardown()
        raise
    return teardown, workers_list


def _resolve_case(case: str) -> tuple[ContenderSpec, load_registry.LoadCase]:
    """Look up `case` in both registries: `harness/registry.py` for
    shape/backend/route (what `bench load` needs to provision the worker),
    `load/registry.py` for the locustfile that drives traffic at it. `bench
    load` only knows how to provision sqlite/postgres backends, so a mock
    slug is rejected with a clear message rather than failing confusingly
    deeper in `_provision`."""
    try:
        spec = registry.get(case)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from None
    if spec.backend not in ("sqlite", "postgres"):
        raise typer.BadParameter(
            f"{case!r} is a {spec.backend!r}-backend contender; bench load only "
            f"provisions sqlite/postgres backends"
        )
    try:
        load_case = load_registry.get(case)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from None
    return spec, load_case


def _server_roles(workers_list: list[ServiceWorker]) -> dict[str, int]:
    """`{"server": pid}` for one worker, `{"server-0": pid, "server-1": pid,
    ...}` for several — one role name per uvicorn process, always distinct
    from the "generator-locust" role(s) tracking the load side."""
    if len(workers_list) == 1:
        return {"server": workers_list[0].proc.pid}
    return {f"server-{i}": w.proc.pid for i, w in enumerate(workers_list)}


def _split_across_workers(total: int, n: int) -> list[int]:
    """Divide `total` users as evenly as possible across `n` worker ports —
    e.g. 10 across 3 ports -> [4, 3, 3]. A port that would get 0 is dropped by
    the caller rather than asked to run a zero-concurrency generator."""
    base, remainder = divmod(total, n)
    return [base + (1 if i < remainder else 0) for i in range(n)]


def _aggregate_locust(results: list[LocustResult]) -> LocustResult:
    """Merge one `LocustResult` per worker port into one: throughput adds
    (independent servers, independent client processes). locust's CSV summary
    gives no raw per-request latencies to pool, so mean/percentiles here are a
    completed-count-weighted average across ports — an approximation, not a
    recomputed pooled percentile. The merged window is the union of the
    per-port measured windows."""
    if len(results) == 1:
        return results[0]
    total_completed = sum(r.completed for r in results) or 1

    def weighted(attr: str) -> float:
        return sum(getattr(r, attr) * r.completed for r in results) / total_completed

    return LocustResult(
        users=sum(r.users for r in results),
        completed=sum(r.completed for r in results),
        failures=sum(r.failures for r in results),
        rps=sum(r.rps for r in results),
        mean_ms=weighted("mean_ms"),
        p50_ms=weighted("p50_ms"),
        p95_ms=weighted("p95_ms"),
        p99_ms=weighted("p99_ms"),
        window_start=min(r.window_start for r in results),
        window_end=max(r.window_end for r in results),
    )


@dataclass(slots=True)
class LevelMeasurement:
    result: LocustResult
    sockets: int  # -1 when not sampled
    generator_util: float  # avg per-generator CPU utilization over its lifetime
    server_util: float  # server CPU over the measured window


async def _wait_for_windows(
    window_files: list[str], tasks: list[asyncio.Task], timeout: float,
) -> float:
    """Block until every measured locust process has reported test_start (its
    window file exists), and return the latest start timestamp — the moment
    all generators are actually running. Bails out (returning "now") if a run
    dies before opening its window or the timeout passes; the caller's gather
    then surfaces the real error."""
    deadline = time.monotonic() + timeout
    while True:
        starts = []
        for window_file in window_files:
            try:
                starts.append(float(Path(window_file).read_text().split()[0]))
            except (OSError, IndexError, ValueError):
                break
        else:
            return max(starts)
        if any(task.done() for task in tasks) or time.monotonic() >= deadline:
            return time.time()
        await asyncio.sleep(0.05)


async def _measure_level(
    workers_list: list[ServiceWorker],
    route: str,
    total_users: int,
    duration: float,
    *,
    expect_bytes: int,
    monitor: ProcessMonitor,
    server_accountant: cpuacct.CpuAccountant,
    limit: int = 1000,
    warmup: float = DEFAULT_WARMUP_S,
    count_sockets: bool = True,
) -> LevelMeasurement:
    """One audited measurement of `route` at `total_users`, split across the
    workers: warm passes first, then the measured passes with CPU accounting
    and the socket sample aligned to the window the locust processes *report*
    (test_start/test_stop timestamps) rather than guessed from sleep math —
    locust runs as two subprocesses per level whose startup and ramp made
    every parent-side guess land outside the actual measured window.

    `generator_util` divides the measured passes' CPU (RUSAGE_CHILDREN
    bracket — the warm passes are already reaped, so excluded) by their
    wall-clock lifetime and the generator count: average per-generator
    utilization, which is what the saturation gate means. `server_util`
    divides the server pids' CPU by the interval the accountant actually
    sampled — opened once every generator has reported test_start, closed
    once they have all exited. That brackets the reported window; it is not
    the window itself (the union of per-generator windows starts at the
    *earliest* test_start), and dividing by the window would have put a
    numerator and a denominator from two different intervals in one ratio.
    """
    per_worker = _split_across_workers(total_users, len(workers_list))
    pairs = [(w, n) for w, n in zip(workers_list, per_worker, strict=True) if n > 0]
    single = len(pairs) == 1

    if warmup:
        await asyncio.gather(*[
            locust_module.warm(
                host=f"http://127.0.0.1:{w.port}",
                locustfile=load_registry.locustfile(),
                route=route, users=n, duration=warmup,
                expect_bytes=expect_bytes, limit=limit,
            )
            for w, n in pairs
        ])

    def make_on_spawn(index: int):
        role = "generator-locust" if single else f"generator-locust-{index}"

        def on_spawn(pid: int) -> None:
            monitor.track(role, pid)

        return on_spawn

    with tempfile.TemporaryDirectory(prefix="rowform-bench-windows-") as tmpdir:
        window_files = [str(Path(tmpdir) / f"window-{i}") for i in range(len(pairs))]
        cpu_before = cpuacct.children_cpu_seconds()
        bracket_start = time.monotonic()
        tasks = [
            asyncio.ensure_future(
                locust_module.run(
                    host=f"http://127.0.0.1:{w.port}",
                    locustfile=load_registry.locustfile(),
                    route=route, users=n, duration=duration,
                    expect_bytes=expect_bytes, limit=limit,
                    window_file=window_files[i],
                    on_spawn=make_on_spawn(i),
                )
            )
            for i, (w, n) in enumerate(pairs)
        ]
        sockets = -1
        try:
            all_started = await _wait_for_windows(window_files, tasks, timeout=duration + 60.0)
            acct_start = time.monotonic()
            server_accountant.start()
            if count_sockets:
                # Mid-window, per the generators' own clocks — sampling after
                # they exit would always see 0 ESTABLISHED sockets.
                await asyncio.sleep(max(0.0, all_started + duration / 2 - time.time()))
                sockets = sum(audit_module.count_established(w.port) for w, _ in pairs)
            results = await asyncio.gather(*tasks)
            generator_cpu = cpuacct.children_cpu_seconds() - cpu_before
            bracket_elapsed = time.monotonic() - bracket_start
            server_util = server_accountant.stop(time.monotonic() - acct_start)["server"]
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    return LevelMeasurement(
        result=_aggregate_locust(list(results)),
        sockets=sockets,
        generator_util=generator_cpu / bracket_elapsed / len(pairs) if bracket_elapsed else 0.0,
        server_util=server_util,
    )


def _fetch(url: str) -> bytes:
    # Loopback only — every URL here is built from 127.0.0.1 and a worker
    # port this process just spawned.
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def _json_number(value: float) -> float | None:
    """`json.dump` writes NaN/Infinity as bare tokens, which RFC 8259 has no
    room for — `jq` and every strict parser then reject the whole artifact.
    A level that completed nothing carries exactly those: locust writes "N/A"
    latencies (so mean/in-flight are nan) and reports no window."""
    return value if math.isfinite(value) else None


def _save(name: str, payload: dict) -> Path:
    out_dir = RESULTS_DIR / "runs" / "loadtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


@app.command()
def run(
    case: str = typer.Option("postgres-flat-rowform", help=load_registry.CASE_HELP),
    rows: int = typer.Option(50_000),
    limit: int = typer.Option(250),
    levels: str = typer.Option("1,128", help="locust concurrency levels to sweep"),
    duration: float = typer.Option(5.0),
    warmup: float = typer.Option(
        DEFAULT_WARMUP_S, help="unmeasured locust ramp before each level's measured window"
    ),
    port: int = typer.Option(8999),
    workers: int = typer.Option(
        1,
        help="uvicorn worker processes to spawn (one port each) — independent of "
        "--levels, the load concurrency swept per level",
    ),
    pg_port: int = typer.Option(
        5432, help="host port for the ephemeral postgres container (postgres cases only)"
    ),
    cross_check_level: int | None = typer.Option(
        None,
        help="also print /noop's rps at this level, for a by-hand headroom sanity check "
        "(the automatic /noop headroom gate always runs at the highest --levels value)",
    ),
    name: str | None = typer.Option(
        None,
        help="if given, write per-second CPU samples + results to "
        "results/runs/loadtests/<name>-<case>.json",
    ),
) -> None:
    """Sweep `--levels` of locust concurrency against `--case`, auditing every
    level (sockets, Little's Law, generator saturation) and the sweep as a
    whole (scaling knee, `/noop` headroom). Exits non-zero if any check
    fails."""
    if workers < 1:
        raise typer.BadParameter("--workers must be >= 1")
    if warmup and warmup < 1:
        # `locust.warm()` rejects it too, but only once the backend is
        # provisioned and the workers are up.
        raise typer.BadParameter("--warmup must be 0 (skip) or >= 1 (locust -t resolution)")
    if duration < 1:
        # Unlike --warmup nothing downstream catches this: `_cmd` formats
        # `-t {seconds:.0f}s`, so --duration 0.5 becomes `-t 0s` and the level
        # measures whatever locust does with a zero deadline, silently.
        raise typer.BadParameter("--duration must be >= 1 (locust -t resolution)")
    parsed_levels = sorted(int(c) for c in levels.split(","))

    all_cases = load_registry.discover()
    match case:
        case "all":
            cases = all_cases
        case "sqlite":
            cases = [c for c in all_cases if c.startswith("sqlite-")]
        case "postgres":
            cases = [c for c in all_cases if c.startswith("postgres-")]
        case _:
            cases = [case]

    for _case in cases:

        async def go(case: str) -> bool:
            spec, load_case = _resolve_case(case)
            route = load_case.route
            teardown, workers_list = await _provision(spec, rows, port, [], workers, pg_port)
            env_start = env_module.capture()
            server_pids = [w.proc.pid for w in workers_list]
            server_accountant = cpuacct.CpuAccountant({"server": server_pids})
            monitor = ProcessMonitor(print_fn=typer.echo)
            for role, pid in _server_roles(workers_list).items():
                monitor.track(role, pid)
            monitor.start()
            ok = True
            level_rows: list[dict] = []
            results: list[tuple[int, float]] = []
            knee: int | None = None
            headroom_ratio = 0.0
            try:
                typer.echo(
                    f"case={case!r} ({route}) — {workers} worker(s) on ports "
                    f"{[w.port for w in workers_list]}\n"
                )
                # The HTTP counterpart of `bench micro`'s equivalence gate:
                # fetch the case's response once, byte-compare it against the
                # rowform reference route for the same backend/shape, and then
                # have locust enforce that byte length on every response
                # (LOCUST_EXPECT). A route that silently returns the wrong
                # payload is refused before a single level is measured.
                base = f"http://127.0.0.1:{workers_list[0].port}"
                payload = _fetch(f"{base}{route}?limit={limit}")
                expect_bytes = len(payload)
                reference_route = f"/{spec.backend}-{spec.shape}-rowform"
                if route != reference_route:
                    reference_payload = _fetch(f"{base}{reference_route}?limit={limit}")
                    if payload != reference_payload:
                        typer.echo(
                            f"equivalence: FAIL — {route} returned {len(payload)} bytes, "
                            f"{reference_route} returned {len(reference_payload)}; "
                            f"refusing to measure inequivalent work"
                        )
                        return False
                verdict = (
                    f"{route} == {reference_route}"
                    if route != reference_route
                    else f"{route} is the reference route"
                )
                typer.echo(
                    f"equivalence: {verdict} — every response must now be exactly "
                    f"{expect_bytes} bytes\n"
                )
                typer.echo(
                    f"{'c':>4} {'sockets':>8} {'rps':>10} {'in-flight':>10} "
                    f"{'server_cpu%':>11} client_cpu% little's law"
                )
                for level in parsed_levels:
                    m = await _measure_level(
                        workers_list,
                        route,
                        level,
                        duration,
                        expect_bytes=expect_bytes,
                        monitor=monitor,
                        server_accountant=server_accountant,
                        limit=limit,
                        warmup=warmup,
                    )
                    result = m.result
                    sat_ok, _ = audit_module.check_generator_saturation(m.generator_util)
                    check = audit_module.check_littles_law(
                        result.rps, result.mean_ms, level, m.sockets
                    )
                    if not sat_ok:
                        check.ok = False
                        check.notes.append(
                            f"generator CPU utilization {m.generator_util:.0%} >= "
                            f"{audit_module.GENERATOR_SATURATION_MAXIMUM:.0%} — the client, not "
                            f"the server, may be the bottleneck at this level"
                        )
                    if result.failures:
                        check.ok = False
                        check.notes.append(f"{result.failures} failed requests")
                    results.append((level, result.rps))
                    status = "OK" if check.ok else "FAIL: " + "; ".join(check.notes)
                    typer.echo(
                        f"{level:>4} {m.sockets:>8} {result.rps:>10.0f} {check.in_flight:>10.2f} "
                        f"{m.server_util:>10.0%} {m.generator_util:>5.0%} {status}"
                    )
                    ok = ok and check.ok
                    level_rows.append({
                        "concurrency": level,
                        "sockets": m.sockets,
                        "rps": result.rps,
                        "mean_ms": _json_number(result.mean_ms),
                        "in_flight": _json_number(check.in_flight),
                        "server_cpu_utilization": m.server_util,
                        "generator_cpu_utilization": m.generator_util,
                        "window_s": _json_number(result.window_end - result.window_start),
                        "ok": check.ok,
                        "notes": check.notes,
                    })

                knee = audit_module.find_scaling_knee(results)
                typer.echo(
                    f"\nscaling knee: {knee if knee is not None else 'not reached within tested levels'}"
                )

                noop = (await _measure_level(
                    workers_list,
                    "/noop",
                    parsed_levels[-1],
                    duration,
                    expect_bytes=len(b"[]"),
                    monitor=monitor,
                    server_accountant=server_accountant,
                    limit=limit,
                    warmup=warmup,
                    count_sockets=False,
                )).result
                if noop.failures:
                    ok = False
                    typer.echo(f"/noop run had {noop.failures} failed requests — headroom unusable")
                fastest_db_rps = max(rps for _, rps in results)
                headroom_ok, headroom_ratio = audit_module.check_noop_headroom(
                    noop.rps, fastest_db_rps
                )
                typer.echo(
                    f"/noop headroom: {headroom_ratio:.2f}x fastest db endpoint "
                    f"({'OK' if headroom_ok else f'FAIL: below {audit_module.NOOP_HEADROOM_MINIMUM}x'})"
                )
                ok = ok and headroom_ok

                if cross_check_level is not None:
                    # Both sides re-measured at the same level: the earlier
                    # version printed /noop's rps at the *highest* level against
                    # the case at this one — a ratio of mismatched operating
                    # points presented as a sanity check.
                    at_level = (await _measure_level(
                        workers_list, route, cross_check_level, duration,
                        expect_bytes=expect_bytes, monitor=monitor,
                        server_accountant=server_accountant, limit=limit,
                        warmup=warmup, count_sockets=False,
                    )).result
                    noop_at_level = (await _measure_level(
                        workers_list, "/noop", cross_check_level, duration,
                        expect_bytes=len(b"[]"), monitor=monitor,
                        server_accountant=server_accountant, limit=limit,
                        warmup=warmup, count_sockets=False,
                    )).result
                    typer.echo(
                        f"case rps at c={cross_check_level}: {at_level.rps:.0f} "
                        f"({'/noop is ' + f'{noop_at_level.rps / at_level.rps:.2f}x at the same level' if at_level.rps else 'n/a'})"
                    )
            finally:
                await monitor.stop()
                for worker in workers_list:
                    await worker.stop()
                await teardown()

            monitor.print_averages()

            if name:
                # Merged with an end snapshot so the artifact records mid-run
                # drift (throttling, frequency sag) instead of a single
                # instant mislabeled as the whole window.
                env_merged = env_module.merge_start_end(env_start, env_module.capture())
                path_out = _save(
                    f"{name}-{case}",
                    {
                        "command": "load run",
                        "config": {
                            "case": case,
                            "rows": rows,
                            "limit": limit,
                            "levels": parsed_levels,
                            "duration": duration,
                            "warmup": warmup,
                            "workers": workers,
                            "route": route,
                            "expect_bytes": expect_bytes,
                            "cross_check_level": cross_check_level,
                        },
                        "levels": level_rows,
                        "scaling_knee": knee,
                        "noop_headroom_ratio": _json_number(headroom_ratio),
                        "ok": ok,
                        "monitor": monitor.to_dict(),
                        "env": env_merged,
                    },
                )
                typer.echo(f"\nwrote {path_out}")
            return ok

        passed = asyncio.run(go(_case))
        if not passed:
            raise typer.Exit(1)


@app.command()
def cases() -> None:
    """List every load-test case — the slugs `--case` accepts, one per case
    route in `service/app.py` (a route whose path is not a harness contender
    slug fails this listing loudly; see `load/registry.py`)."""
    found = load_registry.discover()
    if not found:
        typer.echo("no case routes found in benchmarks/service/app.py")
        raise typer.Exit(1)
    width = max(len(slug) for slug in found)
    for slug, case in sorted(found.items()):
        typer.echo(f"{slug:<{width}}  {case.route}")
