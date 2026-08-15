# Benchmarks, and how to trust them

The numbers, the practices that produced them, and the fourteen published claims that
turned out to be wrong.

One rule sits above the rest: **a result that flatters the thing you built is a bug
report until you have tried to break it.**

---

## Results

Taken 2026-08-15 at `e4402d1` (artifacts on the `bench/2026-08-15-family-split`
branch, indexed in [RUNS.md](RUNS.md)) — the first sweep with the correction-14
contender families. sqlite is an ephemeral 200,000-row database; postgres 16 is an
ephemeral docker container on the same box. 1000 rows per read, 1500 timed iterations
after 200 warmup, **3 trials, one contender per process**, GC off, pinned to two whole
physical cores (`--pin auto`), and **every contender reads inside `BEGIN`…`COMMIT`** —
except `rowform (no transaction)`, which is registered without one precisely so the
cost of the guarantee is visible as a row rather than folded into the others:

```
for shape in flat join wide; do
  just bench micro run --shape "$shape" \
    --iterations 1500 --warmup 200 --trials 3 --isolate --record
done
just bench db up
for shape in flat join wide; do
  just bench micro run --shape "$shape" --backend postgres \
    --iterations 1500 --warmup 200 --trials 3 --isolate --record \
    --pg-dsn "$(just bench db dsn)"
done
just bench db down
```

Medians of the per-trial medians, in milliseconds, lower is better. Ratios come from
`stats.ratio_with_spread`, so `~` marks a pair the trials do not actually order —
either the worst-case interval spans 1.0 or the medians are within 5%. Worst
trial-to-trial spread anywhere below: **10.7%** (sqlite), 6.2% (postgres), 4.1% (mock).

> **These runs report `quotable=False`, on one clause: cpu boost is enabled and cannot
> be disabled without root on this box.** Every other gate passes — clean tree,
> equivalence enforced and self-consistent (child processes hash-verified), one
> contender per process. One thermal-throttle event landed during the flat/sqlite run
> (the detector that now exists caught it); its worst cell spread is 10.7% against the
> 6.2% postgres shows, so read sqlite's third decimal with that in mind.

> **The headline moved, and that is the point of correction 14.** At equal work, the
> compiled hydrator does not beat SQLAlchemy Core's result layer: Core positional is
> 0.89x on `flat`/`join` (sqlite), 0.92x/0.75x (postgres), and a tie on `wide` — and
> the comparison deliberately gives Core the *cheaper* payload builder (unpacking,
> where rowform pays a `getattr` pass). What rowform's API shape is worth is its own
> row: `rowform (idiomatic)` — prepared once, dataclasses straight to orjson — runs
> 0.73–0.94x of equal-work rowform and lands at parity with Core positional, returning
> typed dataclasses where Core returns tuples. Against the ORM, both spellings are
> **1.9–4.9x** faster. The join/postgres equal-work gap (0.75x) bundles the unprepared
> cache key and the two-entity `getattr` pass; a future cell should decompose it.

Runs land in `benchmarks/results/runs/`, which is gitignored on main; chosen ones are
committed to a dated `bench/` branch by hand and indexed in [RUNS.md](RUNS.md).

### sqlite

| contender | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor: no SQLAlchemy)* | 0.9255 | 1.6847 | 2.7011 | | 0.66x | 0.75x | 0.86x |
| raw driver + the same hydrator *(floor: no SQLAlchemy)* | 1.1031 | 1.8045 | 2.7995 | | 0.79x | 0.80x | 0.89x |
| same pool + transaction → dicts *(floor: same plumbing)* | 0.9468 | 1.7804 | 2.7641 | | 0.68x | 0.79x | 0.88x |
| **rowform** `fetch_all()` *(equal work)* | **1.3995** | **2.2507** | **3.1547** | | **1.00x** | **1.00x** | **1.00x** |
| rowform *(idiomatic: prepared once, direct to orjson)* | 1.2562 | 2.0196 | 2.9783 | | 0.90x | 0.90x | 0.94x |
| rowform `fetch_all()` off the engine *(no transaction)* | 1.2426 | — | — | | 0.89x | — | — |
| rowform `execute().scalars()` | 1.4076 | — | 3.2009 | | ~1.01x | — | ~1.01x |
| rowform `execute().all()` | 1.4948 | 2.4222 | — | | 1.07x | 1.08x | — |
| SQLAlchemy Core (positional) | 1.2511 | 2.0094 | 3.0061 | | 0.89x | 0.89x | ~0.95x |
| SQLAlchemy Core (`.mappings()`) | 2.1999 | — | — | | 1.57x | — | — |
| SQLAlchemy ORM | 3.5957 | 5.7349 | 6.1220 | | 2.57x | 2.55x | 1.94x |
| SQLAlchemy ORM (`MappedAsDataclass`) | 3.5709 | 5.7272 | 6.1876 | | 2.55x | 2.54x | 1.96x |

