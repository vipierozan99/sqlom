# SQLAlchemy compatibility

Status: **proposal**. Nothing here is implemented. §2 is measured; everything
after it is analysis.

## 0. The goal

**A SQLAlchemy application should be able to adopt rowform one query at a time,
without giving up its engine, its sessions, its transactions, or its
migrations.**

That is a stronger goal than "our API looks like theirs", and it rules out things
a resemblance goal would allow. Today rowform owns its own pool, so a service
using `AsyncSession` cannot read through rowform *inside its own transaction* at
all — adoption is all-or-nothing at the process level. The goal above makes
adoption granular at the statement level, and it is testable: a suite arm that
runs rowform reads inside a stock `AsyncSession` transaction either passes or it
does not.

---

## 1. Two ways to get there

**Option A — keep rowform's pool, rename everything after SQLAlchemy.**
`create_async_engine`, `engine.connect()`/`begin()`, `Connection.execute()`
returning a `Result`, `Transaction` as a commit/rollback handle. Familiar, but a
parallel universe: rowform's engine is still not SQLAlchemy's engine, so it
cannot share a transaction with one, and every knob (pre-ping, recycle, events,
`echo`, URL handling) has to be re-implemented or refused.

**Option B — don't have an engine. Take SQLAlchemy's.** rowform becomes a row
layer over a connection somebody else owns:

```python
engine = create_async_engine("postgresql+asyncpg://localhost/app")   # theirs

async with engine.connect() as conn:
    users = await rf.fetch_all(conn, sa.select(User).where(User.id > 100))
```

Option B satisfies §0 by construction, and the API question mostly dissolves: the
engine, the transaction, the pool and the URL are SQLAlchemy's own objects, so
they are not *like* SQLAlchemy's, they *are*. **Recommended.** §2 is what it
costs; §5 is what is still open.

The mechanism is one documented call. `AsyncConnection.get_raw_connection()`
returns the pool's proxy, and `.driver_connection` on it is the real
`asyncpg.Connection` / `aiosqlite.Connection` / `psycopg.AsyncConnection` —
exactly the object rowform's `_fetch`/`_execute`/`_stream` hooks already take:

```python
driver = (await conn.get_raw_connection()).driver_connection
rows = await driver.fetch(sql, *params)          # no greenlet, no CursorResult
```

Both of those are public, documented API — a *lower*-risk coupling than the
compiler internals rowform already depends on (`_generate_cache_key`,
`_limit_clause`, `_cached_bind_processor`).

---

## 2. What was measured

Ablation in the scratchpad, not committed; re-derivable from this description.
flat shape, 1000 rows out of 200,000, sqlite/aiosqlite, 300 iterations × 3
trials, GC off, medians, byte-identical payload asserted across every arm before
timing. Every arm runs the same compiled SQL through the same rowform hydrator
and the same serializer; only the connection's provenance varies.

**This box is a shared cloud container with no core pinning** — absolute numbers
are ~1.9x the published table's. Ratios are the comparable quantity
(`docs/METHODOLOGY.md`).

| arm | median ms | vs rowform |
|---|---|---|
| rowform pool / per request | 1.9944 | 1.00x |
| rowform pool / hoisted connection | 1.9007 | 0.95x |
| **SQLAlchemy pool / per request** | **2.3117** | **1.16x** |
| **SQLAlchemy pool / hoisted connection** | **1.9091** | **0.96x** |
| SQLAlchemy DBAPI cursor via `run_sync` / per request | 2.4808 | 1.24x |

### 2a. Per statement, wrapping is free

1.9007 against 1.9091 hoisted — **+0.4%, inside the ±3% trial spread**. Once the
connection is in hand, rowform executing on `driver_connection` costs what
rowform executing on its own pooled connection costs, because it is the same
driver call on the same kind of object. No greenlet is involved: the statement is
awaited from a real coroutine, never through SQLAlchemy's `await_only` shim.

### 2b. Per checkout, it costs 3–4x

Subtracting the hoisted arm from the per-request one: rowform's checkout ≈
**0.094 ms**, SQLAlchemy's ≈ **0.403 ms**. That is the same finding
`PLAN_CORE_COMPILER.md §2g` already recorded from the other direction (~0.18 ms
against ~0.03–0.08 ms on a faster box), arrived at independently.

A second run decomposed it, and checked the obvious misattribution first — a
`NullPool` would have made "checkout" mean "open a new sqlite file", and every
conclusion wrong:

```
pool class:      AsyncAdaptedQueuePool
distinct driver connections over 5 checkouts: 1
```

| arm | median ms | vs rowform |
|---|---|---|
| rowform pool / per request | 1.8517 | 1.00x |
| SQLAlchemy / default | 2.2228 | 1.20x |
| SQLAlchemy / `pool_reset_on_return=None` | 2.1143 | 1.14x |

