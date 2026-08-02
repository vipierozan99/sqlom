# Recorded runs

## 2026-08-02 — transactions equalised, pools equalised, and a harness bug that invalidates every run above

Branch `type-the-rest-of-the-read-path`, **not recorded to a `bench/` branch**: these
numbers are provisional and should be replaced rather than indexed. sqlite and mock
only; postgres was not run.

```bash
for s in flat join wide; do
  just bench micro run --shape=$s --iterations=1500 --warmup=200 --trials=3 --isolate --record
done
uv run python scripts/publish_tables.py benchmarks/results/runs/*/run.json
```

200,000 rows, 1000 per read, 1500 iterations after 200 warmup, 3 trials, one contender
per process, gc off, pinned to cpus 6-9. Worst trial-to-trial spread anywhere: **15.2%**.

`quotable=False` on the usual clause (cpu boost needs root to disable) — and on a second
one the gate cannot see: a browser, an editor and a music player were scheduled onto the
pinned cores throughout. `--pin` pins the benchmark *onto* cores; it cannot keep anything
else *off* them. That is most of the 15.2%.

**1. The harness was measuring itself wrong, and had been for every run in this file.**
`python -m benchmarks` mounted every subcommand eagerly, `benchmarks.cli.load` imports
`benchmarks.load.locust`, and importing locust runs `gevent.monkey.patch_all()`. That
replaces `threading.Thread` process-wide; aiosqlite gives every connection a worker
thread, so the driver was not using real threads. Measured A/B, one process per arm,
three reps:

| | gevent off | gevent on | |
|---|---|---|---|
| hand-rolled floor, sqlite flat | 1.2940 ms | 1.6451 ms | +27% |
| rowform, sqlite flat | 1.3580 ms | 1.8076 ms | +33% |
| ratio | 1.05x | 1.10x | |

Absolutes ~30% slow, ratios skewed ~5% because it does not hit both arms equally.
`load`/`profile` are now mounted lazily and `timing.assert_unpatched_threading()` fails
the run rather than quietly inflating it. **Every earlier entry in this file, and every
table published from one, was taken under the patch.**

**2. Every contender now reads inside `BEGIN`…`COMMIT`.** SQLAlchemy autobegins on first
statement and rolls back on release, so Core and the ORM were always paying for a
transaction while rowform's engine-level `fetch_all()` opened none. Part of rowform's
published margin was a weaker isolation guarantee scored as row-layer speed. The
one-shot path is still registered, as `rowform (no transaction)`, and priced separately:
0.83x on flat.

**3. Pools equalised at `pool_size=4, max_overflow=0`.** rowform ran `1+3`, the
SQLAlchemy contenders ran the engine default `5+10`, and the asyncpg floor ran
`min1/max4`. `1+3` and `4+0` have the same ceiling and different behaviour — SQLAlchemy
closes overflow connections on return, asyncpg retains them. Four concurrent checkouts
over two rounds reused **1** connection under `1+3` and **4** under `4+0`, so the
asyncpg floor was holding four alive while every SQLAlchemy contender re-established
three per request.

**4. A third floor, because "floor" was answering two questions.** `floor: hand-rolled`
(no SQLAlchemy at all) bounds the stack; `floor: on SQLAlchemy` (its pool, its
transaction, hand-written dicts) prices the abstraction for someone already on
SQLAlchemy. The gap between them — ~0.21 ms on flat — is the plumbing, and it used to be
published as though it were row-layer cost. A `hand-written dict (mock)` arm was added
as the parsing floor.

**5. Retractions from the entries below.** "The Core ratios narrowed" (1.26x/1.16x/1.13x)
and "the postgres floor ties at `~0.96x`, the honest reading of *as fast as hand-rolling
the driver*" are both withdrawn: measured under the gevent patch, before the transaction
change, and against a floor that paid neither a checkout nor a transaction.

**Do not quote this entry either.** It is the first sweep of the right *kind*, on a box
too busy for the numbers to mean much. A quiet re-run replaces it.

## 2026-08-01 (later) — re-run after two contender fixes

