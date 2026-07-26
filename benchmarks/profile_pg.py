#!/usr/bin/env python3
"""Profile the asyncpg request path: where does the client's CPU actually go?

Run with the client pinned to one core and Postgres to its own cores — see
benchmarks/pin_and_run.sh, or pass --pin to have this script do it.

Two profilers, because they answer different questions and disagree in
instructive ways:

* **cProfile with a `time.process_time` timer.** Deterministic, so it counts
  every call and attributes *CPU* time rather than wall time. That matters here:
  a wall-clock profile of an asyncio loop is dominated by `epoll_wait`, which is
  exactly the time we do *not* care about. Cost: heavy per-call overhead, so
  absolute numbers inflate and call-heavy code looks worse than it is. Read the
  *shares*, not the milliseconds.
* **pyinstrument**, sampling, for a low-distortion cross-check of the shape.

Both are pointed at the same `request()` coroutine the load benchmark uses, so
the breakdown corresponds to the throughput numbers in docs/BENCHMARKS.md.

Usage:
    python3 benchmarks/profile_pg.py --only sqlom --requests 2000
    python3 benchmarks/profile_pg.py --compare            # sqlom vs async ORM
"""

import argparse
import asyncio
import cProfile
import os
import pstats
import re
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.benchargs import validate
from benchmarks.bench_pg_load import CONTENDERS, DEFAULT_DSN

from benchmarks import profkit
from benchmarks.profkit import categorize, rollup, top_functions


def pin(client_cores, db_cores):
    """Pin this process and the Postgres tree to disjoint core sets.

    Affinity is inherited across fork(), so pinning the postmaster covers
    backends started afterwards; existing ones are pinned explicitly. Must run
    before the pool is created, or pooled connections keep the old mask.

    Every failure here is reported rather than swallowed. A silently unpinned run
    still produces a full, plausible profile — it just measures the client and
    Postgres competing for cores, which is the specific confound pinning exists
    to remove (METHODOLOGY correction 3). Two ways that used to happen quietly:
    `ps -eo comm` truncates to 15 characters and the postmaster is only named
    `postgres` on package installs, so detection can find nothing; and `taskset`
    fails without privileges. Both left `pinned` looking true.
    """
    os.sched_setaffinity(0, {int(c) for c in client_cores.split(",")})
    postmaster = None
    warnings = []
    out = subprocess.run(["ps", "-eo", "pid,ppid,comm"], capture_output=True, text=True).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "postgres":
            parent = subprocess.run(["ps", "-o", "comm=", "-p", parts[1]],
                                    capture_output=True, text=True).stdout.strip()
            if parent != "postgres":
                postmaster = parts[0]
                break
    if postmaster:
        pids = [postmaster] + subprocess.run(["pgrep", "-P", postmaster],
                                             capture_output=True, text=True).stdout.split()
        for pid in pids:
            done = subprocess.run(["taskset", "-a", "-cp", db_cores, pid],
                                  capture_output=True, text=True)
            if done.returncode != 0:
                warnings.append(f"taskset failed for pid {pid}: "
                                f"{done.stderr.strip() or done.stdout.strip()}")
        check = subprocess.run(["taskset", "-cp", postmaster],
                               capture_output=True, text=True)
        if check.returncode == 0:
            actual = check.stdout.rsplit(":", 1)[-1].strip()
            print(f"postgres postmaster pid {postmaster} affinity: {actual}")
        else:
            warnings.append(f"could not read back postmaster affinity: "
                            f"{check.stderr.strip()}")
    else:
        warnings.append(
            "could not identify the postgres postmaster — Postgres is NOT pinned, "
            "so it is competing with the client for cores and this profile does "
            "not measure what it claims"
        )

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return sorted(os.sched_getaffinity(0)), postmaster, warnings


async def build(name, dsn, pool_size, limit):
    factory = next(f for k, f in CONTENDERS.items() if name.lower() in k.lower())
    label = next(k for k in CONTENDERS if name.lower() in k.lower())
    request, teardown = await factory(dsn, pool_size, limit)
    return label, request, teardown


# `rollup`, `top_functions` and `categorize` are imported from profkit above.
# Earlier revisions of this file carried private copies of the first two, which
# silently shadowed the shared ones — the exact drift profkit was extracted to
# prevent. `report()` below still does its own printing rather than calling
# profkit.print_rollup, because it adds explanatory lines this profile needs and
# the checked-in artifact quotes that wording; only the *logic* is shared.


