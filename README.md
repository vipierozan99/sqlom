# ⚡ rowform

[![CI](https://github.com/vipierozan99/sqlom/actions/workflows/ci.yml/badge.svg)](https://github.com/vipierozan99/sqlom/actions/workflows/ci.yml)
[![Python 3.11–3.14](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://github.com/vipierozan99/sqlom/blob/main/pyproject.toml)
[![Typed](https://img.shields.io/badge/typing-py.typed-blue)](https://peps.python.org/pep-0561/)

**SQLAlchemy's schema and SQL. Compiled hydration. No instance state.**

rowform is a read path for high-throughput async Python services. SQLAlchemy Core
compiles your statements and owns your schema; rowform takes the driver's rows and
fills plain dataclasses with generated code — no `Row`, no `Session`, no identity map,
no instrumented attributes.

You keep SQLAlchemy's entire SQL surface, `create_all()`, `Inspector` and Alembic, and
pay for none of its result layer.

```python
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped
import rowform as rf

class Base(rf.Base):
    metadata = sa.MetaData()

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str | None]

db = rf.Engine(create_async_engine("postgresql+asyncpg://localhost/app"))

async with db.begin() as conn:
    users = await conn.fetch_all(
        sa.select(User).where(User.name.like("a%")).limit(100)
    )   # list[User]
```

`db.fetch_all(...)` works straight off the engine too, and is a statement shorter — but
it opens no transaction, so the read has no snapshot to sit in. The scope above is what
the benchmarks measure and what an `AsyncSession` application already does.

One class, three jobs:

| | |
|---|---|
| `User.__table__` | a real `sa.Table` — `create_all()`, `Inspector`, Alembic's `target_metadata` |
| `sa.select(User)`, `User.id > 100` | real SQLAlchemy expressions, compiled by Core |
| `user.id` | an `int` on a plain dataclass, with no `_sa_instance_state` |

> **Status: early.** Implemented, tested against sqlite and PostgreSQL 16, and
> benchmarked. Not packaged, not on PyPI, never run in production.

```bash
uv add "rowform @ git+https://github.com/vipierozan99/sqlom"
uv add asyncpg          # or psycopg[binary] + psycopg-pool, or aiosqlite
```

SQLAlchemy comes with it and is not optional. Which driver is in play comes from the
URL — see [Backends](#-backends) for what each one supports.

---

## 🚫 No implicit queries

The missing ORM features are the point, and the reason is not speed.

`user.posts` reads like a field access. Whether it *is* one depends on whether that
relationship was already loaded — and the call site looks identical either way. When it
is not loaded, reading the attribute is a `SELECT`, so the query moves to wherever the
attribute happens to be touched. SQLAlchemy names the result: "the N plus one problem,
which states that for any N objects loaded, accessing their lazy-loaded attributes
means there will be N+1 SELECT statements emitted". That is round trips, not CPU, and
no row layer is fast enough to fix a latency multiplier.

It is not the only default where an attribute access is I/O:

| | |
|---|---|
| **expire on commit** | after `commit()` every object is expired, so the next `user.name` is a `SELECT` |
| **autoflush** | a flush runs before each `Session.execute()`, so a *read* can emit the writes you had pending |
| **identity map** | the `SELECT` runs, then a row already in the map yields the object that was there — the fetched values dropped |

**Under asyncio, lazy loading does not work at all**: it "will fail under asyncio as no
implicit IO is allowed". The documented ways to live with that — write-only collections,
`lazy="raise"`, `expire_on_commit=False`, and `AsyncAttrs.awaitable_attrs` to make the
load explicit — are this library's position, applied one flag at a time. rowform is
async-only, so it starts there: no instrumented attribute exists to raise from, so
there is nothing to switch off and nobody checking in review whether someone did.

What that buys is checkable: **every round trip corresponds to a statement you wrote.**
Control flow still decides how often one runs, but nothing outside the `fetch_*` calls
on the page can add one — so a request's query count is something a test asserts:

```python
db.observer = lambda sql, *_: seen.append(sql)
await load_dashboard(db)
assert len(seen) == 2          # not N+1, and it stays that way
```

It does not *prevent* N+1: a loop of `fetch_one`s is N+1 just the same, only visible in
review instead of hidden behind a field access. And the unit of work solved real
problems that become yours — insert ordering, write batching, knowing what changed. At
low N against a local database, lazy loading is fine; this is a read path for the
services where it is not.

[naked-sqla](https://github.com/ManiMozaffar/naked-sqla) forecloses the same list from
the other direction — correctness rather than latency — and is worth reading.

---

## 📊 Performance

sqlite, 200k-row table, one contender per process, GC off, pinned to two physical cores,
**cpu boost off**; 1500 iterations and 3 trials for the `@1000` columns, 20000 and 5 for
`@1`. Medians in ms, lower is better; `x` is
against the equal-work rowform row in the same column, and `~` marks a pair the trials do
not actually order. Two rowform rows on purpose: **equal work** strips every API-shape
advantage (statement unprepared, the same per-row payload pass the ORM rows pay),
**idiomatic** is the code an application would write (prepared once, dataclasses straight
to orjson).

**Two read sizes.** The `@1000` columns are the usual 1000-row read, which is ~92% per-row
work. The `@1` column is one row, so it is almost entirely the per-request cost of issuing
a read at all — a different question, and the one that decides whether a library suits an
endpoint that returns a handful of rows. It takes a longer window to measure — at 1500
iterations the cell came back 17–24% dispersed, so it runs 20000 over 5 trials.

| contender | flat @1000 | join @1000 | wide @1000 | flat @1 | | flat @1000 | join @1000 | wide @1000 | flat @1 |
|---|---|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor: no SQLAlchemy)* | 1.9611 | 3.4929 | 6.5579 | 0.1868 | | 0.77x | 0.77x | 0.95x | 0.54x |
| raw driver + the same hydrator *(floor: no SQLAlchemy)* | 2.0235 | 3.6771 | 6.0907 | 0.1894 | | 0.79x | 0.81x | 0.88x | 0.55x |
| same pool + transaction → dicts *(floor: same plumbing)* | 2.1281 | 3.6604 | 6.0838 | 0.3082 | | 0.83x | 0.81x | 0.88x | 0.89x |
| **rowform** `fetch_all()` *(equal work)* | **2.5552** | **4.5244** | **6.9225** | **0.3473** | | **1.00x** | **1.00x** | **1.00x** | **1.00x** |
| rowform *(prepared, equal payload — prices the cache key)* | 2.5399 | 4.4831 | — | 0.3364 | | ~0.99x | ~0.99x | — | ~0.97x |
| rowform *(idiomatic: prepared once, direct to orjson)* | 2.3443 | 3.9408 | 6.3580 | 0.3524 | | 0.92x | 0.87x | 0.92x | ~1.01x |
| rowform `fetch_all()` off the engine *(no transaction)* | 2.3987 | — | — | 0.2263 | | 0.94x | — | — | 0.65x |
| rowform `execute().scalars()` | 2.5931 | — | 6.9705 | 0.3540 | | ~1.01x | — | ~1.01x | ~1.02x |
| rowform `execute().all()` | 2.8261 | 4.7974 | — | 0.3589 | | 1.11x | 1.06x | — | ~1.03x |
| SQLAlchemy Core (positional) | 2.4777 | 4.0978 | 6.6716 | 0.3825 | | ~0.97x | 0.91x | ~0.96x | 1.10x |
| SQLAlchemy Core (positional) *(with pysqlite's transaction recipe — prices the guarantee on Core's side)* | 2.5520 | — | — | 0.4632 | | ~1.00x | — | — | 1.33x |
| SQLAlchemy Core (`.mappings()`) | 4.6774 | — | — | 0.3888 | | 1.83x | — | — | 1.12x |
| SQLAlchemy ORM | 7.9308 | 13.5563 | 14.5928 | 0.5411 | | 3.10x | 3.00x | 2.11x | 1.56x |
| SQLAlchemy ORM (`MappedAsDataclass`) | 7.8324 | 13.5007 | 14.6669 | 0.5401 | | 3.07x | 2.98x | 2.12x | 1.56x |

**SQLAlchemy's ORM takes 2.1–3.1x the equal-work rowform time here, 2.8–4.9x on
postgres. Against Core's result layer, at strictly equal work: a tie on `flat` and
`wide`, Core ahead on `join`** (0.91–0.97x here, 0.81–0.98x on postgres). That ordering is
this table's most load-bearing number, and it is newer than the project: an earlier
revision measured rowform with a prepared statement and C-level serialization its rivals
didn't get, and published the blended margin as a result-layer win (correction 14 in
[METHODOLOGY.md](docs/METHODOLOGY.md)).

**Give Core the same transaction rowform is paying for and the tie becomes exact**:
2.5520 against rowform's 2.5552 on `flat`, which the trials do not order. Idiomatic
rowform is 8% under that.

So the honest summary: **rowform costs about what stock Core costs and returns typed,
JSON-ready dataclasses where Core returns tuples** — while the ORM costs 2–5x for its
instrumented objects. The decomposition rows say where the idiomatic margin lives:
`prepare()` turns out to be worth nothing measurable (the `prepared` row ties the
equal-work one — rowform's statement cache is that cheap), so in the cells that
have a `prepared` rung the whole 8–13% is the serialization path, dataclasses straight
into orjson's C serializer. `wide` has no such rung, so its 8% is assumed to split the
same way rather than shown to.

**The `@1` column is a different result, and reading it as the same one is a mistake.**
Nothing there is per-row: `prepared` and `idiomatic` tie with equal work, because
prepare-once and a C serializer buy nothing on one row. What is left is the cost of
issuing a read, and it orders the libraries differently — rowform 0.3473 against Core's
0.3825, with the transaction rowform pays for putting Core at 0.4632 once it pays for one
too. Until 2026-08-26 rowform *lost* this column to stock Core, 1.17x, because its
`BEGIN` was taking three round trips to aiosqlite's worker thread where one would do
(correction 16). Two-thirds of a fixed cost, invisible in every column to its left. The postgres tables — re-recorded 2026-08-27, where
the `@1` column is a different story again: two real round trips rather than sqlite's
Python, so the transaction alone is **43%** of a single-row read there — the pool
decomposition (going through SQLAlchemy's pool costs 0.164 ms/request against a bare
connection — **more** than `asyncpg.Pool`, correcting an earlier claim to the contrary),
and the mock instrument that isolates the row layer alone are in
[METHODOLOGY.md](docs/METHODOLOGY.md).

Three things matter more than the ratios. **Every contender runs identical SQL**,
compiled by Core, so what is compared is only what happens to the rows afterwards.

**On sqlite, rowform's read is in a real transaction and stock SQLAlchemy's is not.**
pysqlite begins implicitly before DML and never before a SELECT, so `engine.begin()`
around a read reaches the wire as nothing at all — Core's read runs in autocommit, with no
snapshot shared by two statements and with `begin_nested()`'s savepoint landing outside
the transaction its writes open. Both are defects SQLAlchemy documents and declines to fix
by default; rowform applies the documented recipe, so it pays a round trip Core does not.
That is a real difference in what the two provide rather than a handicap, so it is priced
from both sides instead of hidden: `rowform (no transaction)` takes the guarantee off
rowform, and `SQLAlchemy Core (positional, real transaction)` puts it onto Core. **Read
those two rows before reading the ratio you care about.** The floors do send it, because a
floor exists to isolate one variable — and until 2026-08-26 they did not, which made the
same-plumbing floor a round trip lighter than the thing it bounded (correction 16).

And **`wide` shows the smallest win, which is why it is in the table** — it is the shape
full of `DateTime`/`Numeric`/`Enum`/`Uuid` columns, where type processors dominate and both
sides run the same ones.

> **Every gate passes on these runs** — boost off and verified still off at the end of
> each run, clean tree, equivalence enforced and hash-verified per timed process, one
> contender per process, no throttle events. The `@1000` columns hold **under 1.8%**
> trial-to-trial spread on every row but one floor at 5.7%. **The `@1` column is looser**:
> most rows are under 5%, but `rowform (no transaction)` recorded 19.8%, the
> same-plumbing floor 11.9% and `execute().all()` 10.1% — sub-millisecond requests on a
> box where a scheduler hiccup is 30x the median. Their *medians* reproduced across two
> independent runs to within 0.6–4%, which is the reason they are quoted at all, and no
> claim here rests on those three rows. The postgres table in
> [METHODOLOGY.md](docs/METHODOLOGY.md) was re-recorded on 2026-08-27 at the same sha as
> this one: **under 4.0%** trial spread on all three `@1000` cells, one row above 5% in
> its `@1` cell. Absolute times are not comparable to tables published before boost was
> disabled — nor, for postgres, across the server change that table names; ratios are.
> Raw artifacts are on `bench/2026-08-26-sqlite-begin` (sqlite, both shas) and
> `bench/2026-08-27-postgres-attached` (postgres), indexed in
> [RUNS.md](docs/RUNS.md), which also records the **undiagnosed** dispersion problem this
> box shows — in sqlite `join`/`wide` previously, and in the `@1` cell here.

Full numbers, and a log of **sixteen published claims that turned out to be wrong** —
the most recent being the sqlite half of the one before it, floors in this very table that
sent no transaction while rowform did:
[METHODOLOGY.md](docs/METHODOLOGY.md).

---

## 🔌 Backends

Three async drivers, dispatched from the URL. Anything else — a sync engine, MySQL,
Oracle, SQL Server — raises `ConfigurationError` at `rf.Engine(...)` rather than failing
later: rowform runs statements on the driver connection itself, so there has to be one it
knows how to await.

| | `sqlite+aiosqlite` | `postgresql+asyncpg` | `postgresql+psycopg` |
|---|---|---|---|
| reads, writes, transactions, savepoints | ✅ | ✅ | ✅ |
| `fetch_iter` / `stream()` | cursor `fetchmany` | server-side portal | `DECLARE`d cursor |
| …streaming a write with `RETURNING` | ✅ | ✅ | ❌ `UnsupportedError` — postgres will not `DECLARE` a cursor for one |
| `copy_in` | ❌ `UnsupportedError` — use `execute_many` | ✅ binary COPY | ✅ `COPY … FROM STDIN` |
| `pipeline()` | ❌ `UnsupportedError` | ❌ `UnsupportedError` — asyncpg has no such API | ✅ needs libpq 14+ |
| what a driver error arrives as | `sqlite3.*` | `asyncpg.exceptions.*` | `psycopg.errors.*` |

Two of those rows are why the test suite runs every engine test three times rather than
twice. sqlite is put in autocommit and given SQLAlchemy's explicit-`BEGIN` recipe (without
it, savepoints are silently wrong under pysqlite); asyncpg has no implicit transaction at
all, so rowform starts the one `begin()` only promised; psycopg's connection is
transactional in its own right, which is why it was the only driver that could show a
write being discarded ([PLAN_SQLA_API.md](docs/PLAN_SQLA_API.md) §8).

Driver exceptions are **not** wrapped — see the last row, and
[GUIDE.md](docs/GUIDE.md#handling-errors) for what that means when you move a write onto
rowform.

---

## 📚 Documentation

| | |
|---|---|
| [GUIDE.md](docs/GUIDE.md) | recipes — FastAPI, pagination, streaming, testing, pool sizing, migrating off the ORM |
| [API.md](docs/API.md) | every public name, and what it returns |
| [METHODOLOGY.md](docs/METHODOLOGY.md) · [FINDINGS.md](docs/FINDINGS.md) | the numbers and how they were taken; what turned out to be fast and what didn't |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | how to work on it; what the codegen can and cannot reach |

---

## 📖 Usage

### Declaring models

SQLAlchemy's own vocabulary, on a base class of your own:

```python
class Base(rf.Base):
    metadata = sa.MetaData()          # what Alembic's target_metadata points at

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str | None]                       # -> nullable column
    role: Mapped[Role]                              # an Enum class -> sa.Enum
    balance: Mapped[Decimal] = rf.mapped_column(sa.Numeric(12, 2))
    owner_id: Mapped[int] = rf.mapped_column(sa.ForeignKey("orgs.id"))
    slug: Mapped[str] = rf.mapped_column("url_slug", unique=True)
```

Anything `mapped_column()` does not recognise goes straight to `sa.Column`, so
`ForeignKey`, `Index`, `server_default` and `__table_args__` work as they always did.
Python types map through `rf.DEFAULT_TYPE_MAP`, extensible per-base with
`type_annotation_map`.

Instances are ordinary dataclasses: `repr()`, `==`, `dataclasses.fields()` and bare
`orjson.dumps(user)` all work. Class keywords reach `dataclasses.dataclass`, so
`frozen=True`, `kw_only=True` and `slots=True` do what they look like — and because the
base chain is slotted too, `slots=True` gives a *fully* slotted model with no
per-instance `__dict__`. The default stays non-slotted to keep `orjson` on its fast
native-dict path, which slotted instances fall off
([the orjson dataclass trap](docs/FINDINGS.md#the-orjson-dataclass-trap)).

### Reading

```python
await db.fetch_all(sa.select(User))                   # list[User]
await db.fetch_all(sa.select(User.name))              # list[str]
await db.fetch_all(sa.select(User, Post).join(Post))  # list[tuple[User, Post]]

await db.fetch_one(sa.select(User).where(User.id == 1))             # User | None
await db.fetch_one(sa.select(User, Post).join(Post))                # tuple[User, Post] | None
await db.fetch_one(sa.select(sa.func.count()).select_from(User))    # int | None
```

**One selected entity yields that entity; two or more yield a tuple.** The statement
decides, never the model — so `select(User.name, User.id)` returns `(str, int)` in
*that* order and cannot silently mis-assign fields. An `outerjoin` with no match gives
`None` for that slot rather than an object full of `None`s. Every read is overloaded on
arity, so all of the above infer without a cast.

For an export or a backfill, `fetch_iter` reads through a cursor and hydrates a chunk at
a time instead of building one list:

```python
async for user in db.fetch_iter(sa.select(User), chunk=500):
    await sink.write(user)
```

The connection is held for the whole iteration, so a slow consumer holds a pooled
connection while it works. Inside a scope use `conn.fetch_iter`.

**There is a second way to read, and it is SQLAlchemy's.** `execute()` returns a real
`sqlalchemy.Result` — rowform hands its hydrated rows to SQLAlchemy's own result
machinery rather than imitating it, so every accessor is the upstream implementation:

```python
async with db.connect() as conn:
    users = await conn.fetch_all(sa.select(User))                     # list[User]
    users = (await conn.execute(sa.select(User))).scalars().all()     # list[User]
    rows  = (await conn.execute(sa.select(User))).all()               # list[Row]
```

`.scalars()`, `.mappings()`, `.tuples()`, `.unique()`, `.partitions()`, `row.name`,
`NoResultFound` — all of it behaves as it does upstream, which is what lets code move
over a query at a time. Nothing is wrapped on the way in, so you pay for what you take:
measured on the accessor alone, per 1000 rows, `.scalars().all()` costs 0.0049 ms,
`.all()` 0.168 ms and `.mappings().all()` 0.471 ms.

End to end that holds up: measured against `fetch_all` on the same 1000-row read,
one contender per process, `.scalars()` **ties** with it in every cell where both
run, and `.all()` costs **6-11%** — the `Row` per row, and nothing else. What changes
underneath is `Row`/`CursorResult`, not the idiom above it.

### Aliases and self-joins

`sa.orm.aliased()` raises `NoInspectionAvailable` here and always will — it looks for a
`Mapper`, and there is none. `rf.alias()` is the equivalent:

```python
mgr = rf.alias(User, "mgr")

await db.fetch_all(
    sa.select(User, mgr).join(mgr, User.manager_id == mgr.id)
)   # list[tuple[User, User]]
```

A subquery or CTE does not hydrate on its own, since its columns belong to it rather
than to any table. `of=` says the rows are that model's:

```python
active = rf.alias(User, of=sa.select(User).where(User.active).cte("active"))

await db.fetch_all(sa.select(active).order_by(active.id))   # list[User]
```

`of=` demands that model's columns, in order, and nothing else — an extra column is a
`DeclarationError` rather than a row that hydrates wrong while still type-checking.
[GUIDE.md](docs/GUIDE.md#aliases-and-self-joins) has the full rules, including how to
select a model out of a window-function subquery.

### Writing

```python
await db.execute(sa.insert(User).values(name="ada"))
await db.execute_many(sa.insert(User), [{...}, {...}])
await db.execute(sa.update(User).where(User.id == 1).values(hits=User.hits + 1))

rows = await db.fetch_all(sa.insert(User).values(name="ada").returning(User))
```

The class stands in for its table in writes exactly as in reads, so `sa.insert(User)`
and `sa.insert(User.__table__)` are the same statement.

`execute()` returns a `Result` either way: `.rowcount` for a plain write, rows for one
with `returning()`. A write whose `returning()` you forgot gives a *closed* result, so
reading it raises `ResourceClosedError` rather than returning `[]` and reading as
"nothing matched" — SQLAlchemy's behaviour, and the same guard as before under its own
name. `fetch_all()` still refuses a statement that returns no rows.

### Transactions

Two scopes, named as SQLAlchemy names them. `begin()` is begin-once — commits on clean
exit, rolls back on any exception:

```python
async with db.begin() as conn:
    await conn.execute(sa.update(Account)...)
    rows = await conn.fetch_all(sa.select(Account).where(...))

    async with conn.begin_nested():         # a savepoint
        await conn.execute(...)
```

`connect()` is commit-as-you-go: the first statement autobegins and leaving without
`commit()` rolls back, exactly as on an `AsyncConnection`.

```python
async with db.connect() as conn:
    await conn.execute(sa.insert(User).values(name="ada"))
    await conn.commit()
```

The `BEGIN`, the `COMMIT` and the `SAVEPOINT` are SQLAlchemy's, so they behave the same
on every driver — `conn.begin()` and `conn.begin_nested()` hand back its `AsyncTransaction`
unwrapped. Calling `db.fetch_all()` *inside* a scope raises rather than silently reading
from a different pooled connection.

**A scope can be one you did not open.** `bind=` takes an `AsyncConnection` or an
`AsyncSession`, and statements then run on that connection — seeing its uncommitted
writes, rolling back with it:

```python
async with Session() as session, session.begin():
    session.add(AuditRow(...))                        # their ORM write
    await session.flush()                             # rowform will not
    async with db.connect(bind=session) as conn:
        hot = await conn.fetch_all(sa.select(User))   # our rows, their transaction
```

That is what makes adoption incremental: an application keeps its engine, its sessions
and its migrations, and moves one query at a time.

Flush first when binding to a session: rowform reads the connection under it, not the
session, so a pending `add()` is not in the database yet and nothing rowform does will
autoflush it. [API.md](docs/API.md#async-with-engineconnectbindnone-execution_options)
says why that is left to you.

### Schema and migrations

```python
await db.create_all(Base.metadata)      # bootstrap

# alembic/env.py
target_metadata = Base.metadata
```

That is the whole integration. `alembic revision --autogenerate` produces real
`create_table`/`add_column` ops with foreign keys, indexes and constraints, because
`Base.metadata` is an ordinary `MetaData` full of ordinary `Table`s.

### The engine is SQLAlchemy's

`rf.Engine` wraps an `AsyncEngine`. It does not open one, does not pool, and does not
dispose one — the pool, the URL, `pool_size`, `pool_pre_ping`, `pool_recycle`, events
and `echo` are all SQLAlchemy's and reach it the usual way:

```python
sa_engine = create_async_engine("postgresql+asyncpg://localhost/app", pool_size=10)
db = rf.Engine(sa_engine)
...
await sa_engine.dispose()      # yours to open, yours to close
```

The dialect statements compile for is the engine's own — one SQLAlchemy has already run
`initialize()` against, so it knows the server version. Three drivers are supported, and
they do not support the same things: see [Backends](#-backends).

Giving up rowform's own pool costs something, paid per *checkout* rather than per row or
per statement — with the connection in hand, executing on a SQLAlchemy-pooled connection
costs what executing on rowform's own did. What it buys is the `bind=` case above, which
an engine owning its own pool cannot do at any price. The suite prices it against a
floor with no pool at all: **0.164 ms per request on postgres `flat`, ~12% of a 1000-row
read** — more than `asyncpg.Pool` costs, and 3.0x what asyncpg's pool costs with its
release-time reset disabled. It does not scale with the shape: on `join` the two floors
land 0.0001 ms apart, because payload work grows with arity
while per-request pool cost does not. On sqlite, where the pool is Python-only, the same
pair is 0.167 ms apart on `flat` (~6.5% of the read).

Both figures are third attempts, and the first two were wrong the same way. This one read
~0.01 ms until the postgres same-plumbing floor was found to be sending no transaction at
all, and 0.110 ms on sqlite until the sqlite floors were found to be doing likewise —
corrections 15 and 16 in [METHODOLOGY.md](docs/METHODOLOGY.md). A floor that skips a round
trip the thing it bounds is paying makes plumbing look free. The older per-checkout split in
[PLAN_SQLA_API.md](docs/PLAN_SQLA_API.md) §2 was measured under conditions since found
to be broken and has not been re-derived.

### Seeing what runs

An `observer` is called after every statement — engine or scope, read or write —
with the SQL, the round-trip time, and the row count (`None` when the statement returns
none):

```python
def slow_queries(sql: str, seconds: float, rows: int | None) -> None:
    if seconds > 0.05:
        log.warning("slow query %.1fms rows=%s: %s", seconds * 1000, rows, sql)

db = rf.Engine(sa_engine, observer=slow_queries)
```

Leaving it `None` costs one attribute load and a branch per statement, nothing per row.
Exceptions raised inside it are not caught — it runs on the caller's path.
`logging.getLogger("rowform")` adds DEBUG lines per statement compiled and per hydrator
built, carrying the generated source.

---

## 🏗️ How it works

```
sa.select(User)  ──[ SQLAlchemy Core ]──> compiled SQL + bind recipe   (once)
                 ──[ planner ]──────────> what the rows mean            (once)
                                              │
driver rows ─────────────────────────────[ generated hydrator ]──> [ User, ... ]
```

**1. Core compiles; rowform never generates SQL.** A `CoreQuery` holds the compiled
string, the parameter recipe (`positiontup` order, bind processors, `IN` expansion) and
the plan. Compilation is cached, so it costs ~0.001 ms per execute.

**2. The plan comes from the statement, not the model.** A contiguous run of selected
columns that *is* some model's full column list becomes that model; anything else is a
scalar. Columns are compared by identity, since `Column.__eq__` builds SQL rather than
comparing.

**3. Hydration is generated code**, one function per statement shape:

```python
def _hydrate(rows):
    out = []
    append = out.append
    for f0, f1, f2, in rows:          # one UNPACK_SEQUENCE per row
        o0 = _new(_c0)                # object.__new__, no __init__ dispatch
        o0.id = f0                    # plain STORE_ATTR — PEP 659 quickens it
        o0.name = f1
        o0.active = _p2(f2)           # only where a processor is needed
        append(o0)
    return out
```

It is attached to the function as `__source__`, so the codegen is inspectable rather
than magic.

**4. Type conversion comes from SQLAlchemy, both directions.** Each column's own
`result_processor` is asked of the *dialect-adapted* type and inlined, so a `DateTime`
on sqlite or a `Numeric` on postgres decodes exactly as it would through `Row`. Where
the driver already returns the right object the processor is `None` and the field
compiles to a bare store — most columns on asyncpg, and why bypassing `Row` costs
nothing there. Binds go through the same machinery in reverse.

That is not a detail: a per-column lookup is the only one that can be right. A table
keyed by Python type cannot express nullability, since `bool | None` never matches
`bool`, and `type.python_type` is not total — it raises for some types and collapses
`Enum` to bare `str`. Asking the column means the dialect supplies the answer.

**5. The hydrator is built on first execute**, because it needs each column's DBAPI type
code — postgres `Numeric.result_processor` *raises* without one. Once per statement,
then cached.

---

## ⚖️ What this costs

Stated plainly, because most of it is not recoverable:

* **SQLAlchemy is a hard dependency.** There is no standalone mode.
* **Every model carries a metaclass**, so `class User(Base, ABC)` and combining with
  `Protocol` raise `TypeError: metaclass conflict`. A decorator would compose freely,
  but a decorator *factory* — which is what taking `metadata` requires — erases every
  field type to `Any`, and precise types are worth more here. See
  [GUIDE.md](docs/GUIDE.md) for the workarounds.
* **No relationships, no lazy loading, no identity map, no unit of work.** You write
  every join, and insert ordering and write batching are yours. Deliberate —
  [no implicit queries](#-no-implicit-queries) — but still a cost.
* **Column order is inherited-first**, so adding a mixin moves its columns to the front
  of `CREATE TABLE`, and Alembic does not diff column order. Pin it with
  `__column_order__` on a table that already exists.
* **Instances are not tracked.** Mutating one does nothing; there is nothing to flush.

---

## 🤝 Contributing

```bash
git clone https://github.com/vipierozan99/sqlom && cd sqlom
uv sync --all-groups          # groups, not extras: typecheck covers benchmarks/
just test          # sqlite + postgres, plus the type checker
just lint
just typecheck
just bench micro run --shape flat
```

Engine and transaction tests run against **both** sqlite and PostgreSQL from one
parametrised fixture, because the two differ exactly where this design is most exposed:
sqlite hands back strings for temporal types and ints for booleans, postgres does not.
PostgreSQL tests skip with a reason when no server is reachable; `--pg-required` turns
that into a failure.

Types are tested rather than just declared, and the row path is checked against
SQLAlchemy Core as an oracle over *generated* statements, because a fixed schema only
catches the types someone thought to put in it.

[CONTRIBUTING.md](CONTRIBUTING.md) has the rest.
