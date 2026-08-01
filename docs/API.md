# API reference

Everything in `rowform.__all__`, which is the whole public surface. Signatures are
the real ones; the overloads that make the row types exact are noted where they
change what you get back rather than repeated in full.

See [GUIDE.md](GUIDE.md) for how these fit together.

---

## Declaring

### `rowform.Base`

The base your own base inherits from. Yours carries the `MetaData` that Alembic
and `create_all()` point at:

```python
class Base(rowform.Base):
    metadata = sa.MetaData()
    type_annotation_map = {str: sa.Text()}      # optional, per-base type overrides
```

A subclass **with** `__tablename__` builds a `Table` and a dataclass. A subclass
**without** one is a mixin: its `Mapped[]` fields become columns on everything
below it, inherited-first. Class keywords (`frozen=`, `kw_only=`, `slots=`) reach
`dataclasses.dataclass`.

Class attributes it defines: `__table__` (a real `sa.Table`), `__tablename__`,
`__column_order__` (a `tuple[str, ...]`; assign to pin physical column order).

`metadata`, `registry`, `type_annotation_map`, `__table__` and `__tablename__` are
reserved as field names and raise `DeclarationError`.

### `rowform.mapped_column(*args, default=..., default_factory=..., init=True, **kwargs)`

Declares a column. `default`, `default_factory` and `init` are the dataclass
field's; **everything else goes straight to `sa.Column`**, so `primary_key`,
`ForeignKey`, `unique`, `index`, `server_default`, a positional name override and a
positional type all work as they do in SQLAlchemy.

```python
id: Mapped[int] = rowform.mapped_column(primary_key=True)
slug: Mapped[str] = rowform.mapped_column("url_slug", sa.Text(), unique=True)
org_id: Mapped[int] = rowform.mapped_column(sa.ForeignKey("orgs.id"))
```

### `rowform.DEFAULT_TYPE_MAP`

`{python_type: sa.TypeEngine}` used when a `Mapped[T]` names no type explicitly:
`bool`, `int`, `float`, `str`, `bytes`, `datetime`, `date`, `time`, `Decimal`,
`UUID`, `dict`/`list` (JSON), and any `enum.Enum` subclass. Override per base with
`type_annotation_map`, or per column by naming the type.

`Mapped[T | None]` makes the column nullable. A union of more than one non-`None`
type raises `DeclarationError`.

### `rowform.ModelMeta`

