# Benchmark suite rewrite

Replace the current 28 ad-hoc benchmark scripts with one unified, extensible suite
behind a single CLI.

Status: **all 6 phases implemented and gate-verified** (§14a has the full
per-phase log — read it before touching anything, it's the resume point and
records real bugs/trade-offs found while building this, not just intentions).
Two known gaps, both called out in §14a rather than silently left: Postgres
is provisioned/seedable (`bench db`) but `bench micro`/`bench load`/`bench
profile` only drive the sqlite backend so far; `bench record`'s git
branch/commit path was code-reviewed and typechecked but not executed live,
per the standing git-safety rule. Nothing has been committed — every change
in this plan is a working-tree diff, left for review.

---

## 1. Why

The existing suite is methodologically strong and structurally poor. `docs/METHODOLOGY.md`
records eight corrected/retracted claims, and the practices adopted in response are the
real asset here. But those practices are implemented **once per script**, so they drift.

Measured duplication across `benchmarks/` (5,780 lines, 28 files):

| Repeated concept | Copies |
|---|---|
| `sys.path.insert(...parent.parent)` | 20 files |
| `seed()` / `seed_database()` | 9 near-identical (8 share the literal `1 if rng.random() > 0.1 else 0`) |
| async closed-loop worker (`deadline` / `gather` / `perf_counter`+`process_time`) | 8 |
| timing-loop dialects for one concept | 5 mutually incompatible |
| byte-identical-JSON equivalence gate | 3 dialects across 8 files |
| percentile computation | 2 implementations |
| `summarize()` + `print_table()` | 2 copies, differing only in column widths |
| Postgres DSN | 12 literals in 5 spellings; only 3 scripts accept `--dsn` |
| `env = {...}` metadata dict | 3 variants |
| `--pin` affinity handling | 4 Python impls + `taskset` in 6 usage strings + 2 postmaster-detection heuristics |
| contender definitions | `Query(User).where(...)` ~10x; SA Core `.mappings()` ~7x; `make_core_fast` twice **including a verbatim-duplicated 8-line docstring** |
| result writing | 3 files support `--out`, each with the same `json.dumps({"env":…,"results":…})` |

Only two abstractions were ever extracted — `benchargs.py` and `profkit.py` — and both
were extracted *after* drift caused real bugs (`profkit.py:1-7` documents private copies
of `rollup`/`top_functions` silently shadowing the shared ones). That is the pattern to
generalise, not the exception.

Three blockers found while surveying:

1. **The suite cannot run.** No `sqlalchemy`, `fastapi`, `uvicorn`, `locust`,
   `pyinstrument`, `uvloop` or `aiosqlite` was declared in `pyproject.toml` or present in
   the venv. Installation was a manual `pip install` line in `docs/METHODOLOGY.md:435`.
   *(Fixed already — see §12.)*
2. **No Postgres provisioning exists.** No docker/compose/testcontainers/`initdb`. Every
   PG script assumes a live `127.0.0.1:5432` + database `rowform_bench`, seeded only by
   `bench_pg_load.py --seed-only`. Five scripts crash with a raw traceback if absent.
3. **`bench_row_access.py:27` hardcodes `sys.path.insert(0, "/home/user/rowform")`** — a
   path from another machine. That script is currently broken.

---

## 2. Decisions

Settled during planning. Recorded with rationale so they are not silently revisited.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Delete the old scripts**, do not archive | Recoverable from commit `32ad4a1`. Porting the worthwhile content first (§11). |
| D2 | **Keep `benchmarks/results/`** (30 artifacts) | Evidence for every table in `BENCHMARKS.md`; deleting orphans all published numbers. |
| D3 | **No baseline capture, no old-vs-new agreement gate** | User directive. Consequence: correctness rests on the suite's *intrinsic* checks (§4), not on reproducing old numbers. **No ported figure may be claimed to match a published one.** |
| D4 | **Docs last**, in one pass | ~50 references to 21 script filenames across 2,358 lines. |
| D5 | **Publish nothing** | `BENCHMARKS.md` tables stay as-is. New numbers go only to `docs/RUNS.md` as dated runs. |
| D6 | **Ephemeral Postgres via docker, `--network host`, `--cpuset-cpus`** | Host networking avoids the unpinned `docker-proxy` userspace hop that would confound loopback latency; a cpuset covers every backend *from birth*, structurally removing the "affinity is inherited across `fork()`, so pin before the pool opens" hazard that `METHODOLOGY.md` warns about. Version pinned and recorded. |
| D7 | **Add `SqliteEngine` to `rowform`** — full parity, shipped, tested, documented | Chosen over a harness-owned aiosqlite stand-in, which would violate *"benchmark the library's own path, not a hand-rolled stand-in of it"* — the rule that caught a real 4% regression in the shipped engine. |
| D8 | **`MockEngine` for library-only benchmarks** | Returns precomputed rows; removes even sqlite3's C driver cost. Strictly better than raw sqlite3 as a mapper isolator. |
| D9 | **`MockEngine` benchmarks are rowform-only** | SQLAlchemy has no equivalent result-mocking seam; pairing MockEngine-rowform against sqlite-SQLAlchemy would put different work inside the two timed regions — correction 6 exactly. |
| D10 | **Profilers: cProfile, pyinstrument, yappi, py-spy, austin.** No memray | All five verified available in-venv (§12). Protocol stays extensible. |
| D11 | **CodSpeed: out** | `bench micro` already does medians, spread, tie-grouping, isolation and the equivalence gate, none of which CodSpeed does. Cost accepted: no automated PR regression gate. |
| D12 | **Worker sweep capped at 1/2/4; measure saturation rather than assume it** | Per-role CPU accounting + hard gates replace my proposed third generator. Generator interface stays open so `oha`/`wrk` can drop in if gates trip. |
| D13 | **`CorePlan` is best-effort on any core count** | Prefers whole physical cores and disjoint roles; degrades to sharing with a recorded warning; never refuses. |
| D14 | **Suite recipes are typed Python modules**, not YAML | No new parser, importable, `bench suite list` introspects them. |
| D15 | **`results/runs/` gitignored on main** | `bench record` commits artifacts to `bench/<date>-<slug>`; only the index row lands in `docs/RUNS.md` on main. Per `CLAUDE.md`. |

---

## 3. Machine

Recorded because absolutes do not travel between boxes (`METHODOLOGY.md`: re-running gave
~1.35x higher times for *every* contender after the machine changed).

- AMD Ryzen 9 5900HS — **8 physical cores / 16 threads**
- **SMT siblings are adjacent**: `cpu0+cpu1` are one physical core, `cpu2+cpu3` the next, …
- governor `performance`, **boost enabled** (a live noise source — `bench env check` warns)
- Python 3.13.9, GIL enabled
- docker 29.6.1, podman 5.8.2, `taskset` present, **no `numactl`**
- `ptrace_scope=0` → py-spy/austin attach without sudo; `perf_event_paranoid=2` → `perf` needs a sysctl, out of scope

> **Finding worth carrying forward.** `bench_locust.sh` and `verify_concurrency.sh` put
> uvicorn on core 0 and the load generator on core 1. On *this* topology those are SMT
> siblings of one physical core. The published runs were on a different box so this may
> not have applied there, but `CorePlan` must reason in physical cores, never in CPU
> indices.

---

## 4. Methodology invariants (the actual spec)

Extracted from `docs/METHODOLOGY.md`. **These are requirements, not guidelines.** Where
possible each becomes mechanical rather than a habit.