Commit `31974e5`, branch `bench/2026-08-01-contender-fixes` (all eight `run.json`s).
**Superseded, and by more than a re-measurement** — see the 2026-08-02 entry above: this
sweep ran under `gevent.monkey.patch_all()`, so its absolutes are ~30% slow, and it
predates the transaction and pool changes. Findings 2 and 3 below are retracted there.
It was, until then, the sweep METHODOLOGY.md published. Same command, same box, same
`quotable=False` clause. It was re-taken because two contenders were not running the
same race as the rest:

* the `MappedAsDataclass` rows built their payload with `dataclasses.asdict()` — a
  recursive deep copy inside the timed region — where every sibling used a `getattr`
  comprehension, for byte-identical JSON;
* the mock table charged the SQLAlchemy contenders a pool checkout inside the timed
  region while `MockEngine` overrides `_connection` to skip it entirely.

What that changed, and nothing else moved by more than the noise:

| | before | after |
|---|---|---|
| ORM (`MappedAsDataclass`), sqlite wide | 17.3702 ms — **4.75x** | 8.8390 ms — **2.18x** |
| ORM (`MappedAsDataclass`), sqlite flat | 5.1737 ms — 4.31x | 4.6983 ms — 3.61x |
| Core (positional), mock flat | 0.5467 ms — 2.13x | 0.4214 ms — **1.54x** |

So most of the published `MappedAsDataclass` gap was `asdict()`, not the ORM: with the
payload builder equalised it lands *level with stock declarative* (2.18x against 2.24x
on wide), which is the honest reading — the two differ in instrumentation, not in how
expensive they are to read out. And a fifth of the mock Core ratio was a checkout
charged to one side only.

**Read the absolutes loosely on this one.** Worst median spread is 12.9% (sqlite),
30.1% (postgres), 29.2% (mock), against 8.1%/4.5%/7.5% on the sweep below. The die ran
72–90 °C and this chassis throttles; a first attempt on a hot box reported 32.8% and
was discarded, and the numbers above are from a re-run after a cooldown with a settle
gap between groups. The ratios held across all three attempts.

## 2026-08-01 — the first published sweep: both tracks, both backends, isolated

Commit `3757a0d`, branch `bench/2026-08-01-two-tracks` (all eight `run.json`s).
This was the sweep METHODOLOGY.md published until the re-run above, and the first one that
satisfies `Run.quotable`'s isolation clause — `bench micro run` hardcoded
`isolation="combined"` until this commit's parent, so no run before it could.

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

