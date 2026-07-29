# ⚡ rowform: Zero-Overhead Async Data Layer for Python

`rowform` is a data access library for Python built for high-throughput HTTP services (FastAPI, Sanic, Granian). It pairs a **SQLAlchemy-Core-like, statically-typed query builder** with an async execution engine (`asyncpg` or `psycopg3`) that hydrates rows straight into `@dataclass(slots=True)`-style objects — no session, no identity map, no relationship/lazy-loading machinery. You write every join explicitly; in exchange, reads cost close to nothing beyond the driver itself.

> **Status:** early. The core is implemented and benchmarked against both sqlite and a live PostgreSQL 16 under concurrent load — see [Performance](#-performance). It has a 968-test pytest suite (SQL generation, joins, writes, transactions, static types) but is not packaged, not on PyPI, and has never run in production.

---

## ✨ Features

* **Compiled hydration:** a per-model `row -> object` function is code-generated once and reused for every row — plain attribute stores, no reflective `setattr()` loop, no dict per row.
* **Statically typed query builder:** `User.id` is `ColumnExpr[int]`, `user.id` is `int`, `await db.execute(select(User, Post))` is `list[tuple[User, Post]]`. Verified against **both mypy and pyright**.
* **Two schema styles:** a custom-metaclass model, or real stdlib `@dataclass(slots=True)` models that still support `User.id > 100`.
* **A real query builder:** multi-model selects, all four join kinds, aliases and self-joins, `or_`/`and_`/`not_`, `in_`/`exists`/scalar subqueries, `GROUP BY`/`HAVING`, derived tables, set operations, window functions, `CASE`, arithmetic and SQL functions, CTEs (including recursive), `text()`/`literal_column()`/`bindparam()`.
* **Multi-dialect:** a small `Dialect` core with Postgres/sqlite overrides — `IS DISTINCT FROM`, `FOR UPDATE`, `DELETE ... USING` and `ON CONFLICT ... ON CONSTRAINT` are validated per dialect rather than just documented.
* **Writes:** `Insert`/`Update`/`Delete` with `RETURNING`, bulk insert in one statement, expression assignments, `ON CONFLICT` upserts with `excluded()`, and `UPDATE ... FROM` / `DELETE ... USING` across tables.
* **Transactions and savepoints:** `async with db.transaction() as tx:` on both engines, with nesting as savepoints and isolation levels.
* **Async-first:** native `asyncpg` pool integration, ~3.3x SQLAlchemy's async ORM under concurrent load through a real FastAPI stack — see [Performance](#-performance).
* **Extensively guarded:** an unjoined table in `where()`, a cross join with no linking condition, an unlabelled aggregate in a CTE, a bulk insert past the parameter limit — all raise client-side with a clear message instead of producing plausible-looking wrong SQL. See [What's checked for you](#whats-checked-for-you).

---

## 📖 Usage

### If you already know SQLAlchemy

The query-building surface is deliberately named after SQLAlchemy Core, including the two-letter import and the single `execute()` entry point:

```python
import rowform as rf

q = rf.select(User).where(User.active == True)
results = await conn.execute(q)   # list[User] — same call for reads and writes
```

`conn` is a `DatabaseEngine`/`PsycopgEngine`, or a `Transaction` from `db.transaction()`. `execute()` hydrates and returns rows for a `select()`/`Query` or a RETURNING `insert()`/`update()`/`delete()` — same as `fetch_all()`, still available if you'd rather name the row-returning case explicitly. Anything else (a write with no `returning()`) runs and returns the driver's own status report instead.

```python
from rowform import select, insert, update, delete, and_, or_, not_, func

select(User).where(User.active == True).order_by(User.id.desc())
insert(User).values(name="ada")
update(User).values(hits=User.hits + 1).where(User.id == 1)
delete(User).where(User.id == 1)

Query(User, Post).join(Post, Post.user_id == User.id, isouter=True)  # same as .outerjoin(...)
User.email.is_(None)          # IS NULL
User.name.like("a%")          # ilike() is Postgres-only, validated per dialect
Post.score.between(10, 100)

select(func.avg(Post.score)).scalar_subquery()   # usable as a value anywhere
select(User).filter_by(name="ada")               # -> where(User.name == "ada")
select(User).with_for_update(read=True, nowait=True, of=User)

tuple_(User.id, User.name) == (1, "ada")          # row-value comparison
```

`select`/`insert`/`update`/`delete` are plain function aliases for `Query`/`Insert`/`Update`/`Delete` — `Query(User)` and `select(User)` are the exact same object. What doesn't carry over: there is no `Table`/`MetaData`/reflection/DDL layer — columns come from a model class, and **every selected entity's source is checked against the join graph**: `Query(User, Post)` with no `.join()` raises rather than silently rendering a cross join.

### Defining models

Two styles, both statically typed end to end and verified against mypy and pyright:

```python
from rowform import Column, ModelMeta

class User(metaclass=ModelMeta):
    __tablename__ = "users"
    id = Column(int)
    name = Column(str)
    email = Column(str)
    is_active = Column(bool)
```

```python
from rowform import model

@model
class User:
    __tablename__ = "users"
    id: int
    name: str
    email: str
    is_active: bool
```

The first puts columns on a metaclass-generated descriptor; the second is a real stdlib `@dataclass(slots=True)` (`dataclasses.asdict`, `replace()`, `==` all work), made possible because a metaclass data descriptor wins over a same-named class variable in attribute lookup. Both give you `User.id` as `ColumnExpr[int]` and `user.id` as `int`. See [Architecture](#-architecture) for how. If you use the dataclass style, pass `rowform.DATACLASS_DUMP_OPTION` to `orjson.dumps` — orjson otherwise recognizes the dataclass natively and silently ignores your serialization hook.

### Querying, joins, aliases

```python
from rowform import DatabaseEngine

db = DatabaseEngine(dsn="postgresql://user:pass@localhost/db")
await db.connect()

rows = await db.execute(
    select(User, Post)
    .join(Post, Post.user_id == User.id)
    .where(User.is_active == True)
    .order_by(Post.created_at, descending=True)
    .limit(20)
)
for user, post in rows:
    ...
```

`outer_join`/`left_join`, `right_join`, and `full_join` cover the other three kinds — the side that can be unmatched comes back `None`, computed from the join graph rather than assumed. Once the same table appears twice, alias one side:

```python
from rowform import Alias

mgr = Alias(Employee, "mgr")
select(Employee, mgr).join(mgr, Employee.manager_id == mgr.id).where(mgr.active == True)
```

### Filtering: AND / OR / NOT

```python
from rowform import and_, not_, or_

select(User).where(
    or_(User.name == "ada", and_(User.active == True, User.id.in_([1, 2, 3])))
).where(not_(User.email.is_null()))
```

`where()` AND-s its arguments; operators (`|`, `&`, `~`) work too but **parenthesise every operand** — Python binds them tighter than comparisons, same trap as SQLAlchemy. An empty `in_([])` renders `FALSE` rather than the `IN ()` Postgres rejects.

### Aggregates, subqueries, CTEs

```python
from rowform import avg, count, exists, recursive_cte

select(Post.user_id, count().label("posts"), avg(Post.score)) \
    .group_by(Post.user_id).having(count() > 5)

select(User).where(~exists(select(Post.id).correlate(User).where(Post.user_id == User.id)))

busy = select(Post.user_id, count(Post.id).label("n")).group_by(Post.user_id).cte("busy")
select(User, busy.n).join(busy, busy.user_id == User.id).where(busy.n > 5)

tree = recursive_cte(
    "tree",
    select(Node.id, Node.parent_id).where(Node.parent_id == None),
    lambda cte: select(Node.id, Node.parent_id).join(cte, Node.parent_id == cte.id),
)
```

CTEs need no registration — references are collected wherever they appear (`FROM`, `JOIN`, a subquery, another CTE) and emitted once, in dependency order, in the outermost statement's `WITH` clause. A CTE or subquery selecting an unlabelled aggregate is refused at build time (Postgres and sqlite name it differently, so guessing would be wrong on one of them).

### Expressions, functions, windows

```python
from rowform import case, func, row_number

select(Post.id, Post.score * 2, Post.title.concat(" (draft)"))
select(func.lower(Post.title), func.coalesce(Post.score, 0))
select(Post.id, case((Post.score > 100, "hot"), (Post.score > 10, "warm"), else_="cold"))
select(Post.user_id, Post.score, row_number().over(partition_by=Post.user_id, order_by=Post.score))
```

`+ - * /` keep the operand's type; string concatenation is `.concat()`, not `+` (Postgres has no `+` for text). `func.anything(...)` calls any SQL function. Window helpers: `row_number`, `rank`, `dense_rank`, `lag`, `lead`, `first_value`, `last_value`, `ntile`, plus `.over()` on any aggregate.

### Set operations

```python
select(User).where(User.active == True).union(select(User).where(User.id < 100)).order_by("id")
```

`union`, `union_all`, `intersect`, `intersect_all`, `except_`, `except_all` — rows hydrate exactly as a single select. Column counts are checked at build time.

### Writes: INSERT, UPDATE, DELETE, upserts

```python
from rowform import excluded

await db.execute(insert(User).values(name="ada", email="a@b.c"))
await db.execute(insert(User).values([{"name": "a"}, {"name": "b"}]))   # bulk, one round trip

rows = await db.execute(insert(User).values(name="a").returning(User))  # RETURNING -> rows

await db.execute(update(Post).set(score=Post.score + 1).where(Post.id == 1))

await db.execute(
    insert(User).values(email="a@b.c", hits=1)
    .on_conflict_do_update(User.email, set_={"hits": excluded(User.hits)})
)
```

`Delete` with no `where()` raises — say `.all_rows()` if that's the intent. `excluded(col)` is the row that lost the conflict; `on_conflict_do_nothing()`/`on_conflict_do_update()` take either the conflicting columns or `constraint="name"` (Postgres only).

Cross-table writes skip the `ON` clause — the join condition goes in `where()`, and both builders refuse to render without one:

```python
await db.execute(update(Post).set(author=User.name).from_(User).where(User.id == Post.user_id))
await db.execute(delete(Post).using(User).where(User.id == Post.user_id, User.banned == True))
```

`UPDATE ... FROM` works on Postgres and sqlite 3.33+; `DELETE ... USING` is Postgres-only (sqlite: use `Post.user_id.in_(select(...))` instead).

### Transactions

```python
async with db.transaction() as tx:
    await tx.execute("UPDATE accounts SET balance = balance - $1 WHERE id = $2", 100, payer)
    await tx.execute("UPDATE accounts SET balance = balance + $1 WHERE id = $2", 100, payee)
    rows = await tx.execute(select(Account).where(Account.id == payer))
```

Commits on clean exit, rolls back on any exception; nesting gives savepoints. `isolation=` takes `"read_committed"`, `"repeatable_read"`, `"serializable"`, plus `readonly=`/`deferrable=`. Calling `engine.execute()`/`fetch_all()` *inside* a transaction raises — it would silently use a different pooled connection and miss the transaction's uncommitted writes.

### Dialects: Postgres and sqlite

```python
from rowform import SQLITE, POSTGRES

select(User).where(User.email.is_distinct_from(None)).to_sql(dialect=POSTGRES)
# -> "... WHERE email IS DISTINCT FROM $1"
select(User).with_for_update().to_sql(dialect=SQLITE)
# -> ValueError: with_for_update() is not supported on sqlite
```

`to_sql(dialect=...)` is fully additive — every call with no `dialect=` renders exactly as before. `ilike()`, `with_for_update()`, `Delete.using()`, and `on_conflict_..._(constraint=...)` all raise a clear error when rendered for sqlite instead of producing SQL its parser would reject. `is_distinct_from()`/`is_not_distinct_from()` need a dialect to render at all, since Postgres and sqlite spell null-safe comparison completely differently.

### Escape hatches

```python
from rowform import text, literal_column, bindparam

select(User).where(text("email = :addr").bindparams(addr="a@b.c"))
select(literal_column("count(*) + 1"), User.id)     # raw fragment, no validation

stmt = select(User.name).where(User.id == bindparam("id"))   # deferred: build once,
await db.execute(stmt, id=1)                                  # execute many times
await db.execute(stmt, id=2)                                  # with different values

select(User, Post).select_from(Post)   # the one sanctioned, explicit cross join
```

`text()`/`literal_column()` give up the usual join-graph check for that fragment. `bindparam()` can't back `.limit()`/`.offset()`. `.join()` itself is unaffected — it still always requires a real linking condition; `select_from()` is the one explicit, by-name way to add an unconditional extra source.

### What's checked for you

Each of these produces a clear client-side error instead of a plausible wrong result:

| you write | you get |
|---|---|
| a `where()`/`order_by()`/`group_by()` reference to a table not in the query | `ValueError` — not joined |
| `.join(Post, Post.title == "x")` | `ValueError` — the ON clause links no two tables |
| an unaliased self-join | `ValueError`, naming `Alias` as the fix |
| a correlated subquery without `.correlate()` | `ValueError` — indistinguishable from a typo |
| `Delete(User)` with no `where()` | `ValueError` — say `.all_rows()` |
| `execute()`/`fetch_all()` mismatched with `RETURNING` | `ValueError` — either mismatch silently returns `[]` |
| a bulk insert past the parameter limit | `ValueError` naming the row ceiling |
| a `cte()`/`subquery()` selecting an unlabelled aggregate | `ValueError` — add `.label()` |
| `Update(...).from_(T)`/`Delete(...).using(T)` with no `where()` | `ValueError` — that's a cross product |

**Not supported:** relationship declarations (no lazy loading — you write the join), `INSERT ... SELECT` and data-modifying CTEs, schema management (no DDL/migrations/reflection). No de-duplication on a one-to-many join — an inner join yields the left row once per match, as SQL does; the ORM's identity map is precisely the machinery this library skips to be fast.

---

## 🏗️ Architecture

Two paths, depending on whether you need Python objects at all:

```
                                 ┌──(A) object path ────────────────────────────┐
[ PostgreSQL ] ──(driver)──> [ C-tuples ] ──(compiled hydrator)──> [ Slotted object ]
                                                                            │
                                                                    (compiled hook)
                                                                            ▼
                                                                  [ Response (JSON) ]
                                 ┌──(B) json_agg path ──────────────────────────┐
[ PostgreSQL ] ──(json_agg in SQL)──> [ one JSON string ] ──────> [ Response (JSON) ]
```

1. **Descriptor expressions.** `User.id > 100` evaluates a descriptor at class scope, returning a `ColumnExpr` node instead of doing a Python-level comparison — a queryable AST with no SQLAlchemy-style instrumentation. The same overloaded `__get__` (`ColumnExpr[T]` at class scope, `T` at instance scope) is what keeps both model styles statically typed.
2. **Compiled hydration, entirely positional.** A model's column layout is fixed once, so rowform generates a specialized `rows -> [instance]` function per model: plain attribute stores (CPython's specializing interpreter quickens these to `STORE_ATTR_SLOT`), and rows are read by **tuple unpacking** with no column names in the generated code. This is safe because rowform always writes the `SELECT` list itself — there's no `SELECT *` in the builder, so every column's ordinal is known at codegen time. Positional access beats key access by ~2–5x depending on driver, and building a dict per row costs 5–7x more than either.
3. **Slotted storage.** Instances use `__slots__` — a fixed-size array rather than a `__dict__`, about 35% smaller per object. The tradeoff: this is also what forces orjson off its native dataclass fast path, hence `DATACLASS_DUMP_OPTION`.
4. **Path (B) skips 2 and 3 entirely** by shaping rows into JSON in SQL (`Query.to_json_sql`). Implemented but parked — path (A) is the focus.

None of this is "zero-copy" — data still moves from the C-level tuple into Python object storage into JSON bytes. The claim is "fewer intermediate Python-level allocations than an ORM identity-map path," not "no copying happens."

---

## 📊 Performance

Same driver on both sides (psycopg3 async), both libraries at default pool behaviour, through a real FastAPI + uvicorn stack on one core:

| endpoint (FastAPI, one core) | rps | p50 | p99 |
|---|---|---|---|
| `/noop` — framework floor, no database | 8419 | 0.91 ms | 1.56 ms |
| **rowform** | **1319** | **5.93 ms** | **9.21 ms** |
| SQLAlchemy Core | 825 | 9.19 ms | 15.61 ms |
| SQLAlchemy ORM | 396 | 16.09 ms | 77.57 ms |

**~3.3x the ORM, ~1.6x Core** — the tail is where it's most consistent (p99 ~8x tighter than the ORM's across every configuration tested). Against hand-written `asyncpg` + `dict(record)` with no object mapping at all, rowform costs +10–25% CPU — the pitch is ergonomics near hand-written cost, not free abstraction.

Full numbers, methodology, and — importantly — a log of five earlier published claims that turned out to be wrong and were corrected: **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**, **[docs/FINDINGS.md](docs/FINDINGS.md)**, **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.

---

## 🤝 Contributing

Not packaged and not on PyPI — the only supported way to work on it is from a clone:

```bash
git clone https://github.com/vipierozan99/rowform && cd rowform
uv sync --all-extras     # dev tools plus asyncpg/psycopg/orjson
uv run pytest            # 968 tests, including mypy and pyright
```

Without `uv`: `pip install pytest pytest-asyncio` runs the sqlite-backed suite; the Postgres and typing tests skip cleanly until their extras (`asyncpg`, `psycopg[binary]` + `psycopg-pool`, `mypy` + `pyright`) are installed.

The suite has two tiers: everything testable without a server (SQL generation, codegen, joins end-to-end against sqlite) runs anywhere in well under a second; engine/transaction tests need a real PostgreSQL and **skip with a reason** when one isn't reachable. Point them at a specific server with `ROWFORM_TEST_DSN`, or pass `--pg-required` to turn that skip into a CI failure. Both Postgres engines (`asyncpg`, `psycopg3`) are parameterised across the same tests, so a feature can't quietly end up working on only one driver.

Static types are tested, not just declared — `tests/typing/positive.py` uses `typing.assert_type` for exact-type assertions, `tests/typing/negative.py` carries `# type: ignore`/`# pyright: ignore` on lines that must fail, and `tests/test_typing.py` verifies both checkers are clean *and* that stripping those suppressions reproduces the same errors, so a negative test can't quietly stop proving anything.

When contributing: match the existing style (small, focused changes; a new feature ported from SQLAlchemy carries a `# Ported from ...` citation comment on its test), run the full suite plus `mypy rowform` and `pyright rowform` before opening a PR, and prefer a real regression test over a throwaway script when demonstrating a fix.

---

## 📜 License

MIT License. Free for open-source and commercial use.