| Invariant | How it is enforced |
|---|---|
| Byte-identical output before timing | `harness/equivalence.py` — one gate, `sha256` recorded, 3x self-consistency, empty-result guard. `--skip-equivalence` is debug-only and sets `quotable: false`. |
| One contender per process for any published number | `--only` + `--repeat`; `plan.isolation` recorded; combined runs forced `quotable: false`. |
| Price any workaround one contender needs | Both SA Core idioms (`.mappings()` and positional) stay registered as separate contenders. Deleting the slow one would hide the size of correction 8. |
| Measure more than one shape | `shapes/flat.py` + `shapes/join.py`, kept separate (the `join_models.py` docstring explains they must not merge). |
| Medians + spread; group ties instead of ranking | `harness/stats.py` — `tie_group()`; every ratio carries `spread_pct` and a `tie` flag. |
| Audit what is *inside* each timed region | Contenders defined once, so a setup asymmetry cannot exist in one copy only. Per-request `Session` on a warm connection — never a hoisted `Session` (its identity map skips the work, 12.9% in the ORM's favour). |
| Record utilization, not just throughput | `cpu_ms_per_request` + `cpu_utilization` per role, mandatory fields. |
| Never categorise profiler frames by substring | `profiling/attribution.py` compares resolved package dirs (`Path(rowform.__file__).parent`); the repo dir shares the package name. |
| An impossible-by-construction row must read zero | Asserted, not eyeballed — emits a warning into the run JSON. |
| Cross-check instrumented against sampling | `bench profile` runs one of each by default, prints both plus the instrumentation-inflation factor, warns on material divergence. |
| Pin deliberately, in physical cores | `harness/affinity.py` — SMT-aware, reads masks back from `/proc/<pid>/status`, records actual masks. |
| Include a floor and a naive baseline | `raw_asyncpg_codegen` (no objects) and `raw_asyncpg_dict` stay registered. When rowform appeared to beat the floor, that was the tripwire. |
| State the bottleneck | `plan.bottleneck` is a required field. |
| Audit the load generator; never assume concurrency | `load/audit.py` — one implementation of ESTABLISHED-socket counting from `/proc/net/tcp`, Little's Law, and the scaling knee. |
| Calibrate headroom with a do-nothing endpoint | `/noop` gate: must be ≥2x the fastest DB endpoint or the run is not quotable. |
| Benchmark the library's own path | D7 (`SqliteEngine`) and D8 (`MockEngine` overrides exactly one method — §7). |
| GC is a first-order effect | `--gc on|off|both`; the join shape allocates ~2000 objects/iteration and disabling GC collapsed stdev 5–10x for every contender. |
| Never divide a bottom-up estimate into a top-down measurement | Stage decomposition prints sum-of-parts next to the measured whole and reports the residual. |
| Absolutes drift with the machine | Full machine block in every result (§6). |

---

## 5. Architecture

Directory name `benchmarks/` is kept (doc references point into it).

```
benchmarks/
  __main__.py            python -m benchmarks → AsyncTyper root app
  cli/                   env db micro service load profile suite report record
  harness/
    env.py               capture(): machine + git + versions; static + start/end samples
    result.py            Run / Cell / Trial schema, writer, runs/index.jsonl
    stats.py             median, p50/95/99, stdev, spread, tie_group, ratio_with_spread
    timing.py            THREE modes, one impl each:
                           per_iteration()   micro latency samples
                           closed_loop()     rps + cpu_ms + utilization + percentiles
                           best_of()         timeit-style floor for stage decomposition
                         + gc_control() context
    equivalence.py       the one gate
    affinity.py          CorePlan: physical-core aware, best-effort, read-back verify
    cpuacct.py           per-role CPU sampling (per-pid /proc/<pid>/stat, cgroup cpu.stat)
    seed.py              one deterministic seeder (rng_seed=42)
    registry.py          @contender(name, backend, shape, shipped, tags)
  backends/
    sqlite.py            ephemeral temp-file db, PRAGMAs, seed
    postgres.py          EphemeralPostgres (docker, host net, cpuset) | attach(dsn)
  shapes/
    flat.py              single `users` table, 4 definitions (from models.py)
    join.py              two-table join, 3 definitions (from join_models.py)
  engines/
    mock.py              MockEngine(DatabaseEngine) — one override (§7)
  contenders/            one definition each, shared by micro AND service AND profile
  service/
    app.py               the single FastAPI app, lifespan (not on_event)
    launch.py            uvicorn launcher: workers, per-worker pinning, readiness, affinity read-back
  load/
    httpload.py          ported: keeps every validation gate + the RESULT TSV contract
    locust.py            ported locustfile, driven by the CLI
    audit.py             Little's Law + /proc/net/tcp sockets + scaling knee + /noop headroom
  profiling/
    base.py              InProcessProfiler | ExternalProfiler protocols
    cprofile.py yappi.py pyinstrument.py pyspy.py austin.py
    attribution.py       ported profkit.py + the impossible-row tripwire
    render.py            speedscope JSON + folded stacks
  micro/                 pure-Python component benchmarks
  suites/                typed Python recipes = reproducible named runs
  results/               EXISTING artifacts, untouched (D2)
    runs/                new run dirs, gitignored (D15)
```

The single most valuable consolidation is **contenders defined once**. The SA
Core-`.mappings()` contender currently exists ~7 times, and it was the source of the
largest correction in the suite (Core ratios inflated 1.6–2.6x).

---

## 6. Result schema

One JSON per run at `results/runs/<run_id>/run.json`, plus append-only
`results/runs/index.jsonl`.

```json
{
  "schema_version": 1,
  "run_id": "2026-07-29T11-40-00Z_pg-load_a1b2c3d",
  "suite": "pg_load",
  "started_at": "…", "finished_at": "…",
  "invocation": {"argv": ["…"]},
  "git": {"sha": "…", "branch": "main", "dirty": false},
  "env": {
    "host": "…", "kernel": "…", "distro": "…",
    "cpu": {"model": "AMD Ryzen 9 5900HS", "physical_cores": 8, "threads": 16,
            "smt_siblings": {"0": [0,1]}, "governor": ["performance"], "boost": true,
            "mhz_start": [], "mhz_end": [], "throttle_count_delta": 0},
    "mem_total_kb": 0, "loadavg_start": [], "loadavg_end": [],
    "python": {"version": "3.13.9", "impl": "cpython", "gil_disabled": false},
    "packages": {"sqlalchemy": "2.0.51", "asyncpg": "…", "orjson": "…", "…": "…"},
    "db": {"kind": "postgres", "version": "16.x", "provisioning": "docker",
           "image": "postgres:16", "cpuset": "6,7", "network": "host",
           "ssl": false, "settings": {}}
  },
  "plan": {"core_plan": {"server": [0], "generator": [4], "db": [6,7],
                         "smt_shared": false, "degraded": false},
           "workers": 1, "event_loop": "uvloop", "gc": "on",
           "bottleneck": "client", "isolation": "one_contender_per_process"},
  "config": {"shape": "flat", "rows": 200000, "limit": 1000,
             "concurrency": 8, "duration_s": 5, "repeat": 3, "pool_size": 10},
  "equivalence": {"enforced": true, "reference": "rowform",
                  "payload_sha256": "…", "payload_bytes": 90210, "self_consistent": true},
  "cells": [
    {"contender": "sqlalchemy_core_positional", "shipped": true,
     "params": {"concurrency": 8},
     "trials": [{"trial": 0, "rps": 3312.0, "mean_ms": 2.41, "p50_ms": 2.30,
                 "p95_ms": 3.10, "p99_ms": 4.00, "cpu_ms_per_request": 0.29,
                 "cpu_utilization": {"server": 0.97, "generator": 0.41, "db": 0.55},
                 "completed": 16560, "elapsed_s": 5.0}],
     "summary": {"rps": {"median": 0, "min": 0, "max": 0, "spread_pct": 0}}}
  ],
  "ratios": [{"numerator": "rowform", "denominator": "sqlalchemy_orm",
              "metric": "rps", "value": 4.12, "spread_pct": 6.1, "tie": false}],
  "warnings": ["cpu boost enabled", "/noop only 1.7x fastest DB endpoint"],
  "quotable": false,
  "notes": "…"
}
```

Two fields carry the methodology into the artifact instead of leaving it to a reader:

- **`warnings[]`** — audit failures, dirty tree, boost on, saturation breaches.
- **`quotable`** — false if the tree was dirty, isolation was not one-contender-per-process,
  the equivalence gate was skipped, or any audit gate tripped. Makes *"the combined suite
  is for a quick side-by-side, never for publication"* mechanical.

`results/README.md` records that three older JSON sweeps were deleted precisely because
the then-current code could no longer reproduce them. `schema_version` + `invocation` +
`git.sha` exist so that cannot recur.

---

## 7. Three transport tiers

Each tier answers a different question. Mixing them in one table is correction 4.

| tier | instrument | measures | contenders |
|---|---|---|---|
| pure Python | none | hydration on pre-fetched rows, orjson at **4 and 10** field widths, SQL-string compilation, row access per driver row type, `as_dict`, placeholder renumbering, DML batching | rowform vs SQLAlchemy where an equivalent exists |
| `MockEngine` | rowform only (D9) | full library path, zero driver cost — replaces `profile_stages.py` + `estimate_ceilings.py` | rowform variants |
| `SqliteEngine` / Postgres | real driver | cross-library ratios, FastAPI end-to-end, worker scaling | all; both Core idioms |

`delta(MockEngine, SqliteEngine, Postgres)` prices each transport layer — a decomposition
the current suite cannot produce.

### MockEngine: exactly one override

`DatabaseEngine.fetch_all` (`rowform/engine.py:270`) touches the driver in one place:

```python
self._reject_if_in_transaction("fetch_all")                      # real
_require_rows(query)                                             # real
sql, params = query.to_sql(placeholder="$", dialect=POSTGRES)     # real
if has_deferred_params(params): params = bind_params(params, **overrides)   # real
async with self._require_pool().acquire() as conn:               # ← the only seam
    rows = await conn.fetch(sql, *params)
return self._hydrator_for(query)(rows)                           # real
```

`MockEngine(DatabaseEngine)` overrides **only `_require_pool()`**, returning a fake pool
whose connection's `fetch()` yields precomputed tuples. SQL generation, param binding,
hydrator selection and hydration are all shipped code, byte-for-byte — including the
per-request `to_sql()` call that exposed the 4% self-inflicted regression in
`bench_conditional_reset.py`.

Rows are plain tuples (rowform is always positional — it owns the `SELECT` list), so
MockEngine measures the mapper's floor with *no* driver term. Its absolutes are therefore
not comparable to Postgres; it is a mapper instrument only.

---

## 8. Library work: `SqliteEngine` (gates the service tier)

`rowform/sqlite_engine.py`, shipped: exported from `__init__.py`, `sqlite = ["aiosqlite>=0.19"]`
extra, own tests, README as the third engine alongside asyncpg/psycopg.

- **Full parity** with `DatabaseEngine`: `connect`/`close`/`acquire`,
  `fetch_all`/`fetch_json`/`execute`, `transaction()`, native
  `SAVEPOINT`/`RELEASE`/`ROLLBACK TO`, the in-transaction guard.
- **WAL mode + `synchronous=NORMAL` + a small aiosqlite connection pool**, so concurrent
  reads parallelise under c=8. aiosqlite ships no pool, and a single connection would
  serialise the entire FastAPI benchmark.
- **`isolation_level=` and `conditional_reset=` raise `NotImplementedError`** with a
  message naming the sqlite model. sqlite has no server-side session state to reset and no
  real isolation levels (WAL + deferred/immediate/exclusive is the whole model); accepting
  them as no-ops would let a caller believe they took effect.
- `tests/` engine suite runs parameterised over sqlite alongside the two PG engines.

Note that sqlite writes serialise globally, so write-path load numbers will be
concurrency-insensitive by nature. That is a property of sqlite, and should be stated
next to any such figure rather than treated as a result about rowform.

---

## 9. CLI

`AsyncTyper` for async commands, plain `typer` elsewhere. Typer's type-level validation
replaces `benchargs.validate`'s hand-rolled non-positive guards.

```
bench env [check]                       # emit block; check warns on boost/dirty tree/loadavg
bench db up --cores 6,7 --version 16 --ssl on|off --tune fair|default [--attach DSN]
bench db {down,status,dsn,seed --rows N --shape flat|join}
bench contenders list [--shape …] [--backend …] [--only …] [--json]   # registry, scriptable
bench contenders shapes                 # valid --shape values
bench micro run --shape flat --rows 200000 --limit 1000 --iterations 300 \
                --warmup 30 --repeat 5 --only C --isolate --gc on|off|both --pin 0
bench service run --backend pg|sqlite --workers N --cores 0-3 --loop uvloop
bench load run --endpoints … --concurrency 8 --duration 5 --repeat 3 \
               --generator httpload|locust --server-cores --client-cores --db-cores
bench load audit --levels 1,2,4,8,16
bench profile {micro,load,stages} --profiler cprofile,austin --clock cpu,wall \
                                  --render speedscope
bench suite list | run <name>
bench report <run…> | bench record <run…> --note … | bench verify <run_id>
```

`suite` is the extensibility seam: adding a dimension is adding a recipe, not a script.

---

## 10. Profiling

| profiler | kind | clock | async handling | output |
|---|---|---|---|---|
| cProfile | instrumented | CPU (`process_time_ns`) / wall | whole-loop | pstats, text rollup, speedscope |
| yappi | instrumented | **cpu and wall** | **native per-coroutine aggregation** | callgrind → speedscope |
| pyinstrument | in-process sampling | wall | `async_mode="enabled"` | HTML, speedscope |
| py-spy | **external** sampling | wall, `--idle`, `--native` | loop thread | flamegraph SVG, speedscope |
| austin | **external** sampling | **wall + CPU (`--sleepless`)** | thread/greenlet | mojo → speedscope/folded |

The two external samplers are the answer to profiling the FastAPI app under load: zero
in-process overhead, attach to each uvicorn worker PID. yappi covers *"the async nature
must be considered"* at coroutine granularity, which cProfile cannot.

All output normalises to **speedscope JSON + folded stacks**, stored inside the run
directory so profiles are part of the recorded artifact. cProfile keeps its CPU timer
deliberately — a wall profile of an asyncio loop is dominated by `epoll_wait`.

Known bias pair to preserve and report, never resolve by picking the flattering one:
cProfile's per-call overhead inflates call-heavy Python (rowform codegen 29% vs 15%);
samplers cannot see inside C extensions, so orjson's work is charged to its Python caller.

---

## 11. What is deleted, and what is ported out first

All 28 files under `benchmarks/` are deleted (D1; recoverable from `32ad4a1`).
`benchmarks/results/` is kept (D2).

Ported before deletion:

| from | into |
|---|---|
| `models.py` | `shapes/flat.py` |
| `join_models.py` | `shapes/join.py` (kept separate — its docstring explains why) |
| `profkit.py` | `profiling/attribution.py` (+ tripwire) |
| `httpload.py` | `load/httpload.py` — keeps every gate: 200-only, chunked rejected, `Content-Length` required, per-request `wait_for`, payload-size invariance, zero-response failure; keeps the `RESULT` TSV line |
| `locustfile.py` | `load/locust.py` (`FastHttpUser`, `wait_time=constant(0)`) |
| `fastapi_app.py` | `service/app.py` (lifespan instead of deprecated `on_event`; routes generated from the registry) |
| `bench_locust.sh` / `verify_concurrency.sh` | `load/audit.py` — one implementation of the three checks |
| `pin_and_run.sh` | `harness/affinity.py` (no postmaster hunting needed under D6) |
| seeding logic from 9 scripts | `harness/seed.py` |
| `benchargs.py` | dropped — Typer's type validation supersedes it |

`bench_row_access.py`'s hardcoded `/home/user/rowform` is not ported; the measurement
becomes a pure-Python micro-benchmark (§7 tier 1).

### No `sys.path` manipulation

The 20 `sys.path.insert(0, …parent.parent)` calls exist only because the docs invoke
scripts as `python3 benchmarks/bench_sqlite.py`, which puts `benchmarks/` — not the repo
root — at `sys.path[0]`. They are dropped entirely, not ported:

- `rowform` is an **editable** install (`rowform.__file__` resolves into the worktree), so
  `import rowform` already works from any CWD under `uv run`.
- `python -m benchmarks` prepends the CWD, and `just bench` always runs from the justfile's
  directory, so `import benchmarks.harness…` resolves without help.

This is not merely tidying. Inserting at index `0` puts the worktree *ahead* of
site-packages, so the hack would silently shadow an installed `rowform` release — and a
benchmark suite must never be ambiguous about which copy of the library it measured.
Normalising the idiom across 20 files is also what let `bench_row_access.py:27` ship a path
from a different machine unnoticed. `[tool.pyright] extraPaths = ["."]` is the same
workaround duplicated into the type checker and should be re-examined once the new layout
lands.

---

## 12. Dependencies (done)

```toml
[project.optional-dependencies]
sqlite = ["aiosqlite>=0.19"]

[dependency-groups]
bench    = ["fastapi", "locust", "psutil", "rich", "sqlalchemy", "typer", "uvicorn[standard]"]
profile  = ["austin-dist", "py-spy", "pyinstrument", "yappi"]
```

Verified resolved in-venv: sqlalchemy 2.0.51, fastapi, uvicorn 0.52.0, uvloop 0.22.1,
httptools, locust, aiosqlite, pyinstrument 5.1.2, yappi 1.7.6, py-spy 0.4.2, austin 4.0.0.

`justfile`: `just bench …` → `uv run --all-groups python -m benchmarks …`.
Also widened `[tool.ruff.lint.per-file-ignores]` from `benchmarks/*` to `benchmarks/**`,
which the old flat layout did not need but the new package does.

---

## 13. Phases

| # | Work | Verification gate |
|---|---|---|
| 1 | `harness/*`, `backends/*`, `bench env`, `bench db` | `bench env` emits the full block; `bench db up/seed/down` round-trips; container PG reachable over host network with the cpuset verified from the outside |
| 2 | `SqliteEngine` + tests | engine test suite passes parameterised over sqlite; unmappable kwargs raise; WAL + pool verified under concurrent reads |
| 3 | `shapes/`, `contenders/`, `MockEngine`, `bench micro` | equivalence gate passes for every contender; `--gc both` reproduces the stdev collapse on the join shape; stage decomposition prints its residual |
| 4 | `service/app.py`, `launch.py`, `bench load`, `load/audit.py` | Little's Law within 10% at c=1,2,4,8,16; `/noop` ≥2x fastest DB endpoint; generator and DB below saturation; httpload ↔ locust agree within 7% |
| 5 | 5 profiler adapters + render + attribution | impossible row reads 0.0%; instrumented↔sampling cross-check runs by default; py-spy and austin both attach to a live worker under load and render a flamegraph |
| 6 | `report`/`record`/`verify`, `docs/RUNS.md`, rewrite ~50 doc references, delete leftovers | one command reproduces any recorded run; no doc references a deleted path; nothing republished (D5) |

Phases 1–2 and 3–5 are each internally ordered; phase 3 depends on 2 only for the sqlite
service contenders.

---

## 14a. Progress log (living — update per phase, this is the resume point)

Execution order deviates from §13 when research for a later phase is already loaded:
worked phase 2 (SqliteEngine) before phase 1 harness/backends, since the engine
research (below) was already in context. Re-check this section before resuming after
a context reset — it is more current than §13's ordering.

### Phase 2 — SqliteEngine — decisions made while implementing

Read `rowform/engine.py` (asyncpg) and `rowform/psycopg_engine.py` in full to copy their
shape exactly (each engine file is intentionally self-contained: `_require_rows`,
`_reject_if_in_transaction`, the `_Select` alias, and the driver's `*Transaction` /
`*_block` helper are each duplicated per engine file rather than shared — matched, not
"improved").

- **Pool**: aiosqlite ships no pool. `sqlite_engine.py` gets a small internal
  `_SqlitePool` — fixed-size `asyncio.Queue` of `aiosqlite.Connection`s, opened with
  `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` + `isolation_level=None`
  (so raw `BEGIN`/`SAVEPOINT` SQL controls transactions instead of sqlite3's implicit
  ones). Constructor takes `min_size`/`max_size` for signature parity with the other
  two engines' pool kwargs; sqlite has no elastic growth so the pool is fixed at
  `max_size` and `min_size` is accepted but unused beyond that parity.
- **Placeholder/dialect**: `to_sql(placeholder="?", dialect=SQLITE)` — sqlite's
  `default_placeholder` is already `"?"`. `fetch_json` uses
  `to_json_sql(dialect="sqlite")`, already implemented in `query.py` DIALECTS
  (`json_group_array`/`json_object`, bool cast via `json(CASE WHEN ... )`) — no
  query.py changes needed.
- **Transactions**: no `conn.transaction()` context manager on aiosqlite, unlike
  asyncpg/psycopg. Implemented explicit `BEGIN`/`COMMIT`/`ROLLBACK` for depth 0 and
  `SAVEPOINT rowform_sp_<depth>` / `RELEASE` / `ROLLBACK TO` for nested, matching
  the asyncpg/psycopg "nested transaction() -> savepoint" contract.
- **`isolation`/`readonly`/`deferrable` on `.transaction()`**: kept the same
  parameter names as the other two engines for call-site parity, but any non-default
  value raises `NotImplementedError` naming sqlite's real model (WAL +
  deferred/immediate/exclusive BEGIN modes, no session-level isolation levels) —
  per D-equivalent in §8, silently no-op'ing these would let a caller believe they
  took effect.