### postgres (asyncpg)

Re-measured for the first time since the table that stood here was withdrawn (its
headline predated equalised transactions, equalised pools, and the gevent fix).

| contender | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor: no SQLAlchemy)* | 0.4148 | — | — | | 0.71x | — | — |
| same pool + transaction → dicts *(floor: same plumbing)* | 0.3642 | — | — | | 0.62x | — | — |
| **rowform** `fetch_all()` *(equal work)* | **0.5869** | **1.2200** | **1.8118** | | **1.00x** | **1.00x** | **1.00x** |
| rowform *(idiomatic: prepared once, direct to orjson)* | 0.4824 | 0.8846 | 1.6199 | | 0.82x | 0.73x | 0.89x |
| rowform `fetch_all()` off the engine *(no transaction)* | 0.5329 | — | — | | 0.91x | — | — |
| rowform `execute().scalars()` | 0.6116 | — | 1.7840 | | ~1.04x | — | ~0.98x |
| rowform `execute().all()` | 0.6922 | 1.3244 | — | | 1.18x | 1.09x | — |
| SQLAlchemy Core (positional) | 0.5381 | 0.9127 | 1.7573 | | 0.92x | 0.75x | ~0.97x |
| SQLAlchemy Core (`.mappings()`) | 1.5249 | — | — | | 2.60x | — | — |
| SQLAlchemy ORM | 2.8903 | 4.7624 | 4.8480 | | 4.92x | 3.90x | 2.68x |

One oddity worth recording rather than smoothing: the same-plumbing floor comes out
*below* the raw-asyncpg floor (0.3642 vs 0.4148). That is not the floor tripwire — the
"must do less" invariant binds each floor to the contenders it bounds, not floors to
each other, and both sit below every contender — but it does say SQLAlchemy's pool
checkout is cheaper than asyncpg's own here, which contradicts the intuition the two
floors were built on and deserves a decomposition of its own.

### Row layer alone (`mock` backend, zero driver cost)

The instrument that isolates the row layer. No connection, no pool, no transaction —
canned driver rows in, payload out. **No cross-mapper ratios, by design** (correction
14): the two mock seams exclude *different* layers, so each row is a per-mapper
regression floor tracked against its own history, and the rowform row now genuinely
includes the per-request cache-key lookup it always claimed to (it was prepared, and
therefore short-circuited, when the previous table was taken).

| contender | flat | join |
|---|---|---|
| hand-written dicts *(parsing floor)* | 0.0874 | 0.2195 |
| rowform | 0.2232 | 0.5027 |
| SQLAlchemy Core (positional) | 0.2111 | — |
| SQLAlchemy ORM | 1.7100 | 3.0526 |

For the hydrator on its own, use the two hand-rolled floors in the sqlite table, which
differ only in the row layer: **+19.2% / +7.1% / +3.6%** (flat/join/wide) for typed
objects over hand-written dicts.

### Reading the floors

**There are three, because "floor" was answering two questions at once and the reader
could not tell which.**

*Hand-rolled* is the absolute bound: the driver, a trivial connection pool, the DBAPI's
own `commit()`, no SQLAlchemy anywhere. It answers "what would this cost written by
hand, from scratch". Registered twice — once into dicts, once through rowform's
hydrator — so the row layer separates from everything else.