So ~0.11 ms of the ~0.37 ms is the rollback the pool issues on release, and is
tunable. The remaining ~0.26 ms is `greenlet_spawn` + `_ConnectionFairy` +
`AsyncConnection` construction, and is not.

### 2c. Going through SQLAlchemy's *execution* path is not free

The `run_sync` arm — SQLAlchemy's DBAPI-shim cursor driven inside greenlet, with
rowform hydrating its raw rows — costs **+0.17 ms per statement** over executing
on the driver connection directly (2.4808 vs 2.3117). It is also the only arm
whose cost scales with statements rather than with requests. **Reach the driver
connection; do not go through the adapter.**

### 2d. Transaction sharing works, and was verified

Not asserted — run. Against `sqlite+aiosqlite`:

* a rowform read on `driver_connection` inside `async with conn.begin():` **sees
  the transaction's uncommitted INSERT**;
* a write rowform issues on that connection **is rolled back** when SQLAlchemy
  rolls the transaction back;
* the same holds through the ORM, via `await session.connection()` inside
  `async with session.begin():`.

This is §0's goal, demonstrated on one backend. Postgres is unverified here (no
server on this box) and it is the arm that matters most — see §5.1.

### 2e. A correctness bonus, also verified

`rf.SqliteEngine.dialect` is a freshly constructed dialect that has never seen a
server: `server_version_info=None`, `default_schema_name=None`. The dialect on a
live `AsyncEngine` has been through `initialize()`: `server_version_info=(3, 45,
1)`, `default_schema_name='main'`. Compiling against the engine's dialect is
therefore strictly more faithful than what rowform does today, and on postgres —
where Core's output is version-dependent — that gap is not cosmetic.

### 2f. Not measured, deliberately

A stock-Core reference arm was in the first run and is excluded here: it
serialized `Row` objects with a different code path than the dataclass arms, so
its number is not comparable to the others or to the published table. The
published comparison already covers that contender properly.

---

## 3. What Option B looks like

```python
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import rowform as rf

engine = create_async_engine("postgresql+asyncpg://localhost/app", pool_size=10)
Session = async_sessionmaker(engine)

# a plain read
async with engine.connect() as conn:
    users = await rf.fetch_all(conn, sa.select(User).where(User.name.like("a%")))

# inside somebody else's transaction — the point of the whole exercise
async with Session() as session, session.begin():
    session.add(OrmAuditRow(...))                       # their ORM, their write
    hot = await rf.fetch_all(session, sa.select(User))  # our rows, their transaction

# streaming, still a real server-side cursor
async with engine.connect() as conn:
    async for user in rf.fetch_iter(conn, sa.select(User), chunk=500):
        ...
```

`fetch_all` accepts an `AsyncConnection`, an `AsyncSession` (via
`await session.connection()`), or a raw driver connection. Which driver it is
comes from `conn.engine.dialect.driver` — a dict lookup replacing today's
three engine classes.

### What survives, unchanged

`prepare()`/`CoreQuery`, the compiled-statement cache (now keyed per engine in a
`WeakKeyDictionary` rather than held on one), `copy_in()`, `pipeline()`,
`fetch_iter()`, the `observer`, and the whole row layer. All of them take or
produce a driver connection already.

### What gets deleted

| gone | why |
|---|---|
| `_open_pool` / `_close_pool` / `_acquire` / `_SqlitePool` | SQLAlchemy's pool |
| `connect()` / `close()` / `EngineStateError` | no engine to be unconnected |
| `pool_stats()` / `PoolStats` | `engine.pool.status()`, `.checkedout()` |
| `conditional_reset`, `_dirty`, `reset_count` | see §4 |
| `_configure_connection` (asyncpg JSON codecs) | the dialect's own `on_connect` runs when SQLAlchemy owns the connect |
| `_ddl` / `create_all` / `drop_all` / `create_mock_engine` | `conn.run_sync(metadata.create_all)` |
| `_block` and its three driver implementations | `conn.begin()` / `begin_nested()` |
| `_ACTIVE` contextvar, `_reject_if_in_transaction` | you cannot call a connection you were not handed |
| `PsycopgEngine.transaction`'s isolation save/restore | SQLAlchemy's `execution_options` |

That is most of `engine.py`, all of `transaction.py`, and the pool halves of the
three driver modules — roughly the 935 lines `PLAN_CORE_COMPILER.md §1` counts as
"kept for raw-driver execution and pooling", minus the execution part.

---

## 4. The asyncpg reset question, reconsidered

`AsyncpgEngine.conditional_reset` exists because **asyncpg's own pool** runs
`RESET ALL` as a round trip on every release, worth 20–30% of throughput
(`BENCHMARKS.md §6`). SQLAlchemy does not use asyncpg's pool — it pools raw
`asyncpg.connect()` connections in its own `AsyncAdaptedQueuePool` and resets
them with a DBAPI-level `rollback()`, which for the asyncpg adapter is a local
no-op when no transaction was started.

