# Recorded runs

## 2026-08-01 — the two tracks, priced against each other and against Core

Commit `9452819`, branch `bench/2026-08-01-two-tracks` (the three `run.json`s).
Reference box, 200,000 rows (1.2M for join), 1000 per read, 300 timed iterations
after 50 warmup, gc off, pinned to cpus 6-9, postgres from `bench db up`:

```
just bench db up
just bench micro run --shape {flat,join,wide} --iterations 300 --warmup 50 \
  --pg-dsn "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable" --record
```

The question this answers: **what does the compatibility track cost?** `execute()`
returns SQLAlchemy's own `Result`, so it can be measured against `fetch_all()` on
the same statement, the same hydrator and the same one-shot checkout — only the
result layer differs, and the equivalence gate held the payload byte-identical
across all of them.

Medians in ms, lower is better. First postgres numbers this library has; a server
was not available when the two-track work was written.

| contender | flat/sqlite | join/sqlite | wide/sqlite | flat/pg | join/pg | wide/pg |
|---|---|---|---|---|---|---|
| raw driver → dicts *(floor)* | 0.8265 | 1.3804 | — | 1.0061 | — | — |
| raw driver + same hydrator *(floor)* | 0.9277 | 1.5363 | — | — | — | — |
| **rowform `fetch_all()`** | **1.2304** | **1.8378** | **3.7637** | **1.0241** | **1.8242** | **3.1215** |
| **rowform `execute().scalars()`** | **1.2751** | — | **3.7937** | **1.0681** | — | **3.1511** |
| **rowform `execute().all()`** | **1.4167** | **2.0265** | — | **1.2069** | **1.9905** | — |
| SQLAlchemy Core (positional) | 1.5437 | 2.1316 | 4.2761 | 1.4235 | 2.1689 | 3.6162 |
| SQLAlchemy Core (`.mappings()`) | 3.2794 | — | — | 3.1255 | — | — |
| SQLAlchemy ORM | 4.3326 | 6.8642 | 7.8108 | 4.1459 | 7.3483 | 7.4559 |
| SQLAlchemy ORM (`MappedAsDataclass`) | 5.1281 | 8.8247 | 17.2617 | — | — | — |

`.scalars()` is not registered at arity two — it would drop an entity — and
`.all()` is not registered for wide, whose single entity makes it the same
measurement as flat's.

**What it says.**

1. **`.scalars()` costs 1-4%**: +0.045 ms on flat/sqlite, +0.044 on flat/pg,
   +0.03 on wide. Taking the hydrated objects straight out of the `Result` is
   near-free, which is what handing them over unwrapped bought (§8c).
2. **`.all()` costs 9-18%** — +0.186 ms per 1000 rows on flat/sqlite, and the
   same order everywhere. That is one `Row` built per row, and it is the whole
   difference between the two lines.
3. **The compatibility track still beats stock Core**: 1.2751 against 1.5437 on
   flat/sqlite (**1.21x**), 1.0681 against 1.4235 on flat/pg (**1.33x**). Same
   `execute()`, same `Result`, same accessors — the hydrator replaces
   `Row`/`CursorResult` underneath and the idiom above it does not change. Even
   the `.all()` line, which pays for a `Row` per row, comes out ahead (1.4167 vs
   1.5437).

**Two caveats, both the box's.**

`quotable=False` on all three runs. Not a judgement on these numbers in
particular: `bench micro run` hardcodes `isolation="combined"`, which the gate
refuses for any multi-cell run, so no invocation of that command can be quotable
today. Cpu boost was also enabled. Ratios, not absolutes.

With **gc off**, the wide compat arm alone shows a long tail — p95/p50 2.4-2.7
and ~28 severe outliers, reproducibly, across two runs. It is the GC: rerun with
`--gc on` and it is p95/p50 1.03 with one outlier, medians 3.8314 against 3.8671.
`Result`/`ScalarResult` allocate reference cycles that only the collector
reclaims, so a gc-off benchmark overstates the compat track's tail specifically.
The medians are unaffected either way.

**Not comparable to the published tables** in METHODOLOGY.md: those were taken
when rowform owned its pool, and every rowform arm here pays SQLAlchemy's
checkout instead (PLAN_SQLA_API.md §2b, §5.3). Read this table against itself.

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
