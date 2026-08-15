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

`--case` is a contender slug (`bench contenders list`) resolved two ways:
`harness/registry.py` says which shape/backend to provision and which route
it serves; `load/registry.py` says which locustfile drives traffic at it
(each contender is a locust file, discovered by scanning `benchmarks/loadtests/`
rather than by importing `contenders.py`).

`--workers` (how many uvicorn processes are spawned) and `--levels` (how many
locust users are kept in flight) are deliberately separate knobs: each worker
is its own process on its own port, so testing N workers under load means
splitting the requested concurrency across N ports and summing the result —
`_split_across_workers`/`_aggregate_locust` below.

Every process this module spawns (each uvicorn worker, each `locust`
subprocess) is tracked in a `ProcessMonitor`, which samples every tracked
pid's CPU utilization once a second, silently, and prints only the average at
the end. Per level, the server's own CPU utilization over that level's measured
window (`cpuacct.CpuAccountant`, summed across every worker pid) is also
printed live in the sweep table, next to the existing client-side
`generator_util` figure — if `--name` is given, the full time series plus the
run's config/results, including a machine + git snapshot (`harness/env.py`),
are also written to `results/runs/loadtests/<name>-<case>.json`. uvicorn's own
stdout/stderr is discarded (`launch(..., quiet=True)`) so it doesn't interleave
with the run's own progress output.
"""

from __future__ import annotations

import asyncio
import json
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
    recomputed pooled percentile."""
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
    )


