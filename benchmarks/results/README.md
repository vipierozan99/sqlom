# Raw benchmark artifacts

Output from the runs quoted in [`docs/BENCHMARKS.md`](../../docs/BENCHMARKS.md).
Committed as evidence so the tables there can be traced to a real run.

| file | what it is |
|---|---|
| `sqlite_latest.json` | sqlite micro-benchmark, 200k rows, 1000 rows/response, 300 iterations x **5 trials** per approach. Includes the env block and per-trial mean/median/p95; quote medians. |
| `sqlite_setup_cost_ab.txt` | **Fairness correction.** Paired A/B, one process, interleaved: SQLAlchemy's connection/`Session` setup timed inside the loop vs hoisted, at 100 and 1000 rows. Sizes the ~8% the Core ratio was overstated by, and the 12.9% an over-hoisted `Session` would have flattered the ORM by. |
| `sqlite_box_drift.txt` | Proof that the sqlite absolutes moved ~1.35x because the *machine* changed, not the code: the whole pre-change tree re-run on the current box reproduces the current numbers, not the published ones. |
| `sqlite_order_check.txt` | Ordering-bias check for the sqlite suite: three forward runs, one `--reverse` run, and per-approach isolated runs. Shows the sqlite suite is *not* order-biased. |
| `isolated_pinned_raw.txt` | sqlom and the two hand-written asyncpg baselines, **isolated** (one process each), 3 trials, client 2 cores. |
| `isolated_pinned_sqlalchemy.txt` | SQLAlchemy async Core and ORM, isolated, 3 trials, client 2 cores. |
| `isolated_unpinned.txt` | sqlom, async ORM and the codegen-dict floor, isolated, 3 trials, 4 cores shared. |
| `core_sweep_1core_client.txt` | **The headline measurement.** Client pinned to one core, Postgres given 1/2/3 cores, sqlom vs async ORM, 3 trials each. |
| `profile_pg_1core.txt` | **Profiled run**: client pinned to core 0, Postgres to cores 2,3. cProfile (CPU timer) + pyinstrument sampling for sqlom and the async ORM, plus a `sslmode=disable` contrast run. |
| `profile_sqlite.txt` | **Profiled sqlite run** — no event loop, pool or TLS. cProfile + pyinstrument for sqlom, SQLAlchemy Core and ORM, single-threaded on core 0. Isolates the mapper's own cost. |
| `psycopg_both_default.txt` | **The strictest comparison.** psycopg3 async on both sides, both libraries at default pool behaviour, both event loops, data layer only. |
| `psycopg_end_to_end.txt` | The same through FastAPI + uvicorn, with a `/noop` framework floor. |
| `tests.txt` | Output of the pytest suite (529 tests, including mypy and pyright over tests/typing/): SQL generation, codegen, joins end-to-end on sqlite, and — against live Postgres, parameterised over both engines — lifecycle, `fetch_all`/`fetch_json`, joins, transactions, savepoints, isolation, the in-transaction guard, and the conditional-reset invariant. Replaces the hand-rolled `verify_transactions.py`. |
| `hold_time.txt` | Whether sqlom releasing its pooled connection *before* hydrating (SQLAlchemy holds it through row shaping) inflates the comparison. It does not — making Core symmetric measures 3% slower, and starving the pool to 2 against c=8 moves it by 1 rps, because the client is CPU-bound not connection-bound. |
| `locust_end_to_end.txt` | **Independent generator.** The same endpoints re-measured with locust (`FastHttpUser`), plus a head-to-head against `httpload.py` on the same uvicorn process. Confirms the §14 ratios; also shows locust is client-bound on `/noop`, so quote its database endpoints only. |
| `concurrency_verification.txt` | Proof the generator is concurrent, three ways: ESTABLISHED socket count from `/proc/net/tcp`, Little's Law, and throughput scaling across 1/2/4/8/16 connections. |
| `final_comparison.txt` | **The bottom line.** sqlom vs SQLAlchemy Core/ORM, both fully tuned, data layer only, one core, c=8. |
| `fastapi_end_to_end.txt` | The same comparison through FastAPI + uvicorn, server and load generator on separate cores, including a `/noop` framework floor. |
| `pipeline_reset.txt` | Why pipelining the pool reset loses: per-request cost decomposition including an empty pipeline, the `executemany` amortisation contrast, and c=8 throughput. |
| `conditional_reset.txt` | Fixing the pool reset without behaviour change: asyncpg default vs `reset=`no-op vs `conditional_reset=True` at several escape-hatch rates. Async single-thread c=8. |
| `pg_concurrency_uvloop.txt` | The §10 matrix repeated on Postgres: concurrency 1/4/8/32 x {asyncio, uvloop} x {default pool, `reset=`no-op}, client core 0, Postgres cores 2,3. |
| `sqlite_async_uvloop.txt` | asyncio concurrency (c=1/8/32, single thread) and uvloop on the sqlite path, versus a synchronous reference, plus aiosqlite's thread-offload cost. |
| `rust_and_ceilings.txt` | Bounds for a native object builder and a Rust rewrite, plus the empirical psqlpy (Rust/tokio-postgres) comparison including its `as_class` Rust-built objects. |
| `optimize_sqlite.txt` | Attempts to optimize the sqlite path further (cursor reuse, tuple-index bool, zero-callback dicts, `row_factory`, no-slots, no-objects) against a fetch-only floor. Mostly a negative result. |
| `optimize_stack.txt` | Stacked optimizations (uvloop / pool `reset=` no-op / held connection / no TLS), client core 0, Postgres cores 2,3, median of 3, one config per process. |
| `multiprocess_scaling.txt` | 1 vs 2 sqlom worker processes, one core each, Postgres on cores 2,3. |

Three combined-suite sweeps (`pg_load_100rows.json`, `pg_load_1000rows.json`,
`pg_load_100rows_pinned.json`) were **removed** rather than kept. They predated the
runner's affinity/CPU metadata and per-result `trial` field, so the checked-in
`bench_pg_load.py` could not reproduce them — and they were already marked
ordering-biased and non-quotable, so no table in BENCHMARKS.md drew on them. An
artifact that cannot be regenerated from the code in the repo is a liability, not
evidence. The isolated and `core_sweep_*` files above back every published figure.

Anything labelled *combined suite* ran all contenders in a single process and is
biased by contender order; see
[METHODOLOGY correction 2](../../docs/METHODOLOGY.md#2-contender-ordering-inside-one-process).
Prefer the `isolated_*` and `core_sweep_*` files for any number you intend to quote.
