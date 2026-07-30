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

## Engines

`SqliteEngine`, `AsyncpgEngine` and `PsycopgEngine` share `Engine`'s API and differ
only in how they open a pool, run a statement, and open a transaction block.

```python
rowform.SqliteEngine(path, *, min_size=1, max_size=5, observer=None, cache_size=500)
rowform.AsyncpgEngine(dsn, *, conditional_reset=True, observer=None, cache_size=500, **pool_kwargs)
rowform.PsycopgEngine(dsn, *, observer=None, cache_size=500, **pool_kwargs)
```

`pool_kwargs` reach `asyncpg.create_pool` and `psycopg_pool.AsyncConnectionPool`
respectively, including their own `min_size`/`max_size`. `SqliteEngine` accepts no
others and raises `ConfigurationError` for one it does not know.

`AsyncpgEngine.conditional_reset` keeps asyncpg's `RESET ALL` on release only for
connections that could have been dirtied — anything reached through `acquire()` or
`transaction()`. `reset_count` counts the resets actually issued.

`AsyncpgEngine` is imported lazily: `import rowform` does not import the driver.

### Lifecycle

| | |
|---|---|
| `await engine.connect()` | opens the pool; idempotent, returns it |
| `await engine.close()` | closes it and drops the reference; repeatable |
| `async with engine:` | both |
| `engine.pool` | the driver's pool, or `None` |
| `engine.dialect` | the SQLAlchemy dialect statements compile for |
| `engine.observer` | see [Observer](#rowformobserver); reassignable at any time |
| `engine.cached_statements` | how many compiled statements are held, of at most `cache_size` |
| `engine.pool_stats()` | a `PoolStats` snapshot — see below |

### Reading

#### `await engine.fetch_all(statement, **params) -> list`

Hydrated rows. `**params` supplies `bindparam()` values. Overloaded on the
statement's arity: one selected entity gives `list[That]`, two or more give
`list[tuple[...]]` in select order, up to four before the row degrades to `Any`.
Raises `StatementError` if the statement returns no rows.

#### `engine.fetch_iter(statement, *, chunk=1000, **params) -> AsyncIterator`

The same rows, `chunk` at a time, through a cursor — see
[Streaming](GUIDE.md#streaming-a-large-result). Not a coroutine: iterate it, do not
await it. Same arity overloads. Raises `EngineStateError` inside a transaction (use
`tx.fetch_iter`), `ConfigurationError` for `chunk < 1`, and on `PsycopgEngine`
`UnsupportedError` for a statement postgres cannot `DECLARE` a cursor for.

#### `await engine.fetch_one(statement, **params) -> T | None`

The first row, or `None`.

#### `await engine.fetch_value(statement, **params) -> Any`

The first column of the first row, or `None`. Differs from `fetch_one` only for a
multi-entity statement.

### Writing

#### `await engine.execute(statement, **params) -> Any`

Runs a statement that produces no rows; returns the driver's own report — a
rowcount, or asyncpg's status tag. Raises `StatementError` if the statement
returns rows. Writes take `User.__table__`, not `User`.

#### `await engine.execute_many(statement, params: Sequence[dict]) -> Any`

One compiled statement, many parameter sets, one round trip. An empty sequence
returns `None` without touching the database.

### Schema

#### `await engine.create_all(metadata)` / `await engine.drop_all(metadata, *, ignore_missing=True)`

The DDL SQLAlchemy itself would emit, in dependency order, including the
`CREATE TYPE` a postgres enum needs. `create_all` is bootstrap — it has no
`checkfirst`. For an existing database, point Alembic at the same `MetaData`.

### Connections and transactions

#### `async with engine.acquire() as conn:`

A raw driver connection, for anything the engine does not model. On
`AsyncpgEngine` this marks the connection dirty, so it gets the full session reset
on release.

#### `async with engine.transaction(**kwargs) as tx:`

One connection for the block: commits on clean exit, rolls back on any exception.
`kwargs` reach the driver's `BEGIN` (`isolation`, `readonly`, `deferrable` on
postgres); sqlite raises `UnsupportedError` rather than accepting them as no-ops.

### `engine.prepare(statement) -> CoreQuery`

Compiles a statement for this engine's dialect once, so a request pays neither the
compile nor the cache-key lookup. Keeps the statement's row type.

---

## `rowform.Transaction`

Yielded by `engine.transaction()`. Runs `fetch_all`, `fetch_iter`, `fetch_one`,
`fetch_value`, `execute` and `execute_many` with the same signatures as `Engine`,
against this block's pinned connection.

| | |
|---|---|
| `tx.transaction(**kwargs)` | a nested block, implemented as a savepoint |
| `tx.depth` | 0 for the outermost, 1+ for savepoints |
| `tx.connection` | the pinned driver connection |
| `tx.execute("SQL string")` | also accepts raw SQL, for DDL and session state — no parameters, no escaping |

### `rowform.active_transaction() -> Transaction | None`

The innermost `Transaction` running in this task, from a `ContextVar`. This is what
`engine.fetch_all()` consults in order to refuse to run inside a block.

---

## `rowform.PoolStats`

What `engine.pool_stats()` returns: a frozen snapshot with `size` (connections
that exist), `idle` (available right now), `max_size`, `waiting` (callers blocked
on the pool) and an `in_use` property.

`waiting` is `None` on `SqliteEngine` and `AsyncpgEngine`, because neither pool
counts its waiters — a zero would be a claim rather than a measurement.
`PsycopgEngine` reports it, and it is the number that separates "the database is
slow" from "the pool is too small".

---

## `rowform.Observer`

```python
Observer = Callable[[str, float, int | None], None]
```

Called after every statement with the SQL as executed, the seconds it took, and the
row count — `None` for a statement returning no rows. For `fetch_iter` it is called
once for the whole stream, with the total. Exceptions propagate; it runs on the
caller's path.

`logging.getLogger("rowform")` logs at DEBUG only: statement compiled, hydrator
built (with its source), pool opened and closed.

---

## Errors

All inherit `RowformError`, and each also inherits the builtin it replaced.

| | also a | raised when |
|---|---|---|
| `RowformError` | `Exception` | base for everything below |
| `DeclarationError` | `TypeError` | a model that cannot become a table; raised at class creation |
| `ConfigurationError` | `TypeError`, `ValueError` | an engine or transaction option it cannot honour |
| `UnsupportedError` | `NotImplementedError` | the backend cannot express it at all |
| `StatementError` | `ValueError` | right statement, wrong method |
| `PlanError` | `ValueError` | the result's shape and the plan disagree |
| `EngineStateError` | `RuntimeError` | not connected, or an engine read inside a transaction |

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