async def _locust_across(
    workers_list: list[ServiceWorker],
    locustfile: str,
    total_users: int,
    duration: float,
    expect_bytes: int,
    monitor: ProcessMonitor,
    limit: int = 1000,
    warmup: float = DEFAULT_WARMUP_S,
) -> LocustResult:
    per_worker = _split_across_workers(total_users, len(workers_list))
    single = len(workers_list) == 1

    def make_on_spawn(index: int):
        role = "generator-locust" if single else f"generator-locust-{index}"

        def on_spawn(pid: int) -> None:
            monitor.track(role, pid)

        return on_spawn

    results = await asyncio.gather(*[
        locust_module.run(
            host=f"http://127.0.0.1:{w.port}",
            locustfile=locustfile,
            users=n,
            duration=duration,
            expect_bytes=expect_bytes,
            limit=limit,
            warmup=warmup,
            on_spawn=make_on_spawn(i),
        )
        for i, (w, n) in enumerate(zip(workers_list, per_worker, strict=True))
        if n > 0
    ])
    return _aggregate_locust(list(results))


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
            route = f"/{spec.slug}"
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
                typer.echo(
                    f"{'c':>4} {'sockets':>8} {'rps':>10} {'in-flight':>10} "
                    f"{'server_cpu%':>11} client_cpu% little's law"
                )
                # locust's CSV summary exposes no per-response byte count, so
                # unlike the old httpload-based gate this cannot cross-check every
                # response's size against an expected value — `CaseUser.hit()`'s
                # `LOCUST_EXPECT` check is available (see `load/locust.py`) but
                # has nothing to compare against here; a wrong-shaped response
                # would still surface as an HTTP-level failure below.
                for level in parsed_levels:
                    # Sample /proc/net/tcp mid-run, not after: the generator
                    # closes every connection when it returns, so sampling
                    # afterward would always see 0 ESTABLISHED sockets regardless
                    # of whether it kept `level` requests in flight.
                    cpu_before = cpuacct.children_cpu_seconds()
                    server_accountant.start()
                    task = asyncio.ensure_future(
                        _locust_across(
                            workers_list,
                            load_case.file,
                            level,
                            duration,
                            expect_bytes=0,
                            monitor=monitor,
                            limit=limit,
                            warmup=warmup,
                        )
                    )
                    await asyncio.sleep(warmup + duration / 2)
                    sockets = sum(audit_module.count_established(w.port) for w in workers_list)
                    result = await task
                    elapsed = duration + warmup
                    generator_util = (
                        (cpuacct.children_cpu_seconds() - cpu_before) / elapsed if elapsed else 0.0
                    )
                    server_util = server_accountant.stop(elapsed)["server"]
                    sat_ok, _ = audit_module.check_generator_saturation(generator_util)
                    check = audit_module.check_littles_law(
                        result.rps, result.mean_ms, level, sockets
                    )
                    if not sat_ok:
                        check.ok = False
                        check.notes.append(
                            f"generator CPU utilization {generator_util:.0%} >= "
                            f"{audit_module.GENERATOR_SATURATION_MAXIMUM:.0%} — the client, not "
                            f"the server, may be the bottleneck at this level"
                        )
                    if result.failures:
                        check.ok = False
                        check.notes.append(f"{result.failures} failed requests")
                    results.append((level, result.rps))
                    status = "OK" if check.ok else "FAIL: " + "; ".join(check.notes)
                    typer.echo(
                        f"{level:>4} {sockets:>8} {result.rps:>10.0f} {check.in_flight:>10.2f} "
                        f"{server_util:>10.0%} {generator_util:>5.0%} {status}"
                    )
                    ok = ok and check.ok
                    level_rows.append({
                        "concurrency": level,
                        "sockets": sockets,
                        "rps": result.rps,
                        "mean_ms": result.mean_ms,
                        "in_flight": check.in_flight,
                        "server_cpu_utilization": server_util,
                        "generator_cpu_utilization": generator_util,
                        "ok": check.ok,
                        "notes": check.notes,
                    })

                knee = audit_module.find_scaling_knee(results)
                typer.echo(
                    f"\nscaling knee: {knee if knee is not None else 'not reached within tested levels'}"
                )

                noop = await _locust_across(
                    workers_list,
                    load_registry.noop_file(),
                    parsed_levels[-1],
                    duration,
                    expect_bytes=0,
                    monitor=monitor,
                    limit=limit,
                    warmup=warmup,
                )
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
                    at_level = await _locust_across(
                        workers_list,
                        load_case.file,
                        cross_check_level,
                        duration,
                        expect_bytes=0,
                        monitor=monitor,
                        limit=limit,
                        warmup=warmup,
                    )
                    typer.echo(
                        f"case rps at c={cross_check_level}: {at_level.rps:.0f} "
                        f"({'/noop is ' + f'{noop.rps / at_level.rps:.2f}x' if at_level.rps else 'n/a'})"
                    )
            finally:
                await monitor.stop()
                for worker in workers_list:
                    await worker.stop()
                await teardown()

            monitor.print_averages()

            if name:
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
                            "cross_check_level": cross_check_level,
                        },
                        "levels": level_rows,
                        "scaling_knee": knee,
                        "noop_headroom_ratio": headroom_ratio,
                        "ok": ok,
                        "monitor": monitor.to_dict(),
                        "env": env_start,
                    },
                )
                typer.echo(f"\nwrote {path_out}")
            return ok

        passed = asyncio.run(go(_case))
        if not passed:
            raise typer.Exit(1)


@app.command()
def cases() -> None:
    """List every loadtest case discovered under `benchmarks/loadtests/` —
    the slugs `--case` accepts. Deliberately a separate listing from `bench
    contenders list`: the two registries (`harness/registry.py`'s decorated
    factories, `load/registry.py`'s scanned locustfiles) can drift, and this
    is how that drift would actually be seen."""
    found = load_registry.discover()
    if not found:
        typer.echo("no loadtest cases found under benchmarks/loadtests/")
        raise typer.Exit(1)
    width = max(len(slug) for slug in found)
    for slug, case in sorted(found.items()):
        import os

        typer.echo(
            f"{slug:<{width}}  {os.path.relpath(case.file, Path(__file__).resolve().parent.parent)}"
        )
