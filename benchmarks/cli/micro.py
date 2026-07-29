"""`bench micro run|decompose` (PLAN.md §9, phase-3 gate).

Pure in-process micro benchmarks: one contender per shape/backend, gated by
output equivalence before any timing starts.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

import benchmarks.contenders as contenders_pkg  # noqa: F401 -- import for @contender registration side-effects
from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.contenders import flat as flat_contenders
from benchmarks.contenders import join as join_contenders
from benchmarks.harness import env as env_module
from benchmarks.harness import equivalence, registry, result
from benchmarks.harness import seed as seed_module
from benchmarks.harness.stats import median, spread_pct
from benchmarks.harness.timing import best_of, gc_control, per_iteration

app = typer.Typer(help="Pure in-process micro benchmarks.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

_STAGE_MODULES = {"flat": flat_contenders, "join": join_contenders}


async def _mock_handle(shape: str, limit: int) -> list[tuple]:
    """Real rows sourced from a throwaway sqlite db, once, at setup — the
    driver term is paid only here, never inside a MockEngine contender's
    timed `request()` (PLAN.md D8/D9)."""
    import aiosqlite

    db = EphemeralSqlite.create(shape, max(limit * 2, 200))
    try:
        conn = await aiosqlite.connect(db.path)
        try:
            if shape == "flat":
                cur = await conn.execute(
                    "SELECT id, name, email, is_active FROM users "
                    "WHERE is_active = 1 AND id > 100 LIMIT ?",
                    (limit,),
                )
                rows = await cur.fetchall()
                return [(r[0], r[1], r[2], bool(r[3])) for r in rows]
            cur = await conn.execute(
                "SELECT a.id, a.name, a.email, a.is_active, "
                "p.id, p.author_id, p.title, p.score, p.published "
                "FROM j_authors a JOIN j_posts p ON p.author_id = a.id "
                "WHERE a.is_active = 1 AND p.score > 100 LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            return [
                (r[0], r[1], r[2], bool(r[3]), r[4], r[5], r[6], r[7], bool(r[8]))
                for r in rows
            ]
        finally:
            await conn.close()
    finally:
        db.close()


async def _run(
    shape: str, rows: int, limit: int, iterations: int, warmup: int,
    only: str | None, gc_modes: list[str], record: bool,
) -> result.Run | None:
    specs = registry.select(shape=shape, only=only)
    if not specs:
        raise typer.BadParameter(f"no contenders match shape={shape!r} only={only!r}")

    by_backend: dict[str, list[registry.ContenderSpec]] = {}
    for spec in specs:
        by_backend.setdefault(spec.backend, []).append(spec)

    started_at = datetime.now(UTC).isoformat()
    recorded_backend: str | None = None
    recorded_eq: equivalence.EquivalenceResult | None = None
    cells: list[result.Cell] = []

    for backend, backend_specs in by_backend.items():
        db = None
        if backend == "sqlite":
            db = EphemeralSqlite.create(shape, rows)
            handle = db.path
        elif backend == "mock":
            handle = await _mock_handle(shape, limit)
        else:
            typer.echo(f"skipping backend={backend!r}: bench micro has no runner for it yet")
            continue

        try:
            instances = {}
            for spec in backend_specs:
                request, teardown = await spec.factory(handle, limit)
                instances[spec.name] = (request, teardown)

            eq = await equivalence.check({name: req for name, (req, _) in instances.items()})
            typer.echo(
                f"\n[{shape}/{backend}] equivalence: "
                f"{'PASS' if eq.passed else 'FAIL'} "
                f"({eq.payload_bytes} bytes, sha256={eq.payload_sha256})"
            )
            for failure in eq.failures:
                typer.echo(f"  ! {failure}")

            if eq.passed:
                # Recorded run.json covers the first backend group with
                # passing equivalence (typically "sqlite") — mock/other
                # groups still print above but aren't persisted, keeping the
                # recorded schema to one equivalence block (PLAN.md §6).
                record_this_group = record and recorded_backend is None
                if record_this_group:
                    recorded_backend = backend
                    recorded_eq = eq

                for mode in gc_modes:
                    typer.echo(f"  -- gc={mode} --")
                    for name, (request, _) in instances.items():
                        with gc_control(mode):
                            samples = [s * 1000 for s in await per_iteration(request, iterations, warmup)]
                        stdev = statistics.pstdev(samples)
                        typer.echo(
                            f"    {name:<38} median {median(samples):>9.4f} ms  "
                            f"stdev {stdev:>8.4f}  spread {spread_pct(samples):>6.1f}%"
                        )
                        if record_this_group:
                            spec = next(s for s in backend_specs if s.name == name)
                            cells.append(result.Cell(
                                contender=name, shipped=spec.shipped,
                                params={"backend": backend, "gc": mode, "limit": limit},
                                trials=[result.Trial(trial=0, metrics={
                                    "median_ms": median(samples), "stdev_ms": stdev,
                                    "spread_pct": spread_pct(samples), "iterations": iterations,
                                })],
                            ))

            for _, teardown in instances.values():
                await teardown()
        finally:
            if db is not None:
                db.close()

    if not record:
        return None

    env_start = env_module.capture()
    run_obj = result.Run(
        run_id=result.make_run_id(f"micro-{shape}", env_start["git"]["sha"],
                                   datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")),
        suite=f"micro-{shape}", started_at=started_at, finished_at=datetime.now(UTC).isoformat(),
        invocation={"argv": sys.argv}, git=env_start["git"], env=env_start,
        plan={"isolation": "combined", "bottleneck": "cpu"},
        config={"shape": shape, "rows": rows, "limit": limit, "iterations": iterations,
                "warmup": warmup, "gc": gc_modes, "only": only},
        equivalence={
            "enforced": recorded_eq.enforced if recorded_eq else False,
            "reference": recorded_eq.reference if recorded_eq else None,
            "payload_sha256": recorded_eq.payload_sha256 if recorded_eq else None,
            "payload_bytes": recorded_eq.payload_bytes if recorded_eq else 0,
            "self_consistent": recorded_eq.self_consistent if recorded_eq else False,
            "backend": recorded_backend,
        },
        cells=cells, warnings=env_module.warnings_for(env_start),
    )
    path = result.write(run_obj, RESULTS_DIR)
    typer.echo(f"\nrecorded: {path}  (quotable={run_obj.quotable})")
    return run_obj


@app.command()
def run(
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
    rows: int = typer.Option(200_000, help="rows seeded into the ephemeral database"),
    limit: int = typer.Option(1000, help="rows per request"),
    iterations: int = typer.Option(300, help="timed iterations per contender"),
    warmup: int = typer.Option(30, help="untimed iterations before measurement"),
    only: str | None = typer.Option(None, help=registry.ONLY_HELP),
    gc: str = typer.Option("on", help="'on', 'off', or 'both' (PLAN.md §4: GC is a first-order effect)"),
    record: bool = typer.Option(
        False, "--record", help="write a run.json under results/runs/ (PLAN.md §6)"
    ),
) -> None:
    """Run every registered contender for `--shape`, gated by output
    equivalence (per backend group), and print per-iteration medians."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")
    gc_modes = ["on", "off"] if gc == "both" else [gc]
    if any(mode not in ("on", "off") for mode in gc_modes):
        raise typer.BadParameter("--gc must be 'on', 'off', or 'both'")
    asyncio.run(_run(shape, rows, limit, iterations, warmup, only, gc_modes, record))


