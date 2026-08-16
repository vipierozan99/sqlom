"""`bench micro run`.

Pure in-process micro benchmarks: one contender per shape/backend, gated by
output equivalence before any timing starts.

**Two modes, and only one of them produces a publishable number.** By default
every contender is timed in this process, one after another — fast, fine for a
dev loop, and *not* quotable: `Run.quotable` refuses a multi-cell run whose
`plan.isolation` is not `one_contender_per_process`, because contenders sharing
an interpreter share its allocator arenas, its type caches and whatever the
previous contender left resident. `--isolate` spawns `bench micro cell` per
measurement instead, which is what that gate is asking for.

`--trials N` is the other half. A single timed run yields a median and no way to
tell whether it reproduces; the schema has carried `spread_pct` (trial to trial)
and `ratios[].tie` since it was written, and both need more than one trial to
mean anything. `--isolate --trials 5` is the publishing recipe, and slow on
purpose.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
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
from benchmarks.harness.stats import ratio_with_spread, sample_shape
from benchmarks.harness.timing import assert_unpatched_threading, gc_control, per_iteration

app = typer.Typer(help="Pure in-process micro benchmarks.")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

#: Ratios are reported against this contender where it is present — it is the
#: thing the suite exists to defend, so "vs rowform" is the number a reader
#: wants, and reporting it beside its own trial spread is what stops a 3%
#: difference being read as a result.
REFERENCE = "rowform"

# One spec drives both the header and the value rows so they cannot drift apart.
# `outliers m/s` is Tukey mild/severe (see stats.SampleShape); `max/p50` is an
# interference detector, not a dispersion figure. `spread%` is the trial-to-trial
# one and only appears with `--trials`; `vs rowform` likewise, since a ratio
# without an interval around it is the thing METHODOLOGY.md's tie rule exists to
# prevent.
_BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("contender", "<38"),
    ("median ms", ">10"),
    ("IQR%", ">8"),
    ("p95/p50", ">9"),
    ("outliers m/s", ">14"),
    ("max/p50", ">9"),
)
_TRIAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("spread%", ">9"),
    (f"vs {REFERENCE}", ">14"),
)


#: Summarised across trials, and *how* differs by what the metric is for.
#:
#: `median_ms` is a central value, so the median across trials is the
#: representative one. `iqr_pct` is a dispersion figure (`stats.py` calls it
#: *the* one, the reason pytest-benchmark reports IQR at all) but is summarised
#: the same way on purpose: it describes the spread *within* a trial, and the
#: representative answer to "how disturbed is a typical run" is the median of
#: those, not the worst. The across-trial counterpart is `spread_pct`, printed
#: beside it. The rest are detectors — `p95_over_p50` is the tail, `max_over_p50`
#: is explicitly an interference detector (`stats.SampleShape`), and the Tukey
#: counts say how much of the sample was disturbed — so the **worst** trial is
#: what gets reported. A detector summarised by its best case detects nothing.
#:
#: The outlier pair is taken from one trial rather than per metric; `_worst_outliers`
#: says why.
#:
#: The first version of this printed all five from the single fastest trial while
#: the median column came from all of them, which quietly biased every published
#: dispersion figure toward the cleanest run: p95/p50 read 1.02-1.30 where the
#: full trial set reached 4.11, and max/p50 read 1.70 against 9.47.
_SUMMARY_KEYS = [
    "median_ms",
    "iqr_pct",
    "p95_over_p50",
    "outliers_mild",
    "outliers_severe",
    "max_over_p50",
]
_WORST_KEYS = {"p95_over_p50", "outliers_mild", "outliers_severe", "max_over_p50"}


def _summarised(cell_obj: result.Cell, key: str) -> float:
    """One number per metric across a cell's trials — see `_SUMMARY_KEYS`."""
    return cell_obj.summary[key]["max" if key in _WORST_KEYS else "median"]


