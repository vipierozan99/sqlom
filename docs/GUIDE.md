# Guide

Task-oriented recipes. The [README](../README.md) is the tour; this is what to do
once you have decided to use it. [API.md](API.md) is the reference.

Every snippet here was run against a real database before being written down.

- [Getting started](#getting-started)
- [Declaring models](#declaring-models)
- [Reading](#reading)
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
await engine.fetch_all(sa.select(User))                     # list[User]
await engine.fetch_all(sa.select(User.name))                # list[str]
await engine.fetch_all(sa.select(User.name, User.id))       # list[tuple[str, int]]
await engine.fetch_all(sa.select(User, Post).join(Post))    # list[tuple[User, Post]]

await engine.fetch_one(sa.select(User).where(User.id == 1))            # User | None
await engine.fetch_value(sa.func.count().select().select_from(User.__table__))  # int
```

One selected entity yields that entity; two or more yield a tuple, in select
order. An `outerjoin` with no match gives `None` for that slot rather than an
object full of `None`s.

## Aliases and self-joins

**Self-joins** go through `rowform.alias()`, and hydrate as models:

```python
mgr = rowform.alias(User, "mgr")
rows = await engine.fetch_all(
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
await engine.fetch_all(sa.select(active).order_by(active.id))   # list[User]
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
recent = engine.prepare(
    sa.select(User).where(User.id > sa.bindparam("floor")).limit(100)
)
await engine.fetch_all(recent, floor=1000)
```

`fetch_all` caches compiled statements under SQLAlchemy's own structural cache
key, so passing a bare statement is fine; `prepare()` just removes the lookup.

## Pagination

Prefer keyset pagination over `OFFSET`: it stays O(page) as the offset grows, and
does not skip or repeat rows when the table is written to between pages.

```python
page = engine.prepare(
    sa.select(User)
    .where(User.id > sa.bindparam("after"))
    .order_by(User.id)
    .limit(sa.bindparam("size"))
)

first = await engine.fetch_all(page, after=0, size=50)
next_ = await engine.fetch_all(page, after=first[-1].id, size=50) if first else []
```

Order by something unique. If you page by a non-unique column, order by
`(column, id)` and carry both values in the cursor, or rows on a tie boundary can
be skipped.

## Streaming a large result

`fetch_all` builds one list, so peak memory is the whole result. For an export or
a backfill:

```python
async for user in engine.fetch_iter(sa.select(User), chunk=500):
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

async with aclosing(engine.fetch_iter(sa.select(User), chunk=500)) as stream:
    async for user in stream:
        if user.name == target:
            break        # the connection is released at the end of this block
```

Inside a transaction use `tx.fetch_iter`; on the engine it raises, for the same
reason `fetch_all` does.

One driver difference is visible: psycopg streams through a server-side
cursor, and postgres will not `DECLARE` one for `INSERT ... RETURNING`, so that
combination raises `UnsupportedError`. asyncpg streams it through a portal, and
sqlite streams anything.

## Writing and transactions

```python
await engine.execute(sa.insert(User.__table__).values(name="ada"))
await engine.execute_many(sa.insert(User.__table__), [{...}, {...}])
await engine.execute(
    sa.update(User.__table__).where(User.id == 1).values(hits=User.hits + 1)
)

created = await engine.fetch_all(
    sa.insert(User.__table__).values(name="ada").returning(User.__table__)
)   # RETURNING hydrates like any other read
```

**Bulk loading** goes through COPY rather than a statement per row:

```python
await engine.copy_in(User.__table__, rows)     # rows: a list of dicts
```

20 000 rows of a wide shape: 1.8x faster than `execute_many` on asyncpg, 13.6x on
psycopg. Postgres only — sqlite says so and points at `execute_many`. It
is a load path rather than a write path: no RETURNING, no ON CONFLICT. Values go
through the same bind processors as an INSERT, so what lands is what
`execute_many` would have written.

Writes take `User.__table__`, not `User`. `execute()` refuses a statement that
returns rows and `fetch_all()` refuses one that does not, so a `returning()` you
forgot fails loudly instead of returning `[]`.

```python
async with engine.transaction() as tx:
    await tx.execute(sa.update(Account.__table__)...)
    await tx.execute(sa.update(Account.__table__)...)
    rows = await tx.fetch_all(sa.select(Account).where(...))

    async with tx.transaction() as sp:      # a savepoint
        await sp.execute(...)
```

Commits on clean exit, rolls back on any exception, nests as savepoints on every
driver. Call `tx.*` inside the block — `engine.*` raises there, because it would
take a different pooled connection and miss the uncommitted writes.

Postgres transaction options ride on the `BEGIN`:

```python
async with engine.transaction(isolation="serializable", readonly=True) as tx:
    ...
```

sqlite raises `UnsupportedError` for those rather than accepting them as no-ops.

**Pipelining** is worth reaching for when the database is a network hop away:

```python
async with engine.transaction() as tx, tx.pipeline():
    for row in rows:
        await tx.execute(update, **row)
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
    rows = await engine.fetch_all(statement)
except rowform.StatementError:
    ...     # right statement, wrong method
except rowform.RowformError:
    ...     # anything else rowform refused
```

| | |
|---|---|
| `DeclarationError` | a model that cannot become a table — raised at import |
| `ConfigurationError` | an engine or transaction option it cannot honour |
| `UnsupportedError` | the backend cannot express it (sqlite isolation levels) |
| `StatementError` | `execute()` given rows, or `fetch_all()` given none |
| `PlanError` | the result's shape and the plan disagree |
| `EngineStateError` | not connected, or an engine read inside a transaction |

**Driver errors are not wrapped.** A unique-violation is asyncpg's or psycopg's own
exception, because renaming it would hide which server refused what:

```python
import asyncpg

try:
    await engine.execute(sa.insert(User.__table__).values(email=email))
except asyncpg.UniqueViolationError:
    raise EmailTaken(email) from None
```

## Wiring it into FastAPI

One engine for the process, opened and closed with the app:

```python
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request

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
process. `engine.cached_statements` is the number to watch — a fixed statement set
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

The observer is called after every statement — engine or transaction, read or
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
pool = engine.sa_engine.pool
log.info("pool %s", pool.status())
```

Saturation and a slow database look the same from outside and are fixed
differently, so it is worth exporting both.

`logging.getLogger("rowform")` emits at DEBUG and nowhere else: one line per
statement compiled — per compile, not per execute, so it also tells you whether
the cache is working — one per hydrator built, carrying the generated source, and
one per pool open and close.

## Timeouts and cancellation

There is no `timeout=` argument. `asyncio.timeout()` is the mechanism, and it
composes:

```python
async with asyncio.timeout(2):
    users = await engine.fetch_all(sa.select(User))
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
    engine = rowform.Engine(sa_engine)
    try:
        await engine.drop_all(Base.metadata)     # ignore_missing=True by default
        await engine.create_all(Base.metadata)
        await seed(engine)
        yield engine
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

`await engine.create_all(Base.metadata)` bootstraps an empty database. For
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
| `session.scalars(select(User))` | `engine.fetch_all(sa.select(User))` |
| `session.get(User, 1)` | `engine.fetch_one(sa.select(User).where(User.id == 1))` |
| `session.add(user); await session.commit()` | `engine.execute(sa.insert(User.__table__).values(...))` |
| `user.name = "x"; await session.commit()` | `engine.execute(sa.update(User.__table__).where(...).values(name="x"))` |
| `user.posts` (lazy load) | `sa.select(User, Post).join(Post)`, written out |
| `selectinload(User.posts)` | two statements, or one join and group in Python |
| `session.begin()` | `engine.transaction()` |
| `aliased(User)` | `rowform.alias(User, "x")` |

Things that will bite in order of likelihood:

1. **Mutating an instance does nothing.** There is no unit of work to flush it.
2. **Writes take `User.__table__`**, since `sa.insert(User)` has no mapper to
   consult.
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