@app.command()
def decompose(
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
    rows: int = typer.Option(200_000, help="rows seeded into the ephemeral database"),
    limit: int = typer.Option(1000, help="rows per request"),
    number: int = typer.Option(50, help="calls per repeat, for best_of()"),
    repeat: int = typer.Option(5, help="repeats; the minimum per-call time wins"),
) -> None:
    """Decompose the rowform sqlite contender into "fetch" (driver round trip
    + hydration) and "serialize" (orjson), print each alongside the
    separately measured whole request, and report the residual (PLAN.md §4:
    "never divide a bottom-up estimate into a top-down measurement" — this
    prints both and lets the reader see the gap instead of hiding it)."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")

    async def go():
        db = EphemeralSqlite.create(shape, rows)
        try:
            stages, teardown = await _STAGE_MODULES[shape].rowform_stages(db.path, limit)
            try:
                fetch_t = await best_of(stages["fetch"], number, repeat)
                serialize_t = await best_of(stages["serialize"], number, repeat)
                whole_t = await best_of(stages["whole"], number, repeat)
                sum_parts = fetch_t + serialize_t
                residual = whole_t - sum_parts
                typer.echo(f"fetch      {fetch_t * 1000:9.4f} ms")
                typer.echo(f"serialize  {serialize_t * 1000:9.4f} ms")
                typer.echo(f"sum        {sum_parts * 1000:9.4f} ms")
                typer.echo(f"whole      {whole_t * 1000:9.4f} ms")
                pct = (residual / whole_t * 100) if whole_t else 0.0
                typer.echo(f"residual   {residual * 1000:9.4f} ms  ({pct:.1f}% of whole)")
            finally:
                await teardown()
        finally:
            db.close()

    asyncio.run(go())