def _worst_outliers(cell_obj: result.Cell) -> tuple[int, int]:
    """The mild/severe pair from the *most disturbed trial*, not each metric's
    maximum taken separately.

    `stats.sample_shape` defines mild as `count(outside inner) - severe`, so the
    two are anti-correlated across trials: a trial whose outliers turn severe
    reports fewer mild ones. Maximising each independently therefore printed a
    pair no trial produced — 19/33 from a run whose worst single trial was 9/33,
    with a total (52) higher than any trial reached (42). Conservative, but not
    an observation. Ranking on the total and reporting that trial's pair is.
    """
    trials = [
        (
            int(t.metrics.get("outliers_mild", 0)),
            int(t.metrics.get("outliers_severe", 0)),
        )
        for t in cell_obj.trials
    ]
    return max(trials, key=sum) if trials else (0, 0)


def _row(columns: tuple[tuple[str, str], ...], *cells: str) -> str:
    return "    " + "".join(
        f"{cell:{spec}}" for cell, (_, spec) in zip(cells, columns, strict=True)
    )


@app.command()
def run(
    shape: str = typer.Option("flat", help=f"one of {seed_module.SHAPES} — {registry.SHAPE_HELP}"),
    rows: int = typer.Option(200_000, help="rows seeded into the ephemeral database"),
    limit: int = typer.Option(1000, help="rows per request"),
    iterations: int = typer.Option(1000, help="timed iterations per contender"),
    warmup: int = typer.Option(100, help="untimed iterations before measurement"),
    only: str | None = typer.Option(None, help=registry.ONLY_HELP),
    backend: str | None = typer.Option(
        None,
        "--backend",
        help="only this backend ('sqlite', 'postgres', 'mock'). A recorded run "
        "carries one equivalence block, so pick the backend you mean to publish",
    ),
    gc: str = typer.Option(
        "off", help="'on', 'off', or 'both'"
    ),
    pin: str | None = typer.Option(
        "auto",
        "--pin",
        help="comma-separated logical CPUs to pin this process to, 'auto' (two "
        "whole physical cores picked from this machine's own topology), or '' "
        "to disable pinning",
    ),
    trials: int = typer.Option(
        1, help="repeat the whole measurement N times — what spread_pct and the "
        "tie rule are computed from. More than 1 to publish"
    ),
    isolate: bool = typer.Option(
        False, "--isolate", help="time each contender in its own process. Required "
        "for a quotable multi-contender run"
    ),
    record: bool = typer.Option(
        False, "--record", help="write a run.json under results/runs/"
    ),
    pg_dsn: str | None = typer.Option(
        None,
        "--pg-dsn",
        help="run the postgres contenders against this server — its shape tables "
        "(users/j_authors/...) are DROPPED and reseeded first; start a throwaway "
        "one with `bench db up`",
    ),
) -> None:
    """Run every registered contender for `--shape`, gated by output
    equivalence (per backend group), and print per-iteration medians."""
    if shape not in seed_module.SHAPES:
        raise typer.BadParameter(f"shape must be one of {seed_module.SHAPES}")
    gc_modes = ["on", "off"] if gc == "both" else [gc]
    if any(mode not in ("on", "off") for mode in gc_modes):
        raise typer.BadParameter("--gc must be 'on', 'off', or 'both'")
    if trials < 1:
        raise typer.BadParameter("--trials must be at least 1")
    assert_unpatched_threading()
    if pin == "auto":
        # Derived from the machine's own topology rather than hardcoded indices
        # — a fixed default like the old "6,7,8,9" crashes on boxes with fewer
        # CPUs and silently lands on SMT siblings of one physical core on
        # others (the exact finding harness/affinity.py exists to prevent).
        auto_plan = affinity.plan({"bench": 2})
        pin_cpus = auto_plan.roles["bench"]
        for warning in auto_plan.warnings:
            typer.echo(f"  ! {warning}")
    else:
        pin_cpus = [int(c) for c in pin.split(",")] if pin else []
    asyncio.run(
        _run(
            shape, rows, limit, iterations, warmup, only, backend, gc_modes,
            pin_cpus, trials, isolate, record, pg_dsn,
        )
    )