*On SQLAlchemy* holds the plumbing constant instead: SQLAlchemy's pool, SQLAlchemy's
transaction, the same compiled statement, hand-written dicts where rowform runs its
hydrator. It answers the adoption question — "I am already on SQLAlchemy; what does
rowform cost me?" — and it is the comparison to use, because a checkout and a
transaction are not costs rowform introduces. An application with an `AsyncSession` is
paying them before rowform is in the picture.

The distance between those two floors is the answer to "what does SQLAlchemy's plumbing
cost", and the 2026-08-15 sweep puts it near zero on sqlite (0.9255 → 0.9468, ~0.02 ms)
and *negative* on postgres (0.4148 → 0.3642 — SQLAlchemy's checkout beat asyncpg's own
pool). Earlier editions put the gap at 0.21 ms, and before that at 0.3–0.4 ms
attributed to the checkout alone; each successive measurement has shrunk it, the first
two were taken under conditions since found to be broken, and the checkout/transaction
split has still not been derived cleanly. Treat "SQLAlchemy's plumbing is expensive" as
unsupported by the current data.

**One caveat on the same-plumbing floor**: it hoists its compiled SQL and bound
parameters out of the timed region, which rowform cannot — a `bindparam` is re-bound per
call. On this shape the parameters are constant, so a hand-written application would
hoist them too; on one where they vary, the floor is flattered by however much
`CoreQuery.bind()` costs.

### The two tracks

`execute()` returns SQLAlchemy's own `Result`; `fetch_all()` returns hydrated objects.
Taken as `.scalars()`, the compatibility track **ties with the hot one in every cell
where both run** — the `Result` is built but no `Row` ever is. Taken as `.all()` it
costs 7-18%, which is one `Row` per row and the only real difference between the two
lines.

**`wide` is where the shapes converge.** Its columns are
`DateTime`/`Date`/`Numeric`/`Enum`/`Uuid`/nullable, so per-column type processors
dominate — and both sides run the *same* processors, leaving proportionally less to
skip. It is where the ORM gap closes most (1.94x against 2.57x on `flat`) and where
equal-work rowform gets closest to Core (a ~0.95x tie, against 0.89x ordered on
`flat`/`join`). A suite quoting one shape is quoting an extreme without saying so, in
whichever direction.

**And the differences live where the ratios compress them.** On `flat`/sqlite the whole
read is 1.40 ms, of which the row layer is a fraction: swapping the compiled hydrator
for hand-written dicts moves 0.18 ms (the two hand-rolled floors). The mock table is
where row-layer costs are actually visible — 0.09 / 0.22 / 0.21 / 1.71 ms for hand
dicts / rowform / Core / ORM on `flat` — though its rowform and Core rows exclude
different layers and must not be read as a head-to-head (correction 14). End-to-end
ratios compress all of this, and the compression is real — it is what an application
sees — but a reader who takes an end-to-end tie as a statement about result layers has
read it backwards.

### What the gate proves

Every table above passed the equivalence gate: each contender's JSON is compared byte
for byte before any timing starts. The `wide` shape produces **sha256=60c3f426… on
both sqlite and postgres** — the same 194,647 bytes from two drivers that disagree
about how to store almost every column in it, reproduced unchanged by the 2026-08-15
sweep across both contender families. That is the strongest available evidence that
bypassing `Row` is faithful rather than merely fast, and it is the check a
hand-written converter table failed 7 columns of (correction 11).

---

## Practices

**Enforce output equivalence before timing.** Every harness compares each contender's
bytes against a reference and aborts on mismatch — every contender is also re-run for
self-consistency (a lucky single run used to pass for everyone but the reference), and
under `--isolate` each timed child proves by hash that it produced the gated bytes,
since the gated objects and the timed objects live in different processes there. The
HTTP path has its own gate: `bench load run` byte-compares the case's response against
the rowform reference route before any level runs, then has locust enforce that byte
length on every response. What none of this catches: identical output says nothing
about whether one side took an expensive route to it — corrections 8 and 14 passed
these gates perfectly.

**Price any workaround one contender needs and the others do not.** A per-key cast, an
encoding fix-up, a defensive copy: each is a measurement artifact until timed against
the alternative. That the API you chose *requires* it is not evidence it is cheap.

