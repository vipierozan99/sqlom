"""`bench profile micro|load`.

`micro`: in-process profiling of one contender — cProfile (instrumented) and
pyinstrument (sampling) run by default over the same call count, so their
per-request CPU and the resulting instrumentation-inflation factor are always
printed side by side — an instrumented profile is always cross-checked against a
sampling one, since they have opposite blind spots. Also runs the impossible-row
tripwire
(`attribution.check_impossible_rows`) whenever the profiled contender is
tagged `floor` (never touches rowform).

`load`: external profiling — py-spy and austin both attach to a live worker
process while it's under load and render a flamegraph (speedscope JSON) each.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import typer

import benchmarks.micro.contenders  # noqa: F401 -- registration side-effects
from benchmarks.backends import postgres as postgres_backend
from benchmarks.backends.provision import provision as provision_backend
from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.engines import mock as mock_engines
from benchmarks.harness import affinity, registry
from benchmarks.harness import seed as seed_module
from benchmarks.harness.registry import ContenderInit
from benchmarks.harness.timing import assert_unpatched_threading, gc_control
from benchmarks.load import registry as load_registry
from benchmarks.profiling import attribution, render
from benchmarks.profiling.austin import AustinProfiler
from benchmarks.profiling.cprofile import CProfileProfiler
from benchmarks.profiling.pyinstrument import PyinstrumentProfiler
from benchmarks.profiling.pyspy import PySpyProfiler
from benchmarks.service.launch import launch

app = typer.Typer(help="Profiler adapters: in-process (micro) and external (load).")


@app.command()
def micro(
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
    rows: int = typer.Option(20_000),
    limit: int = typer.Option(500),
    only: str | None = typer.Option(None, help=registry.ONLY_HELP),
    backend: str = typer.Option(
        "sqlite",
        "--backend",
        help="'sqlite', 'postgres' (needs --pg-dsn), or 'mock' (zero driver cost — "
        "the row layer alone under the profiler)",
    ),
    pg_dsn: str | None = typer.Option(
        None,
        "--pg-dsn",
        help="server for --backend postgres — its shape tables are DROPPED and "
        "reseeded first; start a throwaway one with `bench db up`",
    ),
    iterations: int = typer.Option(200),
    pin: str | None = typer.Option(
        "auto",
        "--pin",
        help="comma-separated logical CPUs, 'auto' (two whole physical cores), or '' "
        "to disable — the baseline is a timed CPU measurement, pin it like one",
    ),
    out_dir: str = typer.Option(
        "benchmarks/results/runs/profiles", help="where to write speedscope JSON"
    ),
) -> None:
    """Profile one contender on any backend: unprofiled baseline, cProfile, and
    pyinstrument, over the same `iterations` calls, GC off (matching what
    `bench micro` measures).

    The baseline reports main-thread and whole-process CPU separately: cProfile
    and pyinstrument only see the main thread, so on sqlite (where aiosqlite
    runs every statement on a worker thread) the driver's CPU is invisible to
    both profilers — printing it as its own number is what stops the rollup
    shares being silently read as shares of the whole request.
    """
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")
    if backend == "postgres" and not pg_dsn:
        raise typer.BadParameter("--backend postgres needs --pg-dsn (start one: `bench db up`)")
    # The baseline below is a timed measurement; it used to run inside the
    # gevent monkey-patch (this module imported locust at module scope), where
    # ms/req is ~30% slow, looks identical to `bench micro` output, and
    # cProfile sees a threading model the real configuration doesn't have.
    assert_unpatched_threading()
    pin_cpus, pin_warnings = affinity.resolve_pin(pin)
    for warning in pin_warnings:
        typer.echo(f"  ! {warning}")

    async def go() -> bool:
        specs = registry.select(backend=backend, shape=shape, only=only)
        if not specs:
            raise typer.BadParameter(
                f"no {backend} contenders match shape={shape!r} only={only!r}"
            )
        spec = specs[0]
        if len(specs) > 1:
            typer.echo(
                f"{len(specs)} contenders match --only {only!r}; profiling {spec.name!r} "
                f"(narrow the match to pick another)"
            )
        db = None
        if backend == "sqlite":
            db = EphemeralSqlite.create(shape, rows)
            handle: object = db.path
        elif backend == "mock":
            handle = await mock_engines.canned_rows(shape, limit)
        else:
            server = postgres_backend.attach(pg_dsn or "")
            seeded = await server.seed(shape, rows)
            typer.echo(f"seeded {seeded} rows into {shape} on postgres")
            handle = pg_dsn
        ok = True
        try:
            request, teardown = await spec.factory(ContenderInit(handle=handle, limit=limit))
            try:
                for _ in range(10):
                    await request()  # warm up: hydrator/JIT-ish caches, pool

                with gc_control("off"):
                    process0, thread0 = time.process_time(), time.thread_time()
                    for _ in range(iterations):
                        await request()
                    process_cpu = time.process_time() - process0
                    thread_cpu = time.thread_time() - thread0
                # The inflation cross-checks below compare against the
                # main-thread figure, because that is the only CPU the
                # profilers can see.
                baseline_ms_per_req = thread_cpu / iterations * 1000
                process_ms_per_req = process_cpu / iterations * 1000
                other_threads_ms = process_ms_per_req - baseline_ms_per_req
                typer.echo(
                    f"contender: {spec.name!r}  unprofiled: {process_ms_per_req:.4f} ms/req "
                    f"process CPU = {baseline_ms_per_req:.4f} main thread "
                    f"+ {max(other_threads_ms, 0.0):.4f} other threads"
                )
                if other_threads_ms > baseline_ms_per_req * 0.05:
                    typer.echo(
                        f"  ! {max(other_threads_ms, 0.0):.4f} ms/req runs on driver/worker "
                        f"threads, INVISIBLE to cProfile and pyinstrument below — their "
                        f"shares describe the main thread only"
                    )

                cprofiler = CProfileProfiler()
                with gc_control("off"):
                    cprofiler.start()
                    for _ in range(iterations):
                        await request()
                    stats = cprofiler.stop()
                # `pstats.Stats.stats` is a real public-by-convention attribute
                # (attribution.py and `bench profile` both rely on it) that
                # typeshed doesn't declare.
                cprofile_total = sum(v[2] for v in stats.stats.values())  # type: ignore[reportAttributeAccessIssue]
                inflation = (
                    (cprofile_total / iterations * 1000) / baseline_ms_per_req
                    if baseline_ms_per_req
                    else 0.0
                )
                typer.echo(
                    f"cprofile (instrumented): {cprofile_total / iterations * 1000:.4f} ms/req "
                    f"({inflation:.1f}x baseline)"
                )
                attribution.print_rollup(stats, cprofile_total, iterations, baseline_ms_per_req)

                pyi = PyinstrumentProfiler()
                with gc_control("off"):
                    pyi.start()
                    pyi_cpu0 = time.thread_time()
                    for _ in range(iterations):
                        await request()
                    pyi_cpu = time.thread_time() - pyi_cpu0
                    session_profiler = pyi.stop()
                pyi_ms_per_req = pyi_cpu / iterations * 1000
                pyi_inflation = pyi_ms_per_req / baseline_ms_per_req if baseline_ms_per_req else 0.0
                typer.echo(
                    f"pyinstrument (sampling): {pyi_ms_per_req:.4f} ms/req ({pyi_inflation:.1f}x baseline)"
                )
                if inflation and pyi_inflation and inflation / pyi_inflation > 3:
                    typer.echo(
                        "  ! material divergence between instrumented and sampling inflation "
                        "— treat this profile's shares with caution"
                    )

                # The impossible-row tripwire: a "floor" contender never
                # imports rowform, so its rollup must show exactly 0.0% in
                # the rowform categories.
                if "floor" in spec.tags:
                    rolled = attribution.rollup(stats)
                    warnings = attribution.check_impossible_rows(
                        rolled, attribution.IMPOSSIBLE_FOR_NON_ROWFORM
                    )
                    if warnings:
                        for warning in warnings:
                            typer.echo(f"  ! {warning}")
                        ok = False
                    else:
                        typer.echo(
                            f"  impossible-row check: rowform categories read 0.0% for "
                            f"floor contender {spec.name!r} (OK)"
                        )

                out = Path(out_dir)
                out.mkdir(parents=True, exist_ok=True)
                folded = render.pstats_to_folded(stats)
                # Named per contender: fixed names silently overwrote contender
                # A's flamegraphs the moment contender B was profiled.
                (out / f"{spec.slug}.cprofile.speedscope.json").write_text(
                    __import__("json").dumps(render.folded_to_speedscope(folded, spec.name))
                )
                (out / f"{spec.slug}.pyinstrument.speedscope.json").write_text(
                    pyi.to_speedscope(session_profiler)
                )
                typer.echo(
                    f"wrote {out}/{spec.slug}.cprofile.speedscope.json and "
                    f".pyinstrument.speedscope.json"
                )
            finally:
                await teardown()
        finally:
            if db is not None:
                db.close()
        return ok

    with affinity.pin_current_process(pin_cpus) as pin_actual:
        if pin_cpus:
            typer.echo(f"pinned to cpus {pin_actual}")
        if not asyncio.run(go()):
            raise typer.Exit(1)


@app.command()
def load(
    case: str = typer.Option("sqlite-flat-rowform", help=registry.CASE_HELP),
    rows: int = typer.Option(20_000),
    limit: int = typer.Option(500),
    concurrency: int = typer.Option(8, help="locust users generating background traffic"),
    duration: float = typer.Option(6.0),
    port: int = typer.Option(8020),
    pg_port: int = typer.Option(
        5432,
        help="host port for the ephemeral postgres container (postgres cases only) — "
        "pass a free one if a `bench db up` server is standing on 5432",
    ),
    out_dir: str = typer.Option(
        "benchmarks/results/runs/profiles", help="where to write speedscope JSON"
    ),
) -> None:
    """Attach py-spy and austin to a live worker under locust traffic,
    concurrently, and each render a flamegraph (speedscope JSON)."""
    # Imported here, not at module scope: importing locust monkey-patches the
    # whole process with gevent, and `bench profile micro` times a baseline in
    # this interpreter. Only this subcommand may pay that price.
    from benchmarks.load.locust import run as locust_run
    from benchmarks.load.locust import warm as locust_warm

    async def go() -> bool:
        spec = registry.get(case)
        if spec.backend not in ("sqlite", "postgres"):
            raise typer.BadParameter(
                f"{case!r} is a {spec.backend!r}-backend contender; bench profile load only "
                f"provisions sqlite/postgres backends"
            )
        load_case = load_registry.get(case)
        env, teardown = await provision_backend(spec.backend, spec.shape, rows, pg_port=pg_port)
        try:
            workers = await launch(
                "benchmarks.service.app:app",
                base_port=port,
                workers=1,
                cores=[],
                env=env,
            )
        except BaseException:
            # The try/finally below hasn't begun yet — a failed launch must tear
            # the backend down here or the postgres container outlives the run.
            await teardown()
            raise
        worker = workers[0]
        ok = True
        try:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pyspy_out = str(out / f"{case}.pyspy.speedscope.json")
            austin_out = str(out / f"{case}.austin.speedscope.json")

            await locust_warm(
                host=f"http://127.0.0.1:{worker.port}",
                locustfile=load_registry.locustfile(),
                route=load_case.route,
                users=concurrency,
                duration=1.0,
                limit=limit,
            )
            load_task = asyncio.ensure_future(
                locust_run(
                    host=f"http://127.0.0.1:{worker.port}",
                    locustfile=load_registry.locustfile(),
                    route=load_case.route,
                    users=concurrency,
                    duration=duration + 2,
                    limit=limit,
                )
            )
            await asyncio.sleep(2.0)  # let locust ramp up before attaching

            pyspy_task = PySpyProfiler().attach(worker.proc.pid, duration, pyspy_out)
            austin_task = AustinProfiler().attach(worker.proc.pid, duration, austin_out)
            (pyspy_result, austin_result) = await asyncio.gather(
                pyspy_task, austin_task, return_exceptions=True
            )
            await load_task

            for name, result, path_str in (
                ("py-spy", pyspy_result, pyspy_out),
                ("austin", austin_result, austin_out),
            ):
                if isinstance(result, BaseException):
                    typer.echo(f"  ! {name} failed: {result}")
                    ok = False
                    continue
                import json

                data = json.loads(Path(path_str).read_text())
                n_frames = len(data.get("shared", {}).get("frames", []))
                n_samples = sum(len(p.get("samples", [])) for p in data.get("profiles", []))
                if n_samples == 0:
                    typer.echo(f"  ! {name}: flamegraph has 0 samples")
                    ok = False
                else:
                    typer.echo(
                        f"  {name}: flamegraph rendered — {n_frames} frames, {n_samples} samples "
                        f"-> {path_str}"
                    )
        finally:
            for worker in workers:
                await worker.stop()
            await teardown()
        return ok

    if not asyncio.run(go()):
        raise typer.Exit(1)
