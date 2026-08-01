# Benchmarks, and how to trust them

The numbers, the practices that produced them, and the eleven published claims that
turned out to be wrong.

One rule sits above the rest: **a result that flatters the thing you built is a bug
report until you have tried to break it.**

---

## Results

All of these come from one sweep at commit `3757a0d`, rendered by
`scripts/publish_tables.py` from the recorded `run.json` rather than transcribed.
sqlite is an ephemeral 200,000-row database; postgres is a container on the same host.
1000 rows per read, 300 timed iterations after 50 warmup, **5 trials, one contender per
process**, GC off, pinned to cpus 6-9:

```
DSN="postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"
just bench db up          # prints that DSN back on the "up:" line

for shape in flat join wide; do
  for backend in sqlite postgres mock; do
    # wide has no mock contenders, and an empty selection is an error
    if [ "$backend" = mock ] && [ "$shape" = wide ]; then continue; fi
    just bench micro run --shape "$shape" --backend "$backend" \
      --iterations 300 --warmup 50 --trials 5 --isolate --pg-dsn "$DSN" --record
  done
done
```

Medians of the per-trial medians, in milliseconds, lower is better. Ratios come from
`stats.ratio_with_spread`, so `~` marks a pair the trials do not actually order —
either the worst-case interval spans 1.0 or the medians are within 5%. Worst
trial-to-trial spread anywhere below: **8.1%** (sqlite), 4.5% (postgres), 7.5% (mock).

> **These runs report `quotable=False`, on one clause: cpu boost is enabled and cannot
> be disabled without root on this box.** Every other gate passes — clean tree,
> equivalence enforced and self-consistent, one contender per process.
>
> It shows, and in the place the detectors are for rather than in the medians. Worst
> single trial anywhere: `p95/p50` **4.11** and `max/p50` **9.47**, both on sqlite,
> both on SQLAlchemy Core cells rather than rowform's. Against that, the worst
> *median* moved 8.1% across five trials. So the tail is disturbed and the central
> value is not, which is what taking a median of per-trial medians is for — but read
> the ratios, not the absolutes.

Runs land in `benchmarks/results/runs/`, which is gitignored on main; chosen ones are
committed to a dated `bench/` branch by hand and indexed in [RUNS.md](RUNS.md).

> [!WARNING]
> **The `MappedAsDataclass` row is stale and overstates the ORM's cost.** It built
> its JSON payload with `dataclasses.asdict()` where every other contender uses a
> `getattr` comprehension; `asdict()` deep-copies recursively, and on `wide` that
> was ~14 ms of the 17.37 ms cell for byte-identical output. The contender is
> fixed, but these cells were measured before the fix and have not been re-taken —
> a re-run has to happen on the pinned, isolated box this sweep was recorded on,
> not wherever the fix landed. Every other row is unaffected: none of them ever
> used `asdict()`. Treat `MappedAsDataclass` as an upper bound until re-run.

### sqlite

| contender | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor)* | 0.8075 | 1.3351 | — | | 0.67x | 0.73x | — |
| raw driver + the same hydrator *(floor)* | 0.9058 | 1.4891 | — | | 0.75x | 0.82x | — |
| **rowform** `fetch_all()` | **1.2003** | **1.8212** | **3.6604** | | **1.00x** | **1.00x** | **1.00x** |
| rowform `execute().scalars()` | 1.2368 | — | 3.6992 | | ~1.03x | — | ~1.01x |
| rowform `execute().all()` | 1.3757 | 2.0185 | — | | 1.15x | 1.11x | — |
| SQLAlchemy Core (positional) | 1.5163 | 2.1204 | 4.1496 | | 1.26x | 1.16x | 1.13x |
| SQLAlchemy Core (`.mappings()`) | 3.1747 | — | — | | 2.64x | — | — |
| SQLAlchemy ORM | 4.2782 | 6.9755 | 7.8641 | | 3.56x | 3.83x | 2.15x |
| SQLAlchemy ORM (`MappedAsDataclass`) | 5.1737 | 9.0512 | 17.3702 | | 4.31x | 4.97x | 4.75x |