**Measure more than one shape, along more than one axis.** The suite carries `flat`
(narrow), `join` (multi-entity) and `wide` (every type whose driver representation
differs from its Python one). Each exists because it makes a different mistake visible.
Ask which axis your suite holds constant *everywhere* — that is where a bug can live in
every contender at once and be invisible in all of them.

**Three floors, and they answer different questions.** A dicts-only floor bounds the
whole stack; a floor running the *same* hydrator over the same driver separates engine
cost from row-construction cost; a floor on *SQLAlchemy's own pool and transaction*
separates the cost of the abstraction from the cost of the plumbing underneath it.
Two of these existed for a long time and the third did not, which is how a gap that was
mostly pool-and-transaction got published as though it were row-layer work. Whenever a
floor is ambiguous about which question it answers, that ambiguity ends up in the
headline. And a floor coming out *above* the thing it bounds remains this suite's most
valuable tripwire.

**A floor must not hoist work the contender cannot.** The same-plumbing floor
pre-binds its statement once; rowform re-binds per call. That is legitimate only while
the parameters are constant, as they are in these shapes — write it down where it is not.

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
`bench micro`'s default is `--pin auto` — two whole physical cores chosen from the
machine's own topology — because the old hardcoded `6,7,8,9` crashed on small boxes and
silently landed on SMT siblings of one core on other layouts, the exact mistake this
practice describes.

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
a failed one. "Mid-run" must be the generator's own clock: locust runs as two
subprocesses per level (warmup, then measured) whose startup and ramp the parent cannot
see, so the measured process reports its test_start/test_stop timestamps and the socket
sample and both CPU denominators are aligned to that window rather than guessed from
sleep arithmetic — the guessed version could sample the gap between the two processes
(spurious FAIL) or the warmup process (a pass observing the wrong process).

**Calibrate the generator's headroom with a do-nothing endpoint.** A `/noop` route is
the cheapest probe available: if it is not well clear of every endpoint under test, the
client is the bottleneck and the run measures the client. This cuts both ways — locust
reported `/noop` 37% low because it saturates near 5400 rps on one core, so "my numbers
came from a standard tool" is not a correctness argument.

> A second implementation of the measuring instrument is worth more than another run of
> the first.

---

## The fourteen corrections

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

### 12. Scoring an isolation guarantee as row-layer speed

Every contender ran whatever transaction its own API implied. SQLAlchemy's `connect()`
and `AsyncSession` autobegin on first statement and roll back on release; rowform's
engine-level `fetch_all()` opens no transaction at all. So Core and the ORM paid for a
transaction on every read and rowform did not, and the difference went into the ratio as
though it were the row layer.

The fix is one line per contender — every read now runs inside `BEGIN`…`COMMIT` — and it
cost most of the published Core margin. The one-shot path is still registered, as
`rowform (no transaction)`, because it is a real and cheaper way to read; it is priced
next to the transactional row instead of standing in for it.

> When two contenders expose different *semantics* through similarly-named calls, the
> benchmark has to pick which semantics it is measuring and impose them on everyone. The
> tell here was that nobody could say what guarantee the published number described.

### 13. The harness monkey-patching itself

`python -m benchmarks` mounted every subcommand eagerly. One of them imports locust,
which calls `gevent.monkey.patch_all()` at import time, replacing `threading.Thread` for
the whole process. aiosqlite runs every statement on a per-connection worker thread, so
the driver under test was not using real threads. Measured A/B: **+27%** on a hand-rolled
floor, **+33%** on rowform, and the ratio between them moved 1.05x → 1.10x.

Every number this file published, and every entry in `RUNS.md` before 2026-08-02, was
taken that way. Nothing looked wrong: runs completed, the equivalence gate passed, trial
spread stayed tight. The table was simply 30% slow.

It surfaced sideways. A new import deadlocked the suite; the deadlock was fixed by
deferring that import, and the *reason* a benchmark process had a patched `threading` at
all went unasked for another hour. `load`/`profile` are now mounted lazily and
`timing.assert_unpatched_threading()` refuses to time anything under the patch.

