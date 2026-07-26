# How to benchmark this honestly

Seven claims published in this repo turned out to be wrong. Each was caught by
attacking the benchmark rather than trusting it, and each came from a distinct
methodological flaw. They are recorded here because the flaws generalize well
beyond sqlom.

Two of the seven were found by an automated reviewer (CodeRabbit) on the pull
request rather than by me, which is worth recording as its own lesson: corrections
1 through 5 were all found by self-attack, and I had by then written a document
about how to attack benchmarks — and still shipped a harness that timed one side's
connection setup and not the other's. **Self-review has a blind spot exactly where
you are most confident.**

Every figure quoted inside a correction is as-of that correction. Absolute
milliseconds are not comparable between corrections; see "absolute times drift with
the machine" under practices.

If you take one thing from this file: **a benchmark result that flatters the thing
you built should be treated as a bug report until you have tried to break it.**

---

## The seven corrections

### 1. Comparing different payloads (inflated 3.5x → 2.6x)

The first sqlite benchmark had sqlom emitting `"is_active":1` while both SQLAlchemy
variants emitted `"is_active":true`. sqlite has no boolean type, and SQLAlchemy's
`Boolean` column converts on the way out — sqlom simply passed the raw integer
through. **sqlom was skipping type-coercion work its competitors were paying for**,
and the published 3.5x-vs-ORM was partly measuring that.

Fix: `bench_sqlite.py` now asserts every approach emits **byte-identical JSON**
before timing starts, and fails loudly otherwise. The honest figure for that same
approach is **2.63x** (median of 5; earlier revisions of this file said 2.1–2.5x,
which came from the same single noisy run described in correction 5).

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

### 5. Publishing a single run, and ranking a tie

The sqlite table was generated from one run of the suite. Several cells in that run
were unrepresentative: the reflective path measured 3.08 ms against a true median of
2.53 ms (published 2.1x vs. an actual 2.63x), and `@model` dataclass native came out
at 4.3x against an actual 5.15x.

Worse, the table *ranked* the three fastest variants — compiled batch 1.06 ms,
passthrough 1.10, compiled per-row 1.13 — as if that ordering meant something. It
doesn't. Across nine runs their medians span 1.028–1.060 ms while each varies 5–7%
between trials, and the order changes run to run: per-row led the median-of-5 run,
batch led two of three earlier ones. The published ranking was noise presented as a
result.

Fix: `--repeat` plus medians, spread reported alongside every central value, and the
three tied variants grouped into one row rather than ranked.

> **Generalizes to:** one run is an anecdote. Repeat, publish the median *and* the
> spread, and when the spread between two rows exceeds the gap between them, say
> they tie instead of ordering them. Note that the tier structure here was stable
> across every run — it is the fine-grained ordering that was not, so distinguish
> "which tier" from "which of these three" when deciding what you can claim.

---

### 6. Timing one side's connection setup and not the other's (Core ratio inflated 8%)

`bench_sqlite.py` handed the sqlom runners a `sqlite3.Connection` created once before
timing, but built SQLAlchemy's connection (`engine.connect()`) and `Session` *inside*
the timed closure. So SQLAlchemy paid a pool checkout on every measured iteration
that sqlom never paid, and that cost was published as object-mapping overhead — in a
benchmark whose stated purpose was to isolate "the object-shaping path".

This is correction 1 again in a different costume: the contenders were not doing the
same work. The first time it was the *output* that differed (int vs bool); this time
it was the *setup*. Both passed a byte-equivalence gate, because a gate compares
results and cannot see what you timed around them.

Sizing it needed a paired instrument. The effect is ~8% and the suite swings ±14%
(Core) to ±27% (ORM) between runs, so differencing two runs could not resolve it —
the first attempt to do so produced a number of the wrong sign for the ORM.
`ab_setup_cost.py` times every variant in one process, alternating between them each
round. Result: 76 µs charged to Core at 100 rows (13.8% of Core, 12.1% of the ratio),
466 µs at 1000 rows (9.0%, 8.3% of the ratio); the ORM's own setup is ~0 because a
`Session` on a warm pool is cheap next to hydrating 1,000 instances.

The fix has an over-correction that is *larger* than the bug, which is why it is
worth naming: hoisting the `Session` out of the timed loop as well leaves its
identity map alive between iterations, so every iteration after the first returns
already-hydrated instances and skips the work being measured — 12.9% in the ORM's
favour. Fair is a per-request `Session` on an already-checked-out connection.

> **Generalizes to:** an equivalence gate proves the contenders produced the same
> answer, not that they were asked the same question. Separately audit what is
> *inside* each timed region — connection acquisition, session/transaction setup,
> statement compilation, warmup state — and confirm every contender either pays it or
> none does. And when a correction makes your own numbers worse, check whether the
> obvious version of the fix has quietly made someone else's better.

