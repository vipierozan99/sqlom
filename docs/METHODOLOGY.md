# Benchmarks, and how to trust them

The numbers, the practices that produced them, and the fifteen published claims that
turned out to be wrong.

One rule sits above the rest: **a result that flatters the thing you built is a bug
report until you have tried to break it.**

---

## Results

**Two shas, one per table, and the reason is stated rather than smoothed.** The sqlite
and mock tables were taken at `122e035`; the postgres table at `033812a`, which adds the
postgres `join` floors and nothing that touches sqlite. Both are `quotable=True`, taken
the same day on the same box under the same recipe, with artifacts on
`bench/2026-08-16-boost-off-floors` and `bench/2026-08-16-pg-join-floors`, indexed in
[RUNS.md](RUNS.md). Only the postgres table was re-recorded because only postgres
contenders changed; re-recording sqlite would have replaced good numbers with the
dispersion described below, which is the opposite of the point.

sqlite is an ephemeral 200,000-row database; postgres 16 is an ephemeral docker container
on the same box. 1000 rows per read, 1500 timed iterations after 200 warmup, **3 trials,
one contender per process**, GC off, pinned to two whole physical cores (`--pin auto`),
**cpu boost disabled**. **Every contender reads inside `BEGIN`…`COMMIT`** with two
deliberate exceptions and one backend-specific caveat: `rowform (no transaction)` is
registered without one so the cost of the guarantee is a row rather than folded into the
others, and on **sqlite the SQLAlchemy contenders send no `BEGIN` at all** — pysqlite
begins implicitly before DML and never before a SELECT, so `engine.begin()` around a read
reaches the wire as nothing. rowform does send one, because without it pysqlite puts
`begin_nested()`'s savepoint outside its transaction. That asymmetry is real rather than a
handicap, so it is priced from both sides rather than erased: `rowform (no transaction)`
takes the guarantee off rowform and `SQLAlchemy Core (positional, real transaction)` puts
it onto Core (correction 16). The floors *do* send it, because a floor exists to isolate
one variable and had been a round trip lighter than the thing it bounded.

**Two read sizes per shape where both are recorded.** A 1000-row read is ~92% per-row
work, so a per-request cost is ~8% of it and a change in one is invisible; the `@1`
column is the same read with the rows taken out, and is where fixed cost is legible. The
column heading carries the rows-per-read whenever a table holds more than one.

Absolute times are roughly 1.9x the pre-2026-08-16 sweeps' and are *not* comparable to
them: with turbo off the cores sit at base clock. Ratios are the comparable quantity, and
the point of paying that price is that these are the first numbers nothing in the gate
disputes.

```bash
# Boost off, or the gate refuses to call the run quotable. The only step needing
# root, which is why it is its own script and not part of the sweep; `on` restores.
sudo scripts/bench_cpu_boost.sh off
just bench env check          # must print "no warnings"

for shape in flat join wide; do
  just bench micro run --shape "$shape" \
    --iterations 1500 --warmup 200 --trials 3 --isolate --record
done
# The small read. It needs a longer window than the big one and more trials: at
# ~0.35 ms a request, 1500 iterations is a 0.5 s measurement and the cell came
# back 17-24% dispersed. The dispersion is between-trial drift rather than
# sampling noise, so 20000 iterations bounds it and 5 trials bound the estimate.
just bench micro run --shape flat --limit 1 \
  --iterations 20000 --warmup 2000 --trials 5 --isolate --record
for shape in flat join; do
  just bench micro run --shape "$shape" --backend mock \
    --iterations 1500 --warmup 200 --trials 3 --isolate --record
done
just bench db up
for shape in flat join wide; do
  just bench micro run --shape "$shape" --backend postgres \
    --iterations 1500 --warmup 200 --trials 3 --isolate --record \
    --pg-dsn "$(just bench db dsn)"
done
just bench db down
sudo scripts/bench_cpu_boost.sh on
```

**The governor is left at whatever the box normally uses** (`powersave` here), and
that is deliberate: with turbo off this machine's busy core already sits at
`base_frequency`, 1.9 GHz — the sweep below recorded 1874–1894 MHz on its pinned
cores — so pinning `performance` would mostly raise the *idle* floor during the awaits
inside the timed region rather than change the speed the work runs at. Boost is the one
knob that demonstrably moves these numbers, so it is the one the recipe touches.