### postgres (asyncpg)

| contender | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor)* | 0.9818 | — | — | | ~0.96x | — | — |
| **rowform** `fetch_all()` | **1.0215** | **1.7819** | **3.0831** | | **1.00x** | **1.00x** | **1.00x** |
| rowform `execute().scalars()` | 1.0428 | — | 3.0990 | | ~1.02x | — | ~1.01x |
| rowform `execute().all()` | 1.1911 | 1.9951 | — | | 1.17x | 1.12x | — |
| SQLAlchemy Core (positional) | 1.3652 | 2.0904 | 3.6145 | | 1.34x | 1.17x | 1.17x |
| SQLAlchemy Core (`.mappings()`) | 3.1215 | — | — | | 3.06x | — | — |
| SQLAlchemy ORM | 4.2030 | 7.2154 | 7.3335 | | 4.11x | 4.05x | 2.38x |

### Row layer alone (`mock` backend, zero driver cost)

| contender | flat | join | | flat | join |
|---|---|---|---|---|---|
| **rowform** | **0.2567** | **0.5227** | | **1.00x** | **1.00x** |
| SQLAlchemy Core (positional) | 0.5467 | — | | 2.13x | — |
| SQLAlchemy ORM | 3.1768 | 5.5547 | | 12.38x | 10.63x |

> [!WARNING]
> **Stale, and it flattered rowform.** `MockEngine` overrides `_connection` so a
> read never reaches SQLAlchemy's pool — that ~0.4 ms checkout is exactly what
> this instrument exists to exclude — but the SQLAlchemy contenders were still
> calling `engine.connect()` *inside* the timed region. So the checkout was
> excluded on one side and charged to the other, in the one table whose whole
> point is that nothing but the row layer is being measured. They now hold a
> hoisted connection too, with a fresh `Session` per request over it so the ORM
> keeps its per-request identity map. On an unpinned box that moves Core's cell
> from ~0.58 ms to ~0.46 ms, roughly `2.13x` → `~1.6x`; the ORM row is
> proportionally much less affected. Re-run on the pinned box before quoting.

### Reading the floors

**The two sqlite floors hoist one connection for the whole run; rowform checks one out
of SQLAlchemy's pool per read.** That is most of the 0.75x — about 0.3-0.4 ms of fixed
per-checkout cost (`PLAN_SQLA_API.md` §2b), not row-layer work. The postgres floor
acquires per request from asyncpg's own pool, which is why *that* comparison is
apples-to-apples and lands on `~0.96x`: a tie, and the honest reading of "as fast as
hand-rolling the driver". Hold a connection per request — as any `AsyncSession`
application already does — and the sqlite gap closes the same way.

Before the row layer moved onto SQLAlchemy's engine, rowform owned a pool with a ~0.09 ms
checkout and this table read 1.55x/1.16x/1.37x against Core on sqlite. The Core ratios
narrowing to 1.26x/1.16x/1.13x is that trade, paid deliberately and priced here rather
than left in a table taken under the old arrangement.

### The two tracks

`execute()` returns SQLAlchemy's own `Result`; `fetch_all()` returns hydrated objects.
Taken as `.scalars()`, the compatibility track **ties with the hot one in all four cells
where both run** — the `Result` is built but no `Row` ever is. Taken as `.all()` it
costs 11-17%, which is one `Row` per row and the only real difference between the two
lines. Both still come in under stock Core on the same statement.

**`wide` is the honest number.** Its columns are
`DateTime`/`Date`/`Numeric`/`Enum`/`Uuid`/nullable, so per-column type processors
dominate — and both sides run the *same* processors, leaving proportionally less to
skip. It is also where the Core gap closes most — 1.13x on sqlite against 1.26x on
`flat`, and 2.15x rather than 3.56x for the ORM. A suite quoting only `flat` would be
quoting its best case without saying so.

### What the gate proves