@app.command(hidden=True)
def cell(
    slug: str = typer.Option(..., help="contender slug to time"),
    shape: str = typer.Option(...),
    handle: str = typer.Option("", help="sqlite path or postgres DSN; empty for mock"),
    limit: int = typer.Option(1000),
    iterations: int = typer.Option(1000),
    warmup: int = typer.Option(100),
    gc: str = typer.Option("off"),
    out: Path = typer.Option(..., help="write the metrics JSON here"),
) -> None:
    """Time exactly one contender and write its `SampleShape` — plus the
    sha256 of one payload, so the parent can verify this process produced the
    same bytes its equivalence gate approved — to `--out`.

    The child half of `run --isolate`. Not meant to be called by hand, and
    hidden for that reason: it takes an already-provisioned `--handle`; the
    cross-contender equivalence comparison stays the parent's job.

    CPU affinity is inherited from the parent across `exec`, so this lands on the
    same cpus `--pin` chose without being told them (`harness/affinity.py`).
    """
    assert_unpatched_threading()
    asyncio.run(_cell(slug, shape, handle, limit, iterations, warmup, gc, out))


async def _cell(
    slug: str, shape: str, handle: str, limit: int, iterations: int,
    warmup: int, gc_mode: str, out: Path,
) -> None:
    spec = registry.get(slug)
    resolved = await _mock_handle(shape, limit) if spec.backend == "mock" else handle
    target, teardown = await spec.factory(ContenderInit(handle=resolved, limit=limit))
    try:
        # One untimed call before warmup: its bytes are what the parent
        # compares against the hash its equivalence gate approved — without
        # this, the gated objects and the timed objects lived in different
        # processes and child-side divergence went undetected.
        payload = await target()
        with gc_control(gc_mode):
            samples = [s * 1000 for s in await per_iteration(target, iterations, warmup)]
    finally:
        await teardown()
    out.write_text(
        json.dumps({
            "metrics": asdict(sample_shape(samples)),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        })
    )


async def _mock_handle(shape: str, limit: int) -> list[tuple]:
    """Real rows sourced from a throwaway sqlite db, once, at setup — the
    driver term is paid only here, never inside a MockEngine contender's
    timed `request()`.

    Rebuilt rather than passed when `--isolate` spawns a child: the seeder is
    deterministic (`harness/seed.RNG_SEED`), so the child's rows are the parent's
    rows, and shipping a few thousand of them over argv is not.
    """
    import aiosqlite

    if shape == "flat":
        # ORDER BY: without it the fixture's row set is whatever the query
        # planner happens to scan first — deterministic per sqlite version,
        # but an accident, not a property.
        sql = (
            "SELECT id, name, email, is_active FROM users "
            "WHERE is_active = 1 AND id > 100 ORDER BY id LIMIT ?"
        )
    elif shape == "join":
        sql = (
            "SELECT a.id, a.name, a.email, a.is_active, "
            "p.id, p.author_id, p.title, p.score, p.published "
            "FROM j_authors a JOIN j_posts p ON p.author_id = a.id "
            "WHERE a.is_active = 1 AND p.score > 100 ORDER BY a.id, p.id LIMIT ?"
        )
    else:
        raise ValueError(f"no mock row source for shape {shape!r}")

    # 3x the limit: the filters above discard ~10-50% of seeded rows, and at
    # 2x a small --limit silently canned fewer rows than `limit` while
    # params recorded the full number — the check below makes any future
    # shortfall loud instead.
    db = EphemeralSqlite.create(shape, max(limit * 3, 600))
    try:
        conn = await aiosqlite.connect(db.path)
        try:
            cur = await conn.execute(sql, (limit,))
            rows = list(await cur.fetchall())
        finally:
            await conn.close()
    finally:
        db.close()
    if len(rows) != limit:
        raise RuntimeError(
            f"mock row source produced {len(rows)} rows for --limit {limit} — "
            f"seed more rows in _mock_handle or lower --limit"
        )
    if shape == "flat":
        return [(r[0], r[1], r[2], bool(r[3])) for r in rows]
    return [(r[0], r[1], r[2], bool(r[3]), r[4], r[5], r[6], r[7], bool(r[8])) for r in rows]


