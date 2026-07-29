"""`bench profile micro|load` (PLAN.md §9, phase-5 gate).

`micro`: in-process profiling of one contender — cProfile (instrumented) and
pyinstrument (sampling) run by default over the same call count, so their
per-request CPU and the resulting instrumentation-inflation factor are always
printed side by side (PLAN.md §4: "cross-check instrumented against
sampling... runs by default"). Also runs the impossible-row tripwire
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
from benchmarks.backends.provision import provision as provision_backend
from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.harness import registry
from benchmarks.harness import seed as seed_module
from benchmarks.harness.registry import ContenderInit
from benchmarks.load import registry as load_registry
from benchmarks.load.locust import run as locust_run
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
    iterations: int = typer.Option(200),
    out_dir: str = typer.Option(
        "benchmarks/results/runs/profiles", help="where to write speedscope JSON"
    ),
) -> None:
    """Profile one sqlite contender: unprofiled baseline, cProfile, and
    pyinstrument, over the same `iterations` calls."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")

    async def go() -> bool:
        specs = registry.select(backend="sqlite", shape=shape, only=only)
        if not specs:
            raise typer.BadParameter(f"no sqlite contenders match shape={shape!r} only={only!r}")
        spec = specs[0]
        db = EphemeralSqlite.create(shape, rows)
        ok = True
        try:
            request, teardown = await spec.factory(ContenderInit(handle=db.path, limit=limit))
            try:
                for _ in range(10):
                    await request()  # warm up: hydrator/JIT-ish caches, pool

                cpu0 = time.process_time()
                for _ in range(iterations):
                    await request()
                baseline_cpu = time.process_time() - cpu0
                baseline_ms_per_req = baseline_cpu / iterations * 1000
                typer.echo(
                    f"contender: {spec.name!r}  unprofiled: {baseline_ms_per_req:.4f} ms/req"
                )

                cprofiler = CProfileProfiler()
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
                pyi.start()
                pyi_cpu0 = time.process_time()
                for _ in range(iterations):
                    await request()
                pyi_cpu = time.process_time() - pyi_cpu0
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
                (out / "cprofile.speedscope.json").write_text(
                    __import__("json").dumps(render.folded_to_speedscope(folded, spec.name))
                )
                (out / "pyinstrument.speedscope.json").write_text(
                    pyi.to_speedscope(session_profiler)
                )
                typer.echo(f"wrote speedscope JSON to {out}/")
            finally:
                await teardown()
        finally:
            db.close()
        return ok

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
    out_dir: str = typer.Option(
        "benchmarks/results/runs/profiles", help="where to write speedscope JSON"
    ),
) -> None:
    """Attach py-spy and austin to a live worker under locust traffic,
    concurrently, and each render a flamegraph (speedscope JSON)."""

    async def go() -> bool:
        spec = registry.get(case)
        if spec.backend not in ("sqlite", "postgres"):
            raise typer.BadParameter(
                f"{case!r} is a {spec.backend!r}-backend contender; bench profile load only "
                f"provisions sqlite/postgres backends"
            )
        load_case = load_registry.get(case)
        env, teardown = await provision_backend(spec.backend, spec.shape, rows)
        workers = await launch(
            "benchmarks.service.app:app",
            base_port=port,
            workers=1,
            cores=[],
            env=env,
        )
        worker = workers[0]
        ok = True
        try:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            pyspy_out = str(out / "pyspy.speedscope.json")
            austin_out = str(out / "austin.speedscope.json")

            load_task = asyncio.ensure_future(
                locust_run(
                    host=f"http://127.0.0.1:{worker.port}",
                    locustfile=load_case.file,
                    users=concurrency,
                    duration=duration + 2,
                    limit=limit,
                    warmup=1.0,
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