async def profile_one(name, args):
    label, request, teardown = await build(name, args.dsn, args.pool_size, args.limit)
    try:
        for _ in range(args.warmup):
            await request()

        async def drive(n, concurrency):
            """Issue n requests total across `concurrency` workers."""
            if concurrency == 1:
                for _ in range(n):
                    await request()
                return
            per = max(1, n // concurrency)

            async def worker():
                for _ in range(per):
                    await request()

            await asyncio.gather(*[worker() for _ in range(concurrency)])

        # --- wall vs CPU, unprofiled, at BOTH concurrency levels ---
        # c=1 exposes how much of a single request is spent waiting on Postgres;
        # c=N is the saturated regime the throughput numbers come from.
        w0, c0 = time.perf_counter(), time.process_time()
        await drive(args.requests, 1)
        seq_wall = (time.perf_counter() - w0) / args.requests * 1000
        seq_cpu = (time.process_time() - c0) / args.requests * 1000

        w0, c0 = time.perf_counter(), time.process_time()
        await drive(args.requests, args.concurrency)
        n_done = max(1, args.requests // args.concurrency) * args.concurrency
        wall = (time.perf_counter() - w0)
        cpu = (time.process_time() - c0)

        # --- cProfile with a CPU-time timer ---
        # The C profiler requires an integer-returning timer, so use the _ns
        # variant and tell it each unit is a nanosecond. Passing the float
        # `process_time` here fails with "expect int, got float".
        prof = cProfile.Profile(timer=time.process_time_ns, timeunit=1e-9)
        prof.enable()
        await drive(args.profile_requests, args.concurrency)
        prof.disable()
        n_profiled = max(1, args.profile_requests // args.concurrency) * args.concurrency
        stats = pstats.Stats(prof)
        profiled_cpu = sum(t for (_c, _n, t, _ct, _cal) in stats.stats.values())

        # --- sampling cross-check ---
        # cProfile's per-call overhead (3-5x here) biases against call-heavy
        # code, so confirm the shape with a sampling profiler that doesn't.
        # Safe to read as CPU because utilization is ~1.0 at this concurrency.
        sampled = None
        if args.sampler:
            try:
                from pyinstrument import Profiler as SamplingProfiler

                sp = SamplingProfiler(interval=0.0005, async_mode="enabled")
                sp.start()
                await drive(args.profile_requests, args.concurrency)
                sp.stop()
                sampled = sp.output_text(unicode=True, color=False, show_all=False)
            except Exception as exc:  # pragma: no cover
                sampled = f"(sampling profiler unavailable: {exc})"

        return {
            "label": label,
            "seq_wall_ms": seq_wall,
            "seq_cpu_ms": seq_cpu,
            "seq_utilization": seq_cpu / seq_wall,
            "wall_ms": wall / n_done * 1000,
            "cpu_ms": cpu / n_done * 1000,
            "utilization": cpu / wall,
            "throughput": n_done / wall,
            "stats": stats,
            "profiled_cpu": profiled_cpu,
            "n": n_profiled,
            "sampled": sampled,
        }
    finally:
        await teardown()


def report(result, args):
    print(f"\n{'=' * 78}\n{result['label']}\n{'=' * 78}")
    print(f"  sequential (c=1):  {result['seq_wall_ms']:.3f} ms wall/req, "
          f"{result['seq_cpu_ms']:.3f} ms CPU/req, utilization {result['seq_utilization']:.2f}")
    print(f"      -> {(1 - result['seq_utilization']) * 100:.0f}% of a lone request's wall time is "
          f"waiting on Postgres, not running Python")
    print(f"  saturated (c={args.concurrency}):  {result['wall_ms']:.3f} ms wall/req, "
          f"{result['cpu_ms']:.3f} ms CPU/req, utilization {result['utilization']:.2f}, "
          f"{result['throughput']:.0f} rps")
    print(f"      -> utilization near 1.0 means the core is saturated and DB wait is")
    print(f"         fully hidden behind other requests; CPU/req now sets throughput")

    total = result["profiled_cpu"]
    inflation = (total * 1000 / result["n"]) / result["cpu_ms"] if result["cpu_ms"] else 0
    print(f"\n  CPU by library — cProfile, process_time timer, {result['n']} requests.")
    print(f"  Profiled cost is {total * 1000 / result['n']:.3f} ms/req vs {result['cpu_ms']:.3f} "
          f"unprofiled ({inflation:.1f}x instrumentation overhead), so read shares, not ms.")
    print(f"  C builtins are attributed to their callers.\n")
    print(f"    {'library':<22}{'share':>8}{'flat':>8}")
    print(f"    {'-' * 38}")
    flat = dict(rollup(result["stats"], attribute_builtins=False))
    for lib, secs in rollup(result["stats"]):
        if secs / total < 0.005:
            continue
        print(f"    {lib:<22}{secs / total * 100:>7.1f}%{flat.get(lib, 0) / total * 100:>7.1f}%")

    if result.get("sampled"):
        print(f"\n  Sampling cross-check (pyinstrument, 0.5 ms interval, wall≈CPU here):")
        for line in result["sampled"].splitlines()[:args.top + 6]:
            print(f"    {line}")

    print(f"\n  Top {args.top} functions by self CPU:\n")
    print(f"    {'ncalls':>10}{'tot ms':>9}{'cum ms':>9}  where")
    print(f"    {'-' * 74}")
    for ncalls, tot, cum, where in top_functions(result["stats"], args.top):
        print(f"    {ncalls:>10}{tot * 1000:>9.1f}{cum * 1000:>9.1f}  {where[:60]}")


def compare(a, b):
    print(f"\n{'=' * 78}\nWHERE THE DIFFERENCE GOES\n{'=' * 78}")
    ta = a["profiled_cpu"] / a["n"]
    tb = b["profiled_cpu"] / b["n"]
    ra = dict(rollup(a["stats"]))
    rb = dict(rollup(b["stats"]))
    libs = sorted(set(ra) | set(rb), key=lambda l: -(rb.get(l, 0) / b["n"] - ra.get(l, 0) / a["n"]))
    print(f"  {'library':<22}{a['label'][:16]:>17}{b['label'][:16]:>17}{'delta':>11}")
    print(f"  {'-' * 67}")
    # Rescale each side's shares onto its measured (unprofiled) CPU/req so the
    # two columns are comparable despite differing instrumentation overhead.
    for lib in libs:
        va = ra.get(lib, 0) / a["profiled_cpu"] * a["cpu_ms"]
        vb = rb.get(lib, 0) / b["profiled_cpu"] * b["cpu_ms"]
        if max(va, vb) < 0.002:
            continue
        print(f"  {lib:<22}{va:>17.3f}{vb:>17.3f}{vb - va:>+11.3f}")
    print(f"  {'-' * 67}")
    print(f"  {'TOTAL CPU ms/req':<22}{a['cpu_ms']:>17.3f}{b['cpu_ms']:>17.3f}"
          f"{b['cpu_ms'] - a['cpu_ms']:>+11.3f}")
    print(f"  {'throughput (rps)':<22}{a['throughput']:>17.0f}{b['throughput']:>17.0f}"
          f"{b['throughput'] - a['throughput']:>+11.0f}")
    print(f"\n  Shares are rescaled onto each side's measured CPU/req, so columns are")
    print(f"  comparable even though cProfile inflates each differently.")


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--only", default="sqlom")
    p.add_argument("--compare", action="store_true", help="profile sqlom and the async ORM")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--pool-size", type=int, default=10)
    p.add_argument("--requests", type=int, default=2000, help="unprofiled timing requests")
    p.add_argument("--profile-requests", type=int, default=500)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--top", type=int, default=18)
    p.add_argument("--concurrency", type=int, default=8,
                   help="workers for the saturated measurement (default 8, matching the load benchmark)")
    p.add_argument("--sampler", action="store_true",
                   help="also run pyinstrument as a low-overhead cross-check")
    p.add_argument("--pin", default=None, metavar="CLIENT:DB",
                   help="pin client and Postgres to core sets, e.g. 0:2,3")
    args = p.parse_args()
    validate(p, args)
    if args.pin:
        client_cores, db_cores = args.pin.split(":")
        aff, pm, warnings = pin(client_cores, db_cores)
        if pm is None:
            print(f"client pinned to cores {aff}; POSTGRES NOT PINNED", file=sys.stderr)
        else:
            print(f"client pinned to cores {aff}; postgres (pid {pm}) -> cores {db_cores}")
        if warnings:
            # Refusing to run is the point: a pinned profile that silently ran
            # unpinned would be published as though the cores were disjoint.
            print("refusing to profile with pinning in an unknown state; "
                  "fix the above or drop --pin to profile unpinned deliberately",
                  file=sys.stderr)
            return 1
    else:
        print(f"client cores: {sorted(os.sched_getaffinity(0))} (not pinned by this script)")
    print(f"rows/request: {args.limit}, pool: {args.pool_size}")

    names = ["sqlom", "async ORM"] if args.compare else [args.only]
    results = []
    for n in names:
        results.append(await profile_one(n, args))
        report(results[-1], args)
    if len(results) == 2:
        compare(results[0], results[1])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