- **`conditional_reset=`**: constructor rejects this kwarg outright (any value) with
  `NotImplementedError` — it is an asyncpg-pool-reset concept with no sqlite
  equivalent; accepting and ignoring it would be the same false-parity trap.
- **Tests**: `tests/conftest.py` already has `sqlite_path` (session-scoped, seeded
  `t_authors`/`t_books`/`t_tags`, same `Author`/`Book`/`Tag` models used by the PG
  suite) and a `run_query` fixture that hand-rolls the engine's dispatch. New
  `tests/test_engines_sqlite.py` builds a `SqliteEngine` on `sqlite_path` and mirrors
  `test_engines_pg.py`'s test classes (`TestLifecycle`, `TestFetchAll`, `TestFetchJson`,
  `TestJoins`, `TestAcquire`) rather than editing the pg file — kept separate because
  sqlite lacks `FOR UPDATE`, self-referencing right-joins semantics differ, etc.
  (`TestNewQueryFeaturesOnPostgres` stays pg-only, matching its own docstring).

**Phase 2 status: done.** `rowform/sqlite_engine.py` + `tests/test_engines_sqlite.py`
(29 tests, all passing) + README mentions of the third engine. `just lint` and
`just typecheck` clean. `just test` shows 995 passed but 235 pre-existing errors, all
`asyncpg.exceptions.InvalidPasswordError` in `test_engines_pg.py`/`test_transactions_pg.py`
— **this machine's local Postgres is reachable on 127.0.0.1:5432 but its
`postgres`/`postgres` credentials don't match `tests/conftest.py`'s `PG_DSN`/
`benchmarks/bench_pg_load.py`'s `DEFAULT_DSN`**. Pre-existing, unrelated to this work
(nothing in phases 1-2 touches Postgres auth) — `_pg_reachable()` only checks TCP
connect, not auth, so it doesn't skip. Needs a real Postgres with those creds (or
`ROWFORM_TEST_DSN` pointed at one) before phase 1's `bench db up` and phase 4's
service/load gates can be verified end-to-end; flagged again there.

