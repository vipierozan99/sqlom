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

users = await db.fetch_all(
    sa.select(User).where(User.name.like("a%")).limit(100)
)   # list[User]
```

One class, three jobs:

| | |
|---|---|
| `User.__table__` | a real `sa.Table` — `create_all()`, `Inspector`, Alembic's `target_metadata` |
| `sa.select(User)`, `User.id > 100` | real SQLAlchemy expressions, compiled by Core |
| `user.id` | an `int` on a plain dataclass, with no `_sa_instance_state` |

> **Status: early.** Implemented, tested against sqlite and PostgreSQL 16, and
> benchmarked. Not packaged, not on PyPI, never run in production.

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

sqlite, 200k-row table, 1000 rows per read, 300 iterations, 5 trials, one contender per
process, GC off, pinned cores. Medians in ms, lower is better; `x` is against rowform,
and `~` marks a pair the trials do not actually order.

| | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| raw driver → dicts *(floor)* | 0.8075 | 1.3351 | — | | 0.67x | 0.73x | — |
| raw driver + the same hydrator *(floor)* | 0.9058 | 1.4891 | — | | 0.75x | 0.82x | — |
| **rowform** `fetch_all()` | **1.2003** | **1.8212** | **3.6604** | | **1.00x** | **1.00x** | **1.00x** |
| rowform `execute().scalars()` | 1.2368 | — | 3.6992 | | ~1.03x | — | ~1.01x |
| rowform `execute().all()` | 1.3757 | 2.0185 | — | | 1.15x | 1.11x | — |
| SQLAlchemy Core (positional) | 1.5163 | 2.1204 | 4.1496 | | 1.26x | 1.16x | 1.13x |
| SQLAlchemy Core (`.mappings()`) | 3.1747 | — | — | | 2.64x | — | — |
| SQLAlchemy ORM | 4.2782 | 6.9755 | 7.8641 | | 3.56x | 3.83x | 2.15x |
| SQLAlchemy ORM (`MappedAsDataclass`) | 5.1737 | 9.0512 | 17.3702 | | 4.31x | 4.97x | 4.75x |

**1.1–1.3x SQLAlchemy Core's result layer and 2.1–4.3x its ORM**, on both backends. With
the driver removed entirely, the row layer alone is **0.26 ms against Core's 0.55 and
the ORM's 3.18**. Postgres numbers, and the ratios' intervals, are in
[METHODOLOGY.md](docs/METHODOLOGY.md).

Three things matter more than the ratios. **Every contender runs identical SQL**,
compiled by Core, so what is compared is only what happens to the rows afterwards.
**`wide` shows the smallest win, which is why it is in the table** — it is the shape full
of `DateTime`/`Numeric`/`Enum`/`Uuid` columns, where type processors dominate and both
sides run the same ones. And **the sqlite floors hold one connection for the whole run
while rowform checks one out per read**, so most of that 0.75x is SQLAlchemy's ~0.3-0.4 ms
pool checkout rather than row-layer work — against the postgres floor, which acquires per
request like rowform does, the two tie.

These runs report `quotable=False` on exactly one clause: cpu boost is on and needs root
to disable. Everything else the gate checks — clean tree, equivalence enforced, one
contender per process — passes.

Full numbers, and a log of **eleven published claims that turned out to be wrong**:
[METHODOLOGY.md](docs/METHODOLOGY.md).

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
await db.fetch_value(sa.select(sa.func.count()).select_from(User))  # int
```

**One selected entity yields that entity; two or more yield a tuple.** The statement
decides, never the model — so `select(User.name, User.id)` returns `(str, int)` in
*that* order and cannot silently mis-assign fields. An `outerjoin` with no match gives
`None` for that slot rather than an object full of `None`s. `fetch_all` is overloaded on
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
run, and `.all()` costs **11-17%** — the `Row` per row, and nothing else. Both are
still under stock SQLAlchemy Core on the same statement, because what changes
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

`aiosqlite`, `asyncpg` and `psycopg` are supported; which one is in play comes from the
URL, and the dialect statements compile for is the engine's own — one SQLAlchemy has
already run `initialize()` against, so it knows the server version.

Giving up rowform's own pool costs a measured ~0.3 ms per checkout, paid per *checkout*
rather than per row or per statement — with the connection in hand, executing on a
SQLAlchemy-pooled connection costs what executing on rowform's own did. What it buys is
the `bind=` case above, which an engine owning its own pool cannot do at any price.
[PLAN_SQLA_API.md](docs/PLAN_SQLA_API.md) §2 has the numbers and the ablation.

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
uv sync --all-extras
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