Medians of the per-trial medians, in milliseconds, lower is better. Ratios come from
`stats.ratio_with_spread`, so `~` marks a pair the trials do not actually order —
either the worst-case interval spans 1.0 or the medians are within 5%. Worst
trial-to-trial spread in the published cells: **4.2%** (sqlite), 3.0% across the postgres
floors and rowform rows, 4.2% (mock). Two postgres rows are worse and say so in place:
`Core (.mappings())` at 20.3% and `execute().all()` at 10.3%, neither of which any claim
here rests on.

> **Every gate passes on both sweeps.** Clean tree, boost off *and verified still off at
> the end of each run*, equivalence enforced and self-consistent (child processes
> hash-verified), one contender per process, no thermal-throttle events.
>
> **This box does not reproduce sqlite `join` and `wide` reliably, and that is why there
> are two shas.** A boost-off sweep taken at `033812a` reproduced every postgres cell
> and sqlite/`flat` to within ~0.1% of the numbers above, then put the sqlite `join` and
> `wide` floors 16–37% high with 15–32% trial spread — in one case a floor above the
> contender it bounds, which is correction 10's failure. The pattern is a first trial
> matching the published value and later trials 20% slower, so it is dispersion within a
> run rather than a different measurement. Boost was off at both endpoints, load average
> was the benchmark alone, no throttle events, package temperature 44 °C, and the cause
> is **not diagnosed** — `tuned` re-enabling boost was ruled out as the explanation (it
> does set `boost=1`, which the end-of-run check now catches, but stopping it did not
> change the dispersion). Published here is the sweep each table reproduces; the
> instability is recorded as open in [RUNS.md](RUNS.md) rather than presented as noise
> that happens to have gone away.

> **The headline, after correction 14 and the decomposition rungs.** At equal work,
> the compiled hydrator does not beat SQLAlchemy Core's result layer: Core positional
> is 0.83–0.94x across the ordered cells, `~0.96x`/`~1.00x` (ties) on `wide` — and the
> comparison deliberately gives Core the *cheaper* payload builder. The decomposition
> rows say where rowform's idiomatic margin actually lives: `rowform (prepared)` ties
> equal-work `rowform` in all four cells that have one (~0.99–1.00x), so **the
> structural cache key costs nothing measurable and `prepare()` is API convenience, not
> a performance lever**; across those four the entire idiomatic delta (0.79–0.91x, or
> 0.21–0.59 ms per read) is the serialization path — dataclasses straight into orjson's
> C serializer instead of a per-row Python dict pass. `wide` has no prepared rung, so
> its 0.89–0.92x is assumed to split the same way rather than shown to. The idiomatic
> row lands at or near parity with Core positional while returning typed dataclasses
> where Core returns tuples. Against the ORM, equal-work rowform is **2.0–5.4x** faster
> and the idiomatic spelling **2.2–6.4x**.

Runs land in `benchmarks/results/runs/`, which is gitignored on main; chosen ones are
committed to a dated `bench/` branch by hand and indexed in [RUNS.md](RUNS.md).

### sqlite

