# sqlom benchmark results

Every number here was produced by a script in [`benchmarks/`](../benchmarks/), on the
machine described in each section. Raw output artifacts are checked in under
[`benchmarks/results/`](../benchmarks/results/).

Read [METHODOLOGY.md](METHODOLOGY.md) before quoting any of this — several
figures in earlier revisions were wrong, and the reasons are instructive.

**Environment** (all runs)

```
Python      3.11.15            Linux 6.18.5 x86-64, 4 vCPU
asyncpg     0.31.0             PostgreSQL 16.13 (localhost)
sqlalchemy  2.0.51             orjson 3.11.9, attrs 26.1.0
```

---

## 1. sqlite micro-benchmark (single request, latency)

`benchmarks/bench_sqlite.py` — every approach reads the *same* sqlite file through
the *same* driver, so the database round trip is held roughly constant and the
delta is the object-shaping path. 1,000 rows from a 200,000-row table, 300
iterations after 30 warmup. All approaches are asserted to emit **byte-identical
JSON** before timing.

**Median of 5 trials**, with the observed spread across trials:

| approach | median | min | max | spread | vs. ORM |
|---|---|---|---|---|---|
| sqlom compiled, per-row hydrator | 1.028 ms | 1.024 | 1.091 | 7% | 6.46x |
| `@model` dataclass + `OPT_PASSTHROUGH_DATACLASS` | 1.054 ms | 1.013 | 1.067 | 5% | 6.30x |
| sqlom compiled, batch hydrator | 1.060 ms | 1.014 | 1.072 | 5% | 6.27x |
| `@model` dataclass, orjson native path | 1.292 ms | 1.248 | 1.325 | 6% | 5.15x |
| sqlom reflective (`hydrate()` + `as_dict()`) | 2.528 ms | 2.481 | 2.610 | 5% | 2.63x |
| SQLAlchemy 2.0 Core | 4.252 ms | 3.967 | 4.695 | 17% | 1.56x |
| SQLAlchemy 2.0 ORM | 6.647 ms | 6.543 | 6.835 | 4% | 1.00x (baseline) |
| *(DB-side JSON, `json_group_array` — parked)* | *0.467 ms* | *0.452* | *0.487* | *7%* | *14.24x* |

⚠️ **The top three rows are a statistical tie.** Their medians span 1.028–1.060 ms
while each individually varies by 5–7% across trials, and their ordering changes
from run to run — per-row led this run, batch led two of three earlier runs. Read
them as "~1.03–1.06 ms, ~6.3x" and do not infer that one compiled variant beats
another end-to-end. The batch hydrator *is* measurably faster in isolation (123 vs
148 ns/object, §3), but 25 ns/object over 1,000 rows is 0.025 ms — well inside the
noise floor of a ~1.05 ms pipeline.

What *is* consistent across every run: the tier structure. DB-side JSON (~0.47) ≪
compiled object paths (~1.03–1.06) < dataclass-native (~1.29) ≪ reflective (~2.53)
< SQLAlchemy Core (~4.25) < SQLAlchemy ORM (~6.65). And SQLAlchemy Core is the
noisiest contender at 17% spread, so its 1.56x is the softest number in the table.