Every table above passed the equivalence gate: each contender's JSON is compared byte
for byte before any timing starts. The `wide` shape produces **sha256=60c3f426… on
both sqlite and postgres** — the same 194,647 bytes from two drivers that disagree
about how to store almost every column in it. That is the strongest available evidence
that bypassing `Row` is faithful rather than merely fast, and it is the check a
hand-written converter table failed 7 columns of (correction 11).

---

## Practices

**Enforce output equivalence before timing.** Every harness compares each contender's
bytes against a reference and aborts on mismatch. What it does *not* catch: identical
output says nothing about whether one side took an expensive route to it — correction 8
passed this gate perfectly.

**Price any workaround one contender needs and the others do not.** A per-key cast, an
encoding fix-up, a defensive copy: each is a measurement artifact until timed against
the alternative. That the API you chose *requires* it is not evidence it is cheap.

**Measure more than one shape, along more than one axis.** The suite carries `flat`
(narrow), `join` (multi-entity) and `wide` (every type whose driver representation
differs from its Python one). Each exists because it makes a different mistake visible.
Ask which axis your suite holds constant *everywhere* — that is where a bug can live in
every contender at once and be invisible in all of them.

**Two floors, always.** A dicts-only floor bounds the whole stack; a floor running the
*same* hydrator over the same driver separates engine cost from row-construction cost.
Neither number means much alone, and a floor coming out *above* the thing it bounds is
this suite's most valuable tripwire.

**Floors are written out, never generated.** `benchmarks/micro/contenders.py` spells
out every payload builder per shape. Shared helper code is exactly how a floor quietly
stops being one (correction 10).

**One contender per process for any published number.** `--isolate --trials N`, report
medians. Allocator state, CPU caches and thermal drift are all shared within a process:
Core's median moved 32% across three runs differing only in what had run before it, and
`execute().scalars()` measured 3-4% above `fetch_all()` sharing a process with it and
tied with it once separated.

> This one was written down, mechanically checked by `Run.quotable`, and impossible to
> satisfy: `bench micro run` hardcoded `isolation="combined"`, so the command that
> produces the published tables could never pass the gate guarding them. A convention
> nothing can comply with is worse than an unenforced one — the check reported
> `quotable=False` on every run, which trained the reader to ignore it.

**Group ties instead of ranking them.** When the spread between two rows exceeds the
gap between them, say they tie. Report a dispersion figure alongside every central
value so a reader can see when a gap is not a gap.

**Never report a range as dispersion.** `(max - min) / median` is an extreme-value
statistic: E[max − min] ≈ 6.5σ at n=1000, so it grows with sample count and reports
the single worst interruption in the run. Measured here: it could not print below ~50%
given the contenders' real 5–12% CV; it gave 787%, 698% and 74% across three identical
runs while the median moved 1.4%; and it broke the tie test so nothing under ~2.5x
could resolve. `stats.sample_shape()` now reports IQR-as-%-of-median (dispersion),
p95/p50 (tail) and Tukey outlier counts, keeping max/p50 as an explicitly labelled
*interference detector*. pyperf, pytest-benchmark, Google Benchmark and criterion.rs
all report dispersion this way; none reports a range.

> Before trusting a dispersion statistic, feed the harness a load whose variance you
> already know — a fixed-duration spin *and* a no-op, since the pathology only shows at
> one end.

**Control GC explicitly.** The join shape allocates ~2000 objects per iteration and
every contender showed a stdev several times its median; disabling GC collapsed it
5–10x. With GC on, the mean sits above the p95 for some contenders, which is why
medians are what get quoted.

**Never categorize profiler frames by substring on a project name.** Bucketing frames
with `r"/rowform/"` also matches the *repository* directory, which credited the
harness's own dict comprehension to the library and made an ORM run appear to spend 20%
of its time in rowform. Compare against resolved package directories instead.

> A rollup that can attribute work to a library the code never imported is broken. Put
> an impossible-by-construction row in your own output and check it reads zero.

**Cross-check an instrumented profile against a sampling one.** They have opposite
blind spots: cProfile's overhead is per *call*, so it inflates code making many small
calls; a sampler cannot see inside a C extension, so `orjson.dumps` is charged to its
Python caller. On one sqlite profile they disagreed 16% vs 37% on the driver's share.
Publish which profiler a number came from, and when they disagree materially, publish
both.

