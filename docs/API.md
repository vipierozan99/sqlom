# API reference

Everything in `rf.__all__`. `rf` is `import rowform as rf` throughout.
[GUIDE.md](GUIDE.md) has the recipes; [BENCHMARKS.md](BENCHMARKS.md) the numbers.

## Declaring

### `rf.Base`

Subclass it to make your own base, which carries the `MetaData` that
`create_all()` and Alembic point at. A subclass **with** `__tablename__` builds an
`sa.Table` and a dataclass; one **without** is a mixin whose `Mapped[]` fields
become columns on everything below it, inherited-first. Class keywords
(`frozen=`, `kw_only=`, `slots=`) reach `dataclasses.dataclass`.

```python
class Base(rf.Base):
    metadata = sa.MetaData()
    type_annotation_map = {str: sa.Text()}      # optional, per-base type overrides
```

Every concrete model has `__table__`, `__tablename__` and `__column_order__`
(set it in the class body to pin physical column order). `metadata`, `registry`,
`type_annotation_map`, `__table__`, `__tablename__` are reserved field names.

### `rf.mapped_column(*args, default=..., default_factory=..., init=True, **kwargs) -> Any`

`default`, `default_factory` and `init` configure the dataclass field; everything
else goes to `sa.Column`. A leading string renames the column; a `TypeEngine`
positional overrides the annotation's type.

```python
id: Mapped[int] = rf.mapped_column(primary_key=True)
slug: Mapped[str] = rf.mapped_column("url_slug", sa.Text(), unique=True)
org_id: Mapped[int] = rf.mapped_column(sa.ForeignKey("orgs.id"))
```

### `rf.DEFAULT_TYPE_MAP: dict[type, sa.TypeEngine]`

Used when `Mapped[T]` names no type: `bool`, `int`, `float`, `str`, `bytes`,
`datetime`, `date`, `time`, `timedelta`, `Decimal`, `UUID`, `dict`/`list` (JSON),
and any `enum.Enum` subclass. `Mapped[T | None]` makes the column nullable; a
union of more than one non-`None` type is a `DeclarationError`.

### `rf.ModelMeta`

