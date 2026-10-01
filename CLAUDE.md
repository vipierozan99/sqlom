# Goals

rowform is a row layer over SQLAlchemy Core for async services. Core compiles the
SQL and owns the schema, the pool and the transactions; rowform turns driver rows
into typed dataclasses and does nothing else.

In order:

1. **Nothing implicit.** No lazy loads, autoflush, expiry or identity map. Every
   round trip is a statement you wrote, so a request's query count is something a
   test can assert.
2. **Adopt one query at a time.** A rowform read runs inside a stock
   `AsyncSession`/`AsyncConnection` transaction, sees its uncommitted writes and
   rolls back with it (`tests/test_bind.py`). Nothing may make rowform a parallel
   universe with its own engine, pool or vocabulary.
3. **Values equal SQLAlchemy's.** Every column decodes through its own
   `result_processor`; the suite uses Core as the oracle over generated statements.
4. **Cost ≈ Core.** The benchmarks defend *not slower than Core's result layer,
   2–5x faster than the ORM*. They do not claim rowform is faster than Core: a
   plain dataclass constructor is as fast as the generated hydrator, and the
   result layer is a few percent of a read. Do not add code whose only
   justification is a row-layer speedup.

Where a goal conflicts with a lower-numbered one, the lower number wins and the
trade is stated, not made silently.

# Principles

## Think before coding
State assumptions. If several interpretations exist, present them. If a simpler
approach exists, say so. If something is unclear, stop and ask.

## Simplicity first
Minimum code that solves the problem. No speculative features, no abstractions for
single-use code, no error handling for impossible cases. Comments and docstrings
say *why*, not *what*, in one or two sentences; they never carry measurements or
history — those go in git and in `docs/BENCHMARKS.md`.

## Surgical changes
Touch only what the request requires. Match existing style. Remove only the dead
code your own change created; mention other dead code, don't delete it.

## Goal-driven execution
Turn a task into a verifiable check first — a failing test, a benchmark cell, a
type error — then make it pass.

# Conventions

- `import rowform as rf` everywhere: docs, tests, benchmarks, examples.
- Private SQLAlchemy API is read deliberately and listed in `pyproject.toml`
  next to the `<2.1` pin. A new coupling needs a test that pins it.
- Engine and transaction tests run on sqlite, asyncpg and psycopg from one
  parametrised fixture; `--pg-required` makes a missing server a failure.

# Workflow

Docs are written once, at the end. Code and tests land per commit; `README.md`,
`docs/*.md` and public-surface docstrings are updated in one pass when the PR is
opened, from a running list of what the change made stale.

# Commands

- `just lint --fix`, `just typecheck`, `just test <selector>`
- `just bench micro run` — dev loop; `benchmarks/README.md` has the publishing
  recipe. A result worth keeping goes on a `bench/<date>-<topic>` branch and into
  `docs/BENCHMARKS.md` with its commit sha.
