# Guide

Task-oriented recipes. The [README](../README.md) is the tour; this is what to do
once you have decided to use it. [API.md](API.md) is the reference.

Every snippet here was run against a real database before being written down.

- [Getting started](#getting-started)
- [Declaring models](#declaring-models)
- [Reading](#reading)
- [Aliases and self-joins](#aliases-and-self-joins)
- [Pagination](#pagination)
- [Streaming a large result](#streaming-a-large-result)
- [Writing and transactions](#writing-and-transactions)
- [Handling errors](#handling-errors)
- [Wiring it into FastAPI](#wiring-it-into-fastapi)
- [Sizing the pool](#sizing-the-pool)
- [Seeing what runs](#seeing-what-runs)
- [Timeouts and cancellation](#timeouts-and-cancellation)
- [Testing an app that uses rowform](#testing-an-app-that-uses-rowform)
- [Schema and migrations](#schema-and-migrations)
- [Coming from the SQLAlchemy ORM](#coming-from-the-sqlalchemy-orm)
- [Working around the metaclass](#working-around-the-metaclass)

---

## Getting started

Not on PyPI yet, so install from the repository:

```bash
uv add "rowform @ git+https://github.com/vipierozan99/sqlom"
uv add asyncpg          # or psycopg[binary] + psycopg-pool, or aiosqlite
```

SQLAlchemy comes with it and is not optional: it compiles every statement and
owns the schema.

```python
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped
import rowform

class Base(rowform.Base):
    metadata = sa.MetaData()

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = rowform.mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str | None]

sa_engine = create_async_engine("postgresql+asyncpg://localhost/app")
db = rowform.Engine(sa_engine)
try:
    users = await db.fetch_all(sa.select(User).limit(100))
finally:
    await sa_engine.dispose()
```

The engine is SQLAlchemy's. rowform wraps one; it never opens or disposes one, so
its lifetime is whatever your application already does with it.

## Declaring models

Declaration is SQLAlchemy's own vocabulary. Anything `mapped_column()` does not
recognise goes straight to `sa.Column`:

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = rowform.mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str | None]                       # nullable
    role: Mapped[Role]                              # an Enum class -> sa.Enum
    balance: Mapped[Decimal] = rowform.mapped_column(sa.Numeric(12, 2))
    org_id: Mapped[int] = rowform.mapped_column(sa.ForeignKey("orgs.id"))
    slug: Mapped[str] = rowform.mapped_column("url_slug", unique=True)

    __table_args__ = (sa.Index("ix_users_org_name", "org_id", "name"),)
```

The Python type maps to a SQLAlchemy type through `rowform.DEFAULT_TYPE_MAP`.
Override it for one column by naming the type, or for a whole base:

```python
class Base(rowform.Base):
    metadata = sa.MetaData()
    type_annotation_map = {str: sa.Text()}      # every bare `Mapped[str]` becomes TEXT
```

Instances are plain dataclasses, so `repr()`, `==`, `dataclasses.fields()`,
`dataclasses.asdict()` and bare `orjson.dumps(user)` all work. Class keywords
reach `dataclasses.dataclass`:

```python
class User(Base, frozen=True, kw_only=True, slots=True):
    ...
```

Reach for `slots=True` when instance count and GC pressure matter more than
serialization speed — a slotted instance drops off orjson's fast native-dict path
(see [FINDINGS.md](FINDINGS.md#the-orjson-dataclass-trap)).

## Reading

What a row *is* comes from the statement, never from the model:

```python
await db.fetch_all(sa.select(User))                     # list[User]
await db.fetch_all(sa.select(User.name))                # list[str]
await db.fetch_all(sa.select(User.name, User.id))       # list[tuple[str, int]]
await db.fetch_all(sa.select(User, Post).join(Post))    # list[tuple[User, Post]]

await db.fetch_one(sa.select(User).where(User.id == 1))            # User | None
await db.fetch_value(sa.func.count().select().select_from(User.__table__))  # int
```

One selected entity yields that entity; two or more yield a tuple, in select
order. An `outerjoin` with no match gives `None` for that slot rather than an
object full of `None`s.

### The other way to read

`fetch_*` is rowform's. Beside it, `execute()` is SQLAlchemy's — and not an
imitation of it: rowform hands its hydrated rows to SQLAlchemy's own result
machinery, so what comes back is a real `sqlalchemy.Result`.

```python
async with db.connect() as conn:
    users = await conn.fetch_all(sa.select(User))                     # list[User]
    users = (await conn.execute(sa.select(User))).scalars().all()     # list[User]
    rows  = (await conn.execute(sa.select(User))).all()               # list[Row]
```

Every accessor is the upstream implementation — `.scalars()`, `.mappings()`,
`.tuples()`, `.unique()`, `.partitions()`, `row.name`, `row[0]`, `NoResultFound`,
`ResourceClosedError` — which is what lets an existing codebase move a query at a
time without rewriting how it reads rows.

**They differ in exactly one place.** For a *single* selected entity,
`execute().all()` gives `[Row(User,)]` and `fetch_all()` gives `[User]`. At two or
more selected entities the two agree, because the hydrator already produces
tuples there.

**What each costs**, per 1000 rows: nothing is wrapped on the way in, so you pay
for what you take.

| | |
|---|---|
| `fetch_all()` | the hydrator's list, nothing further |
| `execute(...).scalars().all()` | 0.0049 ms — no `Row` is built at all |
| `execute(...).all()` | 0.168 ms — one `Row` per row, on demand |
| `execute(...).mappings().all()` | 0.471 ms |

Those are the accessors alone. End to end against `fetch_all()` on the same read,
one contender per process, `.scalars().all()` **ties** with it and `.all()` costs
**8-14%** (`docs/METHODOLOGY.md`).

So the idiomatic ORM-style read is not measurably off the hot path, and only asking
for actual `Row` objects costs real money. Use `execute()` while porting and where
the `Result` API earns its keep; use `fetch_all()` on the paths you care about.

## Aliases and self-joins

**Self-joins** go through `rowform.alias()`, and hydrate as models:

```python
mgr = rowform.alias(User, "mgr")
rows = await db.fetch_all(
    sa.select(User, mgr).join(mgr, User.manager_id == mgr.id)
)   # list[tuple[User, User]]
```

`sqlalchemy.orm.aliased()` is *not* usable here — it looks for a `Mapper` and
raises `NoInspectionAvailable`. `rowform.alias()` is the equivalent, and it keeps
the types: `mgr.id` is that alias's column, so the join needs no cast.
`User.__table__.alias("mgr")` and `sa.alias(User)` hydrate identically, but their
columns are only reachable as `.c.id` and typed `Column[Any]`, which degrades the
slot to `Any`.

A **subquery or CTE** hydrates only if you say whose rows it holds, since its
columns belong to it rather than to any table:

```python
active = rowform.alias(User, of=sa.select(User).where(User.active).cte("active"))
await db.fetch_all(sa.select(active).order_by(active.id))   # list[User]
```

`of=` demands exactly that model's columns, in order — anything else is a
`DeclarationError` rather than rows that quietly hydrate as `(User, extra)` while
still typed `Select[tuple[User]]`. `select()` on a from clause expands to *all* of
its columns, and without a `Mapper` there is no notion of "the entity's columns" to
narrow that to.

So when the subquery needs an extra column of its own — a window function you want
to filter on — filter inside it and select the model's columns out:

```python
inner = sa.select(User, sa.func.row_number().over(...).label("rk")).subquery()
first = rowform.alias(User, of=(
    sa.select(*[inner.c[c.key] for c in User.__table__.c])
      .where(inner.c.rk == 1)
      .subquery()
))
```

The mark lands on the from clause you passed, not on a wrapper of it, so
`active.id` and the CTE's own `.c.id` stay the same column — wrapping would make
`select(active, cte.c.id)` two from clauses and a cartesian product.

**Hoist the compile** out of the request when you can:

```python
recent = db.prepare(
    sa.select(User).where(User.id > sa.bindparam("floor")).limit(100)
)
await db.fetch_all(recent, floor=1000)
```

`fetch_all` caches compiled statements under SQLAlchemy's own structural cache
key, so passing a bare statement is fine; `prepare()` just removes the lookup.

## Pagination

Prefer keyset pagination over `OFFSET`: it stays O(page) as the offset grows, and
does not skip or repeat rows when the table is written to between pages.

```python
page = db.prepare(
    sa.select(User)
    .where(User.id > sa.bindparam("after"))
    .order_by(User.id)
    .limit(sa.bindparam("size"))
)

first = await db.fetch_all(page, after=0, size=50)
next_ = await db.fetch_all(page, after=first[-1].id, size=50) if first else []
```

Order by something unique. If you page by a non-unique column, order by
`(column, id)` and carry both values in the cursor, or rows on a tie boundary can
be skipped.

## Streaming a large result

`fetch_all` builds one list, so peak memory is the whole result. For an export or
a backfill:

```python
async for user in db.fetch_iter(sa.select(User), chunk=500):
    await sink.write(user)
```

The connection is held for the whole iteration, so a slow consumer holds a pooled
connection while it works — size the pool accordingly, or do the slow part
outside the loop.

Leaving the loop early closes the cursor, but *when* that happens is up to the
garbage collector. If you break out and immediately need the connection back, be
explicit:

```python
from contextlib import aclosing

async with aclosing(db.fetch_iter(sa.select(User), chunk=500)) as stream:
    async for user in stream:
        if user.name == target:
            break        # the connection is released at the end of this block
```

Inside a scope use `conn.fetch_iter`; on the engine it raises, for the same
reason `fetch_all` does.

One driver difference is visible: psycopg streams through a server-side
cursor, and postgres will not `DECLARE` one for `INSERT ... RETURNING`, so that
combination raises `UnsupportedError`. asyncpg streams it through a portal, and
sqlite streams anything.

## Writing and transactions

```python
await db.execute(sa.insert(User.__table__).values(name="ada"))
await db.execute_many(sa.insert(User.__table__), [{...}, {...}])
await db.execute(
    sa.update(User.__table__).where(User.id == 1).values(hits=User.hits + 1)
)

created = await db.fetch_all(
    sa.insert(User.__table__).values(name="ada").returning(User.__table__)
)   # RETURNING hydrates like any other read
```

**Bulk loading** goes through COPY rather than a statement per row:

```python
await db.copy_in(User.__table__, rows)     # rows: a list of dicts
```

20 000 rows of a wide shape: 1.8x faster than `execute_many` on asyncpg, 13.6x on
psycopg. Postgres only — sqlite says so and points at `execute_many`. It
is a load path rather than a write path: no RETURNING, no ON CONFLICT. Values go
through the same bind processors as an INSERT, so what lands is what
`execute_many` would have written.

The model class stands in for its table in writes as in reads, so `sa.insert(User)`
and `sa.insert(User.__table__)` are the same statement. `execute()` returns a
`Result` either way — `.rowcount` for a plain write, rows for one with
`returning()` — and a write with no result set gives a closed result, so reading
it raises `ResourceClosedError` instead of returning `[]`. `fetch_all()` still
refuses a statement that returns no rows.

```python
async with db.begin() as conn:
    await conn.execute(sa.update(Account.__table__)...)
    await conn.execute(sa.update(Account.__table__)...)
    rows = await conn.fetch_all(sa.select(Account).where(...))

    async with conn.begin_nested():            # a savepoint
        await conn.execute(...)
```

Commits on clean exit, rolls back on any exception, nests as savepoints on every
driver. Call `conn.*` inside the block — `db.*` raises there, because it would
take a different pooled connection and miss the uncommitted writes.

Options are SQLAlchemy's `execution_options`, so they are spelled once for every
driver and what a backend honours is SQLAlchemy's answer:

```python
async with db.begin(isolation_level="SERIALIZABLE") as conn:
    ...
async with db.begin(postgresql_readonly=True) as conn:
    ...
```

**Pipelining** is worth reaching for when the database is a network hop away:

```python
async with db.begin() as conn, conn.pipeline():
    for row in rows:
        await conn.execute(update, **row)
```

200 updates took 564 ms one at a time and 42 ms pipelined at 1 ms of latency —
13.5x. On loopback it is marginally slower, so it is not a default. psycopg only.
While the block is open a statement's result is not available (rowcount is -1),
and an error raises when the pipeline synchronises rather than at the statement
that caused it.

## Handling errors

Everything the library rejects on purpose is a `RowformError`, and each class also
inherits the builtin it replaces, so existing `except ValueError` still works.

```python
try:
    rows = await db.fetch_all(statement)
except rowform.StatementError:
    ...     # right statement, wrong method
except rowform.RowformError:
    ...     # anything else rowform refused
```

| | |
|---|---|
| `DeclarationError` | a model that cannot become a table — raised at import |
| `ConfigurationError` | an engine or scope option it cannot honour |
| `UnsupportedError` | the driver has no such capability (COPY, pipelining, streaming a write) |
| `StatementError` | `execute()` given rows, or `fetch_all()` given none |
| `PlanError` | the result's shape and the plan disagree |
| `EngineStateError` | an engine read inside a scope, or ending a `bind=` transaction |

**Driver errors are not wrapped.** A unique-violation is asyncpg's or psycopg's own
exception, because renaming it would hide which server refused what:

```python
import asyncpg

try:
    await db.execute(sa.insert(User.__table__).values(email=email))
except asyncpg.UniqueViolationError:
    raise EmailTaken(email) from None
```

## Wiring it into FastAPI

One engine for the process, opened and closed with the app:

```python
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import create_async_engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sa_engine = create_async_engine(DSN, pool_size=4, max_overflow=12)
    app.state.db = rowform.Engine(app.state.sa_engine)
    try:
        yield
    finally:
        await app.state.sa_engine.dispose()

app = FastAPI(lifespan=lifespan)

def get_db(request: Request) -> rowform.Engine:
    return request.app.state.db

Db = Annotated[rowform.Engine, Depends(get_db)]

@app.get("/users")
async def list_users(db: Db, after: int = 0, size: int = 50) -> list[User]:
    return await db.fetch_all(LIST_USERS, after=after, size=size)
```

Two things worth doing:

* **Hoist your statements to module scope** and `prepare()` them once at startup,
  rather than rebuilding them per request.
* **Return the models directly.** They are plain dataclasses, so FastAPI (and
  orjson) serialize them without a second Pydantic copy of every field.

For a request that needs several statements to agree, open one transaction for
the handler rather than calling `db.*` repeatedly.

## Sizing the pool

The pool is SQLAlchemy's, so sizing it is too — `pool_size`, `max_overflow`,
`pool_timeout`, `pool_recycle`, `pool_pre_ping`, all on `create_async_engine`:

```python
create_async_engine("sqlite+aiosqlite:///app.db", pool_size=1, max_overflow=4)
create_async_engine(dsn, pool_size=4, max_overflow=12, pool_timeout=5)
```

`cache_size` (default 500) caps the compiled statements an engine keeps, evicting
the least recently used. Statements built per request vary in *shape* and so mint
a new cache entry each time; without the cap that dict grows for the life of the
process. `db.cached_statements` is the number to watch — a fixed statement set
sits at a constant, and one pinned to `cache_size` is recompiling forever, which
is worth knowing since compiling is the cost `prepare()` exists to hoist. (An
`IN` list of varying length is *not* one of these: it compiles to a single
expanding placeholder and shares one entry.)

Driver-specific settings go through `connect_args`, as they do for any SQLAlchemy
application.

Rules of thumb: `pool_size + max_overflow` at or below what the server will accept
divided by the number of processes; count a long `fetch_iter` as a connection held
for its whole duration; and remember that a checkout costs ~0.3–0.4 ms more than
rowform's own pool used to, paid per checkout rather than per row — so a handler
that holds one connection for the request pays it once
([PLAN_SQLA_API.md](PLAN_SQLA_API.md) §2).

## Seeing what runs

```python
def slow_queries(sql: str, seconds: float, rows: int | None) -> None:
    if seconds > 0.05:
        log.warning("slow query %.1fms rows=%s: %s", seconds * 1000, rows, sql)

engine = rowform.Engine(sa_engine, observer=slow_queries)
```

The observer is called after every statement — one-shot or scope, read or
write. `rows` is `None` for a statement that returns none, and for `fetch_iter` it
is the total, timed over the whole iteration. Leaving it `None` costs one
attribute load per statement and nothing per row. Exceptions raised inside it are
not caught.

For a tracing span, the same hook works:

```python
def to_tracer(sql: str, seconds: float, rows: int | None) -> None:
    tracer.record("db.query", duration=seconds, attributes={"db.statement": sql, "db.rows": rows})
```

SQLAlchemy's pool answers the other half — whether anything was *waiting* for a
connection while that statement ran:

```python
pool = db.sa_engine.pool
log.info("pool %s", pool.status())
```

Saturation and a slow database look the same from outside and are fixed
differently, so it is worth exporting both.

`logging.getLogger("rowform")` emits at DEBUG and nowhere else, on two occasions:
one line per statement compiled — per compile, not per execute, so it also tells
you whether the cache is working — and one per hydrator built, carrying the
generated source. The pool is SQLAlchemy's, so its own `sqlalchemy.pool` logger is
where checkouts and returns are.

## Timeouts and cancellation

There is no `timeout=` argument. `asyncio.timeout()` is the mechanism, and it
composes:

```python
async with asyncio.timeout(2):
    users = await db.fetch_all(sa.select(User))
```

What matters is what happens to the connection afterwards, since a cancelled
query is routine — any web framework cancels the handler's task when a client
disconnects, and that handler is often awaiting a query.

* **asyncpg and psycopg** cancel server-side and hand back a clean connection.
  Nothing extra is needed, and nothing here interferes.
* **sqlite** needs help. aiosqlite runs each statement in a worker thread, and
  cancelling the awaiting task does not stop that thread, so a connection handed
  straight back would make the next borrower queue behind work nobody is waiting
  for. rowform interrupts the abandoned statement before the connection goes back
  to the pool — SQLAlchemy's pool does not do this for you.

A cancelled `fetch_iter` closes its cursor and releases its connection the same
way. Inside a transaction, cancellation unwinds the block and rolls back, as any
other exception would.

## Testing an app that uses rowform

The model declaration is the table declaration, so fixtures never hand-write DDL:

```python
import pytest, rowform

@pytest.fixture
async def db(tmp_path):
    sa_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
    db = rowform.Engine(sa_engine)
    try:
        await db.drop_all(Base.metadata)     # ignore_missing=True by default
        await db.create_all(Base.metadata)
        await seed(db)
        yield db
    finally:
        await sa_engine.dispose()
```

Drop-then-create rather than delete-the-rows keeps tests independent of each
other's DDL and exercises `create_all` every time.

Test against the database you deploy on where it matters. sqlite hands back
strings for temporal types and integers for booleans while postgres does not, so
a type-sensitive result asserted only on sqlite has not been tested. This
project's own suite parametrises both from one fixture — `tests/conftest.py` is a
working example.

To assert *what* ran rather than what came back, use the observer:

```python
async def test_it_hits_the_index(db):
    seen = []
    db.observer = lambda sql, *_: seen.append(sql)
    await load_dashboard(db)
    assert len(seen) == 2      # not N+1
```

## Schema and migrations

`await db.create_all(Base.metadata)` bootstraps an empty database. For
anything that already exists, point Alembic at the same object:

```python
# alembic/env.py
from myapp.models import Base
target_metadata = Base.metadata
```

`alembic revision --autogenerate` then produces real `create_table`/`add_column`
ops with foreign keys, indexes and constraints, because `Base.metadata` is an
ordinary `MetaData` full of ordinary `Table`s.

One caveat: **column order is inherited-first**, so adding a mixin moves its
columns to the front of `CREATE TABLE`, and Alembic does not diff column order.
Pin it with `__column_order__` on a table that already exists.

## Coming from the SQLAlchemy ORM

The declaration barely changes. What changes is that you write every join, and
nothing is tracked.

| ORM | rowform |
|---|---|
| `session.scalars(select(User))` | `db.fetch_all(sa.select(User))`, or `db.scalars(...)` for the `ScalarResult` |
| `session.execute(select(User))` | `db.execute(sa.select(User))` — the same `Result` |
| `session.get(User, 1)` | `db.fetch_one(sa.select(User).where(User.id == 1))` |
| `session.add(user); await session.commit()` | `db.execute(sa.insert(User).values(...))` |
| `user.name = "x"; await session.commit()` | `db.execute(sa.update(User).where(...).values(name="x"))` |
| `user.posts` (lazy load) | `sa.select(User, Post).join(Post)`, written out |
| `selectinload(User.posts)` | two statements, or one join and group in Python |
| `async_sessionmaker(engine)` | `rf.Engine(engine)` — it wraps yours, it does not replace it |
| `session.begin()` | `db.begin()` |
| `async with db.connect()` | `async with db.connect()` — same scope, same rules |
| `Session(bind=connection)` | `db.connect(bind=connection)` |
| `aliased(User)` | `rf.alias(User, "x")` |

Things that will bite in order of likelihood:

1. **Mutating an instance does nothing.** There is no unit of work to flush it.
2. **`execute()` gives a `Result`, `fetch_all()` gives objects.** Both are here on
   purpose: the first is SQLAlchemy's, so ported code keeps working; the second
   skips the `Row` entirely. Reach for `fetch_all` once the port is done.
3. **No relationships.** A missing join is a missing join, not a lazy load — which
   is the point, but it is a change in habit.
4. **No identity map**, so two reads of row 1 give two distinct objects that
   compare equal by value.

## Working around the metaclass

Every model carries `ModelMeta`, so combining one with `ABC` or `Protocol` raises
`TypeError: metaclass conflict`:

```python
class User(Base, abc.ABC):        # TypeError
    ...
```

For a `Protocol`, you do not need inheritance at all — that is what structural
typing is:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class HasName(Protocol):
    name: str

def greet(thing: HasName) -> str:
    return f"hello {thing.name}"

greet(user)                 # type-checks, and isinstance(user, HasName) is True
```

For an `ABC`, register rather than inherit:

```python
Nameable.register(User)
isinstance(user, Nameable)   # True
```

If you need shared *columns*, use a plain mixin under the same `Base` — it shares
the metaclass, so there is no conflict, and its fields become real constructor
parameters:

```python
class Timestamped(Base):        # no __tablename__: a mixin, not a table
    created: Mapped[dt.datetime]

class Review(Timestamped, kw_only=True):
    __tablename__ = "reviews"

    id: Mapped[int] = rowform.mapped_column(primary_key=True)
    body: Mapped[str]
```