The metaclass: `@dataclass_transform` for the checker, and `User.id` returns the
`sa.Column`. A model cannot also inherit `ABC` or `Protocol`
([workaround](GUIDE.md#the-metaclass-conflict)).

### `rf.alias(model, name=None, *, of=None) -> type[Model]`

A second reference to a model's rows, typed as the model. Without `of=`, another
alias of its table; with `of=`, an existing subquery/CTE/alias declared to hold
that model's rows — it must expose exactly the model's columns, in order, or
`DeclarationError` is raised. `name` with `of=` is refused; name the subquery.
`sqlalchemy.orm.aliased()` does not work on a rowform model (no `Mapper`).

### `rf.model_for(from_clause) -> type | None`

The model a `Table`, an alias of one, or an `of=` subquery was declared for.

## `rf.Engine`

```python
class Engine:
    def __init__(self, engine: AsyncEngine, *, observer: Observer | None = None, cache_size: int | None = 500): ...
```

Wraps a SQLAlchemy `AsyncEngine` — never opens, pools or disposes one. The driver
comes from the URL: `aiosqlite`, `asyncpg`, `psycopg`; anything else, or a sync
`Engine`, is a `ConfigurationError`.

On an aiosqlite engine, wrapping registers two listeners **on the engine you
passed**: pool `connect` sets `isolation_level=None`, and engine `begin` sends a
literal `BEGIN`. This is SQLAlchemy's documented pysqlite recipe (without it
`begin_nested()` savepoints silently land outside their transaction), and it
changes transaction behaviour for every user of that `AsyncEngine`, not only
rowform.

Attributes: `sa_engine`, `dialect` (the engine's own, `initialize()`d), `driver`,
`observer` (reassignable), `cached_statements`.

### One-shot reads

#### `await engine.fetch_all(statement, **params) -> list`

Hydrated rows; `**params` supplies `bindparam()` values. Overloaded on arity: one
selected entity gives `list[T]`, two to four give `list[tuple[...]]` in select
order, more degrades to `list[Any]`. `StatementError` for a statement returning
no rows.

A one-shot `SELECT` **checks out from the pool directly and runs outside any
transaction**: no `Connection`/`AsyncConnection` is built, no `BEGIN` is sent,
and on psycopg the driver's `autocommit` is flipped on for the statement. No
isolation level applies to such a read — a lone statement is one snapshot — so a
read that needs one belongs in a [scope](#scopes). The direct checkout is skipped,
per call and silently, whenever anything listens on the engine's `engine_connect`
event: `engine.execution_options(isolation_level=...)` registers one,
`create_async_engine(url, isolation_level=...)` does not. A write with
`RETURNING` opens a transaction and commits.

#### `await engine.fetch_one(statement, **params) -> T | None`

The first row, shaped as `fetch_all` shapes one; narrowed to `LIMIT 1` when the
statement is a `Select` without its own limit. One selected entity is already
unwrapped, so `fetch_one(sa.select(sa.func.count()).select_from(User))` is an
`int`. Same checkout rules as `fetch_all`.

#### `engine.fetch_iter(statement, *, chunk=1000, **params) -> AsyncIterator`

The same rows through a cursor, `chunk` at a time; iterate, do not await. Always
takes SQLAlchemy's ordinary connection (psycopg's server-side cursor needs a
transaction). The connection is held for the whole iteration. `ConfigurationError`
for `chunk < 1`; on psycopg `UnsupportedError` for `INSERT ... RETURNING`.

A `CompoundSelect` (`union`, `union_all`) hydrates **scalars**, not models, through
any of the above; use `rf.alias(Model, of=stmt.subquery())`.

### One-shot writes

#### `await engine.execute(statement, parameters=None, **params) -> Result`

The compatibility track's one-shot, on SQLAlchemy's ordinary connection: a
`SELECT` runs without committing, anything else is committed. `parameters` is a
dict or a list of dicts (executemany), as `AsyncConnection.execute` takes it;
`**params` merges into it. A statement with no result set gives a *closed*
`Result` — `.rowcount` works, `.all()` raises `ResourceClosedError`.

#### `await engine.scalar(...)` / `await engine.scalars(...)`

`execute(...).scalar()` / `.scalars()`; the rows are buffered, so the result
outlives the connection.

#### `await engine.execute_many(statement, params: Sequence[dict]) -> Any`

One compiled statement, many parameter sets, one round trip; returns the driver's
report (asyncpg: `None`). Empty `params` is a no-op. A statement whose SQL is
rewritten per set — an expanding `IN`, a literal-execute bind — is a
`StatementError`; run those one `execute()` each inside a scope.

#### `await engine.copy_in(table, rows: Sequence[dict], *, columns=None) -> int`

Bulk-load through COPY, postgres only (`UnsupportedError` elsewhere). Values go
through the same bind processors an INSERT would. `columns` defaults to all;
every row must carry each named one. `EngineStateError` inside a scope — use
`conn.copy_in()`.

### Schema

`await engine.create_all(metadata)` / `await engine.drop_all(metadata, *,
ignore_missing=True)`: SQLAlchemy's schema generator through `run_sync`.
`create_all` is bootstrap (`checkfirst=False`); point Alembic at the same
`MetaData` for anything that already exists.

### Scopes

#### `async with engine.connect(bind=None, **execution_options) as conn:`

`AsyncEngine.connect()`: commit-as-you-go — the first statement autobegins,
leaving without `commit()` rolls back.

`bind=` runs on an `AsyncConnection` or `AsyncSession` somebody else owns:
statements see that transaction's uncommitted writes and roll back with it, and
rowform neither begins nor ends anything. Flush the session first — rowform reads
the connection under it, so a pending `add()` is invisible. `execution_options`
with `bind=` and a connection from another driver are both `ConfigurationError`.

#### `async with engine.begin(**execution_options) as conn:`

`AsyncEngine.begin()`: commits on clean exit, rolls back on any exception.
`execution_options` reach `AsyncConnection.execution_options()` —
`isolation_level="SERIALIZABLE"`, `postgresql_readonly=True`.

Inside either scope, `engine.fetch_*`, `engine.execute*` and `engine.copy_in`
raise `EngineStateError`: they would take a different pooled connection. The
guard is a `ContextVar`, so a task started with `asyncio.create_task` inside the
block inherits it and gets the same error.

#### `async with engine.acquire() as driver_conn:`

The raw driver connection, for anything the engine does not model. Nothing is
committed.

### Disconnects

A driver exception that `dialect.is_disconnect` recognises invalidates the pooled
connection, on both the direct and the ordinary checkout, so the next borrower
gets a fresh one. Bound scopes (`bind=`) are never invalidated by rowform — the
connection is the caller's. Driver exceptions are otherwise **not** wrapped.

### `engine.prepare(statement) -> CoreQuery`

Compiles once for this engine's dialect. A bare statement is cached under
SQLAlchemy's structural cache key anyway; this is convenience, not speed.

## `rf.Connection`

Yielded by `connect()` and `begin()`. Two tracks, told apart by name.

**Compatibility track** — SQLAlchemy's `Result` over rowform's hydrated rows, so
every accessor is the upstream implementation:

| | |
|---|---|
| `await conn.execute(stmt, parameters=None, **params)` | `Result`; a list of dicts is an executemany |
| `await conn.scalar(stmt, ...)` / `await conn.scalars(stmt, ...)` | as `AsyncConnection` |
| `await conn.stream(stmt, *, chunk=1000, ...)` / `stream_scalars(...)` | `AsyncResult` / `AsyncScalarResult` over a server cursor |
| `await conn.exec_driver_sql(sql, parameters=None)` | a literal string on the driver; never returns rows |
| `conn.begin()` / `conn.begin_nested()` | SQLAlchemy's `AsyncTransaction`, unwrapped |
| `await conn.commit()` / `rollback()` / `close()` | `EngineStateError` on a `bind=` scope |
| `await conn.execution_options(**opts)` | |
| `conn.in_transaction()` / `in_nested_transaction()` / `closed` | |

**Hot track** — hydrated objects, no `Result`, no `Row`:

| | |
|---|---|
| `await conn.fetch_all(stmt, **params)` | `list[T]`, arity-overloaded as on `Engine` |
| `await conn.fetch_one(stmt, **params)` | `T \| None` |
| `conn.fetch_iter(stmt, *, chunk=1000, **params)` | `AsyncIterator[T]` |
| `await conn.execute_many(stmt, params)` | the driver's report |
| `await conn.copy_in(table, rows, *, columns=None)` | postgres only |
| `conn.pipeline()` | psycopg only: statements go out without waiting; results and errors arrive at the block's end |
| `conn.connection` / `conn.sa_connection` | the driver connection, and SQLAlchemy's |

The tracks differ in one place: for a single selected entity `execute().all()`
gives `[Row(User,)]` and `fetch_all()` gives `[User]`. At two or more they agree.

### `rf.active_connection() -> Connection | None`

The innermost scope open in this task (bound scopes included).

## `rf.Observer`

```python
Observer = Callable[[str, float, int | None], None]
```

Called after every statement with the SQL, the driver round-trip in seconds, and
the row count (`None` when none are returned; for `fetch_iter`, once with the
total). Exceptions propagate. `logging.getLogger("rowform")` logs each compile
and each hydrator build (with source) at DEBUG.

## Errors

All inherit `RowformError` and the builtin they replaced.

| | also a | raised when |
|---|---|---|
| `DeclarationError` | `TypeError` | a model cannot become a table; `alias(of=)` columns mismatch — at class creation |
| `ConfigurationError` | `TypeError`, `ValueError` | an engine or scope option it cannot honour |
| `UnsupportedError` | `NotImplementedError` | the driver has no such capability |
| `StatementError` | `ValueError` | right statement, wrong method |
| `PlanError` | `ValueError` | the result's shape and the plan disagree |
| `EngineStateError` | `RuntimeError` | an engine one-shot inside a scope; ending a `bind=` transaction |

## Lower level

| | |
|---|---|
| `rf.CoreQuery` | one statement compiled for one dialect: `.sql`, `.returns_rows`, `.is_select`, `.entities`, `.bind(params, extracted)`, `.hydrator(dialect, description)` |
| `rf.plan(statement) -> Plan` | what the rows mean: a from clause selected whole is a model, anything else a scalar; a hand-listed full column list stays scalars. `Plan.wrap` is true at two or more entities |
| `rf.compile_hydrator(plan, dialect, coltypes)` | the generated `rows -> list` function; source on `__source__`; `PlanError` if the column counts disagree |
| `rf.result_processor(column, dialect, coltype)` | the dialect-adapted type's decoder, or `None` for a bare store |
| `rf.Driver` / `rf.driver_for(dialect)` | per-driver `fetch`/`stream`/`execute`/`execute_many`/`copy_in`/`pipeline`/`autocommit`; the seam a mock replaces |
| `rf.__version__` | single-sourced into the package metadata |
