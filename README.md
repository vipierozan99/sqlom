# ⚡ rowform

**SQLAlchemy's schema and SQL. Compiled hydration. No instance state.**

`rowform` is a read path for high-throughput Python services. SQLAlchemy Core
compiles your statements and owns your schema; rowform takes the rows from the
driver and fills plain dataclasses with generated code — no `Row`, no
`CursorResult`, no `Session`, no identity map, no instrumented attributes.

You get SQLAlchemy's entire SQL surface, `create_all()`, `Inspector` and Alembic,
and pay for none of its result layer.

```python
import sqlalchemy as sa
from sqlalchemy.orm import Mapped
import rowform as rf

class Base(rf.Base):
    metadata = sa.MetaData()

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str | None]

engine = rf.AsyncpgEngine("postgresql://localhost/app")
await engine.connect()

users = await engine.fetch_all(
    sa.select(User).where(User.name.like("a%")).limit(100)
)   # list[User]
```

One class does three jobs, and there is only one of it:

| | |
|---|---|
| `User.__table__` | a real `sa.Table` — `create_all()`, `Inspector`, Alembic's `target_metadata` |
| `sa.select(User)`, `User.id > 100` | real SQLAlchemy expressions, compiled by Core |
| `user.id` | an `int` on a plain dataclass, with no `_sa_instance_state` |

> **Status: early.** Implemented, tested against sqlite and PostgreSQL 16, and
> benchmarked. Not packaged, not on PyPI, never run in production. It is also a
> recent and substantial rewrite — see
> [docs/PLAN_CORE_COMPILER.md](docs/PLAN_CORE_COMPILER.md) for what changed and
> why, including what was deleted.

---

## 📊 Performance

sqlite, 200k-row table, 1000 rows per read, 300 iterations, GC off, pinned cores.
Medians in ms, lower is better. `just bench micro run --shape <shape>` reproduces.

| | flat | join | wide |
|---|---|---|---|
| raw driver → dicts *(floor)* | 0.9226 | 1.8090 | — |
| raw driver + the same hydrator *(floor)* | 1.0117 | 2.1178 | — |
| **rowform** | **1.0515** | **2.0960** | **4.0700** |
| SQLAlchemy Core (positional) | 1.6337 | 2.4253 | 5.5812 |
| SQLAlchemy Core (`.mappings()`) | 3.6406 | — | — |
| SQLAlchemy ORM | 4.9284 | 8.3629 | 9.4309 |
| SQLAlchemy ORM (`MappedAsDataclass`) | 6.1918 | 10.8190 | 21.0983 |

**1.2–1.6x SQLAlchemy Core's result layer, 2.3–4.7x its ORM, and within ~14% of
hand-rolling the driver.** With the driver removed entirely (`mock` backend, row
shaping only) the row layer alone is **0.31 ms against Core's 0.66 and the ORM's
3.95**.

Three things about that table are more informative than the ratios:

* **Every contender runs identical SQL.** Core compiles it for all of them. What
  is being compared is purely what happens to the rows afterwards.
* **`wide` shows the smallest win, and it is in the table for that reason.** It is
  the shape full of `DateTime`/`Numeric`/`Enum`/`Uuid` columns, where per-column
  type processors dominate — and both sides run the *same* processors, so there is
  proportionally less to skip. Quoting only `flat` would have been flattering.
* **Two floors, always.** One bounds the whole stack; one runs the *same* hydrator
  over the same driver, so engine cost and row-construction cost are separable.
  Keeping only the first is how a benchmark ends up with a floor slower than the
  thing it bounds — twice, here. See
  [docs/METHODOLOGY.md](docs/METHODOLOGY.md) correction 10.

Full numbers and — more usefully — a log of **eleven published claims that turned
out to be wrong**, with how each was caught:
[BENCHMARKS.md](docs/BENCHMARKS.md), [METHODOLOGY.md](docs/METHODOLOGY.md),
[FINDINGS.md](docs/FINDINGS.md).

---

## 📖 Usage

### Declaring models

Declaration is SQLAlchemy's own vocabulary — `Mapped[int]`, `mapped_column()` —
on a base class of your own:

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
`ForeignKey`, `Index`, `server_default`, `__table_args__` and the rest work as
they always did. `Mapped[T | None]` makes the column nullable; the Python type
maps to a SQLAlchemy type through `rf.DEFAULT_TYPE_MAP`, which you can
extend per-base with `type_annotation_map`.

Instances are ordinary dataclasses: `repr()`, `==`, `dataclasses.fields()` and
bare `orjson.dumps(user)` all work. Class keywords reach `dataclasses.dataclass`,
so `class User(Base, frozen=True)`, `kw_only=True` and `slots=True` do what they
look like:

```python
class User(Base, slots=True):
    __tablename__ = "users"

    id: Mapped[int] = rf.mapped_column(primary_key=True)
    name: Mapped[str]
```

