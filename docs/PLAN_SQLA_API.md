# Aligning the engine and transaction API with SQLAlchemy

Status: **proposal**. Nothing here is implemented.

rowform already borrows SQLAlchemy's *declaration* and *statement* surface
verbatim — `Mapped[]`, `mapped_column()`, `sa.select()`, `MetaData`, Alembic. The
execution surface is the one place it invented its own vocabulary:
`engine.fetch_all()`, `engine.transaction()`, `tx.execute()`, `fetch_iter()`. A
reader who knows SQLAlchemy has to learn it, and code written against
`AsyncEngine` cannot be moved over without a rewrite.

This is what closing that gap looks like, what it costs, and where "exactly the
same" is not reachable.

---

## 1. The three things that cannot be identical

Stating these first, because everything below is shaped by them.

**1a. A row is not a `Row`.** `conn.execute(select(user_table)).all()` gives
`[Row(id, name, email), …]` in Core and `[Row(User,), …]` in the ORM. rowform
gives `[User, …]` — no `Row`, which is the entire point of the library. The
divergence is narrower than it sounds, though, and can be stated as one rule:

* multi-entity rows already match — rowform yields a plain tuple, and `Row` *is* a
  tuple, so `select(User, Post)` and `select(User.name, User.id)` behave the same;
* single-entity rows differ — rowform unwraps, SQLAlchemy wraps in a 1-tuple that
  you unwrap with `.scalars()`.

So the only real question is what `.all()` does for a single selected entity
(decision **D2**).

**1b. There is no sync `Connection`.** `conn.run_sync(Base.metadata.create_all)`
— the canonical DDL idiom — needs a real `sqlalchemy.Connection` to hand the
callable. rowform holds a driver connection and a dialect, and never builds one.
`run_sync` stays unimplemented, raising `UnsupportedError` with that reason;
`engine.create_all(metadata)` remains the way (it already runs SQLAlchemy's own
`SchemaGenerator` through `create_mock_engine`, so the DDL is identical).

**1c. `engine.connect()` means something else today.** In rowform it opens the
pool and returns it; in SQLAlchemy it checks out a connection. This is the one
name that *must* change meaning, and it is load-bearing in 35 places. See **D4**.

---

## 2. What it would look like

```python
import sqlalchemy as sa
import rowform as rf

engine = rf.create_async_engine("postgresql+asyncpg://localhost/app", pool_size=10)

# commit-as-you-go
async with engine.connect() as conn:
    result = await conn.execute(sa.select(User).where(User.name.like("a%")))
    users = result.scalars().all()                      # list[User]

# begin-once
async with engine.begin() as conn:
    await conn.execute(sa.insert(User.__table__), [{"name": "ada"}, {"name": "bo"}])
    async with conn.begin_nested() as sp:               # SAVEPOINT
        await conn.execute(sa.update(Account.__table__).values(...))
        await sp.rollback()

# streaming
async with engine.connect() as conn:
    result = await conn.stream(sa.select(User))
    async for user in result.scalars():
        await sink.write(user)

await engine.dispose()
```

Every line of that is valid SQLAlchemy 2.0 async, unchanged, except the engine
constructor.

### Mapping