def _spawn_cell(
    slug: str, shape: str, handle: str, limit: int, iterations: int, warmup: int, gc_mode: str,
) -> dict:
    """One measurement in a process of its own. Raises on a non-zero exit rather
    than recording a hole: a contender that cannot run is a failed run, not a
    missing cell."""
    with tempfile.TemporaryDirectory(prefix="rowform-cell-") as tmp:
        out = Path(tmp) / "cell.json"
        completed = subprocess.run(
            [
                sys.executable, "-m", "benchmarks", "micro", "cell",
                "--slug", slug, "--shape", shape, "--handle", handle,
                "--limit", str(limit), "--iterations", str(iterations),
                "--warmup", str(warmup), "--gc", gc_mode, "--out", str(out),
            ],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"contender {slug!r} failed in its own process "
                f"(exit {completed.returncode}):\n{completed.stderr[-2000:]}"
            )
        return json.loads(out.read_text())


async def _measure(
    spec: registry.ContenderSpec,
    target,
    shape: str,
    handle,
    limit: int,
    iterations: int,
    warmup: int,
    gc_mode: str,
    isolate: bool,
) -> tuple[dict, str | None]:
    """One trial's metrics for one contender, in this process or its own.
    Returns `(metrics, payload_sha256)`; the hash is None in-process (the
    equivalence gate already ran on these exact objects)."""
    if isolate:
        cell_out = _spawn_cell(
            spec.slug, shape, handle if isinstance(handle, str) else "",
            limit, iterations, warmup, gc_mode,
        )
        return cell_out["metrics"], cell_out["payload_sha256"]
    with gc_control(gc_mode):
        samples = [s * 1000 for s in await per_iteration(target, iterations, warmup)]
    return asdict(sample_shape(samples)), None


def _reference_in(group: list[result.Cell]) -> result.Cell | None:
    """The cell everything else is measured against — exact `REFERENCE` name,
    or a unique `REFERENCE`-prefixed one.

    Never for the mock group: each mock is its own row-layer floor and the two
    mock seams exclude *different* layers by construction (`engines/mock.py`),
    so a "SQLAlchemy (mock) vs rowform (mock)" ratio compares two different
    timed regions — this used to be computed and recorded anyway, via the
    prefix fallback picking `rowform (mock)`.
    """
    if group and group[0].params.get("backend") == "mock":
        return None
    exact = [c for c in group if c.contender == REFERENCE]
    if exact:
        return exact[0]
    prefixed = [c for c in group if c.contender.startswith(REFERENCE)]
    return prefixed[0] if len(prefixed) == 1 else None


def _ratios_for(cells: list[result.Cell]) -> list[dict]:
    """Every cell's median against the group's reference, per gc mode, as
    `ratio_with_spread` — value, the worst-case interval the trials allow, and
    the tie flag. Needs one value per trial, which is why this is empty for a
    single-trial run rather than quietly reporting a bare ratio."""
    by_mode: dict[str, list[result.Cell]] = {}
    for cell_obj in cells:
        by_mode.setdefault(cell_obj.params["gc"], []).append(cell_obj)

    ratios = []
    for mode, group in by_mode.items():
        reference = _reference_in(group)
        if reference is None or len(reference.trials) < 2:
            continue
        denominator = [t.metrics["median_ms"] for t in reference.trials]
        for cell_obj in group:
            if cell_obj is reference:
                continue
            numerator = [t.metrics["median_ms"] for t in cell_obj.trials]
            ratios.append(
                {
                    "contender": cell_obj.contender,
                    "against": reference.contender,
                    "gc": mode,
                    **ratio_with_spread(numerator, denominator),
                }
            )
    return ratios