### Phase 1 — harness core + backends + bench env/db — status: done

Built (execution order followed §13 phase 1, plus `shapes/` pulled forward from
phase 3 since `harness/seed.py` needed table DDL/row generators): `shapes/{flat,join}.py`
(ported from `models.py`/`join_models.py`, `generate_rows`/`generate_authors`/
`generate_posts` added, rng-based `is_active`/`published` per the consolidated
`rng.random() > 0.1` idiom), `harness/{seed,registry,stats,timing,equivalence,
affinity,cpuacct,env,result}.py`, `backends/{sqlite,postgres}.py`,
`cli/{env,db}.py`, `__main__.py`.

Discoveries and decisions:

- **`async-typer==0.2.1` is broken against `typer>=0.26`**: it imports `typer.clear`,
  removed in typer 0.26.0 (confirmed 0.25.1 has it, 0.26.0 doesn't, by bisection).
  `pyproject.toml`'s `bench` group had `typer>=0.15` with no ceiling, so a plain
  `uv sync` resolved 0.27.0 and `import async_typer` failed outright. Fixed by
  capping `typer>=0.15,<0.26` in the `bench` group; re-locked (`uv lock`), confirmed
  `AsyncTyper()` imports and works. No upstream fix exists yet (async-typer's last
  release is 0.2.1) — revisit the cap if a newer async-typer drops the `clear` import.
- **`[tool.pyright] include = ["rowform", "tests/typing"]` does not cover
  `benchmarks/`** — `just typecheck` silently skips the entire new package. Verified
  the new phase-1/2 modules are clean by running `basedpyright` against them
  explicitly (0 errors). Deliberately **not** widening `include` to `benchmarks/`
  yet: the old 28 scripts still under `benchmarks/` have ~30 pre-existing type
  errors (unrelated to this work, e.g. `bool` passed to `.where()`, `psqlpy` not
  installed) and widening now would turn `just typecheck` red for code that phase 6
  deletes anyway. **Action for phase 6**: once the old scripts are deleted, add
  `benchmarks` to `[tool.pyright] include` so the new suite gets typechecked by the
  standard command, and re-run `basedpyright` explicitly against `benchmarks/`
  after every new module in the meantime (phases 2-5 checklist item).
- **`harness/seed.py`'s original `rows_for(shape, rows)` dispatcher didn't type-check**:
  returning `list[tuple]` on one branch and `tuple[list, list]` on the other, keyed by
  a runtime `shape: str` comparison, doesn't narrow — every caller's `if shape ==
  "flat": data = rows_for(...)` saw the full union type and failed to unpack it
  (caught by the explicit `basedpyright benchmarks/` run above, not by `just
  typecheck`, which is exactly why that command is being run manually per phase for
  now). Fixed by splitting into `flat_rows(rows)` / `join_rows(authors)` — two
  functions instead of one shape-keyed dispatcher.
- **Gate verification, live on this machine** (confirmed to be the §3 reference
  machine — `bench env`'s own output matches it exactly: AMD Ryzen 9 5900HS, 8
  physical/16 threads, SMT siblings `{0,1}/{2,3}/.../{14,15}`, governor performance,
  boost on, Python 3.13.9 GIL-enabled):
  - `bench env` — full block, matches §3.
  - `bench env check` — correctly warns (boost on, dirty tree) and exits 1.
  - `bench db up` (default port 5432) — succeeded; the port-5432 auth failures seen
    during phase 2's `just test` run were from something transient, not a permanent
    collision (nothing was listening by the time phase 1 ran `bench db up`). Ran the
    full round trip anyway on an explicit alternate port for a clean pinning check:
    `bench db up --cores 6,7 --port 5433` → `docker inspect` from outside confirms
    `CpusetCpus: 6,7` and `NetworkMode: host`. `bench db seed --rows 1000 --shape flat`
    and `--rows 200 --shape join` both round-tripped (verified via `psql` directly:
    1000/200/1000 rows in `users`/`j_authors`/`j_posts`). `bench db down` removed the
    container and cleared state; `bench db status` afterward correctly reports "no
    database is up" and exits 1.
  - Still outstanding from phase 2's log: if `bench db up` is run without `--attach`
    or an explicit port and something else later occupies 5432 with different
    creds, `bench db up` will now fail loudly on the port check rather than silently
    producing a DSN nothing can reach — this is the fix for that class of problem,
    not just a workaround.
- Added `.gitignore` entry for `benchmarks/results/runs/` (D15) and a small
  `results/runs/.state/db.json` for `bench db`'s cross-invocation state (also
  gitignored, being under `results/runs/`).
