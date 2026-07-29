# Recorded runs

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


