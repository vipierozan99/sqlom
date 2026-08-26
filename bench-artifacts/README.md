# 2026-08-27 — the postgres table, re-measured at `e28c2e0`

Raw `run.json` artifacts behind [docs/RUNS.md](../docs/RUNS.md) 2026-08-27 and the
postgres table in README.md / METHODOLOGY.md. One sha, `e28c2e0` (main), and every run
`quotable=True` with boost off and verified still off at the end.

**The server is not the one earlier postgres tables used.** These ran against an attached
PostgreSQL **16.15** on `127.0.0.1:5432` (`--pg-dsn`, stock config: `shared_buffers=128MB`,
`fsync=on`) rather than the ephemeral container `bench db up` provisions. Ratios inside a
column are unaffected — every contender in a cell talks to the same server — but absolute
milliseconds are **not** comparable with the 2026-08-16 postgres tables, and `wide` shows
that plainly: both rowform and Core come in ~15% under their August figures while the ORM
moves 3%, on code neither sha changed.

| run | cell |
|---|---|
| `22-44-29Z`, `22-47-22Z`, `22-50-03Z` | postgres `flat`/`join`/`wide` @1000 — **published** |
| `22-58-14Z` | postgres `flat` @1, 200 warmup — **superseded**, off the documented recipe |
| `23-35-08Z` | postgres `flat` @1, 2000 warmup, 5 trials — **the published `@1` column** |
| `23-02-51Z`, `23-08-21Z`, `23-15-16Z`, `23-25-14Z` | sqlite `flat`/`join`/`wide` @1000 and `flat` @1 — **not published**, see below |

**The sqlite runs are a replication check, not a replacement.** They reproduce the
published 2026-08-26 sqlite table within 0.1–3.4% on every row — which is the useful
result — but they are dispersed where that run was tight (`wide`'s hand-rolled floor at
29.5% trial spread against 5.7%), so the tighter run stays published. A noisier
measurement of the same thing is not an update.

The superseded `@1` run is kept rather than deleted: it is the only evidence that the
warm-up length is what moved those cells (200 warmup put four floors and one compat row
at 17–18% spread; at 2000 only one row is above 5%).

Reproduce: `git checkout e28c2e0` and see the recipe in
[docs/METHODOLOGY.md](../docs/METHODOLOGY.md#reproducing), with `--pg-dsn` pointed at a
postgres 16 of your own.