The class was declared closed here and wasn't: `bench profile micro`'s "unprofiled"
baseline — a timed measurement printed in the same ms/req shape as `bench micro`'s —
still ran under the patch, because `cli/profile.py` imported locust at module scope for
its *other* subcommand. locust is now imported only inside `bench profile load`, and
`profile micro` runs the same assertion `micro` does. The env capture also records
gevent/greenlet and whether the patch is active, so this whole class is now visible in
every artifact.

> A benchmark's own process is part of the measurement. Anything a CLI imports on the
> way to `main()` is inside the experiment.
>
> And: when a symptom is weird enough to need a workaround, the workaround is not the
> finding. The finding is why the symptom was possible.

### 14. The headline rows did less work than their rivals (flat/sqlite: a win became a loss)

Found by an external audit of the suite, not by self-attack — the same blind spot as
the two reviewer-caught corrections above. The `rowform` rows carried two structural
advantages the comparison never priced:

**Prepared once vs. compiled-cache lookup per call.** Every rowform arm hoisted
`engine.prepare(...)` to setup, and a prepared `CoreQuery` short-circuits rowform's
per-call structural cache key — while every SQLAlchemy arm passed a raw `select()` to
`conn.execute()` per iteration, paying SQLAlchemy's `_generate_cache_key()` and
execution-context construction inside the timed region. The cell was labelled "stock
result layer"; part of its margin was prepare-once vs. execute-per-call API shape.

**Serialized in C vs. a per-row Python pass.** rowform arms handed dataclasses straight
to orjson; every comparator built a dict per row in Python first. This violated the
payload rule written at the top of `contenders.py` itself — prose that nothing
mechanical enforced.

The fix is two labelled families, not a silent re-blend: plain `rowform` now does equal
work (unprepared, the same shared per-shape payload builders the ORM rows use — shared
so parity is structural, not re-reviewed), and `rowform (idiomatic)` is the code an
application would write (prepared once, objects straight to orjson), with the delta
between the two rows pricing the API-shape advantages explicitly. The same audit also
retired the cross-mapper mock ratios (each mock seam excludes a different layer — the
mock module said so while the CLI computed them anyway) and made the mock rowform arm
unprepared, so the "catches cache-key regressions" claim is true again.

The recorded sweep that followed (RUNS.md, 2026-08-15) sized the mistake: at equal
work, Core positional is *ordered ahead* of rowform — 0.89x on flat/join (sqlite),
0.75x on join (postgres) — where the blended rows had published a rowform win and then
a tie. The idiomatic row carries the old margin, at parity with Core.

> The two claims — "faster result layer" and "faster endpoint as you'd actually write
> it" — are both real, and blending them made the first one wrong. Fairness rules that
> live in prose drift; every rule here that could become a shared builder, a gate, or a
> test now is one.

---

## Reproducing

```bash
# the bench harness lives in dependency *groups*, not extras — `just bench`
# runs everything through `uv run --all-groups`, so no sync step is required;
# to work outside just, use `uv sync --all-groups`.

# correctness first: a benchmark measures whatever the library does, so a wrong
# library is just a fast wrong answer
just test . --pg-required

just bench env check          # audits boost/turbo, dirty tree, loadavg (exits non-zero)

# dev loop (fast, single trial, NOT publishable — no ratios, quotable=False):
just bench micro run

# the publishing recipe behind any recorded table:
just bench micro run --shape flat --iterations 1500 --warmup 200 --trials 3 --isolate --record
```

Postgres is **two separate pipelines** — they collide on port 5432 if mixed:

```bash
# micro: bring up a server yourself and hand the DSN over (seeding DROPS and
# recreates the shape's tables on it)
just bench db up && just bench db seed
just bench micro run --backend postgres --isolate --trials 3 --record --pg-dsn "$(just bench db dsn)"
just bench db down            # also clears the state file after a reboot/prune

# load & profile-load: provision their OWN container per run. With a
# `bench db up` server still standing, pass --pg-port to avoid its port.
just bench load run --case postgres-flat-rowform --pg-port 5433
just bench load run --case all           # or sqlite / postgres, the group sweeps
just bench profile micro                 # cProfile + pyinstrument, side by side
```

The raw artifacts behind the pre-rewrite corrections were deleted once the lessons were
written down here; they are recoverable from git history, along with the 28 scripts that
produced them (`git checkout 32ad4a1 -- benchmarks/`).