**Remove a layer to find out what a share means.** "rowform is 15% of client CPU" read
as a fact about the row layer; against in-process sqlite it is ~50%, because 53% of the
Postgres cost was transport. The 15% was a statement about sockets.

**Measure the fixed cost of a mechanism before blaming its variable cost.** Pipelining
the pool reset lost, and the plausible explanation — per-statement bookkeeping — was
wrong: an *empty* pipeline costs 221 µs. Add the degenerate case, zero statements and
zero rows, and fixed separates from variable in one measurement.

**Record utilization, not just throughput.** `cpu_ms_per_request` and `cpu_utilization`
are what explain a throughput difference and what catch an impossible result.
Throughput alone would not have exposed correction 3.

**Pin deliberately, and read the mask back.** Know which logical CPUs are SMT siblings
of one physical core. Affinity is inherited across `fork()`, so pin before the pool
opens — connections opened earlier keep the old mask. Pin a single-threaded client to
*one* core, and never trust the requested cpuset as evidence it took effect.

**State the bottleneck.** Every ratio here is measured with the client saturated and
the database barely loaded, which is why the row layer's cost is visible at all. Say
which side is the constraint or the number is uninterpretable.

**Absolute times drift with the machine; only ratios travel.** Re-running the sqlite
suite in a later session gave ~1.35x higher times for *every* contender, including
paths nothing had touched. Checking out the pre-change tree and re-running it
reproduced the *new* numbers, which pins the cause on the box. When a whole table moves
one way by a similar factor, suspect the ruler before the code — and to tell them
apart, run the old code again now rather than reasoning about it.

**Audit the load generator; never assume its concurrency.** Three checks that fail
differently: ESTABLISHED sockets counted from `/proc/net/tcp` mid-run, which is
external evidence rather than the generator's own counter; Little's Law, where
`rps × mean latency` must equal the in-flight count; and scaling, since throughput must
rise with connection count up to a knee. Recording a *passed* audit matters as much as
a failed one.

**Calibrate the generator's headroom with a do-nothing endpoint.** A `/noop` route is
the cheapest probe available: if it is not well clear of every endpoint under test, the
client is the bottleneck and the run measures the client. This cuts both ways — locust
reported `/noop` 37% low because it saturates near 5400 rps on one core, so "my numbers
came from a standard tool" is not a correctness argument.

> A second implementation of the measuring instrument is worth more than another run of
> the first.

---

## The eleven corrections

Each was caught by attacking the benchmark rather than trusting it, and each came from
a distinct flaw. Figures are as-of each correction; absolute milliseconds are not
comparable between them.

Two were found by an automated reviewer rather than by me — after I had already written
a document about how to attack benchmarks, and still shipped a harness that timed one
side's connection setup and not the other's. **Self-review has a blind spot exactly
where you are most confident.**

### 1. Comparing different payloads (inflated 3.5x → 2.6x)

rowform emitted `"is_active":1` where SQLAlchemy emitted `"is_active":true`. sqlite has
no boolean type and SQLAlchemy's `Boolean` converts on the way out, so rowform was
skipping type-coercion work its competitors paid for. Fixed by asserting byte-identical
JSON before timing starts.

> If two implementations produce different bytes, you are not benchmarking the same
> work. Diff the output first.

### 2. Contender ordering inside one process

All contenders ran in one process, rowform first. Later contenders measured slower — so
much so that rowform appeared to beat the *no-object* baselines and to use less CPU
than doing no mapping at all. Isolated, the hand-written baselines are faster than
rowform, as they must be.

> Allocator state, CPU caches and thermal drift are shared. One process per contender,
> repeated, medians.

### 3. Misreading a resource limit as a fair test (retracted 4.2x)

