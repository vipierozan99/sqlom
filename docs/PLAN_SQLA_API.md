# SQLAlchemy compatibility

Status: **implemented**. rowform no longer pools — `rf.Engine` wraps an
`AsyncEngine` (§2 is the measurement that decided it) — and the connection carries
both a SQLAlchemy-shaped track and rowform's own (§6). §8 is what building it
turned up, including two silent-wrongness bugs and one retracted claim.

What is left: §5.1, the whole postgres arm, unrun for want of a server; and §5.3,
the benchmark table, which predates the pool change and has not been re-measured.

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
sa_engine = create_async_engine("postgresql+asyncpg://localhost/app")   # theirs
db = rf.Engine(sa_engine)                                               # ours

users = await db.fetch_all(sa.select(User).where(User.id > 100))
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
the earlier core-compiler work recorded from the other direction (~0.18 ms
against ~0.03–0.08 ms on a faster box), arrived at independently. That design note
has since been removed from the tree; the figure is repeated here because this is
now the only place it survives.

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

`rf.SqliteEngine.dialect` (as it was) is a freshly constructed dialect that has
never seen a server: `server_version_info=None`, `default_schema_name=None`. The dialect on a
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

sa_engine = create_async_engine("postgresql+asyncpg://localhost/app", pool_size=10)
db = rf.Engine(sa_engine)
Session = async_sessionmaker(sa_engine)

# a plain read
users = await db.fetch_all(sa.select(User).where(User.name.like("a%")))

# inside somebody else's transaction — the point of the whole exercise
async with Session() as session, session.begin():
    session.add(OrmAuditRow(...))                       # their ORM, their write
    hot = await (await db.using(session)).fetch_all(sa.select(User))

# streaming, still a real server-side cursor
async for user in db.fetch_iter(sa.select(User), chunk=500):
    ...