| SQLAlchemy 2.0 async | rowform today | proposed |
|---|---|---|
| `create_async_engine(url, **kw)` | `SqliteEngine(path)`, `AsyncpgEngine(dsn)` | `rf.create_async_engine(url, **kw)`, dispatching on the URL; the classes stay |
| pool opens on first checkout | `await engine.connect()` | lazy open (**D4**) |
| `await engine.dispose()` | `await engine.close()` | `dispose()`; `close()` kept as an alias |
| `async with engine.connect() as conn:` | — | `Connection` |
| `async with engine.begin() as conn:` | `async with engine.transaction() as tx:` | `begin()`, yielding a `Connection` |
| `async with conn.begin() as tx:` | `tx.transaction()` | a real `Transaction` (commit/rollback only) |
| `conn.begin_nested()` | `tx.transaction()` at depth ≥ 1 | `begin_nested()` |
| `await conn.commit()` / `.rollback()` | — (block exit only) | explicit, on `Connection` and `Transaction` |
| `await conn.execute(stmt, params) -> Result` | `fetch_all` / `execute`, split by whether rows come back | one `execute()` → `Result` |
| `result.all() / .first() / .one() / .one_or_none()` | `list`, `rows[0] if rows` | `Result` |
| `result.scalars() / .scalar() / .scalar_one()` | `fetch_value()` | `Result`, `ScalarResult` |
| `result.rowcount` | `execute()`'s return value | `Result.rowcount` |
| `await conn.execute(stmt, [d1, d2])` | `execute_many(stmt, seq)` | a list of dicts to `execute()` |
| `await conn.stream(stmt)` → `AsyncResult` | `fetch_iter(stmt, chunk=…)` | `stream()`, `stream_scalars()`, `AsyncResult.partitions(n)` |
| `await conn.exec_driver_sql(sql, params)` | `tx.execute("SQL string")` | `exec_driver_sql()` |
| `conn.run_sync(metadata.create_all)` | `engine.create_all(metadata)` | unsupported (§1b); `create_all` stays |
| `conn.execution_options(isolation_level=…)` | `transaction(isolation=…, readonly=…)` | `execution_options()` |
| `conn.in_transaction()`, `.in_nested_transaction()`, `.closed` | `tx.depth` | added; `depth` stays as an extension |
| `conn.get_raw_connection()` | `engine.acquire()`, `tx.connection` | `conn.raw_connection`; `acquire()` stays |
| `engine.pool.status()` | `engine.pool_stats()` | extension, unchanged |
| `event.listen(engine.sync_engine, "before_cursor_execute", …)` | `engine.observer` | extension, unchanged |
| `sa.exc.IntegrityError` etc. | the driver's own exception | **D6** |

### Extensions that survive unchanged

`prepare()`/`CoreQuery`, `copy_in()`, `pipeline()`, `pool_stats()`, `observer`,
`cached_statements`, `conditional_reset`, and — see **D1** — the `engine.fetch_*`
shorthands. None of these collide with a SQLAlchemy name, and each does something
SQLAlchemy has no equivalent for.

---

## 3. Structure

Today `Transaction` is two SQLAlchemy objects fused: it executes statements
(`AsyncConnection`) *and* it is the unit of commit (`AsyncTransaction`). Splitting
them is the bulk of the work.

```
rowform/engine.py       Engine        pool, dialect, compiled-statement cache,
                                      connect() / begin() / dispose()
rowform/connection.py   Connection    execute / stream / exec_driver_sql /
                                      begin / begin_nested / commit / rollback
                        Transaction   commit / rollback / close / is_active / is_nested
rowform/result.py       Result        all / first / one / one_or_none / scalar /
                                      scalars / partitions / rowcount / keys
                        ScalarResult
                        AsyncResult   the streaming counterpart
```

`Result` is a wrapper over the list the hydrator already produced — one object per
`execute()`, not one per row. It can hydrate **lazily**, which is a small win the
current API cannot get: `result.first()` would hydrate row 0 and drop the rest,
where `fetch_all()[0]` hydrates everything. (It does not recover the `LIMIT 1`
that `fetch_one()` adds — see **D3**.)

### Per-driver hooks

`_block(conn, depth, kwargs)` — a context manager wrapping each driver's own
transaction object — cannot express `await conn.commit()`. It gets replaced by
four verbs, emitted as SQL uniformly on all three drivers:

```python
async def _begin(self, conn, options) -> None      # BEGIN [ISOLATION LEVEL …]
async def _commit(self, conn) -> None
async def _rollback(self, conn) -> None
async def _savepoint(self, conn, name, action) -> None   # SAVEPOINT / RELEASE / ROLLBACK TO
```

