# The benchmark suite

What each command measures, how the pieces fit, and where the guarantees live.
The methodology itself — practices, corrections, recorded results — is
[docs/METHODOLOGY.md](../docs/METHODOLOGY.md); this file is the map.

## The three benchmark types

| command | what it measures | where the cases live |
|---|---|---|
| `just bench micro run` | in-process latency of one read, per contender | `micro/contenders.py` (`@contender` registry) |
| `just bench load run` | end-to-end HTTP throughput under concurrency | `service/app.py` routes (one per case) |
| `just bench profile micro\|load` | where the CPU goes | same registries as above |

Discovery: `just bench contenders list` (micro contenders, with slugs),
`just bench load cases` (load cases — derived from `service/app.py`'s route
table). `micro`/`profile micro` select with `--only` (substring on the display
name); `load run`/`profile load` select with `--case` (exact slug, or
`all`/`sqlite`/`postgres` group sweeps).

## Case identity

One slug names a contender everywhere: `{backend}-{shape}-{kebab(name)}`,
derived by `harness/registry.py` from the `@contender` declaration. A load
case *is* a `service/app.py` route whose path equals such a slug — the
hand-written routes are the ground truth, `load/registry.py` derives the case
list from them, and `tests/test_bench_cases.py` pins the invariants (every
case route resolves to a provisionable contender; every served backend/shape
has the rowform reference route the HTTP equivalence gate compares against).
There are no per-case locustfiles: `load/locustfile.py` is the one traffic
generator, told its route per run via `LOCUST_ROUTE`.

## The two claims (micro contender families)

- **`rowform`** rows make the *result-layer* claim at equal work: unprepared
  statement (paying rowform's cache key per call, as `conn.execute(stmt)` pays
  SQLAlchemy's) and the same shared per-row payload builders the ORM rows use.
- **`rowform (idiomatic)`** rows (`tags=("idiomatic",)`) make the *endpoint*
  claim: prepared once, dataclasses straight to orjson — the code an
  application would write. The delta between the two rows prices the API-shape
  advantages explicitly.

Floors (`shipped=False`, `tags=("floor", ...)`) deliberately do *less* work
than any contender; mocks (`backend="mock"`) are per-mapper regression floors
and get no cross-mapper ratios. `service/app.py` is idiomatic-only by design:
the HTTP benchmark measures endpoints as an application would write them, and
`bench micro`'s equal-work family is where the result layer is isolated.

## Where the guarantees live

- **Equivalence**: `harness/equivalence.py` byte-compares every contender
  before timing and re-runs each for self-consistency; under `--isolate` every
  timed child proves by hash it produced the gated bytes. `bench load run`
  byte-compares the case's response against the rowform reference route, then
  enforces that byte length on every response (`LOCUST_EXPECT`).
- **Environment**: `harness/env.py` snapshots the machine before *and* after a
  run (frequency sag, throttle events, loadavg), warns on turbo/boost —
  including "unknown" — and records whether gevent's monkey-patch was active.
  `Run.quotable` refuses runs that shared a process, failed a gate, or warned.
- **Load audits**: `load/audit.py` — socket counts and CPU utilizations are
  aligned to the measured window the locust process itself reports
  (`LOCUST_WINDOW_FILE`), never guessed from sleep math.
- **Timing hygiene**: `timing.assert_unpatched_threading()` refuses to time
  under gevent (`bench micro` and `bench profile micro` both call it);
  `--pin auto` derives two whole physical cores from the machine's topology.

## Postgres: two separate pipelines

`bench micro` needs a server *you* provide: `just bench db up`, then `--pg-dsn
"$(just bench db dsn)"`. The run seeds the shape itself, **dropping and
recreating** its tables on that server (`just bench db seed` does the same by
hand, for inspecting the data without a run). `bench load run` and `bench
profile load` provision their own throwaway container per run — if a `bench db
up` server is still standing,
pass `--pg-port` to avoid colliding on 5432. `bench db down` tears the server
down and also clears a stale state file after a reboot or `docker system
prune` (state lives in `results/runs/.state/db.json`).

## Publishing a number

The recipe, gates, and the running log of what previous numbers got wrong:
[docs/METHODOLOGY.md](../docs/METHODOLOGY.md) ("Reproducing" and "One
contender per process"), results indexed in [docs/RUNS.md](../docs/RUNS.md).
Short version: `--isolate --trials 3 --record`, check `quotable=True`, commit
the `run.json` to a dated `bench/` branch, index it in RUNS.md.
