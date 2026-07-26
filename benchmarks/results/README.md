# Raw benchmark artifacts

Output from the runs quoted in [`docs/BENCHMARKS.md`](../../docs/BENCHMARKS.md).
Committed as evidence so the tables there can be traced to a real run.

| file | what it is |
|---|---|
| `sqlite_latest.json` | sqlite micro-benchmark, 200k rows, 1000 rows/response, 300 iterations x **5 trials** per approach. Includes the env block and per-trial mean/median/p95; quote medians. |
| `sqlite_order_check.txt` | Ordering-bias check for the sqlite suite: three forward runs, one `--reverse` run, and per-approach isolated runs. Shows the sqlite suite is *not* order-biased. |
| `pg_load_100rows.json` | Postgres load sweep, 100 rows/request, c=1,8,32,64. **Combined suite — ordering-biased**, kept for shape not ratios. |
| `pg_load_1000rows.json` | Same at 1000 rows/request, c=1,8,32. Also combined-suite. |
| `pg_load_100rows_pinned.json` | Combined suite with client on cores 0,1 and Postgres on 2,3. Superseded — the 2-core client was a mistake (see METHODOLOGY correction 3). |
| `isolated_pinned_raw.txt` | sqlom and the two hand-written asyncpg baselines, **isolated** (one process each), 3 trials, client 2 cores. |
| `isolated_pinned_sqlalchemy.txt` | SQLAlchemy async Core and ORM, isolated, 3 trials, client 2 cores. |
| `isolated_unpinned.txt` | sqlom, async ORM and the codegen-dict floor, isolated, 3 trials, 4 cores shared. |
| `core_sweep_1core_client.txt` | **The headline measurement.** Client pinned to one core, Postgres given 1/2/3 cores, sqlom vs async ORM, 3 trials each. |
| `profile_pg_1core.txt` | **Profiled run**: client pinned to core 0, Postgres to cores 2,3. cProfile (CPU timer) + pyinstrument sampling for sqlom and the async ORM, plus a `sslmode=disable` contrast run. |
| `multiprocess_scaling.txt` | 1 vs 2 sqlom worker processes, one core each, Postgres on cores 2,3. |

Anything labelled *combined suite* ran all contenders in a single process and is
biased by contender order; see
[METHODOLOGY correction 2](../../docs/METHODOLOGY.md#2-contender-ordering-inside-one-process).
Prefer the `isolated_*` and `core_sweep_*` files for any number you intend to quote.