`dataclasses.dataclass(slots=True)` rebuilds the class, and the class-level
Column access survives that rebuild — `User.id` is still the `sa.Column`,
`user.id` is still the `int`, and the generated hydrator writes straight into the
slots. The base chain is itself slotted (`rf.Base` and your own `Base` carry
`__slots__ = ()`), so a `slots=True` model is *fully* slotted: no per-instance
`__dict__` at all. That is the layout that actually saves memory and
GC-traversal cost — a slotted class under a dict-carrying base keeps the
managed-dict overhead and saves neither. The default stays non-slotted (its leaf
re-acquires its own `__dict__`) to keep `orjson` on its fast native-dict path,
which a slotted instance drops off (docs/FINDINGS.md,
[the orjson dataclass trap](docs/FINDINGS.md#the-orjson-dataclass-trap)). Reach
for `slots=True` when instance count and GC pressure matter more than
serialization speed.

### Reading

```python
await engine.fetch_all(sa.select(User))                   # list[User]
await engine.fetch_all(sa.select(User.name))              # list[str]
await engine.fetch_all(sa.select(User, Post).join(Post))  # list[tuple[User, Post]]
await engine.fetch_all(sa.select(User.name, User.id))     # list[tuple[str, int]]

await engine.fetch_one(sa.select(User).where(User.id == 1))          # User | None
await engine.fetch_value(sa.select(sa.func.count()).select_from(User))  # int
```

**One selected entity yields that entity; two or more yield a tuple.** That rule
is decided by the statement, never by the model — `select(User.name, User.id)`
returns `(str, int)` in *that* order, and cannot silently mis-assign fields the
way a hydrator built from the declaration would. An `outerjoin` with no match
gives `None` for that slot rather than an object full of `None`s.

The same rule is what makes the types exact: `fetch_all` is overloaded on the
statement's arity, so all of the above infer without a cast.

### Aliases and self-joins

`sa.orm.aliased()` raises `NoInspectionAvailable` here and always will — it looks
for a `Mapper`, and there is none. `rf.alias()` is the equivalent:

```python
mgr = rf.alias(User, "mgr")

await engine.fetch_all(
    sa.select(User, mgr).join(mgr, User.manager_id == mgr.id)
)   # list[tuple[User, User]]
```

It reads as the model does — `mgr.name` is that alias's column, and the alias
hydrates as a `User`, so a self-join needs no cast. `sa.alias(User)` also works
and hydrates the same way; what it does not do is keep the types, since its
columns are only reachable as `.c.name`.

A **subquery or CTE** does not hydrate on its own: its columns belong to it, not
to any table, so there is nothing to recognise. `of=` says the rows are that
model's:

```python
active = rf.alias(User, of=sa.select(User).where(User.active).cte("active"))

await engine.fetch_all(sa.select(active).order_by(active.id))   # list[User]
```

`of=` demands that model's columns, in order, and **nothing else** — an extra
column is a `TypeError` rather than a row that hydrates as `(User, int)` while
still typed `Select[tuple[User]]`. `select()` on a from clause expands to all of
its columns, and without a `Mapper` there is no notion of "the entity's columns"
to narrow that to. So filter on the extras inside the subquery and select out the
model's columns:

```python
inner = sa.select(User, sa.func.row_number().over(...).label("rk")).subquery()
first = rf.alias(User, of=(
    sa.select(*[inner.c[c.key] for c in User.__table__.c])
      .where(inner.c.rk == 1)
      .subquery()
))
```

The mark lands on the from clause you passed, not on a wrapper of it, so
`active.id` and the CTE's own `.c.id` stay the same column — wrapping would make
`select(active, cte.c.id)` two from clauses and a cartesian product.

### Hoisting the compile

`fetch_all` accepts a bare statement and caches the compiled form under
SQLAlchemy's own structural cache key. When you want the lookup gone too, compile
it yourself and bind per call:

```python
recent = engine.prepare(
    sa.select(User).where(User.id > sa.bindparam("floor")).limit(100)
)

await engine.fetch_all(recent, floor=1000)     # list[User]
```

### Writing

```python
await engine.execute(sa.insert(User).values(name="ada"))
await engine.execute_many(sa.insert(User), [{...}, {...}])
await engine.execute(sa.update(User).where(User.id == 1).values(hits=User.hits + 1))

rows = await engine.fetch_all(
    sa.insert(User).values(name="ada").returning(User)
)   # RETURNING hydrates like any other read
```

The class stands in for its table in writes exactly as it does in reads, so
`sa.insert(User)` and `sa.insert(User.__table__)` are the same statement.

`execute()` refuses a statement that returns rows, and `fetch_all()` refuses one
that does not — a write whose `returning()` you forgot fails loudly instead of
returning `[]`.

### Transactions

```python
async with engine.transaction() as tx:
    await tx.execute(sa.update(Account)...)
    await tx.execute(sa.update(Account)...)
    rows = await tx.fetch_all(sa.select(Account).where(...))

    async with tx.transaction() as sp:      # a savepoint
        await sp.execute(...)
```

Commits on clean exit, rolls back on any exception, nests as savepoints on every
driver. Calling `engine.fetch_all()` *inside* a block raises rather than silently
reading from a different pooled connection.

### Schema

```python
await engine.create_all(Base.metadata)      # bootstrap
```

For anything that already exists, point Alembic at the same object:

```python
# alembic/env.py
from myapp.models import Base
target_metadata = Base.metadata
```

That is the whole integration. `alembic revision --autogenerate` produces real
`create_table`/`add_column` ops with foreign keys, indexes and constraints,
because `Base.metadata` is an ordinary `MetaData` full of ordinary `Table`s.

### Engines

`SqliteEngine` (aiosqlite, WAL), `AsyncpgEngine` and `PsycopgEngine` share one
API. Each owns a pool and a dialect; the dialect decides paramstyle and type
handling, so the same statement runs on all three.

`AsyncpgEngine` keeps rowform's **conditional session reset**: asyncpg's pool runs
a `RESET ALL` round trip on every release, worth 20–30% of throughput, and this
engine skips it for connections that only ran compiled statements. Anything
through `acquire()` or `transaction()` is marked dirty and pays it.

---

## 🏗️ How it works

```
sa.select(User)  ──[ SQLAlchemy Core ]──> compiled SQL + bind recipe   (once)
                 ──[ planner ]──────────> what the rows mean            (once)
                                              │
driver rows ─────────────────────────────[ generated hydrator ]──> [ User, ... ]
```

**1. Core compiles; rowform never generates SQL.** A `CoreQuery` holds the
compiled string, the parameter recipe (`positiontup` order, bind processors,
`IN` expansion) and the plan. Compilation is cached, so it costs ~0.001 ms per
execute.

**2. The plan comes from the statement, not the model.** A contiguous run of
selected columns that *is* some model's full column list becomes that model;
anything else is a scalar. Columns are compared by identity — `Column.__eq__`
builds SQL, it does not compare — and an alias's proxied columns resolve through
the `FromClause` actually selected, so self-joins hydrate as models too.

**3. Hydration is generated code.** One function per statement shape:

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

It is attached to the function as `__source__`, so the codegen is inspectable
rather than magic.

**4. Type conversion comes from SQLAlchemy, both directions.** Each column's own
`result_processor` is asked of the *dialect-adapted* type and inlined, so a
`DateTime` on sqlite (stored as a string) or a `Numeric` on postgres decodes
exactly as it would through `Row`. Where the driver already returns the right
object the processor is `None` and the field compiles to a bare store — which is
most columns on asyncpg, and why bypassing `Row` costs nothing there. Binds go
through the same machinery in reverse.

That last point is not a detail. An earlier design hand-maintained a
`{bool: bool}` converter table; measured against a widened row it covered **1 of
8** columns that needed conversion, and the other 7 came back as plausible-looking
values of the wrong type. See [METHODOLOGY.md](docs/METHODOLOGY.md) correction 11.

**5. The hydrator is built on first execute, not at compile time.** It needs each
column's DBAPI type code, because postgres `Numeric.result_processor` *raises*
without one. Once per statement, then cached.

---

## ⚖️ What this costs

Stated plainly, because most of it is not recoverable:

* **SQLAlchemy is a hard dependency.** There is no standalone mode.
* **Every model carries a metaclass**, so `class User(Base, ABC)` and combining
  with `Protocol` raise `TypeError: metaclass conflict`. A decorator would compose
  freely, but a decorator *factory* — which is what taking `metadata` requires —
  erases every field type to `Any`, and precise types were judged worth more.
* **No relationships, no lazy loading, no identity map, no unit of work.** You
  write every join. This is a read path, not an ORM.
* **Column order is inherited-first**, so adding a mixin moves its columns to the
  front of `CREATE TABLE` — and Alembic does not diff column order. Pin it with
  `__column_order__` on a table that already exists.
* **Instances are not tracked.** Mutating one does nothing; there is nothing to
  flush.

---

## 🤝 Contributing

```bash
git clone https://github.com/vipierozan99/rowform && cd rowform
uv sync --all-extras
just test          # 259 tests, sqlite + postgres, plus the type checker
just lint
just typecheck
just bench micro run --shape flat
```

The suite runs engine and transaction tests against **both** sqlite and
PostgreSQL from one parametrised fixture, because the two differ exactly where
this design is most exposed — sqlite hands back strings for temporal types and
ints for booleans, postgres does not. PostgreSQL tests skip with a reason when no
server is reachable; `--pg-required` turns that into a failure, and
`ROWFORM_TEST_DSN` points them elsewhere.

Types are tested, not just declared: `tests/typing/positive.py` asserts exact
inference with `typing.assert_type`, `tests/typing/negative.py` carries a
`# pyright: ignore` on every line that must fail, and the checker runs with
`reportUnnecessaryTypeIgnoreComment` so a suppression that stops being needed
fails the build.

---

## 📜 License

MIT License. Free for open-source and commercial use.