| contender | flat @1000 | join @1000 | wide @1000 | flat @1 | | flat @1000 | join @1000 | wide @1000 | flat @1 |
|---|---|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor: no SQLAlchemy)* | 1.9611 | 3.4929 | 6.5579 | 0.1868 | | 0.77x | 0.77x | 0.95x | 0.54x |
| raw driver + the same hydrator *(floor: no SQLAlchemy)* | 2.0235 | 3.6771 | 6.0907 | 0.1894 | | 0.79x | 0.81x | 0.88x | 0.55x |
| same pool + transaction → dicts *(floor: same plumbing)* | 2.1281 | 3.6604 | 6.0838 | 0.3082 | | 0.83x | 0.81x | 0.88x | 0.89x |
| **rowform** `fetch_all()` *(equal work)* | **2.5552** | **4.5244** | **6.9225** | **0.3473** | | **1.00x** | **1.00x** | **1.00x** | **1.00x** |
| rowform *(prepared, equal payload — prices the cache key)* | 2.5399 | 4.4831 | — | 0.3364 | | ~0.99x | ~0.99x | — | ~0.97x |
| rowform *(idiomatic: prepared once, direct to orjson)* | 2.3443 | 3.9408 | 6.3580 | 0.3524 | | 0.92x | 0.87x | 0.92x | ~1.01x |
| rowform `fetch_all()` off the engine *(no transaction)* | 2.3987 | — | — | 0.2263 | | 0.94x | — | — | 0.65x |
| rowform `execute().scalars()` | 2.5931 | — | 6.9705 | 0.3540 | | ~1.01x | — | ~1.01x | ~1.02x |
| rowform `execute().all()` | 2.8261 | 4.7974 | — | 0.3589 | | 1.11x | 1.06x | — | ~1.03x |
| SQLAlchemy Core (positional) | 2.4777 | 4.0978 | 6.6716 | 0.3825 | | ~0.97x | 0.91x | ~0.96x | 1.10x |
| SQLAlchemy Core (positional) *(with pysqlite's transaction recipe — prices the guarantee on Core's side)* | 2.5520 | — | — | 0.4632 | | ~1.00x | — | — | 1.33x |
| SQLAlchemy Core (`.mappings()`) | 4.6774 | — | — | 0.3888 | | 1.83x | — | — | 1.12x |
| SQLAlchemy ORM | 7.9308 | 13.5563 | 14.5928 | 0.5411 | | 3.10x | 3.00x | 2.11x | 1.56x |
| SQLAlchemy ORM (`MappedAsDataclass`) | 7.8324 | 13.5007 | 14.6669 | 0.5401 | | 3.07x | 2.98x | 2.12x | 1.56x |

### postgres (asyncpg)

