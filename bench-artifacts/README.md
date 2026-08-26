# 2026-08-26 — sqlite's BEGIN

Raw `run.json` artifacts behind [docs/RUNS.md](../docs/RUNS.md) 2026-08-26 and the sqlite
table in README.md / METHODOLOGY.md. Two shas, deliberately:

* **`b7c4c71`** — baseline. `publish_tables.py` is already limit-aware here; the library
  still sends its sqlite `BEGIN` through `conn.exec_driver_sql` (three worker-thread
  hops) and the sqlite floors still send no transaction at all.
* **`17e867e`** — after. `BEGIN` on the driver connection (one hop), and every sqlite
  floor sends the transaction rowform sends.

Every run `quotable=True`, boost off and verified still off at the end of each.

| run | cell |
|---|---|
| `16-39-55Z`, `16-46-15Z`, `16-52-57Z` | baseline `flat`/`join`/`wide` @1000 |
| `16-54-47Z` | baseline `flat` @1, 5000 iterations — **superseded**, 17-24% dispersed |
| `17-01-56Z`, `17-44-29Z` | baseline `flat` @1, 20000 iterations, two independent runs |
| `17-10-53Z`, `17-17-10Z`, `17-23-50Z` | after `flat`/`join`/`wide` @1000 |
| `17-30-21Z`, `17-38-15Z` | after `flat` @1, 20000 iterations, two independent runs |
| `19-56-08Z` | after `flat` @1, 5 trials — **the published `@1` column** |

Two caveats worth carrying with the files. `17-23-50Z` (after `wide`) was recorded while
I was reading earlier `run.json` files with `uv run python` on the same box, which is a
few hundred ms of interpreter startup against a measurement whose premise is that nothing
else runs; its `flat @1` sibling was re-run for that reason and `wide` is quoted anyway
because its spreads stayed under 5%. And `rowform (idiomatic)` at `flat @1` came back
above equal-work rowform in `17-38-15Z` — impossible, it does strictly less work — which
is why the published column is the 5-trial run, where the three tie as they should.

Reproduce: `git checkout <sha>` and see the recipe in
[docs/METHODOLOGY.md](../docs/METHODOLOGY.md#reproducing).