### 7. Mixing a bottom-up floor with a top-down total (ceiling inflated 1.42x → 1.51x)

`estimate_ceilings.py` decomposed a request into stages, then computed the
hypothetical floor for a native object builder by *summing* the stages that would
survive. It divided that floor into the separately **measured** full pipeline. But
the isolated stages summed to 99.8 µs while the measured pipeline was 104.0 µs, and
that unattributed 4.2 µs of composition cost therefore landed entirely on the
speedup side of the ratio. Published ≤1.51x; the correct answer on the same data is
≤1.42x.

Fix: subtract the removable stages *from the measured total*, so numerator and
denominator share a basis, and print the residual instead of absorbing it.

> **Generalizes to:** never divide a bottom-up estimate into a top-down measurement.
> If you decompose something, print the sum of the parts next to the measured whole;
> the gap is real and it has to be assigned somewhere deliberately. A decomposition
> that adds up to exactly 100% is usually a decomposition that hid its residual.

---

## Practices this benchmark suite adopts

**Enforce output equivalence before timing.** Both `bench_sqlite.py` and
`bench_pg_load.py` compare every contender's bytes against a reference and abort on
mismatch. `--skip-equivalence` exists for debugging only.

**One contender per process for any published number.** `--only` plus `--repeat`,
report medians. Expect c=1 cells in the load benchmark to swing 15–20% between
trials; a single low-concurrency run means very little.

**Test for the bias rather than assuming it.** The ordering flaw in correction 2 was
found in the Postgres suite, so the sqlite suite was checked for it too — with
`--reverse` and with `--only` per process. It is *not* affected: forward, reversed
and isolated runs agree within a few percent. The two suites differ because the load
benchmark spans seconds per cell against a separate server whose state evolves,
while the sqlite one is in-process with a fixed iteration count. A flaw found in one
harness is a hypothesis about the others, not a verdict — go and measure.

**Group ties instead of ranking them.** The three fastest sqlite variants span
1.028–1.060 ms while each varies 5–7% across trials, and their order changes between
runs. Publishing them as a ranked list would invent a result; they are reported as a
single tier. Report spread alongside every central value so a reader
can see when a gap is not a gap.

**Never categorize profiler frames by substring on a project name.** The first
version of `profile_pg.py` bucketed frames with `r"/sqlom/"` — which also matches
`/home/user/sqlom/benchmarks/bench_pg_load.py`, because the repository directory
shares the package's name. The result credited the *harness's* dict comprehension to
the sqlom library, and made the SQLAlchemy ORM run appear to spend 20% of its time in
sqlom, which is impossible. Caught only because that impossible row was visible.
`profile_pg.py` now compares against resolved package directories
(`Path(sqlom.__file__).parent`) and distinguishes sqlom's `exec`-generated frames from
SQLAlchemy's by function name, since both use the filename `<string>`.

> **Generalizes to:** a rollup that can attribute work to a library the code never
> imported is broken. Put an impossible-by-construction row in your own output and
> check it reads zero.

**Cross-check an instrumented profile with a sampling one — they have opposite blind
spots.** On the sqlite profile the two disagreed on the sqlite3 driver's share (16% vs
37%) and on orjson's (17% vs 6%). Neither is simply wrong:

- cProfile's overhead is per *call*, so it inflates code that makes many small calls.
  `_hydrate_all` triggers ~200 instrumented builtin calls per request while
  `Cursor.fetchall` is one — the driver therefore looks cheaper than it is.
- pyinstrument samples *Python* frames, so it cannot see inside a C extension.
  `orjson.dumps`'s work is charged to whatever Python frame called it.

So: sampler for the balance between Python components, cProfile for anything
implemented in C (and for exact call counts). Publish which profiler a number came
from, and when they disagree materially, publish both rather than picking the
flattering one.

**Remove a layer to find out what a share means.** "sqlom is 15% of client CPU" read
as a fact about the mapper. Re-profiling against in-process sqlite showed 53% of the
Postgres cost was transport — loop, socket, TLS, pool — and that with it gone the
mapper is ~50%. The 15% was a statement about sockets, not about sqlom. When a
component looks small, check whether you are measuring it or measuring its
surroundings.

**A single low-repeat cell is not a result.** A first pass at the async sqlite
benchmark showed the yielding coroutine at 0.56x with `--repeat 1`, which would have
implied ~90 µs of overhead per `await asyncio.sleep(0)`. Measuring `sleep(0)`
directly put it at 1.4-2.4 µs, so the cell was an outlier; at `--repeat 5` the
variant sits at 0.94x. The tell was that the implied cost was physically implausible
for the operation involved — sanity-check a surprising number against the cost of the
primitive it supposedly comes from before believing it.

