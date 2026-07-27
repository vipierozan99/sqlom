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

⚠️ **Two corrections were applied to this section after review.** Both are detailed
below; read them before quoting anything here.

1. **The comparison was unfair, in sqlom's favour.** SQLAlchemy's connection and
   `Session` were created *inside* the timed closure while the sqlom paths got a
   connection made once outside it, so SQLAlchemy was charged for pool checkout
   that sqlom never paid. Fixed; worth **8% of the Core ratio**.
2. **The machine got ~1.35x slower** between the original run and the re-run, so
   the absolute milliseconds below are all higher than previously published. This
   is not the fix — the pre-change tree reproduces today's numbers, not the old
   ones.

**Median of 5 trials**, with the observed spread across trials:

| approach | median | min | max | spread | vs. ORM |
|---|---|---|---|---|---|
| `@model` dataclass + `OPT_PASSTHROUGH_DATACLASS` | 1.393 ms | 1.371 | 1.530 | 11% | 6.55x |
| sqlom compiled, batch hydrator | 1.543 ms | 1.440 | 1.581 | 9% | 5.92x |
| sqlom compiled, per-row hydrator | 1.553 ms | 1.452 | 1.597 | 9% | 5.88x |
| `@model` dataclass, orjson native path | 1.896 ms | 1.787 | 2.407 | 33% | 4.82x |
| sqlom reflective (`hydrate()` + `as_dict()`) | 3.559 ms | 3.460 | 4.040 | 16% | 2.57x |
| SQLAlchemy 2.0 Core | 5.240 ms | 5.126 | 5.349 | 4% | 1.74x |
| SQLAlchemy 2.0 ORM | 9.132 ms | 8.834 | 9.177 | 4% | 1.00x (baseline) |
| *(DB-side JSON, `json_group_array` — parked)* | *0.561 ms* | *0.543* | *0.570* | *5%* | *16.28x* |

⚠️ **The top three rows are a statistical tie.** They span 1.393–1.553 ms while
each individually varies 9–11% across trials, and their ordering changes from run
to run. Read them as one tier, ~6x the ORM, and do not infer that one compiled
variant beats another end-to-end. The batch hydrator *is* measurably faster in
isolation (123 vs 148 ns/object, §3), but 25 ns/object over 1,000 rows is 0.025 ms
— well inside the noise floor.

What *is* consistent across every run: the tier structure. DB-side JSON ≪ compiled
object paths < dataclass-native ≪ reflective < SQLAlchemy Core < SQLAlchemy ORM.

### Correction 1: the connection was hoisted for sqlom but not for SQLAlchemy

