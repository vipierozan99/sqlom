# Contributing

```bash
git clone https://github.com/vipierozan99/sqlom && cd sqlom
uv sync --all-extras
just test          # sqlite + PostgreSQL, plus the type checker
just lint
just typecheck
just cov           # branch coverage; CI gates at 90%
```

`just` recipes are the interface. `just bench --help` lists the benchmark CLI.

## What CI checks

Four jobs, all of which you can run locally:

| | |
|---|---|
| `just lint` / `just typecheck` | ruff, then basedpyright over `rowform`, `benchmarks` and `tests/typing` |
| `just test . --pg-required` | the suite on Python 3.11, 3.12, 3.13 and 3.14 |
| `just cov --cov-fail-under=90` | branch coverage |
| `uv build` + wheel install | that the artifact still ships `py.typed` |
| `uv sync --resolution lowest-direct` + tests | that the dependency floors in pyproject are real |

Two more workflows run outside the PR: one compares the micro benchmarks against
the merge base and fails a PR more than 1.25x slower (see below before arguing
with it), and a weekly canary runs the suite against SQLAlchemy's **main** branch.

That canary is worth understanding before changing the row path. rowform reads a
dozen SQLAlchemy internals on purpose — `_cached_result_processor`,
`construct_params`, `_generate_cache_key` and friends — which is the design
working, and also a standing bet that they do not move. The canary is how that
bet is watched, and it is deliberately allowed to fail loudly: a scheduled
workflow going red notifies the maintainer, which is the entire mechanism.

## The test suite

Engine and transaction tests run against **both** sqlite and PostgreSQL from one
parametrised fixture, because the two differ exactly where this design is most
exposed: sqlite hands back strings for temporal types and integers for booleans,
postgres does not. A behaviour asserted on only one of them has not been tested.

PostgreSQL tests skip with a reason when no server is reachable. `--pg-required`
turns that skip into a failure — CI uses it, and so should you before opening a
PR. `ROWFORM_TEST_DSN` points them elsewhere; the default expects
`postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench`.

Types are tested, not just declared. `tests/typing/positive.py` asserts exact
inference with `typing.assert_type`, `tests/typing/negative.py` carries a
`# pyright: ignore` on every line that must fail, and the checker runs with
`reportUnnecessaryTypeIgnoreComment` so a suppression that stops being needed
fails the build. If you change a signature, change these.

`tests/test_property_hydration.py` generates the statement — which columns, in
what order, at what arity — and compares against SQLAlchemy Core reading the same
rows. If you touch `planner.py` or `compile.py`, that file is the one that decides
whether you got it right.

## Benchmarks

Read [docs/METHODOLOGY.md](docs/METHODOLOGY.md) before quoting a number, and
especially before adding one. It carries a log of thirteen published claims that
turned out to be wrong, with how each was caught; most of them are mistakes that
are easy to repeat.

The short version:

* **Absolutes are machine-specific.** A number from a CI runner or a laptop under
  load is a trend signal, not a baseline.
* **Every contender must run identical SQL**, so what is compared is the row layer
  and not two different queries.
* **Three floors, always** — one bounding the whole stack, one running the same
  hydrator over the same driver, and one on SQLAlchemy's own pool and transaction —
  so engine cost, row cost and plumbing cost stay separable. Leaving out the third
  is how a pool-and-transaction gap once got published as row-layer speed.
* **Every contender reads inside `BEGIN`…`COMMIT`.** SQLAlchemy autobegins and
  rowform's engine-level `fetch_all()` does not, so a suite that leaves this to
  each contender is comparing isolation guarantees and calling it throughput.
  The one exception is named for it — `rowform (no transaction)` — because the
  cheaper weaker read is worth pricing, just not worth publishing as the headline.
* **`just bench micro run --record`** writes a `run.json`. Commit chosen artifacts
  to a dated branch and note the run in [docs/RUNS.md](docs/RUNS.md), so a number
  can always be traced back to a commit that reproduces it.

If the performance gate fails and you believe the regression is real but
justified, say so in the PR with the numbers — the gate is a question, not a veto.

## Pull requests

* One concern per PR.
* Match the surrounding style, including comment density. Comments here explain
  *why*, and often name the alternative that was rejected; that convention is
  load-bearing in `compile.py` and `planner.py`.
* Say what you verified, not just what you changed.
* If you found a real bug, add the test that fails without your fix.

## Scope

This is a read path, not an ORM. Relationships, lazy loading, an identity map and
a unit of work are all deliberately absent — see
[What this costs](README.md#-what-this-costs) — and a PR adding one of them is a
much larger conversation than a PR. Open an issue first.
