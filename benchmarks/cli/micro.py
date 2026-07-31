"""`bench micro run`.

Pure in-process micro benchmarks: one contender per shape/backend, gated by
output equivalence before any timing starts.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer

import benchmarks.micro.contenders  # noqa: F401 -- import for @contender registration side-effects
from benchmarks.backends import postgres as postgres_backend
from benchmarks.backends.sqlite import EphemeralSqlite
from benchmarks.harness import affinity, equivalence, registry, result
from benchmarks.harness import env as env_module
from benchmarks.harness import seed as seed_module
from benchmarks.harness.registry import ContenderInit
from benchmarks.harness.stats import sample_shape
from benchmarks.harness.timing import gc_control, per_iteration

app = typer.Typer(help="Pure in-process micro benchmarks.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

# One spec drives both the header and the value rows so they cannot drift apart.
# `outliers m/s` is Tukey mild/severe (see stats.SampleShape); `max/p50` is an
# interference detector, not a dispersion figure.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("contender", "<38"),
    ("median ms", ">10"),
    ("IQR%", ">8"),
    ("p95/p50", ">9"),
    ("outliers m/s", ">14"),
    ("max/p50", ">9"),
)


def _row(*cells: str) -> str:
    return "    " + "".join(
        f"{cell:{spec}}" for cell, (_, spec) in zip(cells, _COLUMNS, strict=True)
    )


_HEADER = _row(*(label for label, _ in _COLUMNS))


@app.command()
def run(
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
    rows: int = typer.Option(200_000, help="rows seeded into the ephemeral database"),
    limit: int = typer.Option(1000, help="rows per request"),
    iterations: int = typer.Option(1000, help="timed iterations per contender"),
    warmup: int = typer.Option(100, help="untimed iterations before measurement"),
    only: str | None = typer.Option(None, help=registry.ONLY_HELP),
    gc: str = typer.Option(
        "off", help="'on', 'off', or 'both'"
    ),
    pin: str | None = typer.Option(
        "6,7,8,9", "--pin", help="comma-separated logical CPUs to pin this process to"
    ),
    record: bool = typer.Option(
        False, "--record", help="write a run.json under results/runs/"
    ),
    pg_dsn: str | None = typer.Option(
        None,
        "--pg-dsn",
        help="run the postgres contenders against this server (seeded first); "
        "start one with `bench db up`",
    ),
) -> None:
    """Run every registered contender for `--shape`, gated by output
    equivalence (per backend group), and print per-iteration medians."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")
    gc_modes = ["on", "off"] if gc == "both" else [gc]
    if any(mode not in ("on", "off") for mode in gc_modes):
        raise typer.BadParameter("--gc must be 'on', 'off', or 'both'")
    pin_cpus = [int(c) for c in pin.split(",")] if pin else []
    asyncio.run(
        _run(shape, rows, limit, iterations, warmup, only, gc_modes, pin_cpus, record, pg_dsn)
    )


async def _mock_handle(shape: str, limit: int) -> list[tuple]:
    """Real rows sourced from a throwaway sqlite db, once, at setup — the
    driver term is paid only here, never inside a MockEngine contender's
    timed `request()`."""
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
                (r[0], r[1], r[2], bool(r[3]), r[4], r[5], r[6], r[7], bool(r[8])) for r in rows
            ]
        finally:
            await conn.close()
    finally:
        db.close()