This is *simpler* than what is there now: `SqliteEngine` already emits the SQL by
hand, and the asyncpg/psycopg paths stop needing three different context-manager
shapes. It also removes the psycopg special case in `PsycopgEngine.transaction()`
that saves and restores connection-level `isolation_level`/`read_only`/
`deferrable` — with `BEGIN ISOLATION LEVEL … READ ONLY` those never touch the
connection, so nothing has to be restored before it goes back to the pool.

### Typing

The per-arity overloads move from `fetch_all` to `execute`, and get *better*: the
row type lands on `Result[R]` and flows through `.all() -> list[R]`,
`.first() -> R | None`, `.one() -> R`, `.scalars() -> ScalarResult[R]`. The rule
that decides `R` is unchanged (`planner.py`: one entity unwraps, two or more
wrap), so `tests/typing/` needs its expectations moved, not rewritten.

---

## 4. Decisions

Each has a recommendation; none is settled.

### D1 — Do the `engine.fetch_*` shorthands survive?

SQLAlchemy deleted connectionless execution in 1.4 on purpose. Exact alignment
means `engine.fetch_all()` goes away and every read becomes
`async with engine.connect() as conn: …`.

Against deleting: the one-shot read is rowform's headline path and its benchmark;
`engine.fetch_all(stmt)` is one checkout, and so is the `async with`, so the cost
is purely syntactic. It is also ~250 call sites in tests, docs and benchmarks.

**Recommended:** keep them, documented as shorthands with an exact definition —
`engine.fetch_all(s, **p)` ≡ `connect() → execute(s, p) → .all()` in autocommit
(see **D5**) — and keep the `_ACTIVE` contextvar guard that refuses them inside a
block. SQLAlchemy avoids that whole bug class structurally; rowform would still
need the guard for as long as the shorthands exist.

### D2 — What does `.all()` return for a single selected entity?

* **(a) Exact.** `.all()` → `[(User,), …]`, `.scalars().all()` → `[User, …]`.
  Costs one tuple per row on the hottest shape (~2–3% of the flat micro
  benchmark) and makes the common case wordier — but it is what "exactly the
  same" means, and `for row in result: row[0]` then works.
* **(b) rowform's rule everywhere.** `.all()` → `[User, …]`; `.scalars()` exists
  as a no-op for that case. Zero cost, but code copied from SQLAlchemy that
  indexes rows breaks.

**Recommended: (b), with every scalar accessor implemented faithfully.** Under
(b), SQLAlchemy-shaped code that uses `.scalars().all()` / `.scalar_one()` — which
is how essentially all ORM-style code is written — runs unchanged, and the
divergence is confined to code that indexes a 1-tuple. Document it as the single
stated exception, next to `Mapped[]`'s.

`.one()` / `.one_or_none()` / `.scalar_one()` raise the real
`sqlalchemy.exc.NoResultFound` and `MultipleResultsFound` either way — SQLAlchemy
is already a hard dependency, so that alignment is free.

`.mappings()` has no meaning for an entity row and would raise `UnsupportedError`;
for an all-scalar select it is implementable from `plan.columns`.

### D3 — `fetch_one()`'s implicit `LIMIT 1`

`engine.fetch_one()` narrows a `Select` to one row (`engine._one_row`), because it
knows the statement. `conn.execute(stmt).first()` cannot: by then the rows are
fetched. SQLAlchemy has exactly this behaviour and does not add a LIMIT either.

**Recommended:** keep `fetch_one`/`fetch_value` as extensions that do add it, and
say in one line that `execute(…).first()` does not — lazy hydration means it still
only *hydrates* one row, but it transfers the whole result.

### D4 — Lazy pool, and the `connect()` collision

`engine.connect()` must become "check out a connection". The pool then has to open
on first use, as SQLAlchemy's does. That deletes `EngineStateError("engine is not
connected")`, and with it the ability to fail at boot rather than at first
request.

