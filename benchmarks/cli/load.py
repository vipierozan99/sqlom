"""`bench load run|audit` (PLAN.md §9, phase-4 gate).

Both commands provision their own ephemeral sqlite backend + `--workers`
FastAPI worker(s), run the load generator(s) against it, and tear everything
down — self-contained the same way the old `bench_locust.sh` was, minus the
shell script.

`--workers` (how many uvicorn processes are spawned) and `--concurrency`/
`--levels` (how many requests the load generator keeps in flight) are
deliberately separate knobs, not one conflated with the other: each worker is
its own process on its own port (PLAN.md §5 — no shared-port `--workers`
forking, so pinning stays sound), so testing N workers under load means
splitting the requested concurrency across N ports and summing the result,
which is what `_split_across_workers`/`_aggregate_httpload`/`_aggregate_locust`
below do.

Every process this module spawns (each uvicorn worker, each `locust`
subprocess) is tracked in a `ProcessMonitor`, which samples every tracked
pid's CPU utilization once a second, prints it live, and — if `--name` is
given — writes the full time series plus the run's config/results to
`results/runs/loadtests/<name>.json`. uvicorn's own stdout/stderr is
discarded (`launch(..., quiet=True)`) so it doesn't interleave with those
per-second lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import typer

import benchmarks.contenders  # noqa: F401 -- registration side-effects
from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.harness import registry
from benchmarks.harness.monitor import ProcessMonitor
from benchmarks.harness.registry import ContenderSpec
from benchmarks.harness.stats import percentile
from benchmarks.load import audit as audit_module
from benchmarks.load import httpload
from benchmarks.load import locust as locust_module
from benchmarks.load.httpload import HttpLoadResult
from benchmarks.load.locust import LocustResult
from benchmarks.service.launch import ServiceWorker, launch

app = typer.Typer(help="Load generators and the concurrency/headroom audit.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


async def _provision(
    shape: str, rows: int, limit: int, port: int, server_cores: list[int], workers: int,
) -> tuple[EphemeralSqlite, list[ServiceWorker]]:
    db = EphemeralSqlite.create(shape, rows)
    workers_list = await launch(
        "benchmarks.service.app:app",
        base_port=port,
        workers=workers,
        cores=server_cores,
        backend="sqlite",
        shape=shape,
        handle=db.path,
        limit=limit,
        quiet=True,
    )
    return db, workers_list


def _resolve_case(case: str) -> ContenderSpec:
    """Look up a contender by its slug (`{backend}-{shape}-{name}`) — the
    single selector `run`/`audit` take instead of separate `--shape`/`--only`/
    backend options. `bench load` only knows how to provision a sqlite
    backend today, so a Postgres/mock slug is rejected with a clear message
    rather than failing confusingly deeper in `_provision`."""
    try:
        spec = registry.get(case)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from None
    if spec.backend != "sqlite":
        raise typer.BadParameter(
            f"{case!r} is a {spec.backend!r}-backend contender; bench load only "
            f"provisions a sqlite backend today"
        )
    return spec


def _server_roles(workers_list: list[ServiceWorker]) -> dict[str, int]:
    """`{"server": pid}` for one worker, `{"server-0": pid, "server-1": pid,
    ...}` for several — one role name per uvicorn process, always distinct
    from the "generator" role(s) tracking the load side."""
    if len(workers_list) == 1:
        return {"server": workers_list[0].proc.pid}
    return {f"server-{i}": w.proc.pid for i, w in enumerate(workers_list)}


def _split_across_workers(total: int, n: int) -> list[int]:
    """Divide `total` (connections/users) as evenly as possible across `n`
    worker ports — e.g. 10 across 3 ports -> [4, 3, 3]. A port that would get
    0 is dropped by the caller rather than asked to run a zero-concurrency
    generator."""
    base, remainder = divmod(total, n)
    return [base + (1 if i < remainder else 0) for i in range(n)]


def _aggregate_httpload(results: list[HttpLoadResult]) -> HttpLoadResult:
    """Merge one `HttpLoadResult` per worker port into one: throughput adds
    (independent servers, independent client connections), and latency
    percentiles are recomputed from the pooled raw samples rather than
    averaged — averaging percentiles across unequal-sized samples is not the
    same number as the percentile of the pooled set."""
    if len(results) == 1:
        return results[0]
    pooled_latencies = sorted(lat for r in results for lat in r.latencies_s)
    total_completed = sum(r.completed for r in results)
    elapsed = max(r.elapsed_s for r in results)
    return HttpLoadResult(
        path=results[0].path,
        connections=sum(r.connections for r in results),
        completed=total_completed,
        elapsed_s=elapsed,
        rps=sum(r.rps for r in results),
        mean_ms=(sum(lat for lat in pooled_latencies) / len(pooled_latencies) * 1000)
        if pooled_latencies else 0.0,
        p50_ms=percentile(pooled_latencies, 50) * 1000,
        p95_ms=percentile(pooled_latencies, 95) * 1000,
        p99_ms=percentile(pooled_latencies, 99) * 1000,
        response_bytes=results[0].response_bytes,
        latencies_s=pooled_latencies,
    )


def _aggregate_locust(results: list[LocustResult]) -> LocustResult:
    """Same idea as `_aggregate_httpload`, but locust's CSV summary gives no
    raw per-request latencies to pool — mean/percentiles here are a
    completed-count-weighted average across ports, an approximation, not a
    recomputed pooled percentile. Good enough for the cross-check this feeds
    (PLAN.md's 7% agreement tolerance); not to be read as precise as the
    httpload path's pooled percentiles."""
    if len(results) == 1:
        return results[0]
    total_completed = sum(r.completed for r in results) or 1

    def weighted(attr: str) -> float:
        return sum(getattr(r, attr) * r.completed for r in results) / total_completed

    return LocustResult(
        path=results[0].path,
        users=sum(r.users for r in results),
        completed=sum(r.completed for r in results),
        failures=sum(r.failures for r in results),
        rps=sum(r.rps for r in results),
        mean_ms=weighted("mean_ms"), p50_ms=weighted("p50_ms"),
        p95_ms=weighted("p95_ms"), p99_ms=weighted("p99_ms"),
    )


async def _httpload_across(
    workers_list: list[ServiceWorker], path: str, total_connections: int, duration: float,
    warmup: float = 1.0,
) -> HttpLoadResult:
    per_worker = _split_across_workers(total_connections, len(workers_list))
    results = await asyncio.gather(*[
        httpload.run(port=w.port, path=path, connections=n, duration=duration, warmup=warmup)
        for w, n in zip(workers_list, per_worker, strict=True) if n > 0
    ])
    return _aggregate_httpload(list(results))


async def _locust_across(
    workers_list: list[ServiceWorker], path: str, total_users: int, duration: float,
    expect_bytes: int, monitor: ProcessMonitor,
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
            host=f"http://127.0.0.1:{w.port}", path=path, users=n, duration=duration,
            expect_bytes=expect_bytes, on_spawn=make_on_spawn(i),
        )
        for i, (w, n) in enumerate(zip(workers_list, per_worker, strict=True)) if n > 0
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
    case: str = typer.Option("sqlite-flat-rowform", help=registry.CASE_HELP),
    rows: int = typer.Option(50_000),
    limit: int = typer.Option(100),
    concurrency: str = typer.Option("1,8,32,128", help="load concurrency levels — see --workers for uvicorn process count"),
    duration: float = typer.Option(5.0),
    generator: str = typer.Option("httpload", help="'httpload' or 'locust'"),
    port: int = typer.Option(8000),
    workers: int = typer.Option(
        1, help="uvicorn worker processes to spawn (one port each) — independent of "
        "--concurrency, the number of requests the generator keeps in flight"
    ),
    name: str | None = typer.Option(
        None, help="if given, write per-second CPU samples + results to "
        "results/runs/loadtests/<name>.json"
    ),
) -> None:
    """Run one generator across a concurrency sweep against one endpoint."""
    if generator not in ("httpload", "locust"):
        raise typer.BadParameter("--generator must be 'httpload' or 'locust'")
    if workers < 1:
        raise typer.BadParameter("--workers must be >= 1")
    levels = sorted(int(c) for c in concurrency.split(","))

    async def go() -> None:
        spec = _resolve_case(case)
        path = f"/{spec.slug}"
        db, workers_list = await _provision(spec.shape, rows, limit, port, [], workers)
        monitor = ProcessMonitor(print_fn=typer.echo)
        for role, pid in _server_roles(workers_list).items():
            monitor.track(role, pid)
        if generator == "httpload":
            monitor.track("generator", os.getpid())
        monitor.start()
        cells = []
        try:
            typer.echo(f"Running case={case!r} ({path}) — {workers} worker(s), generator={generator}")
            for level in levels:
                if generator == "httpload":
                    result = await _httpload_across(workers_list, path, level, duration)
                    typer.echo(
                        f"c={level:<4} {result.rps:>9.0f} rps  mean {result.mean_ms:>7.2f} ms  "
                        f"p99 {result.p99_ms:>7.2f} ms"
                    )
                    cells.append({
                        "concurrency": level, "rps": result.rps, "mean_ms": result.mean_ms,
                        "p50_ms": result.p50_ms, "p95_ms": result.p95_ms, "p99_ms": result.p99_ms,
                    })
                else:
                    locust_res = await _locust_across(
                        workers_list, path, level, duration, expect_bytes=0, monitor=monitor,
                    )
                    typer.echo(
                        f"c={level:<4} {locust_res.rps:>9.0f} rps  "
                        f"mean {locust_res.mean_ms:>7.2f} ms  p99 {locust_res.p99_ms:>7.2f} ms  "
                        f"failures={locust_res.failures}"
                    )
                    cells.append({
                        "concurrency": level, "rps": locust_res.rps, "mean_ms": locust_res.mean_ms,
                        "p99_ms": locust_res.p99_ms, "failures": locust_res.failures,
                    })
        finally:
            await monitor.stop()
            for worker in workers_list:
                await worker.stop()
            db.close()

        if name:
            path_out = _save(name, {
                "command": "load run",
                "config": {
                    "case": case, "rows": rows, "limit": limit, "concurrency": levels,
                    "duration": duration, "generator": generator, "workers": workers,
                    "path": path,
                },
                "cells": cells,
                "monitor": monitor.to_dict(),
            })
            typer.echo(f"\nwrote {path_out}")

    asyncio.run(go())