- README: mentioned `SqliteEngine` as the third engine (§8's "shipped ... README
  as the third engine" landed here since it touches the same paragraphs).

### Phase 3 — contenders, MockEngine, bench micro — status: done

Built: `engines/mock.py` (`MockEngine`, overrides only `_require_pool()` per
§7 — confirmed the fake pool needs no `connect()`/`close()` override at all,
since nothing else touches `self.pool` outside `_require_pool()`),
`contenders/{flat,join}.py` (rowform/raw-aiosqlite/SA-Core-mappings/SA-Core-
positional/SA-ORM per shape, plus a MockEngine variant per shape, plus a
`rowform_stages()` helper per shape for decomposition — not a registered
contender), `cli/micro.py` (`run`/`decompose`).

Discoveries and decisions:

- **Registry key collision, found immediately**: `@contender("rowform", ...)`
  for both flat and join shapes collided under the originally-planned
  `REGISTRY: dict[str, ContenderSpec]` keyed by bare name — "rowform" and
  "SQLAlchemy async Core (positional)" are legitimately reused names across
  shapes. Fixed by keying `REGISTRY` on `(shape, name)` instead; `get()`'s
  signature changed to `get(shape, name)` accordingly (was unused elsewhere,
  no other call sites to update).
- **`harness.seed`'s `flat_rows`/`join_rows` split (from the phase-1 log)
  paid off here too** — contenders and `cli/micro.py`'s `_mock_handle()` never
  hit the union-return ambiguity phase 1 found, because there's no
  shape-keyed dispatcher left to hit it through.
- **MockEngine's rows must already be ASYNCPG_CONVERTERS-shaped** (real
  `bool`s, not sqlite's 0/1) since `MockEngine` subclasses the asyncpg
  `DatabaseEngine` and inherits its converters unchanged — `_mock_handle()`
  in `cli/micro.py` sources real rows from a throwaway sqlite db once at
  setup (paying the driver cost only there, never inside a timed
  `request()`) and casts the boolean columns before handing them to
  `MockEngine`.
- **Stage decomposition scope**: implemented for the rowform contender only
  (not generically for every contender) — `rowform_stages()` splits
  `request()` into `fetch`/`serialize`/`whole`, and `bench micro decompose`
  runs each through `best_of()` and prints `fetch + serialize` next to the
  separately measured `whole`, plus the residual. A fully generic
  per-contender staging protocol was in scope for "one definition… shared by
  every tier" but not for the phase-3 gate itself, which only requires that
  *a* decomposition prints *a* residual; deferred rather than gold-plated.
- **Gate verification, live**, all via `just bench micro ...`:
  - `run --shape flat`: `[flat/sqlite]` and `[flat/mock]` equivalence both
    PASS, byte-identical across all 5 sqlite contenders — and the mock
    group's sha256 matched the sqlite group's (rows for both were sourced
    from the same query), a nice cross-check that `_mock_handle()` is honest.
  - `run --shape join --gc both`: at `rows=2000, limit=1000` (~2000
    objects/iteration, matching PLAN.md §4's figure) gc=on -> gc=off collapsed
    stdev **7.3x** for the sqlite rowform contender and **~64x** for the
    MockEngine contender (0.9479ms->0.1291ms and 1.1164ms->0.0173ms) —
    reproduces the claimed 5-10x collapse and then some for the driver-free
    path. (At small sizes, rows=500/limit=200/iterations=20, the effect does
    **not** show — noted here so a future run at toy sizes isn't mistaken for
    a broken harness; GC has to actually be provoked to matter.)
  - `decompose --shape flat` and `--shape join`: residuals were -0.5% and
    +2.4% of the whole respectively — small and centered near zero, as
    expected when `fetch`+`serialize` genuinely are the whole request's only
    two components.
- Typecheck: same manual-scope caveat as phase 1 — ran `basedpyright` against
  every new module explicitly (0 errors after removing one
  `reportUnnecessaryTypeIgnoreComment` on `MockEngine._require_pool`, where
  basedpyright didn't see an override incompatibility to suppress in the
  first place).

### Phase 4 — service, load, audit — status: done

Built: `service/app.py` (`build_app()`, routes generated from the registry,
lifespan not `on_event`, configured from env vars since each worker is a
separate uvicorn subprocess), `service/launch.py` (N single-worker uvicorn
subprocesses, each `taskset`-pinned before exec rather than using uvicorn's
own forking `--workers`), `load/httpload.py` + `load/locust.py` (ported,
every validation gate kept), `load/audit.py` (Little's Law, `/proc/net/tcp`
socket counting, scaling knee, `/noop` headroom, generator saturation,
generator-agreement), `cli/{service,load}.py`.

Discoveries and decisions:

- **Real bug caught by actually running the gate, not just writing the code**:
  the first `bench load audit` run reported `sockets: 0` at every concurrency
  level even though Little's Law and the httpload<->locust cross-check both
  passed. Root cause: `count_established()` was called *after* `await
  httpload.run(...)` returned, but `httpload.run()` closes every connection
  before returning — so the ESTABLISHED sockets were always already gone by
  sampling time. Fixed by starting the httpload run as a task, sleeping
  `warmup + duration/2` (past its internal warmup phase, into the middle of
  the measured window), sampling `/proc/net/tcp` then, and only awaiting the
  task afterward. This is exactly the trap PLAN.md's own audit exists to
  catch — glad it was caught by running the gate rather than trusting the
  code read plausible.
- **"DB below saturation" only partially covered**: added
  `check_generator_saturation()` (client CPU utilization via
  `time.process_time()`/`time.perf_counter()` deltas, threshold 90%) and wired
  it into `bench load audit`. There is no separate "DB" role to measure yet
  for the sqlite backend — sqlite runs in-process inside the one FastAPI
  worker, so "server" and "DB" are the same process/role here. A real
  DB-saturation check needs `harness.cpuacct` pointed at the *Postgres*
  container's cgroup (`cpuacct.cgroup_cpu_seconds`, already written in phase
  1), which only applies once `bench load` gains a `--backend postgres` path
  — not built in phase 4, since the gate's example command
  (`bench load audit --levels ...`) doesn't take `--backend` in §9's spec and
  the sqlite path was sufficient to exercise every other check. Flagged as a
  gap, not silently dropped.
- **`bench load run`/`bench load audit` are self-contained** (provision their
  own ephemeral sqlite db + one uvicorn worker, tear both down after) rather
  than requiring a separate `bench service run` first — matches the old
  `bench_locust.sh`'s self-contained shape. `bench service run` still exists
  standalone for manual `curl`/profiler-attach use (phase 5).
- **Gate verification, live**, all via `just bench load ...` against the
  sqlite backend:
  - `load run --generator httpload` and `--generator locust`: both drive the
    FastAPI service end to end (verified with a live `curl` against a
    `bench service run` process too — `/noop` and `/rowform` both respond
    with correct JSON).
  - `load audit --levels 1,2,4,8,16`: sockets == connections exactly at every
    level; Little's Law in-flight within 0.1 of the target at every level
    (well inside the 10% tolerance); generator CPU utilization 13-24%, far
    below the 90% saturation threshold; `/noop` at 7.2-7.4x the fastest DB
    endpoint (>= the 2x minimum); httpload vs locust agreed within 1.9-2.9%
    (<= the 7% tolerance) at c=8. Exits 0 on success, confirmed.

### Phase 5 — profiler adapters + attribution + render — status: done

Built: `profiling/base.py` (protocols), `profiling/{cprofile,yappi,pyinstrument}.py`
(in-process), `profiling/{pyspy,austin}.py` (external), `profiling/attribution.py`
(ported `profkit.py` + `check_impossible_rows` tripwire), `profiling/render.py`
(pstats/yappi -> folded stacks -> speedscope JSON), `cli/profile.py`
(`micro`/`load`).

Discoveries and decisions:

- **New dependency found necessary while wiring this up**: `austin-dist`
  (declared in §12) ships only the `austin` sampler *binary*; converting its
  output to speedscope needs `austin2speedscope`/`mojo2austin`, which live in
  a separate PyPI package, `austin-python` (import name `austin`, confusingly
  distinct from the `austin` binary) — added to the `profile` dependency
  group, re-locked.
- **Two real bugs found only by actually attaching austin to a live
  process** (writing the adapter code was not enough — both were invisible
  until run):
  1. Austin 4.0's exit code is not a success/failure signal — a fully clean
     sample (0.00% error rate, complete output file) still exits 254.
     `AustinProfiler.attach()` originally treated any nonzero exit as fatal
     and raised on every run, including good ones. Fixed by checking whether
     the raw output file is non-empty instead of trusting the exit code.
  2. `austin -o file` writes the binary "mojo" format by default; feeding it
     straight to `austin2speedscope` fails with a `UnicodeDecodeError` (it
     expects the older text/collapsed format). The real pipeline is `austin`
     (mojo) -> `mojo2austin` (text) -> `austin2speedscope` (JSON) — all three
     ship in `austin-python`; fixed `austin.py` to run both conversion steps.
  3. (From `load/audit.py`, phase 4, but the same lesson bears repeating
     here): trust the artifact, not the process's reported status, whenever
     an external tool's exit-code convention isn't independently verified.
- **Scope call on `render.py`**: pyinstrument ships its own speedscope
  renderer (used directly, not through `render.py`) and py-spy/austin now
  produce speedscope natively through their own toolchains (per the fix
  above), so `render.py`'s generic converter only needed to cover cProfile
  and yappi — both describe a call *graph* (aggregate caller/callee edges),
  not literal per-sample stacks, so folded-stack lines are reconstructed by
  walking each function's most-frequent caller chain (cProfile) or its
  callee tree from every root (yappi) — a standard approximation used by
  tools like `pyprof2calltree`/`flameprof`, not a claim of one-true-stack
  per sample.
- **Gate verification, live**, all via `just bench profile ...`:
  - `profile micro --only "raw aiosqlite"` (a `tags=("floor",)` contender that
    never imports rowform): the impossible-row tripwire printed "rowform
    categories read 0.0%... (OK)" — confirmed by inspecting the actual rollup
    table (no `rowform (library)`/`rowform (codegen)` row printed at all,
    i.e. exactly 0%).
  - The same run's default cross-check: cProfile 1.4x baseline, pyinstrument
    1.6x baseline, both printed, no material-divergence warning (correctly,
    since 1.4/1.6 is nowhere near the 3x threshold).
  - `profile load`: py-spy rendered a flamegraph (90 frames / 1240 samples)
    and austin rendered one too (352 frames / 194879 samples) from the same
    live, loaded uvicorn worker, concurrently, after the two bugs above were
    fixed. Verified by loading both speedscope JSON files back and confirming
    non-empty `samples`/`frames` arrays, not just "the command exited 0".

### Phase 6 — report/record/verify, docs, cleanup — status: done (one gap noted)

Built: `cli/{report,verify,record}.py`, `docs/RUNS.md`, wired `--record` into
`bench micro run` so there's something for the other three commands to
operate on. Deleted all 27 remaining old scripts under `benchmarks/`
(`git rm`, recoverable at commit `32ad4a1` per D1 — not committed, left as a
working-tree change for review). Widened `[tool.pyright] include` to add
`"benchmarks"` now that the old, never-typechecked scripts are gone. Rewrote
the doc references in `docs/BENCHMARKS.md`/`docs/METHODOLOGY.md`/
`benchmarks/results/README.md`.

Discoveries and decisions:

- **`Run.equivalence`/`cells` needed a real writer to test against** — added
  `--record` to `bench micro run` (the simplest, most complete tier) rather
  than to all of `micro`/`load`/`profile` given remaining time; `load`/
  `profile` still only print to stdout. Flagged as a scope gap, not silently
  dropped: `report`/`verify`/`record` all work against any `run.json`
  regardless of which command wrote it, so extending `--record` to the other
  two CLIs later is additive, not a redesign.
- **Docs rewrite: chose a migration-table notice over rewriting every
  reference inline.** 48 `benchmarks/*.py`/`.sh` mentions span two documents
  (`docs/BENCHMARKS.md`, `docs/METHODOLOGY.md`), many inside literal shell
  code blocks (`taskset -c 0 python3 benchmarks/profile_pg.py --pin 0:2,3
  ...`). Editing each occurrence to name a new-suite equivalent inline would
  either corrupt the shell syntax or misrepresent what was *actually run* to
  produce a historical figure — and D3/D5 already forbid re-deriving or
  republishing those figures. Instead added one notice block at the top of
  each document: states plainly that the named scripts are gone (recoverable
  at `32ad4a1`), that command lines below are preserved as the historical
  record of what ran, and gives a full old-script -> new-command mapping
  table. `benchmarks/results/README.md` got a shorter version of the same
  notice. **This does not make every literal path-substring search come up
  empty** — a reader following any reference is never left stuck without
  context, but a script like `grep -c 'benchmarks/bench_sqlite\.py'
  docs/BENCHMARKS.md` still returns >0. Given the choice between that and
  silently-corrupted shell examples or misrepresented history, this was the
  correct trade-off under the time available — flagged here rather than
  claimed as literally zero-references.
- **`bench record`'s git branch/commit operations were not executed
  live** against this repository during implementation, per the standing
  git-safety rule (create commits/branches only when the user asks) — writing
  the *tool* that a user later invokes is not the same as invoking it
  unprompted mid-implementation. Verified by code review (checkout -b ->
  git add -f the run dir -> commit -> checkout back to the original branch;
  appends one row to `docs/RUNS.md` on the original branch) plus lint/typecheck,
  not by an end-to-end run. This is the one phase-6 item without a live
  verification and should be the first thing spot-checked (e.g. against a
  scratch clone) before relying on it.
- **Gate verification, live**, for the two-thirds that could be run safely:
  - `bench micro run --record` writes a `run.json`; `bench report show
    <run_id>` printed its suite/quotable/equivalence/cells correctly
    (`quotable=False` because this repo's tree is dirty throughout this whole
    session — correct behaviour, not a bug).
  - **`bench verify run <run_id>`** re-executed the exact recorded
    `invocation.argv` as `python -m benchmarks micro run --shape flat ...
    --record`, re-ran clean (exit 0, same equivalence sha256), and itself
    wrote a fresh `run.json` — "one command reproduces any recorded run",
    confirmed end to end.
  - `just lint`/`just typecheck`/`just test` all pass with `benchmarks/` now
    in the typecheck scope and every old script deleted: `0 errors, 0
    warnings, 0 notes`, `995 passed, 235 skipped` (skipped = the pg-dependent
    suite, no Postgres container up at the time of this final run).

### Post-implementation fix — `service/launch.py` readiness check was unsound

Reported by the user: `just bench load run` (default `--port 8000`) failed with
`LoadError: warmup failed for /rowform: HTTP 308: b'HTTP/1.1 308 Permanent
Redirect\r\n'`.

Root cause: this machine already had an unrelated service listening on
`0.0.0.0:8000` (confirmed via `ss -ltnp`/`pgrep`) before `bench load run` ever
ran. `service/launch.py`'s `_wait_ready()` only checked "can a TCP connection
be opened on this port" — which an *already-occupied* port satisfies just as
well as a freshly-bound one, just by answering with the wrong service. The
real uvicorn worker's `create_subprocess_exec` presumably failed to bind and
exited immediately, but nothing checked that; `_wait_ready()` declared success
because *something* answered on the port, and every subsequent httpload
request silently went to the foreign service instead, which redirects unknown
paths with a 308.

This affected every command built on `service.launch.launch()`: `bench
service run`, `bench load run`/`audit`, `bench profile load` — not just the
one the user happened to hit.

Fixed in `service/launch.py`:
- `_port_in_use()` check *before* spawning each worker, raising immediately
  with a clear message if the target port is already occupied — matches the
  same pattern already used in `backends/postgres.py` for the exact same
  class of problem (D6's host-networking port collision).
- `_wait_ready()` now also races the TCP-connect poll against the worker
  subprocess's own `wait()`, and raises immediately (rather than waiting out
  the full timeout) if the process exits before answering.

Re-verified after the fix: `bench load run --port 8901` and `bench load audit
--port 8910` both pass cleanly (Little's Law, `/noop` headroom, and the
httpload<->locust cross-check all still OK); the default `--port 8000` now
fails immediately with the new, correct error message instead of silently
talking to the wrong server. `just lint`/`just typecheck`/`just test` still
green (995 passed, 235 skipped).

**Lesson for anything else in this suite that polls "is it up yet" via a bare
socket connect** (there is at least one more: `backends/postgres.py`'s
`EphemeralPostgres._wait_ready()` polls via `asyncpg.connect()`, which would
similarly succeed against an unrelated Postgres already on the target port —
but D6's `_port_in_use()` pre-check there already guards exactly this case,
so it was not vulnerable to begin with. `service/launch.py` was the one place
that had the pre-check pattern established elsewhere but hadn't applied it.)

### Post-implementation addition — `bench contenders list`, consistent help text

User request: make the contender registry inspectable from a script, and
have every other command's `--only`/`--shape` help text point at it — the
registry (`harness/registry.py`) already existed and was the source of truth
internally, but the only way to see what was registered was to read
`contenders/*.py`.

Added `cli/contenders.py`:
- `bench contenders list [--shape] [--backend] [--only] [--json]` — prints
  name/backend/shape/shipped/tags; `--json` is the "from a script" path (a
  plain array, one object per contender, meant for `jq`/`python -c` piping,
  not for human reading).
- `bench contenders shapes` — the valid `--shape` values, standalone.

Added `registry.ONLY_HELP`/`registry.SHAPE_HELP` string constants (one
definition, not one per CLI file) and wired them into every `--only`/`--shape`
option across `cli/{micro,load,profile,service,db}.py` — `db.py`'s `seed
--shape` and `service.py`'s `run --shape`/`--backend` included, even though
neither takes `--only`, since "what are the valid shapes" is the same
question everywhere. Verified: `bench contenders list`, `--json`, and
`--shape join --json` all work; `--help` on `micro run`/`load run`/`load
audit`/`profile micro`/`profile load`/`service run` all now show the pointer.
`just lint`/`just typecheck`/`just test` still green.

### Post-implementation addition — quiet uvicorn logs, PID/CPU monitoring, workers vs concurrency

User request, four parts: (1) `bench load` should not print FastAPI/uvicorn
logs to stdout; (2) it should always track the PIDs of every process it
spawns; (3) it should sample every tracked process's CPU usage once a second,
printing and recording it; (4) an optional `--name` should persist that data
to JSON; (5) `--workers` (uvicorn process count) and `--concurrency`/
`--levels` (load in flight) should be distinct, independently-set knobs.

Built:
- `harness/cpuacct.read_pid_cpu_seconds()` — made public (was
  `_read_pid_cpu_seconds`), reused instead of duplicated.
- `harness/monitor.py` (`ProcessMonitor`) — `track(role, pid)`/`untrack()`,
  `start()`/`stop()`, samples every tracked pid once a second, prints one
  line, accumulates `{"t":…, role: utilization, …}` rows, `to_dict()` for
  persistence. A role whose process already exited just reads 0% (via
  `read_pid_cpu_seconds`'s existing exited-process guard) instead of crashing
  the monitor.
- `service/launch.launch(..., quiet=True)` — discards each worker's
  stdout/stderr instead of inheriting the caller's. Discarded, not piped: a
  captured pipe nobody drains risks filling its buffer and blocking the child
  on a long run, and the port-in-use pre-check (added for the last bug
  report) already catches the most common early-failure mode. `bench service
  run` keeps `quiet=False` (its whole point is visible logs for manual
  `curl`); `bench load run`/`audit` now pass `quiet=True`.
- `load/locust.run(..., on_spawn=...)` — callback invoked with the *measured*
  locust subprocess's pid (not the discarded warmup one), so the caller can
  track it without `load/locust.py` needing to import `ProcessMonitor` itself.
- `cli/load.py`: `--workers` (default 1, independent of `--concurrency`/
  `--levels`) on both `run` and `audit`. Each worker is still its own
  process/port (PLAN.md §5 — no shared-port forking `--workers`, to keep
  pinning sound), so testing N workers under load means splitting the
  requested concurrency across N ports and summing: `_split_across_workers()`
  divides as evenly as possible, `_httpload_across()`/`_locust_across()` run
  one generator invocation per port concurrently, and
  `_aggregate_httpload()`/`_aggregate_locust()` merge the per-port results —
  throughput adds; httpload's latency percentiles are recomputed from the
  *pooled* raw samples (each `HttpLoadResult` already carries `latencies_s`),
  not averaged, since averaging percentiles across unequal samples isn't the
  percentile of the pooled set; locust gives no raw samples, so its merge is
  a completed-count-weighted average, documented as an approximation.
  `--name` writes config + per-level results + `monitor.to_dict()` to
  `results/runs/loadtests/<name>.json` (gitignored under the existing
  `results/runs/` rule, no new `.gitignore` entry needed).
- Every `--only`/`--shape` help string already pointed at `bench contenders`
  (previous change); `--workers`/`--concurrency`/`--levels` now cross-reference
  each other in their own help text so the distinction is visible in
  `--help`, not just in behavior.

Verified live:
- `load run --name smoke1`: no uvicorn INFO lines in the output; one CPU line
  per second (`server(pid=…) NN.N%  generator(pid=…) NN.N%`); JSON on disk
  with `config`/`cells`/`monitor.samples` all populated.
- `load run --workers 2 --concurrency 8`: two `server-0`/`server-1` roles
  tracked at distinct pids, concurrency split 4/4 across the two ports, and
  aggregate rps (~3176) came out roughly double the single-worker c=8 number
  from the earlier smoke test (~1839) — consistent with two independent
  worker processes under reduced per-process contention.
- `load run --generator locust`: `generator-locust(pid=…)` appears in the
  monitor output only once locust's subprocess actually spawns (mid-run), as
  designed.
- `load audit --name audit-smoke`: full gate still passes (sockets ==
  connections, Little's Law in-flight within tolerance at both levels, `/noop`
  5.88x headroom, httpload<->locust delta 1.3%), exits 0, and the CPU monitor
  runs throughout without interfering with any of the timing-sensitive
  mid-run socket sampling from the earlier bug fix.
- `bench service run` (unaffected: still shows live uvicorn logs, `quiet`
  defaults to `False`) and `bench profile load` (unaffected: still attaches
  py-spy/austin and renders flamegraphs) both re-verified after the
  `launch()` signature change.
- `just lint`/`just typecheck`/`just test`: all green (995 passed, 235
  skipped) throughout.

### Post-implementation addition — unique slugs, descriptions, `--case`

User request: every contender needs a unique kebab-case slug and a
description, and `bench load` should select a contender by that slug (`--case`)
instead of separate `--shape`/`--only`/backend params. Slug naming:
`{backend}-{shape}-{name}`.

Built:
- `ContenderSpec` gained `slug`/`description` fields. `harness/registry.py`
  now keys `REGISTRY` by slug (was `(shape, name)`) — computed as
  `f"{backend}-{shape}-{_kebab(name)}"`, guaranteed unique by construction
  since backend+shape+name was already the real identity, just not exposed
  as one string before. `@contender(...)` now requires `description=`
  (no default, no falling back to the factory's docstring — a docstring is
  for the source reader, `description` is for `bench contenders list`,
  which needs to stand on its own). `registry.get()` changed from
  `get(shape, name)` (unused anywhere) to `get(slug)`.
- Every `@contender(...)` call in `contenders/{flat,join}.py` (10 total)
  got a one-line `description=`.
- `service/app.py`: removed its own ad-hoc `slug()` (name-only, re-derived
  independently of the registry) — routes are now `/{spec.slug}` directly,
  so a route path can never drift from what `bench contenders list` reports
  for the same contender. `cli/profile.py` updated to match (`f"/{specs[0].slug}"`
  instead of importing the now-deleted function).
- `cli/load.py`: `run`/`audit` traded `--shape`/`--only` for one `--case`
  (default `"sqlite-flat-rowform"`, matching prior default behavior).
  `_resolve_case()` looks the slug up via `registry.get()`, converts a
  `KeyError` into a `typer.BadParameter` listing every known slug, and
  rejects a non-sqlite-backend case with a clear message (`bench load` still
  only provisions sqlite) rather than failing confusingly deeper in
  `_provision`. `shape`/`path` are now derived from the resolved spec
  (`spec.shape`, `f"/{spec.slug}"`) instead of taken as separate params.
- `cli/contenders.py list` now prints (and `--json` emits) `slug` and
  `description` alongside the existing fields — `slug` is exactly the value
  `--case` takes, so the discovery path is one command.

Verified live:
- `bench contenders list` — all 10 contenders show a unique slug (e.g.
  `sqlite-flat-rowform`, `mock-join-rowform-mockengine`,
  `sqlite-flat-sqlalchemy-async-core-mappings`) and a real description.
- `load run --case sqlite-flat-rowform` and `--case
  sqlite-join-sqlalchemy-async-orm` both work end to end.
- `load run --case mock-flat-rowform-mockengine` fails with a clear
  "is a 'mock'-backend contender; bench load only provisions a sqlite
  backend today" — not a confusing failure deeper in provisioning.
- `load run --case does-not-exist` fails with every known slug listed.
- `load audit --case sqlite-flat-sqlalchemy-async-core-positional` runs the
  full gate end to end (confirmed the exit code is real and not a false
  pass, by re-running without piping through `tail` after a first attempt's
  2-second duration produced a noisy 13.8% httpload/locust delta — a longer
  4s duration passed cleanly at 4.7%, consistent with ordinary jitter at
  very short measurement windows, not a bug).
- `just lint`/`just typecheck`/`just test`: all green throughout (995
  passed, 235 skipped).

## 14. Accepted risks

- **D3 removes the strongest available check on the port.** The refactor changes what sits
  inside timed regions, which is correction 6 — the flaw `METHODOLOGY.md` says self-review
  is blindest to. Mitigated only by D5 (nothing published) and by the intrinsic gates in
  §4. Any future decision to publish should first re-derive the figure and compare against
  `benchmarks/results/`.
- **No CI regression gate** (D11).
- **8 physical cores bounds the worker sweep.** If the saturation gates in D12 trip at 4
  workers, the fix is a cheaper generator (`oha`/`wrk`), not a wider sweep.
- **`docs/BENCHMARKS.md` will cite deleted scripts until phase 6.**
