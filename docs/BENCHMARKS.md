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

## 9. What none of this shows

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