**Benchmark the library's own path, not a hand-rolled stand-in of it.** The
conditional-reset benchmark first showed the engine *slower than raw asyncpg at the same
reset policy* (3218 vs 3361 rps). The raw variants precomputed their SQL once outside
the request; the engine regenerated it per request and then ran a regex to renumber
placeholders. That 4% was real overhead in the shipped code, and only comparing
like-for-like exposed it. If your wrapper measures worse than the thing it wraps, that
is a finding about the wrapper, not a flaw in the comparison.

**All Postgres runs use async single-threaded concurrency at c=8.** A single asyncio
loop under the GIL saturates one core (§10, §11), so extra client cores are wasted and
c=1 leaves a third of the core idle on socket wait. c=8 is the saturated,
representative point; process-level parallelism scales from there linearly.

**Measure the fixed cost of a mechanism before blaming its variable cost.** The first
explanation for pipelining losing was "per-statement bookkeeping" — plausible, and
wrong. Timing an *empty* pipeline (221 µs, no statements queued) showed the cost is
fixed per pipeline, and reusing cursors rather than allocating five per request changed
nothing, which ruled the per-statement theory out. When something is slower than
expected, add the degenerate case — zero statements, zero rows, zero work — to the
table; it separates fixed from variable cost in one measurement.

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

**Absolute times drift with the machine; only ratios travel.** Re-running the sqlite
suite in a later session gave ~1.35x higher times for *every* contender, including
paths the intervening changes did not touch. Rather than guess, the entire
pre-change tree was checked out (`git stash`) and re-run on the same box: it
reproduced the *new* numbers, not the old ones, which pins the cause on the machine.
Shared cloud CPU is not a stable ruler. So: never compare absolute microseconds
across sessions or across sections of a document; when a whole table moves in one
direction by a similar factor, suspect the ruler before the code; and to tell the two
apart, run the old code again *now* rather than reasoning about it.

**Audit the load generator, and never assume its concurrency.** All end-to-end
figures came from `httpload.py`, written for this repo. A generator that silently
serialised would produce `1/latency` for every contender with nothing in the output
looking wrong. Three checks that fail in different ways (§15):

- **Observe, don't infer.** ESTABLISHED sockets counted from `/proc/net/tcp` while
  the run is in flight — external evidence, not the generator's own counter.
- **Little's Law.** `rps x mean latency` must equal the number of in-flight
  requests. It comes out at N to two decimals for N = 1, 2, 4, 8, 16; a serialising
  generator would sit at 1.00. This is retroactive — it can be applied to any
  already-published closed-loop table, and it validates §13-14 after the fact.
- **Scaling.** Throughput must rise with connection count up to a knee. One request
  outstanding cannot go faster by being asked for more.

This audit found no error: the ratios were correct as published. Recording a
*passed* audit matters as much as recording a failed one, otherwise the record
implies verification only happens where something broke.

**Calibrate the generator's headroom with a do-nothing endpoint.** Re-running §14
under locust reproduced sqlom's throughput to 0.1% and bracketed both ratios within
7% — but reported `/noop` 37% low, because locust on one core saturates around
5400 rps, beneath `/noop`'s real throughput. Little's Law caught it (6.70 in flight
instead of 8) before the number could be published. A `/noop` route that does no
work is the cheapest possible probe: if it is not well clear of every endpoint under
test, the client is the bottleneck and the run measures the client. Note that this
cuts both ways — the *heavier* generator understated the endpoints, so "my numbers
came from a standard tool" is not by itself a correctness argument.

> **Generalizes to:** a second implementation of the measuring instrument is worth
> more than another run of the first. Where two independent generators agree, the
> number is a property of the server; where they disagree, at least one is measuring
> itself, and the do-nothing endpoint tells you which.

---

## Reproducing

```bash
pip install sqlalchemy orjson asyncpg psycopg[binary] psycopg-pool attrs
pip install fastapi uvicorn httptools uvloop locust   # end-to-end + generator audit

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

# same driver, both libraries at their defaults (the figure to quote)
taskset -c 0 python3 benchmarks/bench_psycopg.py --repeat 3

# end to end through FastAPI, and the audit of the generator that measures it.
# Both start uvicorn on core 0 and Postgres on 2,3 themselves.
benchmarks/verify_concurrency.sh        # sockets + Little's Law + scaling
benchmarks/bench_locust.sh -u 8 -t 10s -r 3   # locust vs httpload, head to head
```

Raw artifacts from the runs quoted in [BENCHMARKS.md](BENCHMARKS.md) are in
[`benchmarks/results/`](../benchmarks/results/).