The sqlom runners received a `sqlite3.Connection` created once before timing, while
`run_sqlalchemy_core` and `run_sqlalchemy_orm` opened `engine.connect()` /
`Session(engine)` **inside** the timed function. SQLAlchemy therefore paid a pool
checkout on every measured iteration that sqlom did not, and that cost was reported
as object-mapping overhead. Found in review; it is the same class of error as
[correction 1](METHODOLOGY.md#1-comparing-different-payloads-inflated-35x-26x).

Sizing it needed a paired instrument, not two suite runs: the effect is smaller than
the ±14% (Core) and ±27% (ORM) spread between runs. `benchmarks/ab_setup_cost.py`
times all variants in one process, alternating between them each round:

| | 100 rows | 1000 rows |
|---|---|---|
| setup cost charged only to Core | 76 µs (13.8% of Core) | 466 µs (9.0%) |
| setup cost charged only to the ORM | 26 µs (2.5%) | −93 µs (≈0, noise) |
| **sqlom vs Core** | 4.74x → **4.16x** (−12.1%) | 4.01x → **3.68x** (−8.3%) |
| **sqlom vs ORM** | 8.08x → **7.88x** (−2.5%) | 6.48x → **6.54x** (+1.0%) |

The A/B's "before" figure at 1000 rows — 4.01x vs Core — reproduces the previously
published 4.01x exactly, which is what makes it trustworthy as the instrument: it
recovers the old number from the old code path, then isolates one variable.

**So the Core ratio was overstated by ~8% and the ORM ratio was not affected.** The
ORM's own setup is a `Session` on an already-warm pool, which is cheap next to
hydrating 1,000 instances.

The same script also measures the *opposite* mistake, because it is larger and more
tempting: hoisting the `Session` out of the loop as well. Its identity map then
survives between iterations, so every iteration after the first returns
already-hydrated instances and skips the work being measured — worth **12.9%** in
the ORM's favour at 100 rows. A per-request `Session` bound to a live connection is
the only variant that is both realistic and measures hydration every time.

### Correction 2: absolute times moved with the machine, not with the code

Re-running after the fix gave ~1.35x higher absolutes for *every* contender —
including the sqlom paths, whose timed code the fix does not touch. Rather than
assume, the entire pre-change tree (library and harness, via `git stash`) was
checked out and re-run on the same box:

| contender | published (earlier box) | pre-change tree, today | post-fix, today |
|---|---|---|---|
| sqlom compiled (batch) | 1.060 ms | 1.389 ms | 1.543 ms |
| SQLAlchemy Core | 4.252 ms | 5.196 ms | 5.240 ms |
| SQLAlchemy ORM | 6.647 ms | 9.127 ms | 9.132 ms |

The pre-change tree reproduces *today's* numbers, not the published ones. The box
became ~1.35x slower between sessions — ordinary for shared cloud CPU, and the
reason **absolute microseconds in this document must not be compared across
sections**: §2, §3 and §7 were recorded on the earlier box. Ratios within a section
are comparable; milliseconds between sections are not.

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

The order-check table above was recorded on the earlier box, before correction 1.
Its purpose is to show the suite is not *order*-biased, which the connection fix
does not affect — but its absolute values belong to the earlier box, per correction 2.

```bash
python3 benchmarks/bench_sqlite.py --rows 200000 --limit 1000 --iterations 300 --warmup 30 --repeat 5
python3 benchmarks/bench_sqlite.py ... --reverse                    # order check
python3 benchmarks/bench_sqlite.py ... --only reflective --repeat 3  # isolation check
python3 benchmarks/ab_setup_cost.py --limits 100,1000 --rounds 7     # correction 1
```

Artifacts: [`results/sqlite_latest.json`](../benchmarks/results/sqlite_latest.json)
(all 5 trials per approach), [`results/sqlite_order_check.txt`](../benchmarks/results/sqlite_order_check.txt),
[`results/sqlite_setup_cost_ab.txt`](../benchmarks/results/sqlite_setup_cost_ab.txt)
(correction 1), [`results/sqlite_box_drift.txt`](../benchmarks/results/sqlite_box_drift.txt)
(correction 2)

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

Artifacts: [`core_sweep_1core_client.txt`](../benchmarks/results/core_sweep_1core_client.txt),
[`multiprocess_scaling.txt`](../benchmarks/results/multiprocess_scaling.txt),
[`isolated_*.txt`](../benchmarks/results/)

---

## 5. Profiled run: client 1 core, Postgres 2 cores

`benchmarks/profile_pg.py --pin 0:2,3 --compare --sampler`. 100 rows/request, pool
10, saturated at c=8. Two profilers because they disagree usefully: cProfile with a
`process_time` timer (deterministic, attributes **CPU** not wall time, but inflates
call-heavy code ~4.6x) and pyinstrument sampling (low distortion). Wall ≈ CPU at
this concurrency, so the sampler's wall-clock tree reads as CPU.

Artifact: [`results/profile_pg_1core.txt`](../benchmarks/results/profile_pg_1core.txt)

### 5a. Latency-bound vs throughput-bound, in one measurement

| | sqlom | async ORM |
|---|---|---|
| sequential (c=1), wall/req | 0.447 ms | 1.719 ms |
| sequential, CPU/req | 0.289 ms | 1.400 ms |
| sequential utilization | 0.65 | 0.81 |
| **waiting on Postgres** | **35% of wall** | **19% of wall** |
| saturated (c=8), CPU/req | 0.225 ms | 1.293 ms |
| saturated utilization | 1.00 | 1.00 |
| throughput | 4428 rps | 773 rps |

A lone sqlom request spends 35% of its wall time waiting on Postgres; the ORM only
19%, not because its queries are faster but because its Python work is so much
larger that the same wait is a smaller share. At c=8 that wait is fully hidden
behind other requests, utilization hits 1.00 for both, and CPU/req alone sets
throughput.

### 5b. Where sqlom's 0.225 ms/req goes (sampled)

| component | share |
|---|---|
| asyncio event loop dispatch + protocol/TLS, outside the request coroutine | **38%** |
| asyncpg `Connection.fetch` | 19% |
| asyncpg pool acquire/release | **15%** |
| sqlom `_hydrate_all` (generated hydrator) | 11% |
| `orjson.dumps` | 7% |
| sqlom `_default` (generated dict builder) | 4% |

**sqlom's own generated code is ~15% of client CPU.** Roughly 72% is asyncio plus
asyncpg plus pool bookkeeping, and pool acquire/release alone (15%) costs as much as
hydration. That puts a hard ceiling on further mapper micro-optimization: making
hydration free would buy ~15%, whereas the event loop and pool handling are the
larger targets.

(cProfile's instrumented view puts sqlom codegen at 29% rather than 15%. The
discrepancy is exactly what instrumentation bias predicts: `_default` is called
80,000 times per 800 requests, so per-call overhead lands hardest on it. Trust the
sampler for absolute shares, cProfile for call counts.)

### 5c. Where the ORM's extra 1.07 ms/req goes

Shares rescaled onto each side's measured CPU/req, so columns are comparable:

| library | sqlom | async ORM | delta |
|---|---|---|---|
| SQLAlchemy ORM internals | 0.000 | 0.634 | **+0.634** |
| attribute reads while building the payload dict | 0.005 | 0.256 | **+0.251** |
| SQLAlchemy engine / pool / asyncio ext | 0.000 | 0.098 | +0.098 |
| SQLAlchemy SQL / util / dialect | 0.000 | 0.071 | +0.071 |
| asyncio / loop | 0.094 | 0.168 | +0.074 |
| asyncpg | 0.027 | 0.033 | +0.006 |
| orjson | 0.016 | 0.003 | −0.013 |
| sqlom (codegen) | 0.061 | 0.000 | −0.061 |
| **TOTAL CPU ms/req** | **0.225** | **1.293** | **+1.068** |

The top ORM frames name the mechanism precisely — this is the identity-map and
instrumentation cost the design set out to avoid, now demonstrated rather than
asserted (per 800 requests × 100 rows):

| calls | self ms | frame |
|---|---|---|
| 40,000 | 194.6 | `orm/loading.py:_instance` |
| 162,400 | 185.6 | `builtins.getattr` |
| 40,000 | 136.2 | `orm/state.py:InstanceState.__init__` |
| 160,000 | 113.8 | `orm/attributes.py:InstrumentedAttribute.__get__` |
| 40,000 | 113.6 | `orm/instrumentation.py:new_instance` |
| 40,000 | 63.7 | `orm/loading.py:_populate_full` |

Every row gets an `InstanceState`, goes through `new_instance`, is registered in the
identity map, and then every field read goes through `InstrumentedAttribute.__get__`
— four `getattr`s per row become 160,000 instrumented descriptor calls.

⚠️ Note that **0.251 ms/req of the ORM's cost is the harness's own dict
comprehension** (`{n: getattr(u, n) for n in names}`), not SQLAlchemy library code.
It is charged to the ORM here because reading attributes off ORM instances is
unavoidable if you want JSON out, and those reads are what hit the instrumented
descriptors. A different serialization strategy would shift this term.

### 5d. The loopback connection was using TLS

Discovered while reading the profile: `sslproto.py` and `_ssl._SSLSocket.read`
appear in sqlom's top frames. This Postgres has `ssl = on`, and asyncpg's default
`sslmode=prefer` negotiates **TLSv1.3 / AES-256-GCM even over 127.0.0.1**.

| sqlom, c=8 | CPU/req | throughput |
|---|---|---|
| default (TLS on) | 0.225 ms | 4428 rps |
| `?sslmode=disable` | **0.180 ms** | **5536 rps** |

TLS is **~20% of client CPU and ~25% of throughput** here. Every Postgres figure in
§4 includes it. Both contenders pay it, so the *ratios* are essentially unaffected,
but the absolute rps figures understate what a unix-socket or non-TLS local
connection would deliver. Nothing in §4 has been restated, because the comparison it
makes is still apples-to-apples — but the absolute numbers are not the ceiling.

```bash
python3 benchmarks/profile_pg.py --pin 0:2,3 --compare --sampler
python3 benchmarks/profile_pg.py --pin 0:2,3 --only sqlom \
    --dsn "postgresql://postgres:postgres@127.0.0.1:5432/sqlom_bench?sslmode=disable"
```

---

## 6. Acting on the profile: 2.4x more throughput outside the mapper

`benchmarks/optimize_pg.py`. §5 said sqlom's own code is ~15% of client CPU, so the
remaining throughput has to come from the other 85%. Each flag targets one component
the profile named. Client on core 0, Postgres on cores 2,3, c=8, 100 rows/request,
median of 3, one configuration per process.

| config | rps | vs. baseline | CPU ms/req | CPU saved |
|---|---|---|---|---|
| baseline | 4724 | 1.00x | 0.2107 | — |
| `--uvloop` | 5228 | 1.11x | 0.1908 | 9% |
| `--no-tls` | 5440 | 1.15x | 0.1831 | 13% |
| `--no-reset` | 6148 | **1.30x** | 0.1626 | 23% |
| `--hold-conn` | 7995 | **1.69x** | 0.1250 | 41% |
| `--uvloop --no-reset` | 6826 | 1.44x | 0.1464 | 31% |
| `--uvloop --no-reset --no-tls` | 7593 | 1.61x | 0.1315 | 38% |
| `--uvloop --hold-conn --no-tls` | **11132** | **2.36x** | 0.0896 | 57% |

Artifact: [`results/optimize_stack.txt`](../benchmarks/results/optimize_stack.txt)

### Why the pool is slow: it sends a second query

Not bookkeeping — an extra server round trip. `asyncpg`'s
`PoolConnectionHolder.release()` calls `Connection.reset()`, which executes:

```sql
SELECT pg_advisory_unlock_all();
CLOSE ALL;
UNLISTEN *;
RESET ALL;
```

Verified by reading `_protocol.queries_count`, which is how asyncpg itself counts:

| | queries sent per request |
|---|---|
| `pool.acquire()`, default reset | **2.01** |
| `pool.acquire()`, `reset=` no-op | 1.00 |
| held connection, no pool | 1.00 |

So a pooled request costs **two** round trips: yours, and a cleanup statement on
release. `create_pool(..., reset=<no-op coroutine>)` keeps the in-process protocol
reset (transaction rollback, listener clearing) and drops the SQL — worth 1.30x here.

⚠️ **This is a behavioural tradeoff, not a free win.** Skipping `RESET ALL` lets
session state leak between requests: `SET` outside a transaction, temp tables, open
cursors, `LISTEN` registrations, advisory locks. It is safe only if handlers never
touch session state, and it is the kind of thing that works until one handler runs a
`SET`. Prefer it to `--hold-conn`, which buys more (1.69x) but makes client
concurrency and DB connection count the same number — usually not deployable.

The gap between `--no-reset` (1.30x) and `--hold-conn` (1.69x) is the pool's
remaining Python-side cost: `PoolAcquireContext` construction, holder juggling, and
the futures involved in acquire/release. Removing the round trip does not remove
that.

### The rest of the ranking

- **uvloop (1.11x)** is the cheapest to adopt — two lines, no semantic change — but
  the smallest win of the three, which the profile predicted: the 38% "event loop"
  bucket includes TLS and protocol work that uvloop does not eliminate.
- **TLS (1.15x)** is an artifact of this setup (`ssl = on` plus asyncpg's
  `sslmode=prefer`), not something a library can fix. On a remote database you want
  TLS and would pay it; for a local socket you would not.
- **They compose sub-additively.** Individually 1.30 × 1.15 × 1.11 = 1.66x predicted;
  measured 1.61x for the deployable trio, 2.36x with connection holding. Each removes
  work the others also touch.

### What this means for the mapper

The best deployable configuration reaches 7593 rps at 0.13 ms CPU/req. sqlom's
generated code was ~15% of the *baseline's* 0.21 ms — roughly 0.032 ms — so it is now
close to a quarter of the remaining budget. **The order of work is: fix the pool,
adopt uvloop, then optimize the mapper** — and by then the mapper is the largest
single remaining item, which it was not before.

Against the async ORM's 773 rps in the same configuration, the best deployable stack
is ~9.8x and the connection-holding upper bound ~14.4x. Those numbers belong to the
*stack*, not to sqlom: the ORM would gain from uvloop and the pool fix too, and this
has not been measured for it. **Do not read 14.4x as a mapper comparison.**

---

## 7. Profiled sqlite run: the mapper with transport removed

`benchmarks/profile_sqlite.py`. §5 found sqlom's own code to be only ~15% of client
CPU against Postgres, with 38% in the asyncio loop, 19% in the asyncpg fetch and 15%
in pool acquire/release. All three are **transport** — they exist because the database
is another process on the far side of a socket. sqlite deletes them: in-process C
driver, no pool, no TLS, no event loop. What remains is the cost sqlom is actually
responsible for.

Single-threaded, pinned to core 0, 100 rows/request from a 200,000-row table — the
same request shape as §5.

| | sqlom | SQLAlchemy Core | SQLAlchemy ORM |
|---|---|---|---|
| CPU ms/req | **0.100** | 0.437 | 0.742 |
| req/s (1 thread) | **9997** | 2286 | 1346 |
| vs. ORM | **7.4x** | 1.70x | 1.0x |
| utilization | 1.00 | 1.00 | 1.00 |

Utilization is 1.00 with no event loop involved: a synchronous sqlite call is CPU, not
I/O wait, so there is nothing to overlap and no concurrency dimension to sweep.

Artifact: [`results/profile_sqlite.txt`](../benchmarks/results/profile_sqlite.txt)

### Transport is more than half the Postgres cost

| sqlom, 100 rows/request | CPU/req |
|---|---|
| Postgres (asyncpg, pooled, TLS, c=8, 1 core) | 0.215 ms |
| sqlite (in-process) | 0.100 ms |
| **difference = transport** | **0.115 ms (53%)** |

Loop, socket, protocol, TLS and pool together cost slightly *more* than an entire
sqlite request. That is the same conclusion §6 reached from the other direction: the
2.36x found there came from attacking this 0.115 ms, not the mapper.

### Where sqlom's 0.100 ms goes — and where the profilers disagree

| component | cProfile | sampled |
|---|---|---|
| sqlom generated code (`_hydrate_all` + `_default`) | 65% | ~56% |
| sqlite3 driver (`Cursor.fetchall`) | 16% | ~37% |
| `orjson.dumps` | 17% | ~6% (undercounted) |

**The two disagree, and the disagreement is informative rather than a defect.** Each
has a blind spot, in opposite directions:

- **cProfile inflates sqlom's share.** Its overhead is per *call*, and `_hydrate_all`
  triggers ~200 instrumented builtin calls per request (100 × `object.__new__`,
  100 × `list.append`) while `Cursor.fetchall` is a single call. So the driver looks
  cheaper than it is.
- **pyinstrument undercounts orjson.** It samples Python frames, so it cannot see
  inside a C extension; `orjson.dumps`'s work is charged to the Python frame that
  called it. cProfile measures that C function directly, and `orjson.dumps` is one
  call per request so barely inflated — its 17% is the more credible figure.

Reconciled, the honest reading is roughly **half the time in sqlom's generated code,
~30% in the sqlite3 driver, ~15% in orjson.** Trust the sampler for the
hydrate-versus-fetch balance and cProfile for anything implemented in C.

Exact call counts (cProfile, 800 requests × 100 rows) show where the shape comes from:

| calls | self ms | frame |
|---|---|---|
| 800 | 94.8 | `_hydrate_all` (generated) |
| 800 | 57.2 | `orjson.dumps` |
| 80,000 | 51.5 | `_default` (generated, once per row) |

`_default` runs once per *row* because orjson calls back into Python for every
object. That is the cost of not being a stdlib dataclass — and the reason
`OPT_PASSTHROUGH_DATACLASS` matters if you use the `@model` style
([FINDINGS](FINDINGS.md#the-orjson-dataclass-trap)).

### What SQLAlchemy spends it on here

With transport gone, both SQLAlchemy variants remain 4.4x and 7.4x sqlom's cost, and
the profile says where:

| | share of own CPU |
|---|---|
| **Core**: `sqlalchemy/engine` + pool | **74.7%** |
| Core: sqlite3 driver | 5.2% |
| **ORM**: `sqlalchemy/orm` internals | **59.0%** |
| ORM: attribute reads building the payload dict | 23.9% |
| ORM: sqlite3 driver | 2.6% |

Core's cost is almost entirely its own engine/connection layer, not SQL compilation
(1.8%) and certainly not the driver. For the ORM it is identity-map and
instrumentation work plus the instrumented attribute reads needed to get values back
out — the same mechanism §5c named against Postgres, now with no transport to hide
behind.

```bash
taskset -c 0 python3 benchmarks/profile_sqlite.py --compare --sampler
```

---

## 8. What is left in the sqlite path: essentially nothing

`benchmarks/optimize_sqlite.py`. §7 attributed ~50% of the sqlite request to sqlom's
generated code, so this attacks each named cost in turn. 100 rows/request, single
core, median of 5 × 3000 requests, byte-identical output enforced.

| variant | ms/req | req/s | vs. baseline |
|---|---|---|---|
| baseline (objects, N orjson callbacks) | 0.1021 | 9795 | 1.00x |
| `row_factory` (per-row loop inside sqlite3's C code) | 0.1032 | 9688 | **0.99x** |
| + reuse one cursor | 0.1002 | 9983 | 1.02x |
| + tuple-index bool instead of `bool()` | 0.0991 | 10088 | 1.03x |
| + build dicts in one pass (0 orjson callbacks) | 0.0984 | 10165 | 1.04x |
| STACKED (cursor + tuple-bool + one-pass) | 0.0984 | 10165 | **1.04x** |
| no-slots dataclass, orjson native path | 0.0944 | 10588 | 1.08x |
| no objects at all (rows → dicts) | 0.0854 | 11713 | 1.20x |
| *FLOOR: fetch only, no JSON* | *0.0654* | *15286* | *1.56x* |

Artifact: [`results/optimize_sqlite.txt`](../benchmarks/results/optimize_sqlite.txt)

**Every micro-optimization is at or below the noise floor**, and they do not compose:
stacking all three yields 1.04x, the same as the best one alone, because they overlap.
Across smaller runs the same variants measured 0.99x–1.03x, i.e. no reliable effect.
Two are worth naming:

- **`row_factory` made it slower.** Moving the per-row loop into sqlite3's C fetch
  loop sounds like it should win, but it trades one interpreted loop for one Python
  *call* per row, and the call is more expensive than the loop iteration it replaces.
- **Eliminating orjson's per-row callback bought ~1.04x, not the ~15% its share
  suggested.** `_default` fires 80,000 times per 800 requests, which looks alarming;
  replacing it with a single-pass comprehension shows most of that cost is the dict
  construction itself, not the Rust→Python transition.

### The decomposition explains why

| component | ms/req | share |
|---|---|---|
| sqlite3 fetch — driver creating Python values | 0.0654 | **64%** |
| JSON serialization | 0.0200 | 20% |
| **object materialization** | **0.0167** | **16%** |

Objects cost 0.0167 ms of a 0.1021 ms request. Even a *perfect* mapper — free
hydration, free serialization hook — could only reach the no-objects line at 1.20x,
and the floor is 1.56x. **There is no meaningful optimization left inside the object
mapper for this shape.**

### The levers that remain are all outside the mapper's contract

1. **Return fewer or narrower rows** (64% of the request is the driver materializing
   Python values — the only way to reduce it is to ask for less).
2. **Skip objects for JSON-only endpoints** — 1.20x, but then it is not a mapper.
3. **Give up `__slots__`** for orjson's native path — 1.08x, at +55% memory per
   instance (113 vs 72 bytes) and the
   [attribute-leak caveat](FINDINGS.md#rejected-with-reasons).
4. **Push shaping into SQL** — the parked `json_agg`/`json_group_array` path, ~2.2x
   over the best object path ([§1](#1-sqlite-micro-benchmark-single-request-latency)),
   because it skips objects *and* Python-side JSON at once. Comfortably the largest
   remaining lever, and the reason it stays implemented rather than deleted.

```bash
taskset -c 0 python3 benchmarks/optimize_sqlite.py --repeat 5
```

---

## 9. Two hypotheticals: a native object builder, and Rust

Asked whether the driver constructing slotted objects directly, or a Rust
implementation, would help. Both are bounded analytically
(`benchmarks/estimate_ceilings.py`) and one is testable outright
(`benchmarks/compare_rust_driver.py`), because a Rust Postgres driver that builds
Python classes from Rust already exists.

Artifact: [`results/rust_and_ceilings.txt`](../benchmarks/results/rust_and_ceilings.txt)

### The sqlite request, decomposed into what is and isn't removable

Varying column count at fixed rows separates creating Python *values* from per-row
overhead; varying row count separates per-statement cost. 100 rows/request:

| component | µs/req | note |
|---|---|---|
| creating 400 Python values (4 cols × 100 rows) | 43.8 | **109 ns per value** — irreducible if the API returns Python data |
| row tuples + statement stepping | 16.9 | removable by a native builder |
| statement execute/prepare | 5.2 | irreducible |
| Python hydration loop | 13.9 | removable by a native builder |
| `orjson.dumps` + per-row `_default` | 20.0 | orjson is already Rust |
| *sum of the itemized stages* | *99.8* | |
| **measured full pipeline** | **104.0** | the extra 4.2 µs is composition cost no isolated stage captures |

⚠️ **Corrected after review.** That last row used to be missing from the reasoning,
and it mattered: the floor below was built by *summing* the surviving stages
(bottom-up) and then divided into the separately *measured* pipeline (top-down), so
the 4.2 µs nobody could attribute was silently credited to the speedup. Published
**1.51x**; the correct figure on the same data is **1.42x**. Fixed in
`estimate_ceilings.py`, which now subtracts from the measured total and prints the
residual instead of absorbing it.

### Ceiling 1 — driver builds the slotted objects directly

Removes the row tuple (16.9 µs) and the Python hydration loop (13.9 µs). Cannot
remove value creation (43.8), the statement (5.2), JSON (20.0), or the 4.2 µs of
composition cost.

**Optimistic floor 73.2 µs vs 104.0 µs → ≤1.42x** on the original data. Re-running
the corrected script on the current box gives 98.3 vs 134.9 µs → **≤1.37x** (see
§1 correction 2 for why the absolutes moved). Optimistic either way, because a
native builder still allocates each object and writes its slots — in C rather than
bytecode, not for free.

### Ceiling 2 — a Rust extension

orjson is *already* Rust and sqlite3/asyncpg are already C/Cython, so a Rust rewrite
can only replace the 13.9 µs hydration loop and the per-row callback inside the JSON
step. Two variants:

- **Rust mapper still returning Python objects** — bounded by ceiling 1, **≤1.42x**.
  Creating 400 Python values is 42% of the request and is unavoidable the moment the
  API hands back objects whose fields are Python values.
- **Rust returning JSON bytes, no Python objects at all** — then value creation
  vanishes too. Measured anchor for exactly that shape (sqlite `json_group_array`,
  all work below Python): 54.5 µs vs 104.0 → **1.91x** (2.13x on the re-run). Note
  this is *already reachable today in SQL with no Rust written*, and is the parked
  `json_agg` path.

### And the empirical test: a Rust driver is available, and it is slower

[`psqlpy`](https://github.com/psqlpy-python/psqlpy) is built on Rust's
tokio-postgres, and `QueryResult.as_class()` constructs Python instances **from
Rust** — precisely ceiling 1. It works with sqlom's `@model` dataclasses unchanged.

Two confounds were measured and controlled first, not assumed: with
`log_statement=all`, asyncpg's default pool emits **6 log lines per request** (query +
its multi-statement `RESET ALL`) against psqlpy's **2**; and asyncpg negotiates TLS
here while psqlpy connects in the clear (`pg_stat_ssl`). The fair baseline therefore
runs asyncpg with `reset=` no-op and `sslmode=disable`.

Client on core 0, Postgres on cores 2,3, c=8, median of 3, byte-identical output:

| contender | rps | CPU ms/req | vs. fair baseline |
|---|---|---|---|
| asyncpg held conn + sqlom | 8805 | 0.1134 | 1.32x |
| **asyncpg fair + sqlom** (baseline) | **6688** | **0.1495** | **1.00x** |
| asyncpg default + sqlom (TLS, RESET) | 4612 | 0.2167 | 0.69x |
| psqlpy held conn → dicts | 4769 | 0.2097 | 0.71x |
| psqlpy pool → dicts | 3977 | 0.2511 | 0.59x |
| psqlpy pool, `prepared=True` → dicts | 3965 | 0.2519 | 0.59x |
| **psqlpy held conn → RUST-BUILT objects** | **3787** | **0.2640** | **0.57x** |

**The Rust driver is 1.4–1.8x slower, and having Rust build the objects is slower
still than psqlpy's own dict path** (0.57x vs 0.71x). Two readings, and the second
matters more:

- This is **not** "Rust is slower than Python". It is *this* Rust driver against
  asyncpg, which is a mature, heavily tuned Cython implementation with binary-protocol
  codecs and prepared-statement caching. Implementation maturity dominates language
  choice by a wide margin here.
- `as_class` being slower than psqlpy's own dicts suggests it constructs via
  `cls(**kwargs)` — materializing a kwargs dict and invoking `__init__` per row —
  rather than writing slots directly. **So "the driver builds the objects" is not
  automatically a win; it depends entirely on how**, and the ≤1.42x ceiling assumes
  the good version.

### Summary

| hypothetical | bound | evidence |
|---|---|---|
| driver builds slotted objects directly | ≤1.42x | analytical; the one real implementation achieves 0.57x |
| Rust mapper returning Python objects | ≤1.42x | same ceiling — value creation dominates |
| Rust returning JSON, no Python objects | ~1.9x | measured via `json_group_array`; already available in SQL |
| *(for comparison)* transport fixes from §6 | 1.61x deployable | measured |

Neither hypothetical beats what is already available without writing any Rust. The
2.36x in §6 came from fixing the pool and the event loop, and the ~1.9x JSON ceiling
is reachable today in SQL. **A native rewrite is the worst return on effort of the
options measured.**

```bash
taskset -c 0 python3 benchmarks/estimate_ceilings.py
taskset -c 0 python3 benchmarks/compare_rust_driver.py --repeat 3
```

---

## 10. asyncio concurrency and uvloop on the sqlite path

`benchmarks/bench_sqlite_async.py`. There was no async sqlite benchmark before
this — every sqlite measurement above is synchronous — and building one is itself
the answer: **`sqlite3` is a synchronous in-process C library, so a sqlite request
has nothing to await.** §5a measured 35% of a lone Postgres request as socket wait;
sqlite's equivalent is zero. asyncio's whole value is overlapping waits that do not
exist here.

So the question is what concurrency *costs*, and the variants separate the
mechanisms. Every worker performs an equal fixed number of requests (not a
deadline), so variants whose tasks cannot interleave still do the same total work.
Single core, 100 rows/request, median of 5, byte-identical output.

| variant | c=1 rps | c=8 rps | c=32 rps | c=8 CPU ms |
|---|---|---|---|---|
| sync (no asyncio) — reference | 8167 | 8347 | 8345 | 0.1198 |
| coroutine, no yield | 8584 | 8344 | 8332 | 0.1198 |
| coroutine, no yield + **uvloop** | 8107 | 8356 | 8433 | 0.1196 |
| coroutine, one `await` per request | 7672 | 8080 | 7783 | 0.1238 |
| coroutine, one `await` + **uvloop** | 8049 | 8136 | 7624 | 0.1229 |
| *aiosqlite (uses a THREAD)* | *4906* | *5315* | *5469* | *0.1881* |
| *aiosqlite + uvloop (THREAD)* | *5923* | *6436* | *5960* | *0.1678* |

Artifact: [`results/sqlite_async_uvloop.txt`](../benchmarks/results/sqlite_async_uvloop.txt)

### Three results

**1. Concurrency buys nothing — as it must.** Every single-threaded variant sits at
0.93x–1.05x of the synchronous reference across c=1 to c=32. There is no I/O wait to
overlap, so N tasks simply take turns on the one core. Wrapping the request in a
coroutine is nearly free (1.00x–1.05x); adding one real suspension point costs
~0.005 ms/req, consistent with a measured `await asyncio.sleep(0)` at **1.4–2.4 µs**.

**2. uvloop makes no difference here, and that is diagnostic.** 1.02x → 1.03x at
c=32 for the no-yield case; within noise everywhere. uvloop replaces libuv's *I/O
polling and socket* handling, and this path performs no socket operations at all.
Contrast §6, where uvloop was worth 1.11x against Postgres — the same swap helps
exactly to the extent that the workload touches the network. **uvloop is not a
general "faster asyncio" switch; it is a faster I/O layer.**

**3. The one thing that changes behaviour is a thread, and it costs 25-40%.**
`aiosqlite` — the usual answer to "async sqlite" — offloads each call to a worker
thread, so it is *not* single-threaded. It runs at 0.60x–0.79x because every request
now pays a thread handoff and GIL round trip to overlap a wait that was never there.
This is the one place uvloop helps (0.60x → 0.73x at c=1): thread-pool handoff goes
through the loop's `call_soon_threadsafe` and self-pipe, which uvloop implements more
efficiently. It is still well below just calling sqlite3 directly.

### What this means

For an embedded/in-process database, **the right architecture is synchronous calls
and process-level parallelism**, not asyncio. A coroutine wrapper is harmless if you
need one for API symmetry, and uvloop is harmless but pointless. Reaching for
`aiosqlite` to "make it async" makes it 25-40% slower for no benefit.

This is the mirror image of the Postgres finding: there, concurrency is essential
(4608 rps at c=8 versus 2249 at c=1, §5a) because 35% of each request is socket wait.
The determining question is not the mapper or the loop implementation — it is whether
the request waits on anything.

```bash
taskset -c 0 python3 benchmarks/bench_sqlite_async.py --repeat 5 --concurrency 1,8,32
```

---

## 11. The same matrix on Postgres: concurrency and uvloop both matter

§10 found that on the in-process sqlite path, asyncio concurrency and uvloop are both
worth nothing. Running the identical matrix against Postgres is the control, and it
inverts on both axes. Client pinned to core 0, Postgres to cores 2,3, 100 rows/request,
pool 10, median of 3.

**Throughput (rps):**

| config | c=1 | c=4 | c=8 | c=32 | c32/c1 |
|---|---|---|---|---|---|
| default pool / asyncio | 1888 | 4033 | 4373 | 4701 | **2.49x** |
| default pool / uvloop | 2325 | 5065 | 5349 | 5255 | 2.26x |
| `reset=`no-op / asyncio | 2941 | 5353 | 6063 | 5999 | 2.04x |
| **`reset=`no-op / uvloop** | 3091 | 5982 | 6462 | **6951** | 2.25x |

**Client CPU utilization** — this is the mechanism:

| config | c=1 | c=4 | c=8 | c=32 |
|---|---|---|---|---|
| default pool / asyncio | **0.64** | 0.99 | 1.00 | 1.00 |
| `reset=`no-op / uvloop | **0.60** | 0.99 | 1.00 | 1.00 |

Artifact: [`results/pg_concurrency_uvloop.txt`](../benchmarks/results/pg_concurrency_uvloop.txt)

### Concurrency is worth ~2.0–2.5x here, versus 1.00x on sqlite

At c=1 the client sits at **0.64 utilization** — a third of the core is idle waiting on
the socket. Concurrency fills exactly that gap: by c=4 utilization is 0.99 and
throughput has roughly doubled. Past that it flattens, because there is no idle left to
reclaim and the core is the constraint.

That is the whole difference from §10. sqlite's utilization is 1.00 at c=1 already —
nothing to overlap, so nothing to gain. **The concurrency payoff equals the idle
fraction, and you can read it off the utilization column before running a sweep.**

### uvloop: 1.05–1.26x here, versus noise on sqlite

| pool | c=1 | c=4 | c=8 | c=32 |
|---|---|---|---|---|
| default pool | 1.23x | **1.26x** | 1.22x | 1.12x |
| `reset=`no-op | 1.05x | 1.12x | 1.07x | **1.16x** |

Real, and it confirms §10's reading that uvloop is an I/O layer: it pays where there are
sockets and does nothing where there are none. Two qualifications:

- **The gain shrinks once the pool is fixed** (1.22x → 1.07x at c=8). uvloop and
  `reset=`no-op partly overlap — both reduce work per socket round trip, and the RESET
  statement was itself an extra round trip for uvloop to accelerate. Stacking them gives
  6462 rps rather than the 1.22 × 6063 ≈ 7400 a naive multiplication predicts.
- **§6 reported 1.11x at c=8 where this matrix shows 1.22x.** Same script, same flags,
  different session — the server was restarted between them. Treat single-figure uvloop
  claims as ±10%; the defensible statement is "1.05–1.26x on this workload".

### Side by side

| | sqlite (in-process) | Postgres (socket) |
|---|---|---|
| utilization at c=1 | 1.00 | 0.64 |
| concurrency gain (c=1 → 32) | **1.00x** | **2.0–2.5x** |
| uvloop gain | 1.02x (noise) | 1.05–1.26x |
| thread offload (`aiosqlite`) | 0.60–0.79x | n/a |
| best total, no mapper change | — | **3.68x** (1888 → 6951) |

Both axes are worth nothing on sqlite and a combined 3.68x on Postgres — from
concurrency, the pool fix and uvloop together, with the mapper untouched throughout.
**Neither is a property of asyncio or of the mapper; both track one number, the fraction
of a request spent waiting.**

```bash
taskset -c 0 python3 benchmarks/optimize_pg.py --concurrency 8 --uvloop --no-reset --repeat 3
```

---

## 12. Fixing the pool reset without changing behaviour

§6 got 1.30x by passing asyncpg `reset=` a no-op, at the cost of leaking session
state between requests. Three alternatives were tried; the fourth works.

Artifact: [`results/conditional_reset.txt`](../benchmarks/results/conditional_reset.txt)

### What does not work

**Move the reset to acquire instead of release.** Measured with `setup=` doing the
reset and `reset=` a no-op:

| | rps |
|---|---|
| reset on release (asyncpg default) | 5254 |
| reset on acquire (`setup=`) | 5384 |
| no reset at all | 6603 |

No gain. **Where the reset happens is irrelevant; that it is a separate round trip
is the entire cost.**

**Batch it with the query into one round trip.** Right in principle. The reason it
fails is specific, and an earlier revision of this section got it wrong — it claimed
"pipeline bookkeeping costs more per statement than the round trip saves". The real
cause is a **fixed per-pipeline cost**, not a per-statement one.

First, a protocol constraint: pipeline mode requires the extended protocol, which
rejects multi-statement strings (`cannot insert multiple commands into a prepared
statement`), so the 4-statement reset has to be split into 4 separately-prepared
statements. Then, measured on one psycopg3 connection, 100 rows/request, median of 5:

| variant | round trips | statements | µs/req |
|---|---|---|---|
| query only, no pipeline | 1 | 1 | 289.5 |
| **EMPTY pipeline — no statements at all** | **0** | **0** | **221.1** |
| multi-statement reset + query, sequential | 2 | 2 | 465.6 |
| `RESET ALL` + query, pipelined | 1 | 2 | 1042.4 |
| split reset + query, pipelined | 1 | 5 | 1704.3 |

The empty-pipeline row is the whole answer: **entering and leaving pipeline mode costs
221 µs, while the reset it would absorb costs 176 µs as a second round trip.** The
overhead is 1.3x the cost it is meant to remove, before a single statement is queued.
Reusing cursors instead of allocating five per request changes nothing (1636 → 1651 µs
in a side test), confirming the cost is fixed rather than per-statement.

At c=8 async single-threaded the consequence is severe:

| variant | rps | CPU ms/req |
|---|---|---|
| query only, no reset | 5923 | 0.1661 |
| multi-statement reset + query, sequential | 3309 | 0.3021 |
| split reset + query, **pipelined** | **771** | 1.2875 |

**And the flip side — when pipelining *does* pay.** psycopg3's `executemany` is itself
built on pipeline mode, and even "rides" an existing pipeline "in order to avoid
sending unnecessary Sync" (`cursor_async.py:129`), which is the library conceding that
pipeline setup is worth avoiding. It wins by amortising that one fixed cost over many
statements:

| 100 INSERTs | µs/batch | µs/statement |
|---|---|---|
| `execute()` one at a time | 113 677 | 1136.8 |
| `executemany()` (pipelined internally) | **22 871** | **228.7** |

**5.0x faster** — one pipeline setup spread across 100 statements. So the rule is:
pipelining pays when its fixed cost is amortised over many statements, and loses when
it is paid per request to save one statement's round trip. Our case is the second.

Note that `executemany` could not express this problem anyway — it runs *one* command
with a sequence of inputs, so it cannot batch a reset together with a query.

`DISCARD ALL` looked promising as a single statement covering everything, but it
includes `DEALLOCATE ALL`: it wipes the prepared-statement cache and the next request
fails with `prepared statement "_pg3_0" does not exist`. Not viable with prepared
statements.

### What works: reset only when the connection could be dirty

`DatabaseEngine(conditional_reset=True)` — now the default. `fetch_all` and
`fetch_json` execute only generated SELECTs comparing columns to bound parameters,
which cannot leave session state behind, so those connections are provably clean.
`engine.acquire()` hands out a raw connection and marks it dirty; its release pays the
full reset.

Async single-threaded, c=8, client core 0, Postgres cores 2,3, median of 3:

| variant | rps | CPU ms/req | resets/req | vs. default |
|---|---|---|---|---|
| asyncpg default (always RESET) | 3584 | 0.2773 | n/a | 1.00x |
| `reset=`no-op (**leaks session state**) | 4433 | 0.2255 | n/a | 1.24x |
| **conditional, pure sqlom traffic** | **4393** | **0.2275** | **0.00** | **1.23x** |
| conditional, 1 request in 10 uses `acquire()` | 4151 | 0.2404 | 0.10 | 1.16x |
| conditional, 1 request in 2 uses `acquire()` | 3504 | 0.2841 | 0.50 | 0.98x |
| `conditional_reset=False` | 3644 | 0.2742 | n/a | 1.02x |

**`resets/req` is `n/a`, not zero, wherever the counter does not apply.**
`DatabaseEngine.reset_count` instruments only the conditional hook, so it cannot see
asyncpg's built-in reset. An earlier version of this table printed `0.00` for
`conditional_reset=False`, which read as "this variant skips the reset" — the exact
opposite of what it does. Of the three `n/a` rows, `asyncpg default` and
`conditional_reset=False` reset on *every* release via asyncpg; only `reset=`no-op
genuinely does not reset, which is the behavioural tradeoff being measured. The
instrument is now marked rather than the number invented.

**1.23x versus the no-op's 1.24x — the full benefit, with the semantics intact.**
Correctness verified directly: 20 pure requests issue 0 resets; one `acquire()` + `SET
statement_timeout='7s'` issues exactly 1; and the next borrower reads `0`, whereas the
same sequence under `reset=`no-op leaks `9s`.

The gain naturally degrades toward the default as escape-hatch use rises, reaching
break-even around half of all requests — which is the honest and expected shape.

Worth knowing: a custom `reset=` callback does **not** disable asyncpg's protocol-level
cleanup. asyncpg still awaits `Connection._reset()` first, which rolls back an open
transaction and clears client-side listeners. Only the SQL reset is skipped, so even
the plain no-op variant never leaks an open transaction — the leak is confined to
`SET`, temp tables, server-side cursors, `LISTEN` and advisory locks.

### A 4% self-inflicted regression found on the way

The first run of this benchmark showed conditional-reset at only 1.15x, and
`conditional_reset=False` measuring *slower than raw asyncpg at the same reset policy*
(3218 vs 3361 rps). The engine was regenerating SQL on every request and then running
a **regex substitution** to renumber `$` placeholders. `Query` now numbers them during
generation and caches compiled SQL per shape, invalidating on mutation. That closed the
gap (1.02x for the same-policy engine) and lifted conditional reset from 1.15x to
1.23x.

```bash
taskset -c 0 python3 benchmarks/bench_conditional_reset.py --repeat 3
taskset -c 0 python3 benchmarks/bench_pipeline_reset.py --repeat 5   # the pipeline analysis
```

Artifacts: [`results/conditional_reset.txt`](../benchmarks/results/conditional_reset.txt),
[`results/pipeline_reset.txt`](../benchmarks/results/pipeline_reset.txt)

---

## 13. Bottom line: sqlom vs SQLAlchemy, both tuned, with and without FastAPI

Everything above, applied to both sides. `benchmarks/bench_final.py` for the data
layer, `benchmarks/fastapi_app.py` + `benchmarks/httpload.py` for end to end.

**SQLAlchemy is tuned too, and one of its knobs matters a lot.** By default
SQLAlchemy wraps each request in `BEGIN … ROLLBACK`, so it sends **3 statements per
request** against sqlom's 1 (verified with `log_statement=all`). A read-only endpoint
does not need a transaction, and charging those two extra round trips to "ORM
overhead" would be dishonest. With `isolation_level="AUTOCOMMIT"` it sends exactly 1.
It also gets `pool_reset_on_return=None` (the analogue of sqlom's conditional reset),
uvloop, a reused statement object so its compiled-SQL cache hits, and orjson.

### Data layer, one core, async single-thread c=8

| contender | rps | CPU ms/req | vs. tuned ORM |
|---|---|---|---|
| **sqlom (all optimizations)** | **4175** | **0.2395** | **7.18x** |
| sqlom (unoptimized engine) | 3633 | 0.2736 | 6.25x |
| SQLAlchemy Core (tuned) | 1043 | 0.9547 | 1.79x |
| SQLAlchemy Core (default) | 952 | 1.0446 | 1.64x |
| SQLAlchemy ORM (tuned) | 581 | 1.7080 | 1.00x |
| SQLAlchemy ORM (default) | 504 | 1.9201 | 0.87x |

**7.2x the tuned async ORM, 4.0x tuned Core.** Note SQLAlchemy's own tuning is worth
1.10-1.15x — the AUTOCOMMIT fix is real but modest, so the gap is not an artifact of
leaving it misconfigured.

Artifact: [`results/final_comparison.txt`](../benchmarks/results/final_comparison.txt)

### End to end through FastAPI — where most of the gap goes away

uvicorn (uvloop + httptools) pinned to core 0, load generator to core 1, Postgres to
cores 2,3, 8 keep-alive connections. All four endpoints return **byte-identical**
7701-byte payloads via `Response`, bypassing FastAPI's `jsonable_encoder`, so the only
difference between routes is the data layer.

| endpoint | rps | mean | p50 | p95 | p99 |
|---|---|---|---|---|---|
| `/noop` — no database at all | 8297 | 0.96 ms | 0.93 | 1.28 | 1.61 |
| **`/sqlom`** | **2427** | **3.29 ms** | 3.22 | 3.86 | 4.89 |
| `/core` (tuned) | 890 | 8.98 ms | 8.09 | 11.28 | 16.37 |
| `/orm` (tuned) | 506 | 15.79 ms | 11.61 | 61.46 | 65.19 |
| `/orm-default` | 448 | 17.84 ms | 13.00 | 63.34 | 69.33 |

| | data layer | through FastAPI |
|---|---|---|
| sqlom vs ORM (tuned) | 7.18x | **4.80x** |
| sqlom vs ORM (default) | 8.28x | 5.42x |
| sqlom vs Core (tuned) | 4.00x | 2.73x |

**The framework floor is 121 µs/request** (`/noop` at 8297 rps) — routing, ASGI and
HTTP framing, paid identically by every route. Subtracting it gives each data layer's
own cost per request:

| | µs/request above the floor |
|---|---|
| sqlom | 292 |
| SQLAlchemy Core (tuned) | 1003 |
| SQLAlchemy ORM (tuned) | 1856 |

So the ratios survive the web layer but shrink by roughly a third — 7.2x becomes
4.8x — because ~121 µs of fixed overhead is added to both numerator and denominator.
**4.8x is the honest figure to quote for a JSON read endpoint on this shape of query.**

Two things worth reading off the latency columns rather than the throughput ones:

- **sqlom's tail is dramatically tighter.** p99 4.9 ms against the ORM's 65 ms, a 13x
  difference on a metric users actually feel. The ORM's p50 (11.6 ms) to p99 (65 ms)
  spread suggests queueing once the single core saturates.
- **The floor caps everything.** No data layer can exceed 8297 rps here, so as query
  cost falls the mapper matters less; at 100 rows/request sqlom is already using 71%
  of its request budget on the database, the ORM 94%.

```bash
taskset -c 0 python3 benchmarks/bench_final.py --repeat 3
taskset -c 0 python3 -m uvicorn benchmarks.fastapi_app:app --port 8000 \
    --loop uvloop --http httptools --no-access-log &
taskset -c 1 python3 benchmarks/httpload.py --path /sqlom --connections 8 --duration 4
```

Artifact: [`results/fastapi_end_to_end.txt`](../benchmarks/results/fastapi_end_to_end.txt)

---

## 14. The strictest comparison: same driver, both libraries at their defaults

§13 tuned both sides but ran sqlom on asyncpg, which SQLAlchemy cannot use — so
mapper and driver were confounded — and its tuning included skipping the pool's
session reset, a behavioural change. This removes both objections by holding
everything constant except the mapping layer:

* **psycopg3 async on both sides.** `psycopg_pool.AsyncConnectionPool` for sqlom
  (new: `sqlom.PsycopgEngine`), `postgresql+psycopg` for SQLAlchemy.
* **Default pool behaviour on both.** No `reset=` override, no
  `conditional_reset`, no `AUTOCOMMIT`, no `pool_reset_on_return`. Verified with
  `log_statement=all` that both now send the same three statements per request —
  sqlom `BEGIN`/`SELECT`/`COMMIT`, SQLAlchemy `BEGIN`/`SELECT`/`ROLLBACK`.
* Same query, same serializer (orjson), byte-identical output.

### Data layer, one core, async single-thread c=8

| contender | asyncio rps | uvloop rps | uvloop CPU ms/req |
|---|---|---|---|
| **sqlom (psycopg, default pool)** | **1912** | **2135** | **0.4683** |
| SQLAlchemy Core (psycopg, default) | 766 | 801 | 1.2483 |
| SQLAlchemy ORM (psycopg, default) | 487 | 499 | 2.0038 |
| *sqlom vs Core* | *2.50x* | *2.67x* | |
| *sqlom vs ORM* | *3.93x* | *4.28x* | |

Artifact: [`results/psycopg_both_default.txt`](../benchmarks/results/psycopg_both_default.txt)

### End to end through FastAPI, same driver, both defaults

| endpoint | rps | mean | p50 | p95 | p99 |
|---|---|---|---|---|---|
| `/noop` — framework floor | 8419 | 0.95 ms | 0.91 | 1.26 | 1.56 |
| **`/psy-sqlom`** | **1319** | **6.06 ms** | 5.93 | 7.55 | **9.21** |
| `/psy-core` | 638 | 12.53 ms | 12.08 | 14.92 | 28.10 |
| `/psy-orm` | 396 | 20.20 ms | 16.09 | 71.30 | 77.57 |

| | data layer | through FastAPI |
|---|---|---|
| sqlom vs Core | 2.67x | **2.07x** |
| sqlom vs ORM | 4.28x | **3.33x** |

Per-request cost above the 119 µs framework floor: **639 µs** (sqlom), 1449 µs
(Core), 2406 µs (ORM).

Artifact: [`results/psycopg_end_to_end.txt`](../benchmarks/results/psycopg_end_to_end.txt)

### How the four configurations compare

| configuration | vs Core | vs ORM |
|---|---|---|
| asyncpg, both tuned, data layer (§13) | 4.00x | 7.18x |
| asyncpg, both tuned, via FastAPI (§13) | 2.73x | 4.80x |
| **psycopg, both default, data layer** | **2.67x** | **4.28x** |
| **psycopg, both default, via FastAPI** | **2.07x** | **3.33x** |

Two independent effects, each worth roughly a third, and they compound:

- **Driver and pool policy.** Moving sqlom from tuned asyncpg to default psycopg
  costs it more than it costs SQLAlchemy, because sqlom's advantage partly *was*
  asyncpg plus a skipped reset. On the same driver at the same defaults, 7.18x
  becomes 4.28x.
- **The web layer.** A further ~119 µs/request that every route pays equally,
  taking 4.28x down to 3.33x.

**So the range to quote depends entirely on what is being claimed.** For "sqlom's
mapping layer against SQLAlchemy's, same driver, nothing tuned, measured through a
real web framework" the answer is **3.3x over the ORM and 2.1x over Core** — the
most conservative and most defensible figure in this document. The 7.18x headline
required asyncpg *and* a behavioural change, and both belong in the caveat rather
than the claim.

What does not change across any configuration: sqlom's tail. p99 9.2 ms against the
ORM's 77.6 ms here, 4.9 vs 65.2 ms in §13 — consistently around 8x tighter.

```bash
taskset -c 0 python3 benchmarks/bench_psycopg.py --repeat 3
# end to end: start the app, then hit /psy-sqlom, /psy-core, /psy-orm
```

### Is the connection-handling shape fair?

Both engines take a pooled connection per request through `async with`, the same as
SQLAlchemy — there is no connection reuse in the library. But the shapes are not
identical, and the difference is visible in four lines:

```python
# sqlom.PsycopgEngine.fetch_all
async with pool.connection() as conn:
    rows = await (await conn.execute(sql, params)).fetchall()
return hydrate(rows)          # connection already back in the pool

# the SQLAlchemy Core path in bench_psycopg.py
async with engine.connect() as conn:
    result = await conn.execute(stmt)
    payload = [...]           # still holding the connection
```

So sqlom occupies a pooled connection for less of each request. Measured answer to
whether that flatters it: **no, in any configuration tested.** 1000 rows, c=8,
median of 5:

| | pool 10 | pool 2 (fewer connections than workers) |
|---|---|---|
| sqlom (releases before hydrating) | 691 rps | 695 rps |
| Core, payload inside `async with` | 209 rps | 210 rps |
| Core, payload after release | 203 rps | 202 rps |
| *worth to Core* | *−3.0%* | *−3.7%* |

Making Core symmetric makes it **slower**, because it means materialising
`result.mappings().all()` into an intermediate list with no contention to win back
in exchange. The shape the other benchmarks use is marginally the better of the two
*for SQLAlchemy*, so this asymmetry is not inflating any published ratio.

The reason hold time cannot pay here is the fact this suite keeps running into:
**the client is CPU-bound, not connection-bound.** Starving the pool to 2
connections against 8 workers moves Core by 1 rps — the workers queue on the GIL,
not on the pool. Hold time would only start to matter with a client that is *not*
saturated, which is a different benchmark than any in this document.

⚠️ **How not to measure this.** The first attempt used `--limit 100 --repeat 3`, and
two consecutive runs disagreed in *sign* on both pool sizes: −5.4% then +4.5% at
pool 10, **+13.6% then +0.1%** at pool 2. That +13.6% looked exactly like a real
starved-pool advantage for sqlom and was pure noise — it was one run away from being
written up as a finding. Resolving it took a 10x larger payload, so the work moved
across the release point is large relative to the jitter, plus more repeats. Same
lesson as [correction 5](METHODOLOGY.md#5-publishing-a-single-run-and-ranking-a-tie),
learned again.

```bash
taskset -c 0 python3 benchmarks/bench_hold_time.py --limit 1000 --repeat 5 --pools 10,2
```

Artifact: [`results/hold_time.txt`](../benchmarks/results/hold_time.txt)

---

## 15. Auditing the load generator itself

Every end-to-end figure in §13 and §14 came from `benchmarks/httpload.py`, a
raw-socket generator written for this repo. That is a single point of failure: if
it silently serialised requests, throughput would collapse to `1/latency` for
every contender, the ratios could shift or invert, and nothing in the output would
look wrong. So it is checked three independent ways, and then the whole comparison
is re-run with a tool that shares none of its code.

### Is it concurrent? Three checks that fail differently

`benchmarks/verify_concurrency.sh`, server on core 0, generator on core 1,
Postgres on cores 2-3, `/psy-sqlom`:

| connections | ESTABLISHED sockets on :8000 | rps | mean | in flight |
|---|---|---|---|---|
| 1 | 1 | 792 | 1.261 ms | **1.00** |
| 2 | 2 | 1135 | 1.760 ms | **2.00** |
| 4 | 4 | 1287 | 3.106 ms | **4.00** |
| 8 | 8 | 1231 | 6.495 ms | **8.00** |
| 16 | 16 | 1192 | 13.407 ms | **15.98** |

1. **Direct observation.** Sockets counted from `/proc/net/tcp` mid-run — not
   inferred from the generator's own bookkeeping. Exactly N every time.
   (`ss` needs netlink and is unavailable in this container; `/proc` is not.)
2. **Little's Law.** In a closed loop with no think time, `rps x mean latency`
   equals the number of in-flight requests. It lands on N to two decimal places
   at every level. A serialising generator would sit at 1.00 throughout.
3. **Throughput scaling.** 792 → 1287 rps from 1 to 4 connections. A generator
   with one request outstanding cannot get faster by being asked for more.

The knee is shallow and moves between runs — a repeat run peaked at 8 (1403 rps)
rather than 4 — so c=8 sits at or just past saturation, not before it. That
matters only for absolute rps; the ratios are taken at a fixed c for all
contenders.

Applying check 2 retroactively to the §14 table confirms it was concurrent as
published: `/noop` 8419x0.949, `/psy-sqlom` 1319x6.061, `/psy-core` 638x12.533,
`/psy-orm` 396x20.196 — all 7.99-8.00 in flight.

Artifact: [`results/concurrency_verification.txt`](../benchmarks/results/concurrency_verification.txt)

### A second generator: locust

`benchmarks/bench_locust.sh` re-runs §14's endpoints with locust 2.46.2, using
`FastHttpUser` (geventhttpclient — the default `HttpUser` wraps `requests` and
costs about 1 ms of client CPU per request, which on one pinned core makes the
client the bottleneck) and `wait_time = constant(0)`, so N users means N in
flight, the same closed-loop model as `httpload.py --connections N`. Both
generators drive the *same* uvicorn process in the same session, so any
difference between them cannot be blamed on warmup or Postgres state.

u=8, t=10s, median of 3, one discarded warmup per endpoint:

| endpoint | locust rps | httpload rps | delta | in flight (locust) |
|---|---|---|---|---|
| `/noop` | 5417 | 8604 | **-37.0%** | 6.70 |
| `/psy-sqlom` | 1328 | 1326 | **+0.1%** | 7.81 |
| `/psy-core` | 624 | 668 | -6.6% | 7.90 |
| `/psy-orm` | 404 | 416 | -2.9% | 7.94 |

| ratio | locust | httpload | published in §14 |
|---|---|---|---|
| sqlom vs Core | 2.13x | 1.99x | 2.07x |
| sqlom vs ORM | 3.29x | 3.19x | 3.33x |

**The ratios survive an independent generator.** Two tools sharing no code
bracket the published 2.07x/3.33x within about 7%, and agree on sqlom's absolute
throughput to 0.1%.

**But locust cannot measure the framework floor.** `/noop` comes out 37% low, and
check 2 says why: 6.70 in flight instead of 8. Locust on one core saturates around
5400 rps, below `/noop`'s real throughput, so there it measures itself rather than
the server. That is also why the two disagree by a few percent on `/psy-core` and
`/psy-orm` while agreeing on `/psy-sqlom` — nothing about locust is broken, it is
simply a heavier client, and its residual cost is small but not zero at these
rates. Which is the argument for `httpload.py` existing: the `/noop` floor of
119 µs used in §13-14 is only measurable with the cheaper generator.

The general rule this makes concrete: a load generator's headroom must be
*demonstrated*, not assumed. `/noop` is the calibration probe — if it is not well
clear of every endpoint under test, the run is measuring the client.

One resolution caveat: locust rounds sub-100 ms response times to whole
milliseconds, so its percentile columns are ±1 ms and its p95/p99 are not
directly comparable to `httpload.py`'s. Its rps and mean are exact.

```bash
benchmarks/verify_concurrency.sh
benchmarks/bench_locust.sh -u 8 -t 10s -r 3
```

Artifact: [`results/locust_end_to_end.txt`](../benchmarks/results/locust_end_to_end.txt)

---

## 16. What none of this shows

- **The HTTP layer is measured, but only one shape of it.** §13-15 run a real
  FastAPI/uvicorn stack, but with a single worker, no TLS, no middleware, no
  request validation and a hand-built `Response` that bypasses
  `jsonable_encoder`. A route doing Pydantic response validation would add cost
  to every contender equally and compress these ratios further.
- **Postgres is barely loaded.** Small indexed reads from shared buffers. A query
  heavy enough to make the *database* the bottleneck would compress every ratio
  toward 1.0 — arguably the more common production shape, and untested.
- **Localhost, not a network.** No real RTT, so the latency-bound regime where slow
  client code hides behind network wait is untested. That regime should shrink
  these ratios.
- **4 vCPU; process scaling verified only to 2 workers, and only at the data
  layer.** 1 → 2 is linear (1.99x); 16 or 64 workers against one Postgres is
  unmeasured, and multi-worker scaling *through FastAPI* was never run at all.
- **The whole box is 4 cores.** §15's partition — server 1, generator 1, Postgres
  2 — uses all of them, so there is no spare core and no room to scale the
  generator up. That is the ceiling behind locust's `/noop` result.
- **Narrow shape.** One flat 4-column table of small ints and short strings. No
  nested shaping, wide rows, large text/JSONB or writes.
- **Everything the query builder grew after the single-table read is
  unbenchmarked.** Joins, aliases, boolean groups, `IN`/`EXISTS`, aggregates with
  `GROUP BY`/`HAVING`, derived tables, set operations, window functions, `CASE`,
  arithmetic, and every write path (`Insert`/`Update`/`Delete`, `RETURNING`, bulk
  insert) are covered by the pytest suite and appear in no ratio here. In
  particular **no write is benchmarked at all**: every figure in this document is a
  single-table read.
  A joined query costs strictly more than the single-table read measured here: the
  select list is wider, rows are more numerous for a one-to-many, and the hydrator
  builds several objects per row. A transaction costs more still — it marks the
  connection dirty, so its release pays the pool's full session reset. Treat every
  figure here as describing the single-table read path only.
- **No sampling profiler.** §2 is wall-clock timing of isolated stages, not
  `py-spy`/`pyinstrument`; it attributes cost per stage, not per function.
- **Not a production system.** No test suite, not packaged, never deployed.
