# How to benchmark this honestly

Four claims published in this repo turned out to be wrong. Each was caught by
attacking the benchmark rather than trusting it, and each came from a distinct
methodological flaw. They are recorded here because the flaws generalize well
beyond sqlom.

If you take one thing from this file: **a benchmark result that flatters the thing
you built should be treated as a bug report until you have tried to break it.**

---

## The four corrections

### 1. Comparing different payloads (inflated 3.5x → 2.1x)

The first sqlite benchmark had sqlom emitting `"is_active":1` while both SQLAlchemy
variants emitted `"is_active":true`. sqlite has no boolean type, and SQLAlchemy's
`Boolean` column converts on the way out — sqlom simply passed the raw integer
through. **sqlom was skipping type-coercion work its competitors were paying for**,
and the published 3.5x-vs-ORM was partly measuring that.

Fix: `bench_sqlite.py` now asserts every approach emits **byte-identical JSON**
before timing starts, and fails loudly otherwise. The honest figure for that same
approach is ~2.1–2.5x.

> **Generalizes to:** any cross-library comparison. If two implementations produce
> different bytes, you are not benchmarking the same work. Diff the output first.

### 2. Contender ordering inside one process

The load suite ran all contenders in a single process, in dict order, with sqlom
first. That is not neutral: later contenders measured slower. At c=1 it produced a
physically impossible result — sqlom appeared to beat the *no-object* baselines and
to use less CPU than doing no mapping at all.

| c=1, pinned, 100 rows | in-suite | isolated (median of 3) |
|---|---|---|
| sqlom | 1095 rps | **848 rps** |
| raw asyncpg + codegen dict | 777 rps | **1237 rps** |
| raw asyncpg + `dict(Record)` | 667 rps | **1309 rps** |

Isolated, the hand-written baselines are faster than sqlom — as they must be. The
in-suite ordering had it exactly backwards.

Fix: `--only` runs one contender per process and `--repeat` takes medians. The
combined suite is for a quick side-by-side, never for publication.

> **Generalizes to:** any suite that measures several implementations in one
> process. Allocator state, CPU caches, and thermal/frequency drift are all shared.
> One process per contender, repeated, medians.

### 3. Misreading a resource limit as a fair test (retracted 4.2x)

Having flagged that client and server compete for CPU on one box, the next step
pinned Postgres to 2 cores and the client to 2 cores, observed throughput drop, and
revised the headline from ~6x down to 4.2x — attributing the drop to "removing
contention."

Both halves were wrong. A single asyncio event loop under the GIL saturates exactly
one core; the recorded `cpu_utilization` was **0.91–1.00 in every run already
taken**, which said so plainly. Giving the client two cores wasted one, and the drop
came from (a) the loop migrating between two cores and losing cache locality, and
(b) Postgres having *fewer* cores than it did unpinned.

| sqlom, c=8 | CPU ms/req | throughput |
|---|---|---|
| client pinned to 1 core | **0.217** | 4560 rps |
| client pinned to 2 cores | 0.308 | 3168 rps |

The 4.2x figure was an artifact and is retracted. Re-measured with the client on one
core and Postgres on 1/2/3 cores, the ratio is stable at **5.1–6.2x**.

> **Generalizes to:** know your subject's parallelism model before designing the
> resource experiment. Also: I had already recorded the metric that falsified this
> (`cpu_utilization`) and did not look at it. Collect utilization alongside
> throughput, then actually read it.

### 4. Mixing measurement conditions in one table

A serialization table compared "plain dict: 204 ns" against "slots + passthrough:
182 ns" and concluded the passthrough path beat a plain dict. It cannot. The 204
came from a 10-field benchmark and the 182 from a 4-field one.

Fix: [BENCHMARKS.md §3](BENCHMARKS.md#orjson-serialization-list-of-1000-objects)
reports both widths measured by the same harness. At 4 fields it is 68 vs 182 — the
dict wins by 2.7x, as it must.

> **Generalizes to:** never merge numbers from different harnesses into one table.
> Re-measure everything you intend to place in the same column.

---

## Practices this benchmark suite adopts

**Enforce output equivalence before timing.** Both `bench_sqlite.py` and
`bench_pg_load.py` compare every contender's bytes against a reference and abort on
mismatch. `--skip-equivalence` exists for debugging only.

**One contender per process for any published number.** `--only` plus `--repeat`,
report medians. Expect c=1 cells to swing 15–20% between trials; a single low-
concurrency run means very little.

**Record utilization, not just throughput.** `cpu_ms_per_request` and
`cpu_utilization` are what explain a throughput difference, and they are what catch
an impossible result. Throughput alone would not have exposed correction 3.

**Pin deliberately, and know what you are pinning.** `pin_and_run.sh` gives Postgres
and the client disjoint cores. CPU affinity is inherited across `fork()`, so pinning
the postmaster covers backends started afterwards — but pooled connections opened
*before* pinning keep the old mask, so pin first. Pin a single-threaded client to
*one* core, not several.

**Include a floor and a naive baseline.** `raw asyncpg + codegen dict` (no objects)
and `raw asyncpg + dict(Record)` (what you'd write by hand) bracket the result. When
sqlom appeared to beat the floor, that was the signal something was wrong — a
benchmark without a floor has no such tripwire.

**State the bottleneck.** Every ratio here is measured with the *client* saturated
and Postgres barely loaded. That is why the mapper's CPU cost is visible at all. Say
which side is the constraint, or the number is uninterpretable.

---

## Reproducing

```bash
pip install sqlalchemy orjson asyncpg attrs

# latency / component micro-benchmarks (no server needed)
python3 benchmarks/bench_sqlite.py --rows 200000 --limit 1000 --iterations 300 --warmup 30
python3 benchmarks/profile_stages.py

# throughput (needs PostgreSQL)
createdb sqlom_bench
python3 benchmarks/bench_pg_load.py --seed-only

# quick side-by-side — ordering-biased, do not quote
python3 benchmarks/bench_pg_load.py --limit 100 --concurrency 1,8,32,64 --duration 4

# quote-worthy: isolated, repeated, deliberately pinned
bash benchmarks/pin_and_run.sh --db-cores 1,2,3 --client-cores 0 -- \
     --only sqlom --concurrency 8 --duration 4 --repeat 3
```

Raw artifacts from the runs quoted in [BENCHMARKS.md](BENCHMARKS.md) are in
[`benchmarks/results/`](../benchmarks/results/).