uv run python scripts/publish_tables.py benchmarks/results/runs/*_3757a0d/run.json
```

**Superseded** by the re-run above; kept for the comparison it supports.

200,000 rows (1.2M for join), 1000 per read, 300 timed iterations after 50 warmup,
**5 trials, one contender per process**, gc off, pinned to cpus 6-9. Worst
trial-to-trial spread anywhere: **8.1%** — cleaner than the re-run above, which is why
its dispersion figures are the ones worth quoting. Its tables are superseded; what
follows is what the run *changed*.

**Still `quotable=False`, on one clause.** Cpu boost is enabled and
`/sys/devices/system/cpu/cpufreq/boost` is root-only on this box. Clean tree,
equivalence enforced and self-consistent, and one contender per process all pass.

Boost moves the tail and not the median, and the size of that is worth stating rather
than summarising away: the worst single trial anywhere hit `p95/p50` **4.11** and
`max/p50` **9.47**, both on sqlite and both on SQLAlchemy Core cells rather than
rowform's, while the worst median moved 8.1% across five trials. Per backend, worst
`p95/p50` / `max/p50` / median spread: sqlite 4.11 / 9.47 / 8.1%, postgres 1.33 / 2.00
/ 4.5%, mock 1.28 / 1.66 / 7.5%.

**1. The compatibility track ties with the hot one, and the earlier number was an
artifact.** An in-process run on 2026-08-01 (superseded, commit `9452819`) put
`execute().scalars()` 3-4% above `fetch_all()`, and that entry said so. Under real
process isolation the two tie in all four cells where both run — the earlier gap was
the compat contender inheriting the allocator state of the `fetch_all` contender that
had just run in the same interpreter. This is correction 2 (contender ordering inside
one process) recurring in a suite that had already written it down, and it is why the
isolation clause exists.

`execute().all()` costs 11-17% and is a real cost: one `Row` per row.

**2. The Core ratios narrowed, as predicted.** On sqlite, 1.26x/1.16x/1.13x
(flat/join/wide) against the 1.55x/1.16x/1.37x published when rowform owned a pool with
a ~0.09 ms checkout. That is the pool trade (`PLAN_SQLA_API.md` §2b, §5.3) arriving in
the table. Postgres: 1.34x/1.17x/1.17x.

**3. The sqlite floor comparison is not apples-to-apples, and the postgres one is.**
Both sqlite floors hoist a single connection; rowform checks one out per read, so the
0.75x ratio against the same-hydrator floor is mostly SQLAlchemy's per-checkout cost. The
asyncpg floor acquires per request exactly as rowform does, and there the ratio is
`~0.96x` — a tie the harness now flags mechanically, where METHODOLOGY previously
argued it in prose ("1.0075 vs 1.0330, and that run's rowform IQR was 38%").

## 2026-07-29 — SQLAlchemy mock-engine micro benchmarks (flat + join)

Two `bench micro run` sweeps (in-process, not load test) — default flat
config (`rows=200000 limit=100 iterations=10000 warmup=100 gc=off`) and the
same with `--shape=join` — comparing every `sqlite`-backend contender
against its `mock`-backend sibling. The mock siblings for the SQLAlchemy
contenders (Core positional, Core `.mappings()`, ORM, ORM-as-dataclass) are
new this session, via `mock_sqlalchemy_engine()`
(`benchmarks/engines/mock.py`) — a real `AsyncEngine` whose driver is faked
at the aiosqlite DBAPI seam, so SQL compilation, Core result processing and
ORM hydration all run for real; only the actual SQLite call is canned.

Equivalence passed within every group (all contenders sharing a backend
produced byte-identical payloads). Flat's `sqlite` and `mock` groups also
happened to match *each other* byte-for-byte (same sha256) — join's differed
by 5 bytes, unsurprising since the mock backend's rows come from a separate,
much smaller ephemeral seed than the `sqlite` backend's full database, so
which rows land inside `LIMIT` can differ between the two.

Not `--record`ed — no `run.json` under `results/runs/` from this session, so
nothing to archive on a dated branch this time. Spread is high (40-250%):
these are unpinned dev-machine numbers, not a quotable baseline — in
particular, `rowform` vs `raw dict` swapped relative order between this run
and an earlier same-day run, i.e. that specific match-up is inside the noise
floor here and shouldn't be read as a settled result.

| shape | backend | contender                           | median (ms) | stdev (ms) | spread (%) |
| ----- | ------- | ------------------------------------ | ----------- | ---------- | ---------- |
| flat  | sqlite  | rowform                              | 1.1736      | 0.0779     | 85.5       |
| flat  | sqlite  | raw aiosqlite + dict                 | 0.9975      | 0.0560     | 69.5       |
| flat  | sqlite  | SQLAlchemy Core (positional)         | 2.4002      | 0.4382     | 126.1      |
| flat  | sqlite  | SQLAlchemy Core (.mappings())        | 3.9865      | 0.1990     | 111.9      |
| flat  | sqlite  | SQLAlchemy ORM                       | 5.5898      | 0.4781     | 95.4       |
| flat  | sqlite  | SQLAlchemy ORM (DC)                  | 6.7404      | 0.2990     | 69.0       |
| flat  | mock    | rowform (mock)                       | 0.3901      | 0.0135     | 52.2       |
| flat  | mock    | raw mock + dict                      | 0.2475      | 0.0116     | 40.0       |
| flat  | mock    | SQLAlchemy Core (positional) (mock)  | 0.9306      | 0.0313     | 42.0       |
| flat  | mock    | SQLAlchemy Core (.mappings()) (mock) | 2.8695      | 0.1059     | 57.9       |
| flat  | mock    | SQLAlchemy ORM (mock)                | 4.2491      | 0.1606     | 49.5       |
| flat  | mock    | SQLAlchemy ORM (DC) (mock)           | 5.5463      | 0.2564     | 50.0       |
| join  | sqlite  | rowform                              | 2.4898      | 0.1631     | 78.2       |
| join  | sqlite  | SQLAlchemy Core (positional)         | 3.0714      | 0.1834     | 111.3      |
| join  | sqlite  | SQLAlchemy ORM                       | 9.0446      | 0.5755     | 118.0      |
| join  | sqlite  | SQLAlchemy ORM (DC)                  | 12.3136     | 1.2503     | 144.2      |
| join  | mock    | rowform (mock)                       | 1.1106      | 0.1206     | 88.9       |
| join  | mock    | SQLAlchemy Core (positional) (mock)  | 1.5391      | 0.4918     | 249.4      |
| join  | mock    | SQLAlchemy ORM (mock)                | 7.6529      | 0.9338     | 110.3      |
| join  | mock    | SQLAlchemy ORM (DC) (mock)           | 10.9356     | 2.1215     | 159.2      |

Driver-cost share — how much of each `sqlite` contender's total time
disappears once the driver is mocked out (`(sqlite - mock) / sqlite`).
Falls sharply down the ORM/dataclass end: as SQLAlchemy's own mapping work
grows, the driver becomes a smaller fraction of an already-larger total.

| shape | contender                     | sqlite median (ms) | mock median (ms) | driver-cost share |
| ----- | ------------------------------ | ------------------- | ----------------- | ------------------ |
| flat  | rowform                        | 1.1736               | 0.3901             | 66.8%               |
| flat  | raw dict                       | 0.9975               | 0.2475             | 75.2%               |
| flat  | SQLAlchemy Core (positional)   | 2.4002               | 0.9306             | 61.2%               |
| flat  | SQLAlchemy Core (.mappings())  | 3.9865               | 2.8695             | 28.0%               |
| flat  | SQLAlchemy ORM                 | 5.5898               | 4.2491             | 24.0%               |
| flat  | SQLAlchemy ORM (DC)            | 6.7404               | 5.5463             | 17.7%               |
| join  | rowform                        | 2.4898               | 1.1106             | 55.4%               |
| join  | SQLAlchemy Core (positional)   | 3.0714               | 1.5391             | 49.9%               |
| join  | SQLAlchemy ORM                 | 9.0446               | 7.6529             | 15.4%               |
| join  | SQLAlchemy ORM (DC)            | 12.3136              | 10.9356            | 11.2%               |

## 2026-07-29 — postgres load test baseline

Eight `bench load` runs against Postgres, covering both row shapes the
service exposes — a flat select and a two-table join — across five
contenders: rowform, a hand-written raw-asyncpg-into-dict baseline, and
SQLAlchemy async Core (positional params and `.mappings()`), plus the async
ORM. The join shape only has rowform, Core-positional, and ORM contenders
recorded (no join baseline for raw asyncpg-dict). Every run used the same
machine, the same commit (`04809f71`, tree clean), and the same config
(`rows=50000 limit=100 duration=30s warmup=5s workers=1`), swept across
concurrency 1/8/128/512. All levels completed with `ok=true` (no dropped
requests), and each contender's own `/noop`-floor headroom cleared 2.4x, so
the numbers aren't framework-floor noise. No byte-identical equivalence
check ran between contenders (`cross_check_level` was left unset), so treat
this as directional, not a fully quotable head-to-head. Full per-level
tables (latency, CPU, speedup ratios) are in `comparison.md` on
`bench/postgres-loadtest-baseline-20260729` @ `b0f14fd`.

Throughput (req/s) by concurrency:

| shape | contender                        | c=1  | c=8  | c=128 | c=512 |
| ----- | -------------------------------- | ---- | ---- | ----- | ----- |
| flat  | rowform                          | 1185 | 1735 | 1599  | 1469  |
| flat  | raw-asyncpg-dict                 | 1408 | 2713 | 2674  | 2529  |
| flat  | sqlalchemy-async-core-positional | 909  | 1271 | 1238  | 1153  |
| flat  | sqlalchemy-async-core-mappings   | 790  | 1050 | 974   | 869   |
| flat  | sqlalchemy-async-orm             | 650  | 744  | 644   | 560   |
| join  | rowform                          | 932  | 1317 | 1220  | 1164  |
| join  | sqlalchemy-async-core-positional | 739  | 1064 | 942   | 888   |
| join  | sqlalchemy-async-orm             | 495  | 556  | 464   | 400   |