The metaclass. Carries `@dataclass_transform`, which is why field types survive
into the constructor's signature — and why a model cannot also inherit `ABC` or
`Protocol` ([workaround](GUIDE.md#working-around-the-metaclass)).

### `rowform.alias(model, name=None, *, of=None) -> type[Model]`

A second reference to a model's rows, typed as the model.

```python
mgr = rowform.alias(User, "mgr")                       # another alias of its table
sa.select(User, mgr).join(mgr, User.manager_id == mgr.id)   # list[tuple[User, User]]

active = rowform.alias(User, of=sa.select(User).where(User.active).cte("active"))
sa.select(active)                                       # list[User]
```

Returns a proxy declared `type[Model]`, so `mgr.name` and `select(User, mgr)`
infer exactly as the model does — the same type-level fiction as `User.id`. Field
names resolve to the alias's columns, which matters when a column was renamed.

`sqlalchemy.orm.aliased()` cannot do this: it inspects its argument for a
`Mapper`, and a rowform model has none.

`of=` marks an existing from clause — a subquery, CTE or alias — as holding that
model's rows, and demands exactly its columns in order. Anything else raises
`DeclarationError`, because `select()` on a from clause expands to *all* of its
columns and there is no `Mapper` to narrow that to "the entity's". A `name` with
`of=` is refused too; name the subquery or CTE itself.

### `rowform.model_for(from_clause) -> type | None`

The model a `Table` — or an `alias()` of one — was built from, else `None`. This is
how the planner resolves an aliased self-join back to a model.

---

## `rowform.Engine`

```python
rowform.Engine(engine: AsyncEngine, *, observer=None, cache_size=500)
```

Wraps a SQLAlchemy `AsyncEngine`. rowform does not open one, does not pool, and
does not dispose one — pool sizing, URLs, `pool_pre_ping`, `pool_recycle`, events
and `echo` are all SQLAlchemy's and reach it the usual way, through
`create_async_engine`.

```python
sa_engine = create_async_engine("postgresql+asyncpg://localhost/app", pool_size=10)
db = rowform.Engine(sa_engine)
...
await sa_engine.dispose()
```

Which driver is in play comes from the URL: `aiosqlite`, `asyncpg` and `psycopg`
are supported, and anything else is a `ConfigurationError` naming what it got.
So is anything that is not an `AsyncEngine` — a sync `Engine` has no awaitable
connection, and rowform runs statements on the driver connection itself.

Wrapping a `sqlite+aiosqlite` engine registers two event listeners on it, which
is SQLAlchemy's own documented recipe for pysqlite: without them `begin_nested()`
savepoints are **silently** wrong, because pysqlite does not open a transaction
for a `SAVEPOINT` but does for the DML after it.

### Attributes

| | |
|---|---|
| `engine.sa_engine` | the `AsyncEngine` it wraps |
| `engine.dialect` | its dialect — `initialize()`d, so it knows the server version |
| `engine.driver` | the `Driver` chosen from the URL |
| `engine.observer` | see [Observer](#rowformobserver); reassignable at any time |
| `engine.cached_statements` | how many compiled statements are held, of at most `cache_size` |

There is no `close()` or `pool_stats()`: the pool is SQLAlchemy's, so
`engine.sa_engine.pool.status()` is where its counters live, and disposing the
engine is the caller's business. `connect()` does exist — it opens a scope rather
than the engine; see [below](#async-with-engineconnectbindnone-execution_options).

### Reading

#### `await engine.fetch_all(statement, **params) -> list`

Hydrated rows. `**params` supplies `bindparam()` values. Overloaded on the
statement's arity: one selected entity gives `list[That]`, two or more give
`list[tuple[...]]` in select order, up to four before the row degrades to `Any`.
Raises `StatementError` if the statement returns no rows.

#### `engine.fetch_iter(statement, *, chunk=1000, **params) -> AsyncIterator`

The same rows, `chunk` at a time, through a cursor — see
[Streaming](GUIDE.md#streaming-a-large-result). Not a coroutine: iterate it, do not
await it. Same arity overloads. Raises `EngineStateError` inside a scope (use
`conn.fetch_iter`), `ConfigurationError` for `chunk < 1`, and on psycopg
`UnsupportedError` for a statement postgres cannot `DECLARE` a cursor for.

#### `await engine.fetch_one(statement, **params) -> T | None`

The first row, or `None`.

#### `await engine.fetch_value(statement, **params) -> Any`

The first column of the first row, or `None`. Differs from `fetch_one` only for a
multi-entity statement.

### Writing

#### `await engine.execute(statement, parameters=None, **params) -> Result`

The compatibility track's one-shot: opens a scope of its own, runs the statement,
closes it. `parameters` is a dict, or a list of dicts for an executemany, exactly
as `AsyncConnection.execute` takes it; `**params` is rowform's extension and
merges into it. The model class stands in for its table, so `sa.insert(User)` and
`sa.insert(User.__table__)` are the same statement.

`.rowcount` for a plain write, rows for one with `returning()`. A statement with
no result set gives a *closed* `Result`, so reading it raises
`ResourceClosedError` rather than returning `[]`.

A statement that returns rows runs without committing; one that does not is
committed. Not a nicety: a write run on a connection from `AsyncEngine.connect()`
sits inside whatever transaction the driver opened for it, and the pool's
rollback-on-release then discards it — on two of the three drivers, silently.

#### `await engine.scalar(statement, parameters=None, **params) -> Any`
#### `await engine.scalars(statement, parameters=None, **params) -> ScalarResult`

`execute(...).scalar()` and `execute(...).scalars()`, each in a scope of its own.
The rows are buffered by the time `execute()` returns, so the result outlives the
connection it used.

#### `await engine.execute_many(statement, params: Sequence[dict]) -> Any`

One compiled statement, many parameter sets, one round trip — rowform's own, so
it returns the driver's report rather than a `Result`. The SQLAlchemy spelling of
the same thing is `execute(stmt, [ ... ])`. An empty sequence returns `None`
without touching the database.

#### `await engine.copy_in(table, rows, *, columns=None) -> int`

Bulk-load through the server's COPY path, in a scope of its own. postgres only;
the others raise `UnsupportedError` naming `execute_many()` instead. `columns`
defaults to every column of the table — name a subset to let server defaults fill
the rest, and every row must carry each name.

Values go through the same bind processors a parameterised INSERT uses, because
COPY bypasses the statement path where those normally run. Refused inside a scope
(`EngineStateError`): it would take a different connection and commit on its own,
so a rollback of the surrounding block would leave the loaded rows behind. Use
`conn.copy_in()` there.

### Schema

#### `await engine.create_all(metadata)` / `await engine.drop_all(metadata, *, ignore_missing=True)`

SQLAlchemy's own `SchemaGenerator` through `run_sync`, in dependency order,
including the `CREATE TYPE` a postgres enum needs. `create_all` is bootstrap —
`checkfirst=False`. `drop_all`'s `ignore_missing` *is* `checkfirst`, so it asks
the catalogue rather than dropping blind. For an existing database, point Alembic
at the same `MetaData`.

### Connections and transactions

#### `async with engine.acquire() as conn:`

A raw driver connection, for anything the engine does not model — the same
`driver_connection` every read and write runs on. Nothing is committed; use
`begin()` if the work needs to be.

#### `async with engine.connect(bind=None, **execution_options) as conn:`

A connection scope — `AsyncEngine.connect()`. Commit-as-you-go: the first
statement autobegins, and leaving without `commit()` rolls back.

`bind=` runs on a connection somebody else owns — an `AsyncConnection` or an
`AsyncSession`. Statements then run on the same physical connection, so they see
that transaction's uncommitted writes and roll back with it. rowform neither
begins nor ends anything in that case, and for the same reason a bound scope does
not register in `active_connection()`. A connection from a different driver is
refused: the compiled SQL carries one paramstyle.

```python
async with Session() as session, session.begin():
    session.add(AuditRow(...))                            # their ORM write
    await session.flush()                                 # rowform will not
    async with db.connect(bind=session) as conn:
        hot = await conn.fetch_all(sa.select(User))
```

**Flush before you read.** "Uncommitted" means uncommitted *in the database*.
rowform reads the connection under the session, not the session, so nothing it
does triggers autoflush — a `session.add()` that has not been flushed is still
pending in the identity map, and the read will not see it. Flushing is left to
you deliberately: a read that silently flushed somebody else's session would
reorder their writes. Binding to an `AsyncConnection` has no such state and needs
no flush.

#### `async with engine.begin(**execution_options) as conn:`

The same, with a transaction already open: commits on clean exit, rolls back on
any exception — `AsyncEngine.begin()`.

`execution_options` reach `AsyncConnection.execution_options()`, which is where
isolation is spelled: `isolation_level="SERIALIZABLE"`,
`postgresql_readonly=True`, `postgresql_deferrable=True`. What a backend will and
will not honour is SQLAlchemy's answer, not a table maintained here. They are
refused alongside `bind=`, since that transaction is not rowform's to configure.

### `engine.prepare(statement) -> CoreQuery`

Compiles a statement for this engine's dialect once, so a request pays neither the
compile nor the cache-key lookup. Keeps the statement's row type.

---

## `rowform.Connection`

Yielded by `engine.connect()` and `engine.begin()`. Carries **two tracks**, told
apart by name rather than by semantics.

**The compatibility track** is SQLAlchemy's, to the letter — `execute()` returns a
real `sqlalchemy.Result` built over rowform's hydrated rows, so `.scalars()`,
`.mappings()`, `.tuples()`, `.unique()`, `Row` attribute access and
`NoResultFound` are the upstream implementations rather than imitations:

| | |
|---|---|
| `await conn.execute(stmt, parameters=None, **params)` | `Result`; a list of dicts is an executemany |
| `await conn.scalar(stmt, ...)` / `await conn.scalars(stmt, ...)` | as `AsyncConnection` |
| `await conn.stream(stmt, *, chunk=1000, ...)` | `AsyncResult` over a server cursor |
| `await conn.stream_scalars(stmt, ...)` | `AsyncScalarResult` |
| `await conn.exec_driver_sql(sql, parameters=None)` | a literal string on the driver |
| `conn.begin()` / `conn.begin_nested()` | SQLAlchemy's `AsyncTransaction`, unwrapped |
| `await conn.commit()` / `await conn.rollback()` / `await conn.close()` | `EngineStateError` on a `bind=` scope — that transaction is the caller's |
| `await conn.execution_options(**opts)` | |
| `conn.in_transaction()` / `conn.in_nested_transaction()` / `conn.closed` | |

**The hot track** is rowform's: hydrated objects, no `Result`, no `Row`, no wrap.

| | |
|---|---|
| `await conn.fetch_all(stmt, **params)` | `list[T]`, arity-overloaded as on `Engine` |
| `await conn.fetch_one(stmt, **params)` | plus the `LIMIT 1` |
| `await conn.fetch_value(stmt, **params)` | |
| `conn.fetch_iter(stmt, *, chunk=1000, **params)` | `AsyncIterator[T]` |
| `await conn.execute_many(stmt, params)` | the driver's report, not a `Result` |
| `await conn.copy_in(table, rows, *, columns=None)` | postgres only |
| `conn.pipeline()` | psycopg only — see below |
| `conn.connection` / `conn.sa_connection` | the driver connection, and SQLAlchemy's |

**What differs between them, and only this:** for a *single* selected entity
`execute().all()` gives `[Row(User,)]` and `fetch_all()` gives `[User]`. At two or
more the hydrator already produces tuples and the two agree.

Nothing is wrapped on the way in, so the compatibility track costs mostly what you
take from it rather than a flat toll — per 1000 rows, `.scalars().all()` 0.0049 ms,
`.all()` 0.168 ms, `.mappings().all()` 0.471 ms. `.scalars()` is the cheap one:
SQLAlchemy is told the source yields scalars, so no `Row` is built at all.

End to end against `fetch_all()` on the same read, one contender per process,
`.scalars().all()` **ties** with it and `.all()` costs **11-17%**
(`docs/METHODOLOGY.md`). Building the `Result` is not free in principle, but it
does not show above the trial spread; building a `Row` per row does.

### `async with conn.pipeline():`

Statements go out without waiting for each result; the replies are collected when
the block exits. Worth it only where the round trip is the cost: over 200 updates
it is slightly *slower* on loopback (56 ms against 44 ms) and **13.5x** faster at
1 ms of network latency (42 ms against 564 ms).

It is on the connection because a pipeline belongs to one. Two
inherent consequences: a statement's result is unavailable while the block is
open (psycopg reports a rowcount of -1), and an error raises when the pipeline
synchronises rather than at the statement that caused it — it does still raise,
and still rolls the transaction back.

asyncpg and sqlite raise `UnsupportedError`: asyncpg exposes no such API, and
sqlite is a local file with no round trip to hide.

### `rowform.active_connection() -> Connection | None`

The innermost `Connection` scope open in this task, from a `ContextVar`. This is
what `engine.fetch_all()` consults in order to refuse to run inside one. A scope
opened with `bind=` does not register: its transaction belongs to the caller.

---

## `rowform.Driver`

The execution primitives one driver needs — `fetch`, `stream`, `execute`,
`execute_many`, and optionally `copy_in` and `pipeline`. `rowform.driver_for(dialect)`
picks the one a dialect names. Public because it is the seam a mock engine
replaces, not because an application calls it.

---

## `rowform.Observer`

```python
Observer = Callable[[str, float, int | None], None]
```

Called after every statement with the SQL as executed, the seconds it took, and the
row count — `None` for a statement returning no rows. For `fetch_iter` it is called
once for the whole stream, with the total. Exceptions propagate; it runs on the
caller's path.

`logging.getLogger("rowform")` logs at DEBUG only: statement compiled, and
hydrator built (with its source).

---

## Errors

All inherit `RowformError`, and each also inherits the builtin it replaced.

| | also a | raised when |
|---|---|---|
| `RowformError` | `Exception` | base for everything below |
| `DeclarationError` | `TypeError` | a model that cannot become a table; raised at class creation |
| `ConfigurationError` | `TypeError`, `ValueError` | an engine or scope option it cannot honour |
| `UnsupportedError` | `NotImplementedError` | the backend cannot express it at all |
| `StatementError` | `ValueError` | right statement, wrong method |
| `PlanError` | `ValueError` | the result's shape and the plan disagree |
| `EngineStateError` | `RuntimeError` | an engine read inside `connect()`/`begin()`; `commit()`/`rollback()`/`close()` on a `bind=` scope |

Driver exceptions are **not** wrapped.

---

## The compiler internals

Public because they are inspectable and testable, not because a typical
application calls them.

### `rowform.CoreQuery`

One statement compiled for one dialect. `engine.prepare()` returns it.

| | |
|---|---|
| `query.sql` | the compiled string (a template, if the statement expands an `IN`) |
| `query.returns_rows` | whether anything hydrates |
| `query.is_select` | a SELECT, as opposed to a write with RETURNING |
| `query.entities` | the `Plan`, or `None` |
| `query.bind(params=None, extracted=None)` | `(sql, parameters)` in the driver's shape |
| `query.hydrator(dialect, description)` | the generated function, built on first use and cached |

### `rowform.plan(statement) -> Plan`

What a statement's rows mean: a contiguous run of selected columns that *is* some
model's full column list becomes that model, anything else is a scalar. Columns are
compared by identity, since `Column.__eq__` builds SQL rather than comparing.
Raises `PlanError` for a statement selecting nothing.

`Plan` carries `entities`, the flat `columns` list, and `wrap` — true when two or
more entities were selected, which is the single rule the exact `fetch_all` typing
depends on.

### `rowform.compile_hydrator(plan, dialect, coltypes) -> Callable`

Builds the `rows -> list` function for one planned statement. `coltypes` are the
DBAPI type codes from `cursor.description`, positionally aligned with
`plan.columns` — a length mismatch is a `PlanError` rather than a mis-assignment.
The generated source is on the returned function's `__source__`.

### `rowform.result_processor(column, dialect, coltype) -> Callable | None`

One column's value decoder, taken from the *dialect-adapted* type — the same call
`Row` itself runs on. `None` means the driver already returns the right object, in
which case the field compiles to a bare store.

### `rowform.__version__`

The installed version, single-sourced into the package metadata.
