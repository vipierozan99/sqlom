# The benchmark suite

Three commands. Numbers and how to read them: [docs/BENCHMARKS.md](../docs/BENCHMARKS.md).

| command | measures | cases |
|---|---|---|
| `just bench micro run` | latency of one read, in-process, per contender | `micro/contenders.py` |
| `just bench micro memory` | peak allocation of one read, per contender | `micro/contenders.py` |
| `just bench load run` | HTTP throughput under concurrency, via locust | `service/app.py` routes |

Every case has one slug, `{backend}-{shape}-{name}`: `just bench contenders list`
and `just bench load cases` print them. A load case is a `service/app.py` route
whose path is a contender slug; `tests/test_bench_cases.py` pins that.

## Dev loop

```bash
just bench micro run --shape flat                 # sqlite, one trial, not quotable
just bench micro run --shape flat --backend mock  # rowform's row layer alone, canned rows
```

## Publishing a number

```bash
sudo scripts/bench_cpu_boost.sh off   # turbo off; the gate refuses otherwise
just bench env check                  # boost, loadavg, gevent patch, clean tree
for shape in flat join wide; do
  just bench micro run --shape "$shape" --iterations 1500 --warmup 200 --trials 3 --isolate --record
done
just bench db up                      # postgres container for the postgres cells
just bench micro run --backend postgres --shape flat --iterations 1500 --warmup 200 \
  --trials 3 --isolate --record --pg-dsn "$(just bench db dsn)"
just bench db down
sudo scripts/bench_cpu_boost.sh on
```

`--isolate` runs one contender per process; `--trials` is what makes a ratio a
range rather than a point. Runs land in `benchmarks/results/runs/` (gitignored);
a run worth keeping is committed to a `bench/<date>-<topic>` branch and cited in
`docs/BENCHMARKS.md` by sha.

Every contender's JSON is byte-compared before timing starts (`harness/equivalence.py`),
and the postgres cells additionally pin transaction parity on the wire
(`tests/test_bench_wire_parity.py`).