@app.command()
def audit(
    case: str = typer.Option("sqlite-flat-rowform", help=registry.CASE_HELP),
    rows: int = typer.Option(50_000),
    limit: int = typer.Option(1000),
    levels: str = typer.Option("1,2,4,8,16"),
    duration: float = typer.Option(4.0),
    port: int = typer.Option(8010),
    workers: int = typer.Option(
        1, help="uvicorn worker processes to spawn (one port each) — independent of "
        "--levels, the load concurrency swept per level"
    ),
    cross_check_level: int = typer.Option(
        8, help="concurrency at which locust cross-checks httpload"
    ),
    name: str | None = typer.Option(
        None, help="if given, write per-second CPU samples + results to "
        "results/runs/loadtests/<name>.json"
    ),
) -> None:
    """The phase-4 gate, in one command: Little's Law + socket count per
    level, the scaling knee, `/noop` headroom, and one httpload<->locust
    cross-check. Exits non-zero if anything fails."""
    if workers < 1:
        raise typer.BadParameter("--workers must be >= 1")
    parsed_levels = sorted(int(c) for c in levels.split(","))

    async def go() -> bool:
        spec = _resolve_case(case)
        path = f"/{spec.slug}"
        db, workers_list = await _provision(spec.shape, rows, limit, port, [], workers)
        monitor = ProcessMonitor(print_fn=typer.echo)
        for role, pid in _server_roles(workers_list).items():
            monitor.track(role, pid)
        monitor.track("generator", os.getpid())
        monitor.start()
        ok = True
        level_rows = []
        try:
            typer.echo(f"auditing case={case!r} ({path}) — {workers} worker(s) on ports "
                       f"{[w.port for w in workers_list]}\n")
            typer.echo(f"{'c':>4} {'sockets':>8} {'rps':>10} {'in-flight':>10}  little's law")
            results = []
            for level in parsed_levels:
                # Sample /proc/net/tcp mid-run, not after: httpload closes
                # every connection when it returns, so sampling afterward
                # would always see 0 ESTABLISHED sockets regardless of
                # whether the generator kept `level` requests in flight.
                warmup = 0.5
                cpu_start, wall_start = time.process_time(), time.perf_counter()
                task = asyncio.ensure_future(
                    _httpload_across(workers_list, path, level, duration, warmup)
                )
                await asyncio.sleep(warmup + duration / 2)
                sockets = sum(audit_module.count_established(w.port) for w in workers_list)
                result = await task
                generator_util = (time.process_time() - cpu_start) / (
                    time.perf_counter() - wall_start
                )
                sat_ok, _ = audit_module.check_generator_saturation(generator_util)
                check = audit_module.check_littles_law(result, sockets)
                if not sat_ok:
                    check.ok = False
                    check.notes.append(
                        f"generator CPU utilization {generator_util:.0%} >= "
                        f"{audit_module.GENERATOR_SATURATION_MAXIMUM:.0%} — the client, not "
                        f"the server, may be the bottleneck at this level"
                    )
                results.append(result)
                status = "OK" if check.ok else "FAIL: " + "; ".join(check.notes)
                typer.echo(
                    f"{level:>4} {sockets:>8} {result.rps:>10.0f} {check.in_flight:>10.2f} "
                    f"gen_cpu {generator_util:>5.0%}  {status}"
                )
                ok = ok and check.ok
                level_rows.append({
                    "concurrency": level, "sockets": sockets, "rps": result.rps,
                    "in_flight": check.in_flight, "generator_cpu_utilization": generator_util,
                    "ok": check.ok, "notes": check.notes,
                })

            knee = audit_module.find_scaling_knee(results)
            typer.echo(
                f"\nscaling knee: {knee if knee is not None else 'not reached within tested levels'}"
            )

            noop = await _httpload_across(workers_list, "/noop", parsed_levels[-1], duration)
            fastest_db_rps = max(r.rps for r in results)
            headroom_ok, ratio = audit_module.check_noop_headroom(noop.rps, fastest_db_rps)
            typer.echo(
                f"/noop headroom: {ratio:.2f}x fastest db endpoint "
                f"({'OK' if headroom_ok else f'FAIL: below {audit_module.NOOP_HEADROOM_MINIMUM}x'})"
            )
            ok = ok and headroom_ok

            httpload_at_level = next(
                (r for r in results if r.connections == cross_check_level), results[-1]
            )
            locust_result = await _locust_across(
                workers_list, path, httpload_at_level.connections, duration,
                expect_bytes=httpload_at_level.response_bytes, monitor=monitor,
            )
            agree_ok, delta = audit_module.check_generator_agreement(
                httpload_at_level.rps, locust_result.rps
            )
            typer.echo(
                f"httpload {httpload_at_level.rps:.0f} rps vs locust {locust_result.rps:.0f} rps "
                f"at c={httpload_at_level.connections}: delta {delta:.1%} "
                f"({'OK' if agree_ok else 'FAIL: exceeds 7%'})"
            )
            ok = ok and agree_ok
        finally:
            await monitor.stop()
            for worker in workers_list:
                await worker.stop()
            db.close()

        if name:
            path_out = _save(name, {
                "command": "load audit",
                "config": {
                    "case": case, "rows": rows, "limit": limit, "levels": parsed_levels,
                    "duration": duration, "workers": workers, "path": path,
                    "cross_check_level": cross_check_level,
                },
                "levels": level_rows,
                "scaling_knee": knee,
                "noop_headroom_ratio": ratio,
                "generator_agreement_delta": delta,
                "ok": ok,
                "monitor": monitor.to_dict(),
            })
            typer.echo(f"\nwrote {path_out}")
        return ok

    passed = asyncio.run(go())
    if not passed:
        raise typer.Exit(1)