def _format_ratio(cell_obj: result.Cell, group: list[result.Cell], ratios: list[dict], mode: str) -> str:
    if cell_obj is _reference_in(group):
        return "1.00x"
    entry = next(
        (r for r in ratios if r["contender"] == cell_obj.contender and r["gc"] == mode), None
    )
    if entry is None:
        return "—"
    return f"{entry['value']:.2f}x{' tie' if entry['tie'] else ''}"


async def _run(
    shape: str,
    rows: int,
    limit: int,
    iterations: int,
    warmup: int,
    only: str | None,
    backend_filter: str | None,
    gc_modes: list[str],
    pin_cpus: list[int],
    trials: int,
    isolate: bool,
    record: bool,
    pg_dsn: str | None = None,
) -> result.Run | None:
    specs = registry.select(shape=shape, only=only, backend=backend_filter)
    if not specs:
        raise typer.BadParameter(
            f"no contenders match shape={shape!r} only={only!r} backend={backend_filter!r}"
        )

    by_backend: dict[str, list[registry.ContenderSpec]] = {}
    for spec in specs:
        by_backend.setdefault(spec.backend, []).append(spec)

    started_at = datetime.now(UTC).isoformat()
    # Captured before any measurement so the recorded loadavg/frequencies
    # describe the machine the run *started* on, not the machine the run
    # itself just loaded; merged with an end snapshot at record time so
    # mid-run drift (throttling, frequency sag) is visible in the artifact.
    env_snapshot_start = env_module.capture()
    recorded_backend: str | None = None
    recorded_eq: equivalence.EquivalenceResult | None = None
    cells: list[result.Cell] = []
    columns = _BASE_COLUMNS + (_TRIAL_COLUMNS if trials > 1 else ())
    header = _row(columns, *(label for label, _ in columns))

    with affinity.pin_current_process(pin_cpus) as pin_actual:
        if pin_cpus:
            typer.echo(f"pinned to cpus {pin_actual}")
        if isolate:
            typer.echo("isolation: one contender per process")

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
            instances = {}
            try:
                for spec in backend_specs:
                    target, teardown = await spec.factory(init)
                    instances[spec.name] = (target, teardown)

                # The gate runs in this process even under `--isolate`: it is a
                # correctness check on bytes, not a measurement, and comparing
                # payloads across processes would only add ways to be wrong.
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
                    # passing equivalence — pick it with `--backend`, since the
                    # recorded schema carries one equivalence block.
                    record_this_group = record and recorded_backend is None
                    if record_this_group:
                        recorded_backend = backend
                        recorded_eq = eq

                    for mode in gc_modes:
                        group_cells = [
                            result.Cell(
                                contender=spec.name,
                                shipped=spec.shipped,
                                params={"backend": backend, "gc": mode, "limit": limit},
                                trials=[],
                            )
                            for spec in backend_specs
                        ]
                        for trial in range(trials):
                            for spec, cell_obj in zip(backend_specs, group_cells, strict=True):
                                target = instances[spec.name][0]
                                metrics, child_sha = await _measure(
                                    spec, target, shape, handle, limit,
                                    iterations, warmup, mode, isolate,
                                )
                                if child_sha is not None and child_sha != eq.payload_sha256:
                                    raise RuntimeError(
                                        f"contender {spec.name!r} produced different bytes "
                                        f"in its timed process ({child_sha[:12]}…) than the "
                                        f"equivalence gate approved "
                                        f"({str(eq.payload_sha256)[:12]}…) — the gated and "
                                        f"the timed work have diverged"
                                    )
                                cell_obj.trials.append(
                                    result.Trial(
                                        trial=trial,
                                        metrics={**metrics, "iterations": iterations},
                                    )
                                )
                        for cell_obj in group_cells:
                            cell_obj.summarize(_SUMMARY_KEYS)
                            # The paired worst-trial counts, recorded beside the
                            # per-metric maxima: the independent maxima can name
                            # a mild/severe pair no trial produced (see
                            # `_worst_outliers`), and only the print path used
                            # to carry the corrected pairing.
                            mild, severe = _worst_outliers(cell_obj)
                            cell_obj.summary["outliers_worst_trial"] = {
                                "mild": mild,
                                "severe": severe,
                            }

                        group_ratios = _ratios_for(group_cells)
                        typer.echo(f"  -- gc={mode}, trials={trials} --")
                        typer.echo(header)
                        for cell_obj in group_cells:
                            values = [
                                f"{_summarised(cell_obj, 'median_ms'):.4f}",
                                f"{_summarised(cell_obj, 'iqr_pct'):.1f}",
                                f"{_summarised(cell_obj, 'p95_over_p50'):.2f}",
                                "{}/{}".format(*_worst_outliers(cell_obj)),
                                f"{_summarised(cell_obj, 'max_over_p50'):.2f}x",
                            ]
                            if trials > 1:
                                values += [
                                    f"{cell_obj.summary['median_ms']['spread_pct']:.1f}",
                                    _format_ratio(cell_obj, group_cells, group_ratios, mode),
                                ]
                            typer.echo(_row(columns, cell_obj.contender, *values))

                        if record_this_group:
                            cells.extend(group_cells)

            finally:
                # In a finally, not at the end of the try: a failed equivalence
                # check or child spawn must not leak the pools/engines the
                # factories opened (up to 4 postgres connections per contender).
                for _, teardown in instances.values():
                    await teardown()
                if db is not None:
                    db.close()

    if not record:
        return None

    env_merged = env_module.merge_start_end(env_snapshot_start, env_module.capture())
    run_obj = result.Run(
        run_id=result.make_run_id(
            f"micro-{shape}",
            env_merged["git"]["sha"],
            datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        ),
        suite=f"micro-{shape}",
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        invocation={"argv": sys.argv},
        git=env_merged["git"],
        env=env_merged,
        plan={
            "isolation": "one_contender_per_process" if isolate else "combined",
            "bottleneck": "cpu",
        },
        config={
            "shape": shape,
            "rows": rows,
            "limit": limit,
            "iterations": iterations,
            "warmup": warmup,
            "trials": trials,
            "gc": gc_modes,
            "only": only,
            "backend": recorded_backend,
            "pin_requested": pin_cpus,
            "pin_actual": pin_actual,
        },
        equivalence={
            "enforced": recorded_eq.enforced if recorded_eq else False,
            "reference": recorded_eq.reference if recorded_eq else None,
            "payload_sha256": recorded_eq.payload_sha256 if recorded_eq else None,
            "payload_bytes": recorded_eq.payload_bytes if recorded_eq else 0,
            "self_consistent": recorded_eq.self_consistent if recorded_eq else False,
            # Under --isolate every timed child also proved (by hash) that it
            # produced the gated bytes; in-process runs are covered by the
            # gate having run on the very objects that were timed.
            "checked_in_timed_processes": isolate,
            "backend": recorded_backend,
        },
        cells=cells,
        ratios=_ratios_for(cells),
        warnings=env_module.warnings_for(env_merged),
    )
    path = result.write(run_obj, RESULTS_DIR)
    typer.echo(f"\nrecorded: {path}  (quotable={run_obj.quotable})")
    if not run_obj.quotable:
        for reason in _unquotable_reasons(run_obj):
            typer.echo(f"  not quotable: {reason}")
    return run_obj


def _unquotable_reasons(run_obj: result.Run) -> list[str]:
    """Why `quotable` is False, in the terms the gate uses. Printed rather than
    left to be rediscovered by reading `Run.quotable` — the flag is only useful
    if the fix is obvious from the message."""
    reasons = []
    if not run_obj.equivalence.get("enforced", False):
        reasons.append("equivalence gate did not run")
    if run_obj.equivalence.get("self_consistent") is False:
        reasons.append("the reference contender was not self-consistent")
    if (
        len(run_obj.cells) > 1
        and run_obj.plan.get("isolation") != "one_contender_per_process"
    ):
        reasons.append("contenders shared a process — pass --isolate")
    # The dirty-tree gate is also an env warning, so it arrives from there.
    reasons.extend(run_obj.warnings)
    return reasons
