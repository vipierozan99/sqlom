# ⚡ sqlom: Zero-Overhead Async Data Layer for Python

`sqlom` is a data access library concept for Python built for high-throughput HTTP services (FastAPI, Sanic, Granian). It aims to reduce ORM overhead by skipping session tracking, identity maps, and dynamic class reflection, pairing a **descriptor-driven query builder** with an **`asyncpg`-backed execution engine** that hydrates rows into `@dataclass(slots=True)`-style objects and serializes them via `orjson`.

It relies on pure Python plus existing C-extensions (`asyncpg` + `orjson`) rather than a custom Rust/FFI layer.

> **Status:** early, but no longer hypothetical. The core is implemented and benchmarked against both sqlite and a live PostgreSQL 16 under concurrent load; every number below comes from a script in [`benchmarks/`](benchmarks/) with results checked in. It has a pytest suite (770 tests) covering SQL generation, codegen, joins, predicates, grouping, expressions, set operations, CTEs, writes, upserts, transactions and static types, but it is not packaged, not on PyPI, and has never run in production. Read [what none of this shows](docs/BENCHMARKS.md#16-what-none-of-this-shows) before believing any of it applies to your workload.

---

## 🎯 Key Features (proposed)

* **Compiled hydration:** A per-model `row -> object` function is code-generated once, so field stores are plain `STORE_ATTR` bytecode against a fixed slot rather than a `setattr()` loop. ~4.9x faster than a reflective loop in isolation.
* **Query builder:** A SQLAlchemy-Core-like builder (`User.id > 10`) built on descriptors, **statically typed** — `User.id` is `ColumnExpr[int]`, `user.id` is `int`, and `fetch_all(Query(User, Post))` is `list[tuple[User, Post]]`. Verified against mypy *and* pyright in `tests/typing/`.
* **Two schema styles:** a custom-metaclass model, or real stdlib `@dataclass(slots=True)` models that still support `User.id > 100`.
* **Slotted objects:** 73 B/instance vs 113 B for a `__dict__`-backed equivalent.
* **Async-first:** Native `asyncpg` pool integration — ~6x SQLAlchemy's async ORM under concurrent load, i.e. ~1 core to serve what the ORM needs ~6 cores for. Costs ~10-25% more CPU than doing no object mapping at all.
* **A real query builder:** multi-model selects returning tuples, all four join kinds, aliases and self-joins, `or_`/`and_`/`not_`, `in_`/`exists`/scalar subqueries, `GROUP BY`/`HAVING`, derived tables, set operations, window functions, `CASE`, arithmetic and SQL functions, and CTEs — including recursive ones — collected into the `WITH` clause automatically.
* **Writes:** `Insert`/`Update`/`Delete` builders with `RETURNING`, bulk insert in one statement, expression assignments (`set(score=Post.score + 1)`), `ON CONFLICT` upserts with `excluded()`, and `UPDATE ... FROM` / `DELETE ... USING` across tables.
* **Transactions and savepoints:** `async with db.transaction() as tx:` on both engines, with `Query` reads on the transaction's connection, nesting as savepoints, and isolation levels. Calling `engine.fetch_all()` inside a block raises rather than silently using another connection.
* **Postgres `json_agg` support:** Implemented (`Query.to_json_sql`), but **not the current focus** — see [If you only ever emit JSON](#if-you-only-ever-emit-json-use-the-database).

---

## 🧪 Tests

```bash
uv sync --all-extras          # installs dev tools plus asyncpg/psycopg/orjson
uv run pytest                 # 770 tests, including two type checkers
```

Without `uv`, `pip install pytest pytest-asyncio` and `python3 -m pytest tests/` still work; the Postgres-backed and typing tests just skip until their extras (`asyncpg`/`psycopg[binary]`/`psycopg-pool`, `mypy`/`pyright`) are installed too.

Two tiers. Everything testable without a server is — SQL generation, code
generation, validation, and joins end-to-end against sqlite — so most of the suite
runs anywhere in about a tenth of a second. Engine and transaction tests need
PostgreSQL and **skip with a reason** when it is unreachable; pass `--pg-required`
to turn that skip into a failure where a server is expected. Point them elsewhere
with `SQLOM_TEST_DSN`.

Both PostgreSQL engines are parameterised in the engine and transaction tests, so
a feature cannot end up quietly asyncpg-only — `PsycopgEngine` began life with no
`acquire()` at all.

| file | covers |
|---|---|
| `test_conditions.py` | predicate forms: value, `IS NULL`, column-to-column, and what each binds |
| `test_query_sql.py` | exact SQL and params per shape, plus every validation error |
| `test_predicates.py` | `or_`/`and_`/`not_`, operator forms, `in_`, empty `IN`, `exists`, scalar subqueries |
| `test_aliases.py` | aliases and self-joins, including prefix-collision refusal |
| `test_join_kinds.py` | all four joins and which side each one makes nullable |
| `test_grouping.py` | aggregates, `GROUP BY`, `HAVING`, `DISTINCT`, `OFFSET`, derived tables |
| `test_expressions.py` | arithmetic, SQL functions, `CASE`, window functions, fragment validation |
| `test_set_operations.py` | `UNION`/`INTERSECT`/`EXCEPT`, chaining, compound ordering |
| `test_dml.py` + `test_dml_pg.py` | `Insert`/`Update`/`Delete`, `RETURNING`, bulk limits, executed on both engines |
| `test_ctes.py` | `WITH`, nesting and dependency order, recursive CTEs, collection from every position |
| `test_upsert.py` | `ON CONFLICT DO NOTHING`/`DO UPDATE`, `excluded()`, executed upserts on sqlite |
| `test_update_from.py` | `UPDATE ... FROM`, `DELETE ... USING`, the qualification rule, builder-order independence |
| `test_dml_advanced_pg.py` | all three of the above against a real server on both engines, plus the Postgres-only forms |
| `test_hydrators.py` | generated code for single-model, joined, outer-joined and single-column rows |
| `test_joins_sqlite.py` | joins end to end — real SQL *and* real hydrator, so a select list and hydrator that disagree get caught |
| `test_engines_pg.py` | lifecycle, `fetch_all`, `fetch_json`, every query feature against a real server |
| `test_transactions_pg.py` | commit, rollback, savepoints, isolation, the in-transaction guard, the session-reset invariant |
| `test_transaction_base.py` | the shared `Transaction` base's abstract contract and `__repr__`, with no database |
| `test_engine_helpers.py` | pure-function engine internals (the deprecated `$`-placeholder shim) that need no database |
| `test_dataclass_model.py` | the `@model` decorator's validation, its metaclass descriptor, and orjson's dataclass-passthrough serialization |
| `test_typing.py` + `typing/` | mypy and pyright over `assert_type` positives and expected-error negatives |

The suite keeps earning its keep. Bugs it found, none of which were visible by
reading the code: a filtering join returned 1-tuples instead of instances; a
`readonly=True` transaction on psycopg left the pooled connection permanently
read-only for the next borrower; `for f0 in rows` binds each row *tuple* to `f0`
rather than unpacking it, so any single-column select (and any single-column model)
came back nested; a RIGHT-joined single-entity query took the fast hydrator and
built an object whose every field was `None`, because dispatch and the cache key
were deciding nullability two different ways; and a subquery exposed output names
for unlabelled aggregates that the derived table does not actually have.

Running the *same* feature against both backends earns its keep too, separately
from the bugs above. `ON CONFLICT DO UPDATE SET n = n + excluded.n` is accepted by
sqlite and rejected by Postgres as an ambiguous column reference; the sqlite tests
passed, and only the Postgres run showed that the target had to be qualified.

---

## 🔬 Type-level tests

Types are only as good as the evidence that they hold, and type errors are invisible to `pytest` by default. **Yes, you can test types in Python** — three techniques, all used in [`tests/typing/`](tests/typing/):

**1. `typing.assert_type` for positive assertions.** Stdlib since 3.11; a checker verifies the inferred type matches *exactly*, and at runtime it does nothing.

```python
assert_type(Author.id, ColumnExpr[int])                       # passes
assert_type(Query(Author, Book), Query[tuple[Author, Book]])  # passes
assert_type(Author.id, ColumnExpr[str])                       # checker error
```

Exactness is the point: `assert_type(x, object)` fails even though everything is an object, so a type that has silently decayed to `Any` is caught. That is the failure mode that matters, because `Any` makes every other assertion pass.

**2. Expected errors for negative assertions.** A checker reporting nothing does not prove mistakes get caught. Both checkers can be told a line must fail:

```python
Author.id > "abc"   # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
```

With mypy's `warn_unused_ignores` and pyright's `reportUnnecessaryTypeIgnoreComment`, a suppression that stops being needed becomes an error. That is what turns a comment into a test.

**3. Run the checkers from the suite.** `tests/test_typing.py` shells out to both and asserts they are clean. Both, because they disagree — pyright is stricter about descriptor overloads, and the two use different diagnostic codes for the same mistake. A third test *guards the guard*: it strips every suppression and requires mypy to then report an error on each of those lines, so the negative file cannot quietly stop proving anything.

Verified by breaking things on purpose: loosening `__gt__` to accept `Any`, and letting `Column.__get__` return `ColumnExpr[Any]`, each fail the suite.

### What is deliberately untyped

- **`@model` dataclass models.** Their descriptors live on the *metaclass*, and no checker models a metaclass data descriptor shadowing a class attribute. `ModelMeta` models are fully typed; that is the trade for real `dataclasses` interop.
- **Columns off an `Alias` or `Subquery`** — `ColumnExpr[Any]`, since both resolve names from a runtime map via `__getattr__`. Reach the column off the model for the precise type.
- **`sum_` and `avg`** — `Aggregate[Any]`. Postgres widens `sum(int)` to bigint and `avg(int)` to numeric, which arrives as `Decimal`; claiming `int` would be a lie the checker would then enforce. `count` is `int`, `min_`/`max_` keep the column's type.
- **`column == wrong_type`.** Not catchable, by anyone: Python falls back to `object.__eq__`, which accepts anything. `>` and `<` have no such fallback and *are* caught. SQLAlchemy has the same hole for the same reason.
- **Six or more selected entities** degrade to `tuple[Any, ...]`, and **outer-join nullability** is not tracked — `outer_join` is called after the row type is fixed, so the second slot is typed `Post` where at runtime it is `Post | None`.

```bash
pip install mypy pyright
python3 -m pytest tests/test_typing.py
```

---

## 📦 Installation

Not packaged and not on PyPI — `pip install sqlom` installs something else, or
nothing. This is a benchmarked concept, so the only supported way to run it is from
a clone:

```bash
git clone https://github.com/vipierozan99/sqlom && cd sqlom
uv sync --extra asyncpg --extra psycopg --extra orjson   # or --all-extras
uv run python -c "from sqlom import Query; print('ok')"
```

Without `uv`: `pip install orjson` (required for JSON serialization), `pip install
asyncpg` (for `DatabaseEngine`), `pip install "psycopg[binary]" psycopg-pool` (for
`PsycopgEngine`) — each is an optional extra (`sqlom[orjson]`, `sqlom[asyncpg]`,
`sqlom[psycopg]`) once installed from a clone.

To reproduce the benchmarks as well, see
[docs/METHODOLOGY.md](docs/METHODOLOGY.md#reproducing).

---

## 🛠️ Usage Example

### 0. If you already know SQLAlchemy

The query-building surface is deliberately named after SQLAlchemy Core, so most of
what you already know transfers directly:

```python
from sqlom import select, insert, update, delete, and_, or_, not_, func

select(User).where(User.active == True).order_by(User.id.desc())
insert(User).values(name="ada")
update(User).values(hits=User.hits + 1).where(User.id == 1)
delete(User).where(User.id == 1)

Query(User, Post).join(Post, Post.user_id == User.id, isouter=True)  # same as .outerjoin(...)
User.email.is_(None)          # IS NULL — only None is accepted, same restriction as SQLAlchemy's is_()
User.email.is_not(None)       # IS NOT NULL
Post.score.desc()             # pass straight to order_by(), instead of descending=True
User.name.like("a%")          # same on both; ilike() is Postgres-only (no ILIKE on sqlite)
Post.score.between(10, 100)   # -> Post.score >= 10 AND Post.score <= 100

select(func.avg(Post.score)).scalar_subquery()   # ScalarSubquery — a real Expression,
                                                  # usable as a value: in a comparison,
                                                  # a SELECT-list entry (once .label()d),
                                                  # or an UPDATE assignment
select(User).add_cte(some_cte)                   # SQLAlchemy's name for with_()
```

`select`/`insert`/`update`/`delete` are plain function aliases for `Query`/`Insert`/`Update`/`Delete` — construct with whichever reads better; `Query(User)` and `select(User)` are the exact same object. `Update.values()` is an alias for `.set()` (SQLAlchemy spells both `Insert` and `Update`'s assignment method `.values()`); `.outerjoin()`/`outer_join()` and `.join(..., isouter=True, full=True)` are equivalent spellings of the same four join kinds described in [§3](#3-joins-and-selecting-more-than-one-model); `add_cte()` is SQLAlchemy's name for `with_()` (its `nest_here=` isn't supported — sqlom always hoists every CTE to the outermost statement, and passing it raises rather than silently doing something else). `CompoundSelect` (what `union()`/`intersect()`/etc. return) has the same `.cte()`/`.subquery()`/`.with_()`/`.add_cte()` and every one of the six set operators, including chaining a further `intersect_all`/`except_all`. What doesn't carry over: there is no `Table`/`MetaData`/reflection/DDL layer underneath — columns come from a model class (§1 below), not a schema object, which is the one deliberate divergence the rest of this README explains. There is also no `cast()`, `tuple_()`, `text()`/`literal_column()`/`bindparam()`, or `true()`/`false()`/`null()` — sqlom binds every value as a parameter and validates the few places a fragment is accepted (§6), so there is deliberately no raw-SQL escape hatch to port.

### 1. Define Your Schema

There are two real constraints when combining `@dataclass(slots=True)` with a query-builder descriptor of the same name:

1. `@dataclass` only turns *annotated* class variables into fields. `id = Column(int)` with no annotation is invisible to it.
2. `slots=True` generates a `__slots__` entry per field. A class-level attribute (like a `Column` descriptor) sharing the same name as a slot raises `ValueError: 'id' in __slots__ conflicts with class variable`.

The default model style sidesteps both by having the metaclass own slot generation *and* the descriptor protocol, storing instance values under a shadow name:

```python
from sqlom import Column, ModelMeta

class User(metaclass=ModelMeta):
    __tablename__ = "users"

    id = Column(int)
    name = Column(str)
    email = Column(str)
    is_active = Column(bool)
```

```python
# Simplified internals — not stdlib dataclasses
class Column:
    def __init__(self, py_type: type):
        self.py_type = py_type
        self.name = None
        self._storage_name = None

    def __set_name__(self, owner, name):
        self.name = name
        self._storage_name = f"_{name}"

    def __get__(self, obj, owner=None):
        if obj is None:
            # class-level access -> AST expression node for the query builder
            return ColumnExpr(owner, self.name, self.py_type)
        return getattr(obj, self._storage_name)

    def __set__(self, obj, value):
        setattr(obj, self._storage_name, value)


class ModelMeta(type):
    def __new__(mcs, name, bases, namespace):
        columns = {k: v for k, v in namespace.items() if isinstance(v, Column)}
        namespace["__slots__"] = tuple(f"_{n}" for n in columns)
        namespace["__columns__"] = columns
        return super().__new__(mcs, name, bases, namespace)
```

**Static typing works, and is tested.** An earlier version of this README said it couldn't without a plugin or a stub. That was wrong: an overloaded descriptor `__get__` expresses the dual return type directly.

```python
User.id                 # ColumnExpr[int]   — class access
user.id                 # int               — instance access
User.id > 100           # Condition
User.id > "abc"         # type error, in both mypy and pyright
User.typo               # type error
```

The overload is the whole trick — `__get__(self, obj: None, ...) -> ColumnExpr[T]` for class access, `__get__(self, obj: object, ...) -> T` for instance access — and `Column(int)` is a `Column[int]`, so `T` follows from the declaration. `Query` is generic in its row type, so this holds end to end:

```python
authors = await db.fetch_all(Query(User))                    # list[User]
pairs   = await db.fetch_all(Query(User, Post).join(...))     # list[tuple[User, Post]]
counts  = await db.fetch_all(Query(Post.user_id, count()))    # list[tuple[int, int]]
```

Verified against **both mypy and pyright** in `tests/typing/`, run from `pytest`. See [§Type-level tests](#-type-level-tests) for how that is done and what is deliberately left untyped.

### 1b. Or use real stdlib dataclasses

An earlier version of this README claimed the two *couldn't* be combined at all. That was wrong, and [`sqlom/dataclass_model.py`](sqlom/dataclass_model.py) is the working counterexample. The `__slots__`-vs-class-variable collision only bites because both names live on **the class**. Attribute lookup on a class consults `type(cls).__mro__` first, and a **data descriptor found on the metaclass wins over the class's own entry** — so putting the column descriptors on a per-model metaclass gives you both halves:

```python
from sqlom import model

@model
class User:
    __tablename__ = "users"

    id: int
    name: str
    email: str
    is_active: bool
```

```python
User.id                 # -> ColumnExpr  (metaclass data descriptor wins)
User.id > 100           # -> Condition, for the query builder
user.id                 # -> 1           (plain slot read)
dataclasses.is_dataclass(User)   # True
dataclasses.asdict(user)         # works; so do replace(), ==, repr(), match
```

`@dataclass(slots=True)` rebuilds the class via `cls.__class__(...)`, which preserves a custom metaclass, so the two compose cleanly. Both styles cost the same (72 B/object, and see the table below).

⚠️ **If you use this style, pass `sqlom.DATACLASS_DUMP_OPTION` to `orjson.dumps`.** orjson recognizes dataclasses natively and will silently *ignore* your `default=` hook for them — and its native path is slow for slotted classes ([details](docs/FINDINGS.md#the-orjson-dataclass-trap)). That flag (`orjson.OPT_PASSTHROUGH_DATACLASS`) routes them back to sqlom's compiled hook and is worth ~30% end-to-end.

### 2. Query & Serialization (FastAPI)

```python
from fastapi import FastAPI
from fastapi.responses import Response
import orjson
from sqlom import Query, DatabaseEngine

from myapp.models import User   # your model, declared as in step 1 above
                                # (sqlom exports framework primitives only)

app = FastAPI()
db = DatabaseEngine(dsn="postgresql://user:pass@localhost/db")

@app.on_event("startup")
async def startup():
    await db.connect()

@app.on_event("shutdown")
async def shutdown():
    # Closes the pool and clears the reference. Skipping this leaks connections
    # against the server on reload.
    await db.close()

@app.get("/users")
async def get_users():
    query = (
        Query(User)
        .where(User.is_active == True)
        .where(User.id > 100)
        .limit(100)
    )

    users: list[User] = await db.fetch_all(query)

    # ModelMeta models aren't stdlib dataclasses, so orjson needs a hook.
    # compile_json_default(User) generates a straight-line dict literal for
    # this model; it's cached on the class as User.__json_default__.
    json_bytes = orjson.dumps(users, default=User.__json_default__)

    return Response(content=json_bytes, media_type="application/json")
```

### 3. Joins and selecting more than one model

`Query` takes several entities, like SQLAlchemy's `select(User, Post)`, and rows come back as tuples in select order:

```python
rows = await db.fetch_all(
    Query(User, Post)
    .join(Post, Post.user_id == User.id)
    .where(User.is_active == True)
    .order_by(Post.created_at, descending=True)
    .limit(20)
)

for user, post in rows:
    ...
```

The ON clause is an ordinary column comparison (`Post.user_id == User.id`). It binds no parameter, so placeholder numbering is unaffected — a `WHERE` after a join still starts at `$1`.

**`outer_join` gives `None` for a missing match**, not an object with every field set to `None`:

```python
rows = await db.fetch_all(
    Query(User, Post).outer_join(Post, Post.user_id == User.id)
)
for user, post in rows:
    if post is None:       # this user has no posts
        ...
```

You can also select individual columns, and join purely to filter:

```python
Query(User, Post.title).join(Post, Post.user_id == User.id)   # -> (User, str)
Query(User).join(Post, Post.user_id == User.id) \
           .where(Post.title == "x")                          # -> [User]
```

That last shape returns plain `User` instances, not 1-tuples: a join changes the SQL, not the row shape, so it reuses the same compiled hydrator as a plain select.

Once a join is present, every column is rendered table-qualified (`users.id`), because `id` would otherwise be ambiguous. A single-table query still emits bare names, so its SQL is unchanged.

**Aliases and self-joins.** Once the same table appears twice, the model class no longer identifies which side a column came from, so alias one of them. Columns reached off the alias carry the alias as their source, all the way through rendering:

```python
from sqlom import Alias

mgr = Alias(Employee, "mgr")
rows = await db.fetch_all(
    Query(Employee, mgr)
    .join(mgr, Employee.manager_id == mgr.id)
    .where(mgr.active == True)
)
# SELECT employees.id, ..., mgr.id, ... FROM employees
#   JOIN employees AS mgr ON employees.manager_id = mgr.id WHERE mgr.active = $1
```

**All four join kinds**, and the part that matters is which side can be `None`:

| | keyword | nullable side |
|---|---|---|
| `join` | `JOIN` | neither |
| `outer_join` / `left_join` | `LEFT OUTER JOIN` | the joined table |
| `right_join` | `RIGHT OUTER JOIN` | **everything already in the query, including the primary entity** |
| `full_join` | `FULL OUTER JOIN` | both |

So `Query(User, Post).right_join(Post, ...)` can yield `(None, post)`. Nullability is computed from the join graph rather than assumed, and it only ever grows: an over-marked entity costs a few redundant NULL checks per row, while an under-marked one hands back an object whose every field is `None` as though it were data.

### 4. OR, AND, NOT

`where()` AND-s its arguments — several arguments or several calls are the same thing. Anything else is explicit:

```python
from sqlom import and_, not_, or_

Query(User).where(
    or_(User.name == "ada",
        and_(User.active == True, User.id.in_([1, 2, 3])))
).where(not_(User.email.is_null()))
```

Operators work too — `(a) | (b)`, `(a) & (b)`, `~(a)` — but **parenthesise every operand**. Python binds `|` and `&` tighter than the comparison operators, so `User.id > 1 | User.id < 9` parses as `User.id > (1 | User.id) < 9`. Same trap as SQLAlchemy, same reason.

Groups always render their own brackets, since `AND` binds tighter than `OR` in SQL and an unbracketed nested clause silently changes meaning. Repeated `where()` calls do *not* add an outer pair, so `WHERE a AND b` stays exactly that.

`in_` / `not_in` take a sequence or a subquery. An **empty** sequence renders as `FALSE` (or `TRUE` for `not_in`) rather than the `IN ()` that Postgres rejects — an empty collection is an ordinary thing for calling code to arrive at.

### 5. GROUP BY, aggregates, subqueries

```python
from sqlom import avg, count, exists, max_, min_, sum_

# aggregate per group, filtered after grouping
Query(Post.user_id, count().label("posts"), avg(Post.score))
    .group_by(Post.user_id)
    .having(count() > 5)
    .order_by(count(), descending=True)

# correlated EXISTS. correlate() is required, and deliberately so: guessing
# which outer reference is a correlation and which is a typo is how a mistake
# becomes a cross join.
Query(User).where(
    ~exists(Query(Post.id).correlate(User).where(Post.user_id == User.id))
)

# IN, and a scalar subquery as a value
Query(User).where(User.id.in_(Query(Post.user_id).where(Post.score > 10)))
Query(Post).where(Post.score > Query(avg(Post.score)).scalar_subquery())

# a derived table in FROM, joined like any other source
busy = Query(Post.user_id, count().label("n")).group_by(Post.user_id).subquery("busy")
Query(User, busy.n).join(busy, busy.user_id == User.id).where(busy.n > 5)
```

`count()` is `count(*)`; `count(col)`, `count(col, distinct=True)`, `sum_`, `avg`, `min_`, `max_` take a column. Only `count` is typed (as `int`) — `avg` of an integer is `numeric` in Postgres and `sum` is `bigint`, so guessing a Python type would pick a converter and corrupt the value. `.label()` names an expression, which is also how a subquery exposes it.

Also here: `distinct()`, `offset()`.

Nothing checks that every non-aggregated selected column is grouped. That is the database's job and it produces a clear error; duplicating the rule here would only add a second place to be wrong.

### 6. Expressions: arithmetic, functions, CASE, windows

Anything that produces a value can go in a select list, a predicate, `GROUP BY`, `ORDER BY` or an `UPDATE ... SET`:

```python
from sqlom import case, func, row_number, sum_

Query(Post.id, Post.score * 2, Post.title.concat(" (draft)"))
Query(Post).where(Post.score * 2 > 100)
Query(func.lower(Post.title), func.coalesce(Post.score, 0))

Query(Post.id, case((Post.score > 100, "hot"), (Post.score > 10, "warm"), else_="cold"))

Query(Post.user_id, Post.score,
      row_number().over(partition_by=Post.user_id, order_by=(Post.score, "DESC")))
Query(Post.id, sum_(Post.score).over(order_by=Post.id,
                                     frame="ROWS UNBOUNDED PRECEDING"))
```

`+ - * / %` and unary `-` keep the operand's type, so `Post.score * 2` still compares against ints. **String joining is `.concat()`, not `+`** — Postgres has no `+` for text, so a `+` that type-checked would produce SQL the server rejects. `.operate("#>>", x)` reaches an operator this library doesn't wrap.

`func.anything(...)` calls any SQL function; `sql_function("lower", x, py_type=str)` does the same with a declared result type. Window helpers: `row_number`, `rank`, `dense_rank`, `lag`, `lead`, `first_value`, `last_value`, `ntile`, and `.over()` on any aggregate. A windowed aggregate needs no `GROUP BY` — it produces a value per row.

**Three places accept a SQL fragment rather than a value, and all three are validated** — everything else in the builder binds parameters and never interpolates:

| | accepted |
|---|---|
| function names | an identifier: `[A-Za-z_][A-Za-z0-9_]*` |
| `.operate()` operators | 1–4 operator characters |
| window `frame=` | letters, digits, underscores and spaces |

`count()` has three forms: `count()` is `count(*)`, `count(col)` counts non-nulls, and **`count(Model)`** is `count(*)` that also supplies the `FROM` table — so `Query(count(Post))` works where `Query(count())` has no table to select from and says so.

### 7. Set operations

```python
Query(User).where(User.active == True) \
    .union(Query(User).where(User.id < 100)) \
    .order_by("id").limit(20)
```

`union`, `union_all`, `intersect`, `intersect_all`, `except_`, `except_all`. Rows hydrate exactly as for a single select, since a compound presents the same interface to the engine. Chaining the *same* operator extends the compound; a *different* one nests, because `UNION` and `EXCEPT` don't associate the way flattening would imply.

Operand column counts are checked when you build it — a mismatch is otherwise a confusing server error. `ORDER BY` on a compound references output column *names* (a compound has no single table to qualify against), and applies to the whole result.

### 8. Writes: INSERT, UPDATE, DELETE, RETURNING

```python
from sqlom import Delete, Insert, Update

await db.execute(Insert(User).values(name="ada", email="a@b.c"))

# Bulk: one statement, one round trip
await db.execute(Insert(User).values([{"name": "a"}, {"name": "b"}]))

# RETURNING makes it a read, so it goes through fetch_all and hydrates
ids = await db.fetch_all(Insert(User).values([...]).returning(User.id))
rows = await db.fetch_all(Insert(User).values(name="a").returning(User))  # [User]

# Read-modify-write in one statement
await db.execute(Update(Post).set(score=Post.score + 1).where(Post.id == 1))

gone = await db.fetch_all(Delete(User).where(User.id == 1).returning(User))
```

`execute()` reports what the driver reports — asyncpg's status tag (`"INSERT 0 3"`), psycopg's rowcount — rather than something normalised across them, because normalising would hide the difference between "no rows matched" and "the statement did nothing".

Three deliberate frictions:

- **`Delete` with no `where()` raises.** Emptying a table is fine, but it has to say `.all_rows()`. A forgotten `where()` is not a mistake worth making easy.
- **`execute()` refuses a statement with `RETURNING`** and `fetch_all()` refuses one without. Either mismatch otherwise returns `[]`, which reads as "nothing matched".
- **Bulk inserts are bounded.** `values([...])` renders one multi-row `VALUES`, which is a single round trip and — unlike `executemany` on asyncpg — supports `RETURNING`. The cost is a parameter per column per row, so it refuses to exceed the statement parameter limit rather than letting the server reject the batch. `max_rows_per_statement(Model)` gives the ceiling.

Writes outside `transaction()` commit on their own: a lone statement runs in autocommit on both pools. Group them in a transaction when they need to be atomic. DML built here still can't dirty a connection (no `SET`, no temp table, no `LISTEN`), so the conditional session reset still applies.

### 9. Upserts: ON CONFLICT

```python
from sqlom import Insert, excluded

# Skip the row if it would violate a unique index
await db.execute(Insert(User).values(email="a@b.c").on_conflict_do_nothing(User.email))

# Overwrite the stored row with the incoming one
await db.execute(
    Insert(User).values(email="a@b.c", hits=1)
    .on_conflict_do_update(User.email, set_={"hits": excluded(User.hits)})
)

# Accumulate: a bare column is the STORED row, excluded() is the incoming one
await db.execute(
    Insert(Counter).values(key="hits", n=1)
    .on_conflict_do_update(Counter.key, set_={"n": Counter.n + excluded(Counter.n)})
)

# Conditional: leave the row alone when the condition doesn't hold
keep_max = Insert(Gauge).values(key="k", v=5).on_conflict_do_update(
    Gauge.key, set_={"v": excluded(Gauge.v)}, where=Gauge.v < excluded(Gauge.v)
)

# Bulk upsert is still one statement, and RETURNING still works
rows = await db.fetch_all(
    Insert(User).values([{...}, {...}])
    .on_conflict_do_update(User.email, set_={"hits": excluded(User.hits)})
    .returning(User.id, User.hits)
)
```

`excluded(col)` is the row that failed to insert — SQLAlchemy spells it `stmt.excluded.email`, which needs the statement in a variable first; a free function composes inline. Getting `hits = excluded.hits` and `hits = hits + excluded.hits` the wrong way round produces valid SQL with the wrong answer, so the tests assert stored values rather than generated text.

Both forms take either the conflicting columns or `constraint="name"` for a named unique constraint (Postgres only — sqlite has no `ON CONSTRAINT`). `on_conflict_do_nothing()` with no arguments swallows *any* unique violation on the table; `on_conflict_do_update` requires a target, because there is no way to say "the row that lost" without knowing which index it lost on.

One non-obvious portability point, found by running these against a real server rather than only sqlite: inside `DO UPDATE`, references to the target table are **qualified** (`SET n = counter.n + excluded.n`). Postgres has both the table and `excluded` in scope there, so a bare `n` on the right is an `AmbiguousColumnError`; sqlite accepts the bare form, so the sqlite tests alone would have shipped SQL Postgres rejects. The assignment *target* stays unqualified, since Postgres rejects `SET t.col = ...`.

`DO NOTHING` plus `RETURNING` is how you find out whether the row was new: no row comes back when the conflict fires.

### 10. CTEs, including recursive

```python
from sqlom import Query, count, recursive_cte

busy = Query(Post.user_id, count(Post.id).label("n")).group_by(Post.user_id).cte("busy")

# Used exactly like a table: it renders as its name, and whichever query
# references it hoists the definition into its own WITH clause
Query(User, busy.n).join(busy, busy.user_id == User.id).where(busy.n > 5)
Query(busy.user_id, busy.n).where(busy.n > 5)          # or as the FROM source

# Recursive: walk a tree
tree = recursive_cte(
    "tree",
    Query(Node.id, Node.parent_id).where(Node.parent_id == None),
    lambda cte: Query(Node.id, Node.parent_id).join(cte, Node.parent_id == cte.id),
)
await db.fetch_all(Query(tree.id, tree.parent_id).order_by("id"))
```

**There is nothing to register.** References are collected from wherever they appear — `FROM`, `JOIN`, an `ON` clause, a `WHERE` subquery, an `EXISTS`, a `CASE` arm, another CTE's body — and emitted once, in dependency order, in a single `WITH` clause owned by the outermost statement. That collection is a reflective walk over the node graph rather than a visitor per node type, precisely so that an expression type added later cannot silently drop its CTEs and produce SQL that fails at the server with *relation does not exist*. `with_(cte)` forces one in for the case where the reference is somewhere sqlom cannot see (raw SQL via `sql_function`).

`recursive_cte` takes the recursive term as a **callable** because that term has to reference a CTE that does not exist until its own column names are known — and those come from the base query. A lambda resolves the ordering without a two-phase API. `UNION ALL` by default; pass `union_all=False` for `UNION`, which de-duplicates and so terminates on cycles.

**A cycle under `UNION ALL` is an infinite loop inside the database**, not an error sqlom can catch — it has no idea whether your data is acyclic. This is not theoretical: the first version of one of these tests pointed a row at itself and hung Postgres until the backend was terminated by hand. Both behaviours have tests now, including that `union_all=False` terminates on the same data.

Two things a recursive CTE forced into the design, both worth knowing about:

- **A CTE must name its output columns**, so an unlabelled aggregate is refused at build time: Postgres calls `count(id)` "count" and sqlite calls it `"count(id)"`, and exposing a guessed name would render a reference to a column that does not exist. `.label("n")` is the fix. (This was a latent bug in `Subquery`, which had the same hole and now shares the same check.)
- **The reference walk is over a cyclic graph.** A recursive CTE's body refers to the CTE itself, so the walk carries an identity guard; without it, building the SQL is a `RecursionError`. Both facts have regression tests.

A `WITH` clause in front of `INSERT`/`UPDATE`/`DELETE` works too. What is *not* supported is the data-modifying CTE (`WITH moved AS (DELETE ... RETURNING *) INSERT INTO ... SELECT * FROM moved`): `cte()` takes a select, and there is no `INSERT ... SELECT` builder to consume one.

### 11. UPDATE ... FROM and DELETE ... USING

```python
# Copy across a join in one statement
await db.execute(
    Update(Post).set(author=User.name).from_(User).where(User.id == Post.user_id)
)

# Delete by a condition on another table
await db.execute(
    Delete(Post).using(User).where(User.id == Post.user_id, User.banned == True)
)
```

**There is no `ON` clause** — the join condition goes in `where()`, which is how SQL spells it, and which is why a missing condition is a silent cross product rather than a syntax error. Both builders refuse to render without one; `all_rows()` does not license it either, because `USING other` with no condition deletes each target row once per row of `other`, which is not what "delete everything" asked for.

Once a second table is in play, every column *reference* becomes qualified — in `set()` values, in `where()`, and in `returning()`, which may name the other table's columns. `SET` targets stay unqualified, because Postgres rejects `SET t.col = ...`. A single-table `UPDATE`/`DELETE` is untouched and renders exactly as before.

Builder order does not matter: `from_()` may come before or after `set()` and `where()`. That is why column references are validated when the statement renders rather than in the method that received them — still client-side, before anything reaches the server, just not at the exact call that wrote it.

Portability: `UPDATE ... FROM` works on Postgres and on sqlite 3.33+ (2020). **`DELETE ... USING` is Postgres-only** — sqlite has no such form, and the portable spelling is `Delete(Post).where(Post.user_id.in_(Query(User.id).where(...)))`, which is tested as equivalent.

### 12. What is checked for you

Each of these otherwise produces plausible wrong results rather than an error:

| you write | you get |
|---|---|
| `Query(User).where(Post.title == "x")` without a join | `ValueError` — Post is not in the query |
| `.join(Post, Post.title == "x")` | `ValueError` — the ON clause links no two tables, so it is a cross join |
| `.join(Tag, Post.user_id == User.id)` | `ValueError` — the ON clause never mentions Tag |
| `.join(User, ...)` — unaliased self-join | `ValueError`, naming `Alias` as the fix |
| a join whose alias collides with a table name | `ValueError` — both would render as the same prefix |
| `.order_by(Post.id)` / `.group_by(Post.id)` without a join | `ValueError` — same reason as `where` |
| a correlated subquery without `.correlate()` | `ValueError` — indistinguishable from a typo |
| `Delete(User)` with no `where()` | `ValueError` — say `.all_rows()` if that is the intent |
| `execute()` on a statement with `RETURNING`, or `fetch_all()` on one without | `ValueError` — either mismatch silently returns `[]` |
| a bulk insert past the parameter limit | `ValueError` naming the row ceiling, instead of a server error |
| `count(distinct=True)` or `count(Model, distinct=True)` | `ValueError` — `count(DISTINCT *)` is a syntax error |
| a function name, operator or window frame that is not one | `ValueError` — the three places a fragment is accepted are all validated |
| `Query(count())` with nothing else | `TypeError` — no table to select from |
| `Query(subquery)` | `TypeError` — no model to hydrate into; select its columns |
| `Update(...).from_(T)` or `Delete(...).using(T)` with no `where()` | `ValueError` — that is a cross product, not a join |
| a `cte()` or `subquery()` selecting an unlabelled aggregate | `ValueError` — SQL gives it no usable name; add `.label()` |
| `on_conflict_do_update()` with no conflict target | `ValueError` — the database cannot tell which row lost |
| `excluded(OtherModel.col)` | `ValueError` — `excluded` is the target row, checked by source identity not by name |
| `constraint=` that is not a plain identifier | `ValueError` — it is rendered unquoted, so it is not a place for arbitrary text |
| two DML sources rendering to the same qualifier | `ValueError` — every column reference would be ambiguous |

The ON check looks for a comparison linking the joined table to one already present, *anywhere* in the clause — so `and_(Post.user_id == User.id, Post.published == True)` is fine, while `and_(Post.published == True, Post.score > 1)` is not. Either side may be an expression rather than a bare column: `Node.id == tree.parent_id + 1` links the two tables just as much as a plain equality does.

**Still not supported:** relationship declarations, so no lazy loading and no `selectinload` equivalent — you write the join. `INSERT ... SELECT`, and therefore the data-modifying CTE built on it. Schema management: there is no DDL, no migrations, and no reflection. `to_json_sql()` handles a single-model query with no joins or grouping and raises otherwise, rather than guessing at a nested shape.

There is no de-duplication: an inner join to a one-to-many yields the left row once per match, as SQL does. SQLAlchemy's ORM collapses those via its identity map — which is precisely the machinery sqlom skips to be fast, so this is a real behavioural difference and not an oversight.

### 13. Transactions

`engine.fetch_all()` takes a pooled connection per call and hands it straight back, which is right for a one-shot read and useless for anything atomic — two calls run on two connections. `engine.transaction()` pins one connection for the block, commits on clean exit and rolls back on any exception:

```python
async with db.transaction() as tx:
    await tx.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2", 100, payer)
    await tx.execute("UPDATE accounts SET balance = balance + $1 WHERE id = $2", 100, payee)

    # Query objects work here too, on the transaction's connection, reusing the
    # same compiled hydrator — so a read inside a transaction costs what a read
    # outside it costs.
    rows = await tx.fetch_all(Query(Account).where(Account.id == payer))
```

Nesting gives savepoints, so an expected inner failure doesn't discard the outer work:

```python
async with db.transaction() as tx:
    await tx.execute(...)                    # kept
    try:
        async with tx.transaction() as sp:
            await sp.execute(...)            # rolled back to the savepoint
    except ExpectedConflict:
        pass
```

`isolation=` takes `"read_committed"`, `"repeatable_read"` or `"serializable"`, plus `readonly=` and `deferrable=`. Both engines support the same API; `PsycopgEngine` translates to psycopg's connection-level setters.

Two things worth knowing:

- **A transaction is slower per statement than a plain read, deliberately.** `DatabaseEngine`'s conditional reset skips the pool's `RESET ALL` round trip on the strength of an invariant — `fetch_all` only ever emits plain parameterised SELECTs, which cannot leave session state behind. A transaction body can run `SET`, `LISTEN`, `CREATE TEMP TABLE` or take advisory locks, so `transaction()` routes through `acquire()` and marks the connection dirty, and its release pays the full reset. That is verified, not assumed: a `SET statement_timeout` inside a block does not reach the next borrower. See [§12](docs/BENCHMARKS.md#12-fixing-the-pool-reset-without-changing-behaviour) for what the reset costs.
- **Calling `engine.fetch_all()` *inside* a transaction raises.** It would take a different pooled connection, so it would miss the transaction's uncommitted writes and not roll back with it — a bug that returns plausible data. Use `tx.fetch_all()`. The check is a contextvar read on the hot path, measured at +0.11% (inside the noise).

Semantics are covered by [`tests/test_transactions_pg.py`](tests/test_transactions_pg.py) on **both** engines: commit, rollback, read-your-writes, invisibility to other connections, savepoint depth and nesting, the guard, isolation levels, and the session-reset invariant.

### 14. DB-side JSON (not the focus yet)

`Query.to_json_sql` / `DatabaseEngine.fetch_json` push row shaping and JSON encoding into the database and hand back response-ready bytes, skipping Python objects entirely. It works and it benchmarks well, but it's a different product than an object mapper — **treat it as experimental and out of scope for now.** The object path above is the one being optimized.

```python
# experimental
return Response(content=await db.fetch_json(query), media_type="application/json")
```

---

## 🏗️ Architecture Under the Hood

Two paths, depending on whether you need Python objects at all:

```
                                 ┌──(A) object path ────────────────────────────┐
[ PostgreSQL ] ──(asyncpg)──> [ C-tuples ] ──(compiled hydrator)──> [ Slotted object ]
                                                                            │
                                                                    (compiled hook)
                                                                            ▼
                                                                  [ Response (JSON) ]
                                 ┌──(B) json_agg path ──────────────────────────┐
[ PostgreSQL ] ──(json_agg in SQL)──> [ one JSON string ] ──────> [ Response (JSON) ]
```

1. **Descriptor expressions.** `User.id > 100` evaluates a descriptor at class scope, returning a `ColumnExpr` node rather than doing a Python-level comparison — this gives the query builder a queryable AST without needing SQLAlchemy-style instrumentation.
2. **Compiled hydration, and it is entirely positional.** A model's column layout is fixed and known once, so sqlom generates a specialized `rows -> [instance]` function per model (inspect it via `fn.__source__`). Field stores are plain attribute assignments so CPython 3.11's specializing interpreter can quicken them to `STORE_ATTR_SLOT`, and rows are read by **tuple unpacking** — `for f0, f1, f2, f3, in rows:` — with no column names in the generated code at all.

   That is available only because sqlom writes the `SELECT` list, so it knows every column's ordinal at codegen time; there is no `SELECT *` in the builder, which is what makes positional safe rather than reckless. Measured, positional access beats key access by **1.97x** (asyncpg `Record`) to **4.76x** (`sqlite3.Row`), and building a dict per row costs 5–7x and dominates both — see [`row_access.txt`](benchmarks/results/row_access.txt). It is also why a two-model join is a non-event here: two `id` columns collide by *name*, never by ordinal, which is exactly what broke SQLAlchemy Core's `.mappings()` idiom in [correction 8](docs/METHODOLOGY.md#8-charging-one-contender-for-a-workaround-the-others-never-needed-core-ratios-inflated-16-26x).
3. **Slotted storage.** Instances use `__slots__`, so attribute storage is a fixed-size array rather than a `__dict__` — 72 vs 113 bytes per object here. Note the tradeoff: this is also what forces orjson off its native dataclass fast path (see [the orjson dataclass trap](docs/FINDINGS.md#the-orjson-dataclass-trap)).
4. **Path (B) skips 2 and 3 entirely** by shaping in SQL. Implemented but parked; path (A) is the focus.

None of this makes the pipeline "zero-copy" — data still moves from the C-level tuple into Python object storage into JSON bytes. The claim to make is "fewer intermediate Python-level allocations than an ORM identity-map path," not "no copying happens." And per the profile, path (A)'s remaining cost is dominated by the driver materializing Python values, not by sqlom.

---

## 📊 Performance

Full results in **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**, engineering conclusions in
**[docs/FINDINGS.md](docs/FINDINGS.md)**, and — please — **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**,
which logs five published claims that turned out to be wrong and why.

### Bottom line: 3.3x the ORM, 1.6x Core, measured the strictest way

Same driver on both sides (psycopg3 async), **both libraries at their default pool
behaviour** — nothing tuned anywhere — through a real FastAPI + uvicorn stack on one
core. Verified with `log_statement=all` that both send the same three statements per
request, and all endpoints return byte-identical 7701-byte payloads.

| endpoint (FastAPI, one core) | rps | p50 | p99 |
|---|---|---|---|
| `/noop` — framework floor, no database | 8419 | 0.91 ms | 1.56 ms |
| **sqlom** | **1319** | **5.93 ms** | **9.21 ms** |
| SQLAlchemy Core | 825 | 9.19 ms | 15.61 ms |
| SQLAlchemy ORM | 396 | 16.09 ms | 77.57 ms |

**3.33x the ORM, 1.57x Core.** The ratio depends heavily on what you allow yourself
to change:

| configuration | vs Core | vs ORM |
|---|---|---|
| psycopg, both default, via FastAPI | **1.57x** | **3.33x** |
| psycopg, both default, data layer | 2.01x | 4.28x |
| asyncpg, both tuned, via FastAPI | 1.79x | 4.80x |
| asyncpg, both tuned, data layer | 2.51x | 7.18x |
| sqlite, single table, no transport | 1.49x | 5.22x |
| sqlite, **two models across a join** | 1.17x | 4.05x |

Two independent effects, each worth about a third, compounding: sqlom's advantage
partly *was* asyncpg plus a skipped session reset (7.18x → 4.28x on the same driver at
defaults), and the web layer adds ~119 µs/request that every route pays equally
(4.28x → 3.33x). **Quote 3.3x**; the 7.18x needed asyncpg *and* a behavioural change.

> ⚠️ **Every Core figure above was corrected downward** after this table was first
> published. The old ones (2.07x / 2.67x / 2.73x / 4.00x / 3.87x) were inflated
> 1.6–2.6x because the harness shaped Core's rows with `.mappings()`, whose keys are
> `quoted_name` and need a per-key `str()` cast that orjson forces — and that cast was
> 62% of Core's whole time on sqlite. Zipping the flat row against names captured once
> is equally idiomatic and produces identical bytes. Found only by adding the join
> benchmark, where `.mappings()` is unusable because both tables have an `id`; Core
> then came out *closer* to sqlom on more work, which is impossible and was the tip-off.
> The ORM figures are unaffected (that path uses `getattr`). Full write-up:
> [METHODOLOGY correction 8](docs/METHODOLOGY.md#8-charging-one-contender-for-a-workaround-the-others-never-needed-core-ratios-inflated-16-26x).

**On a two-model join the Core advantage nearly disappears** — 1.17x at 1000 rows,
1.32x at 100 — because both libraries then do almost the same cheap thing, and sqlom
additionally builds two Python objects per row. The ORM advantage largely survives
(4.05x). Without the orjson `default=` hook, shaping the same objects with `getattr`
is *slower* than Core on a join. See
[`sqlite_join.txt`](benchmarks/results/sqlite_join.txt).

What holds across every configuration is the tail: sqlom's p99 is consistently ~8x
tighter than the ORM's (9.2 ms vs 77.6 ms here). Details, including the statement
counts and why SQLAlchemy's own tuning is worth only 1.10-1.15x, in
[§13](docs/BENCHMARKS.md#13-bottom-line-sqlom-vs-sqlalchemy-both-tuned-with-and-without-fastapi)
and [§14](docs/BENCHMARKS.md#14-the-strictest-comparison-same-driver-both-libraries-at-their-defaults).

Those figures come from a load generator written for this repo, so
[§15](docs/BENCHMARKS.md#15-auditing-the-load-generator-itself) audits it: socket
counts read from `/proc/net/tcp`, Little's Law, and a re-run under **locust**, which
reproduces sqlom's throughput to 0.1% and brackets both ratios within 7% (2.13x Core,
3.29x ORM). Locust cannot measure the `/noop` floor — on one core it saturates first,
and Little's Law catches it — which is why the cheaper generator exists. That locust
run predates [correction 8](docs/METHODOLOGY.md#8-charging-one-contender-for-a-workaround-the-others-never-needed-core-ratios-inflated-16-26x),
so its **Core** figure carries the same inflation as the rest and is not re-run here;
it corroborates the generator and the ORM ratio, not the Core one.

### With transport removed (sqlite, single-threaded, 100 rows/req)

No event loop, no pool, no TLS — the mapper's own cost:

| approach | CPU ms/req | req/s (1 thread) | vs. ORM |
|---|---|---|---|
| **sqlom** | **0.100** | **9997** | **7.4x** |
| SQLAlchemy Core | 0.437 | 2286 | 1.70x |
| SQLAlchemy ORM | 0.742 | 1346 | 1.0x |

⚠️ The Core row uses the `.mappings()` idiom that
[correction 8](docs/METHODOLOGY.md#8-charging-one-contender-for-a-workaround-the-others-never-needed-core-ratios-inflated-16-26x)
found to be unfair, so **sqlom's margin over Core here is overstated** — at 1000 rows
the same correction took 3.87x down to 1.49x. This specific 100-row cell has not been
re-measured, and no number is invented for it; the ORM row is unaffected.

The lead survives removing transport, so it is not an artifact of sockets masking
differences. And this path is at its floor: object materialization is only **16%** of
the request (64% is sqlite3 creating Python values), so every micro-optimization tried
— cursor reuse, tuple-index bool, zero-callback dicts, `row_factory` — came in at
1.04x or worse. See [§8](docs/BENCHMARKS.md#8-what-is-left-in-the-sqlite-path-essentially-nothing).

### Latency: ~5.9x on a single request

sqlite micro-benchmark, 1000 rows/response, median of 5 trials, all approaches
asserted to emit byte-identical JSON *and* to be stable across repeated calls:

| approach | median | vs. ORM |
|---|---|---|
| sqlom compiled (per-row / batch) and `@model` + passthrough | 1.39–1.55 ms | **~5.9x** |
| `@model` dataclass, orjson native path | 1.90 ms | 4.8x |
| sqlom reflective (unoptimized) | 3.56 ms | 2.6x |
| SQLAlchemy 2.0 Core | 5.24 ms | 1.7x |
| SQLAlchemy 2.0 ORM | 9.13 ms | 1.0x |

⚠️ These replace an earlier version of this table, for two reasons — both in
[§1](docs/BENCHMARKS.md#1-sqlite-micro-benchmark-single-request-latency). The old
comparison timed SQLAlchemy's connection setup but not sqlom's, which overstated the
Core ratio by ~8%; and the measurement box became ~1.35x slower between runs, which
moved every absolute figure without changing the ranking.

⚠️ **The Core row is also inflated by
[correction 8](docs/METHODOLOGY.md#8-charging-one-contender-for-a-workaround-the-others-never-needed-core-ratios-inflated-16-26x).**
No corrected absolute is spliced in here, because the re-measurement ran on a
different box and mixing conditions in one table is
[correction 4](docs/METHODOLOGY.md#4-mixing-measurement-conditions-in-one-table).
Measured together in one isolated run today: sqlom's best **1.26 ms**, Core
positional **1.88 ms**, Core `.mappings()` **4.92 ms**, ORM **7.01 ms** — so the
honest reading of this row is **sqlom ≈1.5x Core**, and Core is nearly a tie with
`@model` + orjson native rather than 3x behind it. Raw:
[`core_idiom.txt`](benchmarks/results/core_idiom.txt).

The first row is three variants that are a **statistical tie** — their ordering
changes between runs, so they're grouped rather than ranked. Verified free of the
ordering bias that affects the Postgres suite (reverse-order and per-process runs
agree).

### Read this before believing the 6x

- **It is a core-count claim, not a latency claim.** A sqlom client is one asyncio
  loop under the GIL and saturates exactly one core (measured CPU utilization
  0.91–1.00, always). Scaling is by process and is linear (2 workers → 1.99x). The
  useful reading is *cores needed for a target throughput*: ~4,400 req/s takes 1 core
  with sqlom, roughly 6 with the async ORM.
- **The comparison is against SQLAlchemy's ORM, not against writing it yourself.**
  Hand-written asyncpg + `dict(record)` reaches 3877 rps vs sqlom's 3168 in the same
  configuration. sqlom costs **+10–25% CPU** over building no objects at all. The
  pitch is ergonomics at near-hand-written cost.
- **The client is the bottleneck here and Postgres is barely loaded.** That is why
  the mapper's CPU cost is visible. A query heavy enough to make the database the
  bottleneck would compress every ratio toward 1.0 — untested, and arguably the more
  common production shape.
- **The HTTP layer is real but narrow.** One uvicorn worker, no TLS, no middleware,
  no response validation, and a hand-built `Response` that bypasses
  `jsonable_encoder`. A route doing Pydantic validation would add cost to every
  contender equally and compress these ratios further. Multi-worker scaling through
  FastAPI is unmeasured.
- **Against Postgres, sqlom's generated code is only ~15% of client CPU** — 38% is the
  asyncio event loop, 19% the asyncpg fetch, 15% pool acquire/release. But that is a
  fact about *sockets*, not the mapper: profiled against in-process sqlite, transport
  turns out to be **53% of the Postgres cost** and sqlom's share rises to ~50%. Fix
  transport first for a remote DB; the mapper pays back directly for a local one.
- **The benchmark's loopback connection negotiates TLSv1.3**, which costs ~20% of
  client CPU. Ratios are unaffected (both sides pay it) but absolute throughput is
  understated: 5440 rps with `sslmode=disable` vs 4724 with it on.
- **Concurrency and uvloop pay exactly the idle fraction.** At c=1 the Postgres client
  is 0.64 utilized (a third of the core waiting on the socket), so concurrency is worth
  2.0-2.5x and uvloop 1.05-1.26x. On the in-process sqlite path utilization is already
  1.00 at c=1, so concurrency is worth **1.00x** and uvloop is noise — and reaching for
  `aiosqlite` to "make it async" *costs* 25-40%, because it uses a thread. uvloop is an
  I/O layer, not a faster asyncio. See
  [§10](docs/BENCHMARKS.md#10-asyncio-concurrency-and-uvloop-on-the-sqlite-path) and
  [§11](docs/BENCHMARKS.md#11-the-same-matrix-on-postgres-concurrency-and-uvloop-both-matter).
- **A Rust rewrite is the worst return on effort measured.** Creating a Python value
  costs ~109 ns and there are four per row (42% of a 100-row request) — any API
  returning objects with Python fields pays that regardless of implementation
  language, capping a native builder at **≤1.42x**. Tested empirically: `psqlpy`
  (Rust/tokio-postgres) constructing our slotted dataclasses *from Rust* runs at
  **0.57x** of asyncpg + sqlom's Python hydrator, once pool policy and TLS are
  controlled. See [§9](docs/BENCHMARKS.md#9-two-hypotheticals-a-native-object-builder-and-rust).
- **Most of the remaining throughput is outside the mapper.** asyncpg's pool runs
  `RESET ALL` as a *second round trip* on every release (2.01 queries sent per request,
  verified). `DatabaseEngine(conditional_reset=True)` — the default — recovers **1.23x
  of the available 1.24x without any behaviour change**, by resetting only connections
  that `acquire()` handed out raw. Moving the reset to acquire gains nothing, and
  batching it via psycopg3 pipelining is 2-4x *worse*. See
  [§6](docs/BENCHMARKS.md#6-acting-on-the-profile-24x-more-throughput-outside-the-mapper)
  and [§12](docs/BENCHMARKS.md#12-fixing-the-pool-reset-without-changing-behaviour).

### If you only ever emit JSON, use the database

`Query.to_json_sql` / `DatabaseEngine.fetch_json` push shaping and encoding into
Postgres and beat every object path by ~2.2x. Implemented but **parked** — it isn't
an object mapper. If that fits your endpoint, sqlom's object path is the wrong tool.

---

## 📜 License

MIT License. Free for open-source and commercial use.