**This benchmark was checked for the ordering bias that affects the Postgres suite
([METHODOLOGY correction 2](METHODOLOGY.md#2-contender-ordering-inside-one-process))
and is not subject to it.** Running the suite in reverse order, and running each
approach in its own process, both reproduce the same figures:

| approach | forward suite | reversed suite | isolated (median of 3) |
|---|---|---|---|
| sqlom compiled (batch) | 1.019–1.131 | 1.032 | 1.050 |
| sqlom compiled (per-row) | 1.066–1.095 | 1.056 | 1.050 |
| dataclass + passthrough | 1.049–1.073 | 1.091 | 1.111 |
| sqlom reflective | 2.517–2.550 | 2.431 | 2.536 |
| SQLAlchemy Core | 3.919–4.129 | 4.060 | 3.819 |
| SQLAlchemy ORM | 6.629–6.802 | 6.505 | 6.552 |

```bash
python3 benchmarks/bench_sqlite.py --rows 200000 --limit 1000 --iterations 300 --warmup 30 --repeat 5
python3 benchmarks/bench_sqlite.py ... --reverse                    # order check
python3 benchmarks/bench_sqlite.py ... --only reflective --repeat 3  # isolation check
```

Artifact: [`results/sqlite_latest.json`](../benchmarks/results/sqlite_latest.json)
(all 5 trials per approach), [`results/sqlite_order_check.txt`](../benchmarks/results/sqlite_order_check.txt)

---

## 2. Where a single request's time goes

`benchmarks/profile_stages.py`, compiled pipeline, 1,000 rows:

| stage | cost | share |
|---|---|---|
| sqlite query + row fetch | 0.63 ms | **65%** |
| orjson serialization | 0.19 ms | 20% |
| hydration into objects | 0.12 ms | **12%** |

Two follow-ups that bound the remaining headroom:

- **`conn.execute(...)` alone costs 0.005 ms.** Essentially the whole 65% is the
  driver materializing 4,000 C values into Python `int`/`str` objects. An object
  path fundamentally needs those objects.
- **Streaming the cursor instead of `fetchall()` is a wash** — 0.715 vs 0.706 ms.
  `fetchall()` is already a C-level loop.

So driving hydration to zero would still leave ~85% of a request's cost. This is a
statement about **latency**; see §4 for why it does not describe throughput.

---

## 3. Component micro-benchmarks

### Hydration, 1,000 rows, isolated

| strategy | ns/object | speedup |
|---|---|---|
| reflective `hydrate()` loop (`setattr`) | 601 | 1.0x |
| compiled per-row (`row[i]` subscripts) | 148 | 4.1x |
| compiled batch (tuple unpacked by `for`) | **123** | **4.9x** |

### orjson serialization, list of 1,000 objects

Measured at two field counts because the penalty has a large fixed per-object
component; ratios are vs. a plain dict at the same width.

| shape | 4 fields | 10 fields | vs. dict (4f) |
|---|---|---|---|
| plain `dict` (orjson native) | 68 ns | 137 ns | 1.00x |
| `@dataclass` no slots (native fast path) | 91 ns | 182 ns | 1.34x |
| `@dataclass(slots=True)` + `OPT_PASSTHROUGH_DATACLASS` + compiled hook | 182 ns | 377 ns | 2.67x |
| `@dataclass` no slots + `OPT_PASSTHROUGH_DATACLASS` + compiled hook | 259 ns | 559 ns | 3.79x |
| `@dataclass(slots=True)` (orjson native **fallback**) | 421 ns | 672 ns | **6.16x** |

The last row is the trap — see [FINDINGS.md](FINDINGS.md#the-orjson-dataclass-trap).

### Memory per hydrated instance

| model | bytes/object |
|---|---|
| `ModelMeta` (slots) | 72 |
| `@model` dataclass (slots) | 72 |
| dataclass without slots | 113 |

---

## 4. Concurrent load against real PostgreSQL

`benchmarks/bench_pg_load.py` — closed loop: `c` worker tasks issue request-shaped
queries back-to-back for 4 s. A "request" is query + materialize + produce JSON
bytes. Pool size 10, 100 rows/request, byte-identical output enforced.

**All figures below are isolated** (one contender per process, median of 3 trials).
Combined-suite numbers are biased by contender ordering — see
[METHODOLOGY.md](METHODOLOGY.md#2-contender-ordering-inside-one-process).

### 4a. The client is single-core by nature

A single asyncio event loop under the GIL saturates exactly one core and cannot
use more. Measured `cpu_utilization` (CPU-seconds per wall-second) is
**0.91–1.00 for every contender in every configuration**.

Consequently, giving one client process a second core does not help — it *hurts*,
because the loop migrates and loses cache locality:

| sqlom, c=8 | CPU ms/req | throughput |
|---|---|---|
| client pinned to 1 core | **0.217** | 4560 rps |
| client pinned to 2 cores | 0.308 | 3168 rps |

### 4b. Ratio vs. Postgres core count (client pinned to 1 core)

| Postgres cores | sqlom | async ORM | ratio |
|---|---|---|---|
| 1 | 4560 rps (0.217 ms CPU) | 741 rps (1.346) | 6.15x |
| 2 | 4111 rps (0.242) | 672 rps (1.484) | 6.12x |
| 3 | 3599 rps (0.278) | 701 rps (1.426) | 5.13x |

The ratio is essentially independent of Postgres's core count: **~6x, with
5.1–6.2x as honest spread.** These are small indexed reads served from shared
buffers, so Postgres sustains ~4,500/s on well under one core and the *client* is
the binding constraint in every configuration.

Note that more Postgres cores slightly *slow the client* (0.217 → 0.278 ms
CPU/req) despite the client having its own dedicated core. That is shared L3 and
memory bandwidth, not CPU time.

### 4c. Full field, client 2 cores / Postgres 2 cores

Kept because it is the only configuration where every contender was measured
isolated at two concurrency levels.

| approach | c=1 rps | c=1 CPU | c=8 rps | c=8 CPU |
|---|---|---|---|---|
| raw asyncpg + `dict(Record)` | 1309 | 0.433 | 3877 | 0.257 |
| raw asyncpg + codegen dict *(floor)* | 1237 | 0.441 | 4039 | 0.247 |
| **sqlom (compiled)** | **848** | **0.604** | **3168** | **0.308** |
| SQLAlchemy async Core | 730 | 0.982 | 1112 | 0.889 |
| SQLAlchemy async ORM | 472 | 1.593 | 759 | 1.310 |

Against a hand-written no-object loop, sqlom costs **+10% CPU** with cores shared
(0.237 vs 0.215 at 4 cores unpinned) and **+25%** here. Cheap for what it buys,
but not free — and note the hand-written baselines beat it, as they must.

### 4d. Process scaling

Processes are the only way a GIL-bound mapper uses more cores. Postgres pinned to
cores 2,3; each worker pinned to its own core.

| sqlom workers | per-worker | total |
|---|---|---|
| 1 | 4398 rps | 4398 rps |
| 2 | 4415, 4324 rps | **8739 rps (1.99x)** |

Per-worker throughput is unchanged when a second worker joins, so the practical
reading is **cores required for a target throughput**: ~4,400 req/s needs 1 core
with sqlom and roughly 6 with SQLAlchemy's async ORM.

```bash
python3 benchmarks/bench_pg_load.py --seed-only
bash benchmarks/pin_and_run.sh --db-cores 1,2,3 --client-cores 0 -- \
     --only sqlom --concurrency 8 --duration 4 --repeat 3
```

Artifacts: [`results/pg_load_100rows.json`](../benchmarks/results/pg_load_100rows.json),
[`pg_load_1000rows.json`](../benchmarks/results/pg_load_1000rows.json),
[`core_sweep_1core_client.txt`](../benchmarks/results/core_sweep_1core_client.txt),
[`multiprocess_scaling.txt`](../benchmarks/results/multiprocess_scaling.txt),
[`isolated_*.txt`](../benchmarks/results/)

---

## 5. What none of this shows

- **No HTTP layer.** The load benchmark drives the data layer directly. A real
  FastAPI/uvicorn stack adds routing, validation and ASGI overhead per request
  that would compress every ratio here. "Requests/sec for your API" is unmeasured.
- **Postgres is barely loaded.** Small indexed reads from shared buffers. A query
  heavy enough to make the *database* the bottleneck would compress every ratio
  toward 1.0 — arguably the more common production shape, and untested.
- **Localhost, not a network.** No real RTT, so the latency-bound regime where slow
  client code hides behind network wait is untested. That regime should shrink
  these ratios.
- **4 vCPU; process scaling verified only to 2 workers.** 1 → 2 is linear (1.99x);
  16 or 64 workers against one Postgres is unmeasured.
- **Narrow shape.** One flat 4-column table of small ints and short strings. No
  joins, nested shaping, wide rows, large text/JSONB, writes or transactions.
- **No sampling profiler.** §2 is wall-clock timing of isolated stages, not
  `py-spy`/`pyinstrument`; it attributes cost per stage, not per function.
- **Not a production system.** No test suite, not packaged, never deployed.