Pinning the client to 2 cores instead of 1 dropped throughput, and the drop was
attributed to "removing contention". But a single asyncio loop under the GIL saturates
exactly one core — the recorded `cpu_utilization` was 0.91–1.00 in every run already
taken. The second core was wasted, and the loop lost cache locality migrating between
them (0.308 against 0.217 CPU ms/req).

> Know your subject's parallelism model before designing a resource experiment. And
> read the metric you already collected: this one had been recorded all along.

### 4. Mixing measurement conditions in one table

A serialization table concluded a passthrough path beat a plain dict, comparing 204 ns
from a 10-field benchmark against 182 ns from a 4-field one. Measured at equal width
the dict wins by 2.7x, as it must.

> Never merge numbers from different harnesses into one table.

### 5. Publishing a single run, and ranking a tie

The sqlite table came from one run, and it *ranked* three variants whose medians span
1.028–1.060 ms while each varies 5–7% between trials and whose order changes run to
run. The ranking was noise presented as a result.

> One run is an anecdote. Publish median *and* spread, and distinguish "which tier"
> from "which of these three" when deciding what you can claim.

### 6. Timing one side's connection setup and not the other's (Core ratio inflated 8%)

rowform got a connection created once before timing; SQLAlchemy built its connection
and `Session` *inside* the timed closure. That pool checkout was then published as
object-mapping overhead. This is correction 1 in a different costume — the first time
the *output* differed, this time the *setup* did — and both passed a byte-equivalence
gate, because a gate compares results and cannot see what you timed around them.

Sizing it needed a paired instrument, since the effect is ~8% while the suite swings
±14–27% between runs. The fix also has an over-correction *larger* than the bug:
hoisting the `Session` out as well leaves its identity map alive between iterations, so
every later iteration returns already-hydrated instances and skips the work being
measured — 12.9% in the ORM's favour. Fair is a per-request `Session` on an
already-checked-out connection.

> Audit what is *inside* each timed region — connection acquisition, session setup,
> compilation, warmup state — and confirm every contender either pays it or none does.
> When a correction makes your own numbers worse, check whether the obvious version of
> the fix quietly made someone else's better.

### 7. Mixing a bottom-up floor with a top-down total (ceiling inflated 1.42x → 1.51x)

A hypothetical floor was computed by *summing* the stages that would survive, then
divided into a separately **measured** pipeline. The stages summed to 99.8 µs against a
measured 104.0 µs, so 4.2 µs of unattributed composition cost landed entirely on the
speedup side of the ratio.

> Never divide a bottom-up estimate into a top-down measurement. Print the sum of the
> parts next to the measured whole; a decomposition that adds up to exactly 100% is
> usually one that hid its residual.

### 8. Charging one contender for a workaround the others never needed (Core ratios inflated 1.6-2.6x)

The largest correction, and the one that survived longest. Every Core contender shaped
rows through `.mappings()`, whose keys are `quoted_name` — a `str` subclass orjson
refuses — so every key of every row got an explicit `str()` cast. On sqlite at 1000
rows that cast was **62% of Core's entire measured time**: 4.88 ms against 1.86 ms for
`dict(zip(names, row))`, byte-identical output. Five published Core figures moved, all
in the flattering direction. The ORM ratios were untouched, since that contender uses
`getattr` and never calls `.mappings()`.

**How it was found matters more than what it was.** Not by re-reading the Core runner —
that line had been read many times and carried a comment correctly explaining why the
cast was needed. It fell out of adding a *two-model join*, where `.mappings()` is
unavailable because both tables have an `id`. Core then came out closer to rowform on
the join than on the single table, which is the wrong direction: a join is strictly more
work, so it cannot *close* a gap. The impossible-looking result was the whole signal.

Both idioms stay registered in every harness and the positional one is what gets
quoted. Deleting the slow one would have hidden the size of the mistake.

> 1. **Price every workaround.** A comment explaining why it is necessary is not
>    evidence that it is cheap.
> 2. **A comparison is only as fair as the API you chose for the other side.** Two
>    correct ways to use a library can differ by 2.6x, and finding the other side's
>    best is the benchmarker's job, not the library author's.
> 3. **Add a second shape.** Seven rounds of self-attack on one workload missed this
>    entirely, because every contender was consistently mis-measured and the ranking
>    looked stable. A new shape changes which APIs are even *available*.