**Recommended:** lazy open behind a lock, plus an explicit extension
(`await engine.open()`, or reuse `async with engine:`) for services that want the
pool up and validated at startup. Ordering matters for migration: `connect()`
changing meaning silently is the one break that would compile and then behave
differently, so it should land as `connect()` **raising** with a pointer for one
release before it is repurposed.

### D5 — Autobegin

SQLAlchemy autobegins on the first `execute()` and requires an explicit
`commit()`; a block that only reads emits `BEGIN … COMMIT`. `PsycopgEngine`
already pays that (its docstring says so, deliberately). `AsyncpgEngine` does not
— `pool.acquire()` is autocommit — so faithful autobegin would add two round trips
to every read on the fastest backend.

**Recommended:** implement autobegin faithfully on `Connection`, support
`isolation_level="AUTOCOMMIT"` as the standard escape hatch (SQLAlchemy's own
answer to the same cost), and define the `engine.fetch_*` shorthands as autocommit
so the benchmark path is unchanged and honestly labelled.

### D6 — Exception wrapping

SQLAlchemy raises `sa.exc.IntegrityError` and friends; rowform passes the driver's
exception through, on purpose. `except sa.exc.IntegrityError` is common enough in
ported code to matter.

**Recommended: out of scope for this change**, tracked separately. Doing it right
means the dialect's `dbapi_exception_translation` and `is_disconnect` on every
statement path, which is a different piece of work with its own cost to measure.

### D7 — Pool keyword translation

SQLAlchemy sizes pools with `pool_size` / `max_overflow` / `pool_timeout` /
`pool_recycle` / `pool_pre_ping`; asyncpg and psycopg use `min_size` / `max_size`.

**Recommended:** `create_async_engine` translates the SQLAlchemy names
(`pool_size` → `min_size`, `pool_size + max_overflow` → `max_size`, `pool_timeout`
→ each pool's timeout) and passes anything else through untouched, so
driver-native names keep working. `pool_recycle`/`pool_pre_ping` raise rather than
silently no-op where the driver's pool has no equivalent — same policy as
`SqliteEngine`'s refusal of `isolation`.

---

## 5. Phasing

Each phase is independently shippable and independently verifiable.

1. **`Result` + `Connection.execute()`, additive.** Add `result.py` and give the
   existing `Transaction` an `execute()` that returns a `Result`, alongside
   today's methods. → verify: new tests for every `Result` accessor on both
   backends; existing suite untouched and green.
2. **Split `Connection` from `Transaction`; `engine.connect()` / `begin()`.**
   Replace `_block` with the four verbs per driver. → verify: `test_transactions.py`
   rewritten against the new shape, plus new tests for explicit
   `commit()`/`rollback()`, `begin_nested()`, and autobegin-without-commit
   discarding writes on all three drivers.
3. **`create_async_engine` + URL dispatch + pool-kwarg translation.** → verify: an
   engine built from each of the three URL forms is the right class with the right
   pool sizes; unknown/unhonourable kwargs raise.
4. **`stream()` / `AsyncResult` / `exec_driver_sql` / `execution_options`.** →
   verify: `test_streaming.py` ported; `partitions()` yields the same rows in the
   same chunks as `fetch_iter`.
5. **Sweep.** ~600 call sites across `tests/`, `docs/`, `benchmarks/`. → verify:
   full suite plus `just lint`/`just typecheck` green, and a micro benchmark run
   compared against the pre-change commit to show the row path did not move.

Phase 5 is where the decision on old names has to be final: aliases forever, one
release of deprecation warnings, or a clean break. Given "not packaged, not on
PyPI, never run in production", **a clean break is the honest option** — the
aliases would otherwise be permanent, and permanent aliases are how a library ends
up with two vocabularies, which is the thing this change exists to remove.