So the mechanism `conditional_reset` works around **does not exist** under Option
B: it is retired rather than lost, and the analogous knob is
`pool_reset_on_return`, measured in §2b at ~0.11 ms on sqlite. This is reasoning
from how the dialect is built, not a measurement — postgres was not available on
this box. It is §5.1's first item.

---

## 5. Open questions and sharp edges

1. **Everything in §2 is sqlite.** The per-checkout tax, the transaction sharing,
   and §4's reset claim all need the same ablation against
   `postgresql+asyncpg` and `postgresql+psycopg` before any of it is published.
   Postgres is where the round trip dominates, which cuts both ways: the fixed
   ~0.3 ms checkout matters proportionally less, and any *extra round trip*
   matters far more.

2. **Autocommit semantics diverge across drivers when there is no explicit
   transaction.** Executing on `driver_connection` outside a SQLAlchemy
   transaction means the adapter never starts its emulated one. On asyncpg that
   is autocommit — a write commits. On psycopg the driver connection is
   transactional in its own right, so the same write would be rolled back by the
   pool's reset on release. Reads are unaffected either way. This needs pinning
   down and probably needs rowform to refuse writes outside an explicit
   transaction rather than behave differently per driver.

3. **The per-request tax is real and the benchmark story must say so.** The
   published "1.2–1.6x SQLAlchemy Core" figure is a per-request-acquire
   comparison on *rowform's* pool. On SQLAlchemy's pool the same comparison
   narrows, because both sides now pay the same checkout. The honest fix is to
   report both connection sources rather than to quietly keep quoting the
   favourable one.

4. **Which suggests keeping rowform's pool as an option, not a default.** If the
   row API is `rf.fetch_all(conn, stmt)`, then rowform's pool does not need an
   engine class at all — only something whose `acquire()` yields a driver
   connection. One row API, three connection sources (SQLAlchemy connection,
   SQLAlchemy session, rowform pool). That is not the "two vocabularies" problem,
   because there is only one vocabulary; it is one extra provider behind it.
   Whether the ~0.3 ms is worth carrying a pool for is a decision to make *after*
   §5.1's postgres numbers exist.

5. **Mixing rowform's execution with psycopg pipeline mode** on a connection
   SQLAlchemy also uses is untested, and is the most likely place for the two to
   confuse each other.

6. **Exception wrapping.** Statements rowform runs raise the driver's exception,
   not `sa.exc.IntegrityError` — a visible seam in an otherwise seamless story,
   and now more visible because the surrounding code is SQLAlchemy's. Out of
   scope for the first pass; tracked separately.

---

## 6. The API surface, either way

Independent of A vs B, these names should be SQLAlchemy's:

| SQLAlchemy 2.0 async | rowform today | proposed |
|---|---|---|
| `await conn.execute(stmt, params) -> Result` | `fetch_all` / `execute` split | `execute()` → `Result` (keep `fetch_all` as the shorthand) |
| `result.all() / .first() / .one() / .one_or_none()` | `list`, `rows[0] if rows` | `Result` |
| `result.scalars() / .scalar_one()` | `fetch_value()` | `Result`, `ScalarResult` |
| `await conn.execute(stmt, [d1, d2])` | `execute_many(stmt, seq)` | a list of dicts |
| `await conn.stream(stmt)` → `AsyncResult` | `fetch_iter(stmt, chunk=…)` | `stream()`, `AsyncResult.partitions(n)` |
| `sa.exc.NoResultFound` / `MultipleResultsFound` | — | raise the real ones (free; sa is already a dependency) |

One row-shape decision remains open regardless of A or B: for a *single* selected
entity SQLAlchemy hands back a 1-tuple (`Row(User,)`) that `.scalars()` unwraps,
where rowform yields the `User` directly. Multi-entity rows already match, since
`Row` is a tuple. Options: match exactly and pay a tuple per row (~2–3% of the
flat benchmark), or keep rowform's unwrapping and implement `.scalars()` /
`.scalar_one()` faithfully so SQLAlchemy-shaped code still runs — the divergence
then only bites code that indexes a 1-tuple.

---

## 7. Phasing

1. **Port §2's ablation to postgres, both drivers.** → verify: §5.1 and §5.2
   answered with numbers, `conditional_reset`'s fate decided.
2. **`rf.fetch_all(conn, stmt)` over an `AsyncConnection`**, driver dispatched
   from the dialect, statement cache keyed per engine. Additive; the existing
   engines keep working. → verify: a new suite arm running every existing read
   test inside a stock `AsyncSession` transaction.
3. **`Result` / `ScalarResult` / `AsyncResult`** and the §6 renames.
4. **Retire the engine's pool half** (§3's deletion table), or demote it to an
   opt-in provider per §5.4.
5. **Benchmarks and docs**: report both connection sources; restate the headline
   claim honestly (§5.3).