```

`using()` accepts an `AsyncConnection` or an `AsyncSession` (via
`await session.connection()`). Which driver is in play comes from
`dialect.driver` — a dict lookup replacing the three engine classes.

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
three driver modules — roughly the 935 lines the earlier design note counted as
"kept for raw-driver execution and pooling", minus the execution part.

---

## 4. The asyncpg reset question, reconsidered

`AsyncpgEngine.conditional_reset` exists because **asyncpg's own pool** runs
`RESET ALL` as a round trip on every release, worth 20–30% of throughput
(measured at the time, in a benchmark note since removed). SQLAlchemy does not
use asyncpg's pool — it pools raw
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

## 6. Two tracks, not one compromise

The row-shape question — does `.all()` hand back `[User]` or `[Row(User,)]`? — had
no good answer while there was one method. Matching SQLAlchemy exactly cost a tuple
per row on the hottest path; keeping rowform's unwrapping left a divergence that
bit silently on `select(User.name)`, where `row[0]` returns the first *character* of
a string rather than failing.

The answer was to stop choosing. `execute()` is SQLAlchemy's and pays for it;
`fetch_all()` is rowform's and does not. The tracks are told apart by name, so no
statement means one thing under one spelling and another under the other:

| | compatibility track | hot track |
|---|---|---|
| all rows | `(await conn.execute(s)).scalars().all()` | `await conn.fetch_all(s)` |
| first row | `(await conn.execute(s)).scalar_one_or_none()` | `await conn.fetch_one(s)` |
| one value | `await conn.scalar(s)` | `await conn.fetch_value(s)` |
| stream | `await conn.stream(s)` → `AsyncResult` | `conn.fetch_iter(s, chunk=…)` |
| executemany | `await conn.execute(s, [d1, d2])` | `await conn.execute_many(s, rows)` |
| raw SQL | `await conn.exec_driver_sql(sql, params)` | — |

The compatibility track is not an imitation: rowform hydrates, then hands the rows
to SQLAlchemy's own `IteratorResult`, so `.scalars()`, `.mappings()`, `.tuples()`,
`.unique()`, `.partitions()`, `Row` attribute access, `NoResultFound` and
`ResourceClosedError` are the upstream implementations and cannot drift from them.

Lifecycle needed no compromise at all and is 1:1: `connect()`, `begin()`,
`begin_nested()`, `commit()`, `rollback()`, `execution_options()`,
`in_transaction()`, and `bind=` for a connection somebody else owns.

Still open, and deliberately: **exception wrapping**. A driver error surfaces as
the driver's, not as `sa.exc.IntegrityError`. Doing it properly means the dialect's
`dbapi_exception_translation` and `is_disconnect` on every statement path, which is
its own piece of work with its own cost to measure (§5.6).

---
## 7. Phasing

1. ~~Port §2's ablation to postgres~~ — **not done**, no server available. §5.1
   stands open and is the first thing to run where one exists; every number in
   this document is sqlite.
2. **Done.** `rf.Engine(sa_engine)`, driver dispatched from the dialect,
   `connect(bind=...)` for a caller's connection or session. `tests/test_bind.py`
   asserts the point: reads inside `conn.begin()` and inside an `AsyncSession`
   see uncommitted writes and roll back with them.
3. **Done.** `execute()`/`scalar()`/`scalars()`/`stream()`/`stream_scalars()`/
   `exec_driver_sql()` over real SQLAlchemy `Result` and `AsyncResult` objects,
   plus the lifecycle renames. `tests/test_result.py` walks the shape matrix.
4. **Done.** §3's deletion table is applied. §5.4's "keep the pool as an opt-in
   provider" was *not* taken, on the explicit call that the checkout cost is
   worth the deletion.
5. Docs done. **Benchmarks re-run but not re-published**: the README table
   predates this, and its rowform arms now pay SQLAlchemy's checkout. §5.3 stands.

---

## 8. What building it turned up

Two silent-wrongness bugs, both caught by the existing suite, both inherent to
executing on a connection SQLAlchemy pooled rather than one rowform opened.

**8a. A one-shot write was discarded on two drivers of three.** `execute()` ran on
a connection from `AsyncEngine.connect()`, and a statement run straight on the
driver connection sits inside whatever transaction *that driver* opened for it —
pysqlite's implicit BEGIN, psycopg's transactional connection. The pool resets with
a rollback on release, so the write vanished. Only asyncpg, being autocommit, would
have committed. rowform's own sqlite pool used `isolation_level=None`, which is why
this could not happen before.

Fixed by running writes through `sa_engine.begin()`
(`Engine._checkout(commit=True)`), so all three agree, on the safe answer.

**8b. Savepoints were silently wrong on sqlite.** pysqlite opens a transaction
before DML but not before a `SAVEPOINT`, so the savepoint SQLAlchemy issues for
`begin_nested()` landed outside the transaction the following INSERT opened, and
rolling the outer block back left the inner block's rows behind — measured, 5 rows
where 4 were expected. This is SQLAlchemy's own documented pysqlite caveat, and its
documented recipe (`isolation_level=None` on connect plus an explicit `BEGIN` on
the `begin` event) fixes it. `SqliteDriver.configure()` registers it when an
aiosqlite engine is wrapped — the failure is silent, so opting in was the wrong
default.

Both are the concrete form of §5.2, which predicted the *direction* — "autocommit
semantics diverge across drivers" — and named psycopg. It did not predict that
sqlite was affected too, or that savepoints were a second, separate case.

One thing had to be kept rather than inherited: **cancellation**. A cancelled
aiosqlite read leaves its statement running in a worker thread, and SQLAlchemy's
pool hands the connection straight to the next borrower. `Driver.on_cancelled`
survives from the old pool for exactly that; asyncpg and psycopg need nothing.

And one removal worth naming: **`pool_stats()` is gone**, with `PoolStats`. It
described rowform's pool. SQLAlchemy's is `engine.sa_engine.pool.status()` — which
costs the one number psycopg's pool reported and SQLAlchemy's does not, the count
of callers *waiting* for a connection.

**8c. The compatibility track's cost was mostly self-inflicted.** The first
version wrapped every single-entity row in a 1-tuple before handing it to
`IteratorResult`, and `ScalarResult` then undid the wrap with `itemgetter(0)` —
which is why `.scalars().all()` measured *more* expensive than `.all()` (0.210 vs
0.191 ms per 1000 rows) and why §6's guidance said the granularity of "pay only
if used" was the method name.

SQLAlchemy already had the seam: `IteratorResult(..., _source_supports_scalars=True)`
takes the scalars themselves and builds a `Row` only if one is asked for.
Switching to it **deleted** the wrapping helper and moved the numbers to
`.scalars().all()` 0.0049 ms, `.all()` 0.168 ms, `.mappings().all()` 0.471 ms —
43x cheaper on the most idiomatic call, with every accessor byte-identical
(the compatibility suite passed unchanged, which is the proof).

So the granularity *is* per accessor after all, and the guidance is simpler than
§6's: take `.scalars()` and the compatibility track costs almost nothing; take
rows and you pay for rows. The flag is spelled `_source_supports_scalars` on
`IteratorResult` and `source_supports_scalars` on `ChunkedIteratorResult` —
upstream's inconsistency, and a second internal coupling on top of
`SimpleResultMetaData`.