| contender | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| one dedicated connection → dicts *(floor: no pool)* | 0.8690 | — | — | | 0.64x | — | — |
| raw driver + pool, `reset` off → dicts *(floor: prices asyncpg's reset)* | 0.9296 | — | — | | 0.68x | — | — |
| raw driver → dicts *(floor: no SQLAlchemy)* | 1.0008 | 1.9468 | — | | 0.73x | 0.74x | — |
| same pool + transaction → dicts *(floor: same plumbing)* | 1.0360 | 1.9520 | — | | 0.76x | 0.74x | — |
| **rowform** `fetch_all()` *(equal work)* | **1.3637** | **2.6411** | **5.1598** | | **1.00x** | **1.00x** | **1.00x** |
| rowform *(prepared, equal payload — prices the cache key)* | 1.3521 | 2.6274 | — | | ~0.99x | ~0.99x | — |
| rowform *(idiomatic: prepared once, direct to orjson)* | 1.1397 | 2.0892 | 4.6134 | | 0.84x | 0.79x | 0.89x |
| rowform `fetch_all()` off the engine *(no transaction)* | 1.3034 | — | — | | ~0.96x | — | — |
| rowform `execute().scalars()` | 1.3926 | — | 5.1696 | | ~1.02x | — | ~1.00x |
| rowform `execute().all()` | 1.6189 | 2.8658 | — | | 1.19x | 1.09x | — |
| SQLAlchemy Core (positional) | 1.2816 | 2.1796 | 5.1655 | | 0.94x | 0.83x | ~1.00x |
| SQLAlchemy Core (`.mappings()`) | 3.5055 | — | — | | 2.57x | — | — |
| SQLAlchemy ORM | 7.3421 | 12.0354 | 12.5414 | | 5.38x | 4.56x | 2.43x |

**The previous sweep's oddity was a bug in a floor, not a finding about pools.** The
same-plumbing floor sat *below* the raw-asyncpg floor because it was sending no
transaction at all: `sa_conn.begin()` marks a transaction in Python, and SQLAlchemy
emits `BEGIN` lazily with the first statement it routes itself — but that floor
deliberately awaits the driver connection directly, so the read went out as a bare
`SELECT` in autocommit while every contender it bounds sent `BEGIN`/`SELECT`/`COMMIT`.
Two round trips light. Correction 15 has the audit; the floor now opens the transaction
on the driver connection, and the ordering anomaly is gone (1.0008 → 1.0360, above the
raw floor rather than below it, and the same way round on `join`).

With that fixed and `asyncpg.Pool`'s release-time reset priced as its own rung, the
ladder reads cleanly for the first time — each step one variable, all four floors
sending identical SQL and identical payloads:

| step | ms | over `no pool` | of a 1000-row read |
|---|---|---|---|
| one dedicated connection, no pool | 0.8690 | — | — |
| + `asyncpg.Pool` acquire/release, `reset` off | 0.9296 | 0.0606 | 4.4% |
| + its reset round trip (`asyncpg.Pool` as shipped) | 1.0008 | 0.1317 | 9.7% |
| SQLAlchemy's pool instead | 1.0360 | 0.1670 | 12.2% |

**This inverts what the 2026-08-15 sweep published.** Going through SQLAlchemy's pool
costs **0.167 ms per request against a bare connection — 1.27x what `asyncpg.Pool` as
shipped costs, and 2.8x what it costs with its reset disabled.** The earlier claim
(SQLAlchemy's checkout ~0.008 ms, asyncpg's ~0.058 ms, SQLAlchemy ~7x cheaper) was
entirely the missing `BEGIN`/`COMMIT`; it is withdrawn. Two independent boost-off sweeps
agree on all four rungs to within ~1%, which is the strongest thing said about any
number here.

Two caveats on reading the table, both in the conservative direction for SQLAlchemy.
Its 0.167 ms is the *pool path*, not the checkout alone — the floor also builds a
`Connection` and awaits `get_raw_connection()` per request, so the checkout by itself is
smaller. And asyncpg's reset is not waste: `SELECT pg_advisory_unlock_all(); CLOSE ALL;
UNLISTEN *; RESET ALL;` buys session hygiene that SQLAlchemy's `reset_on_return='rollback'`
does not attempt (through the asyncpg adapter it is a no-op with no transaction open,
confirmed against `log_statement=all`). Comparing the shipped defaults charges asyncpg
for work the other side skips, which is correction 8's mistake — hence the `reset` off
rung, which is the like-for-like comparison, and it is the one SQLAlchemy loses by most.

Design consequence, and it is no longer "the pool is free": **riding SQLAlchemy's pool
costs ~12% of a 1000-row read against no pool at all, and ~8% against the cheapest
pooled alternative measured.** That buys the `bind=` case an engine owning its own pool
cannot do at any price, and it is paid per checkout rather than per row — so it shrinks
as reads get larger — but it is a real trade to state rather than a rounding error.

**The `join` floors close an extrapolation, and it was the wrong size.** The join column
carries this suite's largest ORM multiple, and until now had no postgres floor under it:
"idiomatic runs ~13% above its floor" was carried over from sqlite. Measured, the
same-plumbing floor is 1.9520 ms and `rowform (idiomatic)` is 2.0892 — **+7.0%, not
~13%** (sqlite's own figure is +11.3%, so the borrowed number was closer to sqlite's than
to postgres's, which is exactly the failure mode of borrowing it). Equal-work rowform is
+35.3% over the same floor and Core positional +11.7%. And the two `join` floors land
0.3% apart (1.9468 hand-rolled against 1.9520 on SQLAlchemy's plumbing), against 3.5%
apart on `flat` — at arity two the payload work grows while the per-request pool cost does
not, so the plumbing matters proportionally less. That is the argument for instrumenting
each shape instead of scaling one shape's floor.

### Row layer alone (`mock` backend, zero driver cost)

The instrument that isolates the row layer. No connection, no pool, no transaction —
canned driver rows in, payload out. **No cross-mapper ratios, by design** (correction
14): the two mock seams exclude *different* layers, so each row is a per-mapper
regression floor tracked against its own history, and the rowform row now genuinely
includes the per-request cache-key lookup it always claimed to (it was prepared, and
therefore short-circuited, when the previous table was taken).

| contender | flat | join | | flat | join |
|---|---|---|---|---|---|
| hand-written dicts *(parsing floor)* | 0.2179 | 0.5240 | | — | — |
| rowform | 0.5579 | 1.2481 | | — | — |
| SQLAlchemy Core (positional) | 0.5203 | — | | — | — |
| SQLAlchemy ORM | 4.2696 | 7.5750 | | — | — |

For the hydrator on its own, use the two hand-rolled floors in the sqlite table, which
differ only in the row layer: **+18.3% / +8.2% / +3.4%** (flat/join/wide) for typed
objects over hand-written dicts.

### Reading the floors

**There are three (four on postgres `flat`), because "floor" was answering two
questions at once and the reader could not tell which — and then two of them disagreed
about the pool, which turned out to be a bug in one of them.**

*Hand-rolled* is the absolute bound: the driver, a trivial connection pool, the DBAPI's
own `commit()`, no SQLAlchemy anywhere. It answers "what would this cost written by
hand, from scratch". Registered twice — once into dicts, once through rowform's
hydrator — so the row layer separates from everything else.

*On SQLAlchemy* holds the plumbing constant instead: SQLAlchemy's pool, a real
transaction, the same compiled statement, hand-written dicts where rowform runs its
hydrator. It answers the adoption question — "I am already on SQLAlchemy; what does
rowform cost me?" — and it is the comparison to use, because a checkout and a
transaction are not costs rowform introduces. An application with an `AsyncSession` is
paying them before rowform is in the picture.

*No pool* and *`reset` off* (postgres `flat` only) decompose the pool the other two
argued about. *No pool* is one dedicated asyncpg connection with no pool at all — the
bottom of the ladder, so the distance from it to each pooled floor is what going through
that pool costs per request. *`reset` off* is the hand-rolled floor with
`create_pool(reset=<no-op>)`, which removes `asyncpg.Pool`'s release-time
`RESET ALL` round trip and nothing else, so asyncpg's cost splits into machinery
(0.0606 ms) and that round trip (0.0712 ms) instead of arriving as one number compared
against a pool that skips the hygiene entirely.

Both `flat` and `join` carry the hand-rolled and same-plumbing pair on postgres now.
`join` was the gap worth closing: it holds the largest ORM multiple in the suite and its
floor claim was borrowed from sqlite, which turned out to be the wrong size (+11.3% there
against +7.0% measured here).

The distance from *no pool* to *on SQLAlchemy* is the answer to "what does SQLAlchemy's
plumbing cost": **0.167 ms per request on postgres `flat`, ~12% of a 1000-row read**, and
more than either asyncpg pooled path. It is not a constant to carry between shapes — on
`join` the same-plumbing floor sits only 0.3% over the hand-rolled one, because the
payload work grows with arity while the per-request pool cost does not. On sqlite, where
the pool is Python-only, the same-plumbing floor sits 0.167 ms over the hand-rolled one
on `flat` (~6.5% of the read) — a figure that only became meaningful once both floors
sent the same transaction, since the earlier 0.110 ms compared a floor that opened one
against a floor that did not (correction 16). Earlier editions put the postgres
gap at 0.21 ms, then at 0.3–0.4 ms attributed to the checkout alone, then — two sweeps
ago — at *negative*, which is what a floor missing two round trips looks like. The
current answer is neither "expensive" nor "free": ~12% of a 1000-row `flat` read, paid
per checkout, buying a capability rather than nothing.

**One caveat on the same-plumbing floor**: it hoists its compiled SQL and bound
parameters out of the timed region, which rowform cannot — a `bindparam` is re-bound per
call. On this shape the parameters are constant, so a hand-written application would
hoist them too; on one where they vary, the floor is flattered by however much
`CoreQuery.bind()` costs.

### The two tracks

`execute()` returns SQLAlchemy's own `Result`; `fetch_all()` returns hydrated objects.
Taken as `.scalars()`, the compatibility track **ties with the hot one in every cell
where both run** — the `Result` is built but no `Row` ever is. Taken as `.all()` it
costs 6-18%, which is one `Row` per row and the only real difference between the two
lines.

**`wide` is where the shapes converge.** Its columns are
`DateTime`/`Date`/`Numeric`/`Enum`/`Uuid`/nullable, so per-column type processors
dominate — and both sides run the *same* processors, leaving proportionally less to
skip. It is where the ORM gap closes most (2.05x against 2.99x on `flat`, sqlite) and
where equal-work rowform gets closest to Core (a ~0.96x tie, against 0.94x/0.88x ordered
on `flat`/`join`). A suite quoting one shape is quoting an extreme without saying so, in
whichever direction.

**And the differences live where the ratios compress them.** On `flat`/sqlite the whole
read is 2.66 ms, of which the row layer is a fraction: swapping the compiled hydrator
for hand-written dicts moves 0.06 ms (the two hand-rolled floors). The mock table is
where row-layer costs are actually visible — 0.22 / 0.56 / 0.52 / 4.27 ms for hand
dicts / rowform / Core / ORM on `flat` — though its rowform and Core rows exclude
different layers and must not be read as a head-to-head (correction 14). End-to-end
ratios compress all of this, and the compression is real — it is what an application
sees — but a reader who takes an end-to-end tie as a statement about result layers has
read it backwards.

### What the gate proves

Every table above passed the equivalence gate: each contender's JSON is compared byte
for byte before any timing starts. The `wide` shape produces **sha256=60c3f426… on
both sqlite and postgres** — the same 194,647 bytes from two drivers that disagree
about how to store almost every column in it, reproduced unchanged by the 2026-08-16
sweep across both contender families. The postgres `flat` cell is the one worth naming
here: **all 13 rows — 4 floors and 9 contenders — emit the same 77,535 bytes**, so the
pool ladder above is comparing paths to an identical payload. Byte equality is not
enough on its own, though — it is exactly what let a floor skip `BEGIN`/`COMMIT` for
three sweeps (correction 15). That is the strongest available evidence that
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

## The sixteen corrections

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


### 15. A floor that opened no transaction, and the pool finding built on it

Self-attack, prompted by asking whether a claim was *measured* rather than whether it
was plausible. The published pool decomposition — "SQLAlchemy's pool checkout costs
~0.008 ms against `asyncpg.Pool`'s ~0.058 ms, so riding SQLAlchemy's pool is the
cheapest pooled path" — was not a pool measurement at all.

`pg_flat_sa_plumbing_dict` wrapped its read in `async with sa_conn.begin()`. That marks
a transaction in Python; SQLAlchemy emits the `BEGIN` **lazily**, when the first
statement is routed through SQLAlchemy. The floor deliberately awaits the *driver*
connection instead — the whole point of that arm is to measure the row layer without
`greenlet_spawn` in the path — so SQLAlchemy never had a statement to attach the `BEGIN`
to. The read went out as a bare `SELECT` in autocommit, while every contender the floor
bounds sent `BEGIN`/`SELECT`/`COMMIT`. Two round trips light, on the one backend where a
transaction is round trips rather than Python.

That is correction 10 from the other side — a floor doing *less* work than the thing it
bounds — and it accounts for the whole of the recorded oddity. The same-plumbing floor
came out below the raw-asyncpg floor because it skipped `BEGIN`/`COMMIT`, not because
SQLAlchemy's checkout is cheap. With the transaction opened on the driver connection the
ordering anomaly disappears and the sign of the headline flips: **SQLAlchemy's pool path
costs 0.167 ms per request against a bare connection, 1.27x `asyncpg.Pool` as shipped
and 2.8x it with `reset` disabled.** The 7x-cheaper claim is withdrawn.

**Why the equivalence gate could not catch it.** Both spellings return byte-identical
payloads — that is what the gate compares. Transaction *spelling* is invisible to it,
and so is anything else about how the bytes were obtained. What caught it was asking the
server: `log_statement=all` on the bench container, then counting `BEGIN`/`COMMIT` per
iteration for every contender in the cell. That audit is now recorded in
`contenders.py`'s module docstring as the check to repeat whenever a floor is added,
because "does this contender send what its name claims" is a different question from
"does it return the right bytes", and only the second one has a gate.

> Three sweeps quoted a plumbing cost derived from this floor, each one shrinking it —
> 0.3–0.4 ms, then 0.21 ms, then ~0.01 ms, then negative. A number that keeps
> approaching zero and then crosses it is not converging; it is a bug getting closer to
> the surface. "Treat SQLAlchemy's plumbing as expensive" was unsupported, but so was
> the replacement, and the honest reading was available the whole time from the wire.

### 16. The sqlite floors sent no transaction, and rowform's cost three round trips

Correction 15 one backend over, found the same way — by asking what a gap *contains* and
tracing the wire rather than reasoning about it. The question was why `rowform` sits
0.64 ms above the `raw driver + the same hydrator` floor on sqlite `flat`.

pysqlite begins implicitly before DML and never before a SELECT, so `sa_conn.begin()`
around a read sends nothing at all. The sqlite floors were built on that fact, with the
module docstring arguing that a floor issuing `BEGIN` by hand would do *more* work than
the contenders it bounds. True of the SQLAlchemy contenders. Not true of rowform, which
applies SQLAlchemy's own pysqlite recipe (`SqliteDriver.configure`) because without it
`begin_nested()`'s savepoint lands outside its transaction — a silent data-loss bug, and
the reason that code exists.

So on this backend rowform's read was inside a real transaction and every contender and
floor it was compared against was in autocommit. Two consequences, both published:

* **`floor: on SQLAlchemy (dict)` was pricing the row layer plus a transaction.** README
  said "what separates it from rowform is the row layer and nothing else". It was a round
  trip lighter than the thing it bounded.
* **The sqlite pool figure was measured against that floor.** "0.110 ms (~4%)" came from
  a pair where one side sent `BEGIN` and the other did not.

Tracing the hops turned up a second thing, this one a real cost rather than a
mismeasurement: rowform sent its `BEGIN` through `conn.exec_driver_sql`, which reaches
aiosqlite as three worker-thread hops — `cursor()`, `execute()`, `close()` — where the
driver's own `execute` makes the cursor inside the same hop. Seven hops per `begin()`
scope against Core's six for the same read. Sending it on the driver connection instead
is worth **0.108 ms per request** on `flat @1000` (−4.1%) and **0.099 ms** at `flat @1`
(−22.3%), with `rowform (no transaction)` — the one row that never sends a `BEGIN` — flat
at +0.4% as the control.

**What the fix to the floors costs the claim.** Making the floors wire-equivalent narrows
their gap to rowform, which flatters rowform. That direction is uncomfortable and the
justification cannot be the outcome; it is `contenders.py`'s own rule, "match whatever the
contenders' transaction actually costs on this backend". Core is left at six hops on
purpose — sending no `BEGIN` is what stock SQLAlchemy really does here, and charging it
for one would measure a library nobody runs. The asymmetry is priced from both sides
instead, by `rowform (no transaction)` and by a new `SQLAlchemy Core (positional, real
transaction)` row.

**Why no gate caught it, again.** Correction 15 recorded that the equivalence gate cannot
see transaction spelling, because both spellings return identical bytes. That lesson was
recorded for postgres and the sqlite half of the same bug went on shipping. The audit
that catches it is per-driver: `log_statement=all` on postgres, and on sqlite counting
`aiosqlite.Connection._execute` calls per request, which is now what
`tests/test_transactions.py::TestSqliteBeginCost` asserts — a hop count, because the two
spellings are behaviourally identical and every other test passes either way.

---

## Reproducing

```bash
# the bench harness lives in dependency *groups*, not extras — `just bench`
# runs everything through `uv run --all-groups`, so no sync step is required;
# to work outside just, use `uv sync --all-groups`.

# correctness first: a benchmark measures whatever the library does, so a wrong
# library is just a fast wrong answer
just test . --pg-required

# exits non-zero on any of: boost/turbo on *or* unreadable, an active gevent
# monkey-patch, a dirty tree, a high loadavg
just bench env check

# dev loop (fast, single trial, NOT publishable — no ratios, quotable=False):
just bench micro run

# the publishing recipe behind any recorded table:
just bench micro run --shape flat --iterations 1500 --warmup 200 --trials 3 --isolate --record
```

Postgres is **two separate pipelines** — they collide on port 5432 if mixed:

```bash
# micro: bring up a server yourself and hand the DSN over. The run seeds the
# shape itself, DROPPING and recreating its tables on that server; `just bench
# db seed` does the same by hand, for inspecting the data without a run.
just bench db up
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
