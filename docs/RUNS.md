# Recorded runs

## 2026-08-01 — the published sweep: both tracks, both backends, isolated

Commit `3757a0d`, branch `bench/2026-08-01-two-tracks` (all eight `run.json`s).
This is the sweep [METHODOLOGY.md](METHODOLOGY.md) publishes, and the first one that
satisfies `Run.quotable`'s isolation clause — `bench micro run` hardcoded
`isolation="combined"` until this commit's parent, so no run before it could.

```
just bench db up
just bench micro run --shape {flat,join,wide} --backend {sqlite,postgres,mock} \
  --iterations 300 --warmup 50 --trials 5 --isolate --pg-dsn "$DSN" --record

uv run python scripts/publish_tables.py benchmarks/results/runs/*_3757a0d/run.json
```

200,000 rows (1.2M for join), 1000 per read, 300 timed iterations after 50 warmup,
**5 trials, one contender per process**, gc off, pinned to cpus 6-9. Worst
trial-to-trial spread anywhere: **8.1%**. Tables are in METHODOLOGY.md rather than
duplicated here; what follows is what the run *changed*.

**Still `quotable=False`, on one clause.** Cpu boost is enabled and
`/sys/devices/system/cpu/cpufreq/boost` is root-only on this box. Clean tree,
equivalence enforced and self-consistent, and one contender per process all pass.
Boost moves the tail, not the median — `p95/p50` is 1.03-1.12 across every table.

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
Both sqlite floors hoist a single connection; rowform checks one out per read, so its
0.75x against the same-hydrator floor is mostly SQLAlchemy's per-checkout cost. The
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