async def _run(
    shape: str,
    rows: int,
    limit: int,
    iterations: int,
    warmup: int,
    only: str | None,
    gc_modes: list[str],
    pin_cpus: list[int],
    record: bool,
    pg_dsn: str | None = None,
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

    with affinity.pin_current_process(pin_cpus) as pin_actual:
        if pin_cpus:
            typer.echo(f"pinned to cpus {pin_actual}")

        for backend, backend_specs in by_backend.items():
            db = None
            if backend == "sqlite":
                db = EphemeralSqlite.create(shape, rows)
                handle = db.path
            elif backend == "mock":
                handle = await _mock_handle(shape, limit)
            elif backend == "postgres":
                if not pg_dsn:
                    typer.echo(
                        f"skipping backend={backend!r}: pass --pg-dsn to run it "
                        f"(start a server with `bench db up`)"
                    )
                    continue
                # Seeded here rather than assumed: the postgres contenders read
                # the same deterministic rows the sqlite ones do, and a server
                # left over from another shape would otherwise be measured
                # against whatever it happened to contain.
                server = postgres_backend.attach(pg_dsn)
                seeded = await server.seed(shape, rows)
                typer.echo(f"seeded {seeded} rows into {shape} on postgres")
                handle = pg_dsn
            else:
                typer.echo(f"skipping backend={backend!r}: bench micro has no runner for it yet")
                continue

            init = ContenderInit(handle=handle, limit=limit)
            try:
                instances = {}
                for spec in backend_specs:
                    target, teardown = await spec.factory(init)
                    instances[spec.name] = (target, teardown)

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
                    # recorded schema to one equivalence block.
                    record_this_group = record and recorded_backend is None
                    if record_this_group:
                        recorded_backend = backend
                        recorded_eq = eq

                    for mode in gc_modes:
                        typer.echo(f"  -- gc={mode} --")
                        typer.echo(_HEADER)
                        for name, (request, _) in instances.items():
                            with gc_control(mode):
                                samples = [
                                    s * 1000
                                    for s in await per_iteration(request, iterations, warmup)
                                ]
                            shape_stats = sample_shape(samples)
                            typer.echo(
                                _row(
                                    name,
                                    f"{shape_stats.median_ms:.4f}",
                                    f"{shape_stats.iqr_pct:.1f}",
                                    f"{shape_stats.p95_over_p50:.2f}",
                                    f"{shape_stats.outliers_mild}/{shape_stats.outliers_severe}",
                                    f"{shape_stats.max_over_p50:.2f}x",
                                )
                            )
                            if record_this_group:
                                spec = next(s for s in backend_specs if s.name == name)
                                cells.append(
                                    result.Cell(
                                        contender=name,
                                        shipped=spec.shipped,
                                        params={"backend": backend, "gc": mode, "limit": limit},
                                        trials=[
                                            result.Trial(
                                                trial=0,
                                                metrics={
                                                    **asdict(shape_stats),
                                                    "iterations": iterations,
                                                },
                                            )
                                        ],
                                    )
                                )

                for _, teardown in instances.values():
                    await teardown()
            finally:
                if db is not None:
                    db.close()

    if not record:
        return None

    env_start = env_module.capture()
    run_obj = result.Run(
        run_id=result.make_run_id(
            f"micro-{shape}",
            env_start["git"]["sha"],
            datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        ),
        suite=f"micro-{shape}",
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        invocation={"argv": sys.argv},
        git=env_start["git"],
        env=env_start,
        plan={"isolation": "combined", "bottleneck": "cpu"},
        config={
            "shape": shape,
            "rows": rows,
            "limit": limit,
            "iterations": iterations,
            "warmup": warmup,
            "gc": gc_modes,
            "only": only,
            "pin_requested": pin_cpus,
            "pin_actual": pin_actual,
        },
        equivalence={
            "enforced": recorded_eq.enforced if recorded_eq else False,
            "reference": recorded_eq.reference if recorded_eq else None,
            "payload_sha256": recorded_eq.payload_sha256 if recorded_eq else None,
            "payload_bytes": recorded_eq.payload_bytes if recorded_eq else 0,
            "self_consistent": recorded_eq.self_consistent if recorded_eq else False,
            "backend": recorded_backend,
        },
        cells=cells,
        warnings=env_module.warnings_for(env_start),
    )
    path = result.write(run_obj, RESULTS_DIR)
    typer.echo(f"\nrecorded: {path}  (quotable={run_obj.quotable})")
    return run_obj