### 9. Charging Core for iteration a tuned caller avoids (0.27-0.37 ms/1000 rows)

Correction 8's class again, in the other half of Core's result API. `for row in result`
costs, per row, two generator frames, a try/except-wrapped `fetchone`, a DBAPI
`fetchone()` and a `functools.partial` call; `.all()` replaces all of it with one
C-level `fetchall()` plus a comprehension. On a query costing ~1.6 ms that is
0.27–0.37 ms, inflating every Core ratio a second time on top of correction 8.

> *Twice* now, the obvious idiom for the library being compared against was materially
> slower than its own tuned equivalent. Assume there is a third and go looking before
> publishing.

### 10. A floor that does more work than the thing it bounds

The invariant *"include a floor; rowform beating it is the tripwire"* was already
written down. It fired twice more, both times for a reason it did not cover: the floor
was not doing *less* work.

**The floor built its objects more expensively.** It used `User(**kwargs)` while rowform
used its generated hydrator, and keyword binding through `__init__` is ~2.1x slower
than `object.__new__` plus attribute stores — so the floor came out above what it
bounded.

**The floor read its rows more expensively.** A shared helper built floor payloads with
`{f: v for f, v in zip(fields, row)}` — a zip, a lookup and a membership test per
column — against a hydrator emitting one `UNPACK_SEQUENCE` and a straight-line store
per field. On sqlite the floor still came out below; on **postgres it came out above**,
because an `asyncpg.Record` is more expensive to index than a tuple and the floor
indexed four times where the hydrator unpacked once. Same code, opposite verdict,
decided entirely by the driver's row container.

> "The floor must do strictly less work" is not a property you establish by reading it
> once. It has to hold **per backend**, because the row container, the type processors
> and the object model all change underneath it.

### 11. Measuring one type shape, and generalising from it

Every figure this repo ever published came from one row layout, `int/str/str/bool`.
Nothing chose it to flatter anything — nobody thought of "which *types*" as a benchmark
dimension at all. It is also the one layout where bypassing `Row` looks free: the only
column needing conversion is a boolean, which a hand-written
`SQLITE_CONVERTERS = {bool: bool}` covered exactly.

Measured against a widened shape, **8 of 13 columns came back wrong on sqlite** —
temporal types as strings, `Numeric` as float, `Enum` as its member name, `Uuid` as
hex, `JSON` as text, `Boolean` as int. Not slower: *wrong*, and silently, every value a
plausible-looking Python object of the wrong type.

The equivalence gate did not catch it, because the gate only ever ran on the flat shape.
Correction 8's third lesson had been adopted and a join shape added — but a join is a
different *arity*, not a different *type mix*, so it exercised precisely the same
converters. The `wide` shape now exists and is gated like the others, and the converter
table is gone in favour of asking SQLAlchemy for each column's own `result_processor`,
which is correct per dialect by construction.

> A benchmark shape has more axes than width and arity. Types are one. So are
> nullability, cardinality and value size.
>
> And note the shape of the near-miss: the flat shape made a wrong implementation look
> correct **and** fast, which is the most dangerous combination a benchmark can produce.
> Speed that comes from skipping work is only a win if the work was unnecessary, and a
> suite that never exercises the work cannot tell you which.

---

## Reproducing

```bash
uv sync --all-extras

# correctness first: a benchmark measures whatever the library does, so a wrong
# library is just a fast wrong answer
just test . --pg-required

just bench env check                     # machine, pinning, governor
just bench micro run                     # the tables above
just bench db up && just bench db seed    # ephemeral postgres in docker
just bench load run --case <slug>        # end to end through FastAPI + locust
just bench profile micro                 # cProfile + pyinstrument, side by side
```

The raw artifacts behind the pre-rewrite corrections were deleted once the lessons were
written down here; they are recoverable from git history, along with the 28 scripts that
produced them (`git checkout 32ad4a1 -- benchmarks/`).
