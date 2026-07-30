# Core-as-compiler: using SQLAlchemy for SQL, rowform for rows

Evaluate (and, if adopted, build) a mode where **SQLAlchemy Core compiles the SQL
and owns the schema, while rowform's generated hydrator owns row shaping** — so a
caller gets DDL, reflection and Alembic from SQLAlchemy without paying for `Row`,
`CursorResult`, or ORM instrumentation on the read path.

Status: **investigation complete, nothing built.** If adopted this is a **rewrite, not
an add-on** — SQLAlchemy Core becomes a hard dependency, 72% of the current library is
deleted, and tests/benchmarks/docs are reworked around the new model rather than
extended to cover both. See §1a before anything else.

Every number below was measured in throwaway scripts under `/tmp` — not in the repo,
not committed. What is verified vs not:

| | |
|---|---|


This is a sibling to `PLAN.md` (the benchmark-suite rewrite), not a replacement.
It reuses that suite's harness and its methodology invariants (`PLAN.md` §4)
rather than inventing new ones.

---

## 1. Why

`docs/FINDINGS.md` commits rowform to "query builder + row hydrator, not an ORM".
Three schema features are the obvious follow-on work and are each a large lift that
duplicates a mature ecosystem: DDL generation (`CREATE TABLE` from a model),
reflection (introspect a live table and diff it against a model), and migrations
(versioned upgrade/downgrade scripts). Migrations in particular are a separate tool
even in SQLAlchemy's own world.

The observation driving this plan: **the schema half and the row-shaping half of
SQLAlchemy are independent.** Bypassing the result layer costs you nothing on the
schema side, because DDL returns no rows and reflection is a cold path. So
`Table`/`MetaData` can be the schema source of truth — feeding `create_all()`,
`Inspector`, and Alembic's `target_metadata` — while rowform's compiled hydrator
handles the hot read path. That retires all three schema features at once: the
companion tool already exists and is called Alembic.

---

## 1a. Scope: this is a rewrite, not an add-on

**Decision: if this path is taken, it is a different library.** Not an optional
integration module bolted onto rowform, and not a second way to use it. The old
usage patterns are not preserved — SQLAlchemy Core becomes a hard dependency, its
query builder replaces rowform's, and `Mapped[]` + `class User(Base)` becomes the
only declaration shape. Tests, benchmarks and docs get reworked around the new model
rather than extended to cover both.

An earlier draft of this plan hedged ("optional integration module… rowform must keep
working without SQLAlchemy installed"). That hedge is withdrawn; §8 and §9 are updated
accordingly.

### What SQLAlchemy Core replaces

| module | LOC | fate |
|---|---|---|
| `rowform/expr.py` | 2,149 | **retired** — SQLAlchemy expressions |
| `rowform/query.py` | 1,321 | **retired** — `sa.select()` |
| `rowform/dml.py` | 843 | **retired** — `sa.insert/update/delete` |
| `rowform/dialects.py` | 140 | **retired** — SQLAlchemy dialects |
| | **4,453 of 6,220 (72%)** | |

### What survives

| module | LOC | fate |
|---|---|---|
| `rowform/compile.py` | 215 | **the reason to do this at all** — hydrator codegen; gains the §5c statement planner |
| `rowform/engine.py` + `sqlite_engine.py` + `psycopg_engine.py` | 935 | kept for raw-driver execution and pooling; loses its SQL-generation call sites |
| `rowform/transaction.py` | 201 | kept, or delegated to SQLAlchemy |
| `rowform/model.py` | 106 | reworked into `ModelMeta`/`Base` (§5a) |
| `rowform/column.py` | 127 | mostly retired; the `__columns__` shape survives for codegen |
| `rowform/__init__.py` | 183 | rewritten API surface |

The honest summary: **the compiled hydrator is the asset, and everything else was
scaffolding around it.** This plan keeps the asset and rents the scaffolding.

### The thesis this rests on

The rewrite should be performance-neutral while gaining DDL, reflection, Alembic and
SQLAlchemy's entire SQL surface, because compilation is cached (~0.001 ms/execute,
§2b) and everything else on the hot path is already rowform's.

**Confirmed (§2g):** approach 4 measures **0.98x current rowform with a hoisted
connection and 1.02x per request** — neutral within ±2% — against stock Core at 1.48x
per request. The performance justification holds; the remaining unknowns (§7) are
correctness, not speed.

---

## 2. What was measured

All figures: 1000 rows from a 200,000-row `users` table (`benchmarks/shapes/flat.py`),
median of 5 trials, `gc` disabled, connections hoisted out of the timed region
(`docs/BENCHMARKS.md` correction 1), byte-identical JSON asserted across every
variant in a comparison.

### 2a. Two corrections to earlier explanations

Recorded because both were wrong in a way that changes conclusions.

1. **Core does *not* rebuild result metadata per execute.** `CursorResult._init_metadata`
   caches `CursorResultMetaData` on the compiled object (`engine/cursor.py:1605-1610`).
   An earlier reading of "74.7% of Core's own CPU in `sqlalchemy/engine` + pool"
   (`docs/BENCHMARKS.md:612`) attributed the cost to connection/metadata management.
   That is wrong: `engine/result.py` and `engine/cursor.py` *are* `sqlalchemy/engine`,
   and that 74.7% is row shaping.

2. **rowform's extra aiosqlite round trip is not a cost.** `SqliteEngine._fetch_rows`
   (`rowform/sqlite_engine.py:136-138`) does `execute` then `fetchall` — two thread
   handoffs — where SQLAlchemy's adapter buffers both in one (`connectors/asyncio.py:244-255`).
   Predicted collapsing it via `aiosqlite.execute_fetchall()` would be a free win.
   Measured the opposite: 1 round trip = 1.0702 ms, 2 round trips = 1.0366 ms.
   **Retracted; do not act on it.**

### 2b. Core's per-execute fixed cost is ~1%

Measured directly, sync sqlite:

| per-execute fixed cost | measured | note |
|---|---|---|
| `_compile_w_cache()` cache hit | 0.0010 ms | |
| `construct_params()` | 0.0006 ms | |
| `CursorResultMetaData()` build | 0.0052 ms | cached per compiled stmt → ~0 amortised |
| `_adapt_to_context()` | 0.0002 ms | |
| pool checkout `connect()`/`close()` | 0.0064 ms | |
| **total** | **~0.014 ms** | ~1% of a 1000-row query |

Confirmed structurally: `execute(stmt)` reading `result.cursor.fetchall()` instead
of Rows measures **0.856 ms** against a **0.853 ms** raw-`sqlite3` floor. The entire
compile → `ExecutionContext` → pool → metadata path is ~0.003 ms.

**All of Core's overhead is per-row.**

### 2c. Where the per-row cost is

| layer | cost/1000 rows | what it buys | verdict |
|---|---|---|---|
| result iteration | 0.27–0.37 ms | streaming, `yield_per`, unique filters | **discard → `.all()`** |
| per-row type processors | 0.08–0.11 ms | dialect type correctness | **special-case** |
| `Row.__init__` | 0.18–0.29 ms | positional + string + Column-object access, ambiguity errors | **discard if unused** |
| `.mappings()` + `str(k)` | +2.1 ms | dict-like rows | **never on a hot path** |

Iteration is the biggest free win. `for row in result` costs, per row: two generator
frames (`cursor.py:2248`, `result.py:565`), a try/except-wrapped
`CursorFetchStrategy.fetchone` (`cursor.py:1163`), a DBAPI `fetchone()`, and a
`functools.partial` call. `.all()` replaces all of it with one C-level `fetchall()`
plus a list comp.

Processors are already conditionally free: `_effective_processors` (`result.py:218`)
returns `None` when every processor is `None`, so `_apply_processors` is skipped
entirely. The 0.08–0.11 ms measured here is **sqlite's `Boolean` int→bool** — on
asyncpg it would be zero.

### 2d. Hydrator choice matters as much as engine choice

Pure hydration, 1000 sqlite tuples → objects, no I/O and no JSON:

| | median | vs dicts |
|---|---|---|
| plain dicts (list comp) | 0.1435 ms | 1.00x |
| **rowform compiled hydrator** (`object.__new__`) | **0.1679 ms** | **1.17x** |
| `User(**kwargs)` dataclass `__init__` | 0.3513 ms | 2.45x |

rowform produces *typed dataclass instances* for 17% over bare dicts, and **2.1x
faster than a naive dataclass comprehension** — `object.__new__` plus plain
attribute stores, skipping `__init__` dispatch and keyword binding
(`rowform/compile.py:79-102`).

### 2e. The full pipeline, all variants using rowform's hydrator

Async aiosqlite, WAL set on the file at creation, so the comparison isolates the
**engine layer** rather than secretly comparing two hydrators:

| variant | median | vs floor | vs rowform |
|---|---|---|---|
| stock Core: iterate | 1.9836 ms | 1.85x | 1.70x |
| Core `.all()` + Integer col + `_result_disable_adapt_to_context` | 1.5872 ms | 1.48x | 1.36x |
| custom dialect `ExecutionContext` (best in-Core) | 1.3679 ms | 1.28x | 1.17x |
| **rowform (shipped)** | **1.1645 ms** | **1.09x** | **1.00x** |
| raw driver + rowform's hydrator | 1.1051 ms | 1.03x | 0.95x |
| raw driver + `User(**kwargs)` | 1.3540 ms | 1.27x | 1.16x |
| **raw driver → dicts (true floor)** | **1.0702 ms** | **1.00x** | 0.92x |

Readings:

- **rowform sits at 1.09x the true floor.** Its whole engine layer — SQL generation,
  param binding, pool acquire, converters — costs ~5% over hand-rolling the driver
  with the same hydrator. Nothing meaningful left to win there.
- **Core's best achievable in-process is 1.17x rowform**; stock is 1.70x. The residual
  0.20 ms/1000 rows is `ExecutionContext` + compile-cache lookup + greenlet + the
  `cursor.fetchall()` deque→list copy.
- **Picking the wrong hydrator costs as much as all of Core.** Raw driver + kwargs
  (1.354) ≈ all of Core + rowform's hydrator (1.368).

### 2f. A methodology trap this run walked into

`PLAN.md` §4 already lists the invariant *"Include a floor and a naive baseline —
when rowform appeared to beat the floor, that was the tripwire."* It fired again
here, for a new reason: an intermediate run used `User(**kwargs)` as the "floor"
hydrator while rowform used its generated one, so the floor was slower than the
thing it was bounding. **A floor must do strictly less work than every contender,
including in object construction.** Two floors are now measured (dicts-only, and
raw-driver + the *same* hydrator) so the engine cost and hydrator cost are separable.

---

### 2g. Approach 4 measured — the thesis holds (closes R2)

`ablate8` (hoisted connection) and `ablate9` (per-request acquire). Byte-identical
JSON asserted across every arm. Absolute scale differs between §2e and these runs
(machine variance across sessions); **ratios are the comparable quantity.**

Hoisted connection:

| variant | median | vs floor | vs rowform |
|---|---|---|---|
| dialect seam (approach 3) | 1.0115 ms | 1.23x | 1.08x |
| seam + hydrate off the adapter's deque | 1.0947 ms | 1.34x | 1.17x |
| seam + `exec_driver_sql` (skip compilation) | 1.0308 ms | 1.26x | 1.10x |
| **Core as compiler only (approach 4)** | **0.9138 ms** | **1.12x** | **0.98x** |
| rowform (shipped) | 0.9340 ms | 1.14x | 1.00x |
| raw driver → dicts (true floor) | 0.8194 ms | 1.00x | 0.88x |

Per-request connection acquire — the realistic web-service shape, and the arm
`ablate8` omitted for approach 4:

| variant | median | vs floor | vs rowform |
|---|---|---|---|
| stock Core `.all()`, SQLAlchemy pool per request | 1.6758 ms | 1.64x | 1.48x |
| **Core compiles + rowform's pool + hydrator** | **1.1593 ms** | **1.13x** | **1.02x** |
| rowform (shipped), pool per request | 1.1321 ms | 1.11x | 1.00x |
| Core compiles + hoisted raw conn | 1.0750 ms | 1.05x | 0.95x |
| rowform's pool → dicts (floor) | 1.0241 ms | 1.00x | 0.90x |

**§1a's thesis is confirmed: the rewrite is performance-neutral, within ±2% of
current rowform in both scenarios** (0.98x hoisted, 1.02x per-request), while stock
Core sits at 1.48x per-request. R2 is closed.

Four secondary results, three of them negative:

1. **Approach 4 beats approach 3 on speed as well as safety** (0.9138 vs 1.0115).
   §3's recommendation no longer trades performance for portability.
2. **Retracted: hydrating off the adapter's `_rows` deque.** Predicted to save a
   deque→list copy; measured *slower* (1.0947 vs 1.0115). Do not do this.
3. **Retracted: `exec_driver_sql` to skip compilation.** Measured a wash-to-slightly-
   worse (1.0308 vs 1.0115), exactly as §2b's ~0.001 ms compile-cache cost predicts.
   There was never anything there to win.
4. **R5 answered, negatively.** `isolation_level="AUTOCOMMIT"` was *slower* than the
   default (1.2193 vs 1.1944), so the hypothesis that SQLAlchemy's implicit
   BEGIN/COMMIT is a material per-request cost is not supported.

And one design consequence worth naming: **SQLAlchemy's pool acquire is several times
more expensive than rowform's.** Hoisted→per-request costs Core ~0.18 ms
(1.0115 → 1.1944) against rowform's ~0.03–0.08 ms. That is why the target
architecture in §5d executes on rowform's pool rather than SQLAlchemy's — Core
compiles, rowform's engine runs it.

---

## 3. Approaches considered

| # | approach | best measured | verdict |
|---|---|---|---|
| 1 | rowform as-is (own SQL builder + hydrator) | 1.00x rowform | shipped; no schema/DDL/Alembic |
| 2 | tune stock Core (`.all()`, no `.mappings()`, column types) | 1.36x rowform | trivial, public API, no new code |
| 3 | custom dialect `execution_ctx_cls` → override `_setup_result_proxy` | 1.17x rowform | works, but couples to dialect internals — see §4 |
| 4 | **Core as compiler only** — compile with Core, execute on the raw driver | **0.98x rowform hoisted / 1.02x per-request** (§2g) | **recommended** |
| 5 | patch `Mapper`/`DeclarativeBase` to drop `instance_state` | — | non-starter; fork of `sqlalchemy.orm` |

Approach 4 was refined after §5's investigation: the model does not need to be
*derived from* a `Table`, it can **be** the table's declaration. That removes the
schema-duplication cost this plan originally listed as approach 4's main drawback.

§2g then measured it as **faster than approach 3** (0.9138 vs 1.0115 hoisted), so the
recommendation no longer trades speed for portability — approach 4 wins on both.

Approach 4 is recommended over 3 on portability and safety grounds, not speed:

- It touches **no private attributes** and overrides **no dialect class**.
- Approach 3 mutates `execution_ctx_cls`, so reflection runs through the override
  too; it is only safe because it gates on an execution option that reflection
  never sets. Approach 4 has no such coupling by construction.
- Approach 3 returns a plain `list` where SQLAlchemy expects a `CursorResult`. That
  survives the async path only because `_ensure_sync_result`
  (`ext/asyncio/result.py:936-942`) does `result._is_cursor` inside
  `try/except AttributeError` and falls through a branch commented *"legacy
  execute(DefaultGenerator) case"*. That is not a supported contract.
- Approach 3's per-dialect concern is the whole `ExecutionContext` subclass tree
  (~21 classes). Approach 4's is `paramstyle`, a documented stable property.

---

## 4. Portability

### 4a. Type processors — the real hazard, and it is inverted

Bypassing `Row` skips `_processors`, which are dialect-specific and sometimes
load-bearing:

| dialect | processor burden | risk of bypassing `Row` |
|---|---|---|
| **sqlite** | highest — `Date`/`DateTime`/`Time` are *stored as strings*; `str_to_datetime_processor_factory` is mandatory (`sqlite/base.py:1211`) | **severe** — a `DateTime` column silently returns a raw string |
| mysql | moderate — `Time` arrives as `timedelta`, `Boolean` from tinyint, ENUM/SET | moderate |
| mssql | moderate — `DATETIMEOFFSET`, `uniqueidentifier` (`mssql/base.py:1283-1436`) | moderate |
| asyncpg | minimal — `AsyncpgJSON.result_processor` returns `None` (`asyncpg.py:278`); driver decodes natively | low |
| oracle | minimal — conversion pushed into the driver via `_cx_oracle_outputtypehandler`; `result_processor` returns `None` (`cx_oracle.py:562`) | low |

**The dialects where bypassing is safest are where it wins least.** On asyncpg most
processors are already `None`, so `Row` degenerates to `tuple(data)` and there is no
processor loop to skip. The measured 0.08–0.11 ms saving was sqlite's `Boolean`.

`benchmarks/shapes/flat.py` is `int/str/str/bool` — the one shape where sqlite's gap
is a single int→bool that `SQLITE_CONVERTERS` already covers. **Add a `DateTime`
column to the benchmark shapes before trusting any of this** (§7 R1).

### 4b. Paramstyle — four conventions among plausible drivers

```
sqlite+aiosqlite    qmark             positional=True
pg+asyncpg          numeric_dollar    positional=True
pg+pg8000           format            positional=True
pg+psycopg2         pyformat          positional=False
mssql+pyodbc        named             positional=False
oracle+cx_oracle    named             positional=False
```

`compiled.positiontup` is `None` for the non-positional ones — pass
`construct_params()`'s dict instead. One branch, not one code path per dialect.
All the above set `supports_statement_cache = True`.

### 4c. Row container types

Not every driver returns tuples — asyncpg buffers `Record` objects
(`postgresql/asyncpg.py:550`), pyodbc returns `pyodbc.Row`. All are sequences, so
rowform's `for f0, f1, f2, f3, in rows` unpacking works unmodified.

---

## 5. How it works

**Decision: keep SQLAlchemy parity.** Declaration uses SQLAlchemy's own vocabulary
(`Mapped[int]`), and result shapes mirror SQLAlchemy's (`select(Model)` → models,
`select(Model.id, Other)` → tuples). The alternative — keeping rowform's
`Column[int] = Column(int)` and avoiding the `sqlalchemy.orm` import — was
considered and rejected in favour of familiarity.

### 5a. Declaration: a base class, not a decorator

Reimplementing declarative is unnecessary. `Mapped` is an ordinary generic, so the
whole "scanner" is two lines — no `_ClassScanMapperConfig`, no
`instrumentation.register_class()`, no `Mapper`, no `instance_state`:

```python
hints = get_type_hints(cls, include_extras=True)
fields = {n: get_args(h)[0] for n, h in hints.items() if get_origin(h) is Mapped}
```

**The entry point must be a base class with a `dataclass_transform`-decorated
metaclass, not a decorator.** An earlier draft used `@sa_model(metadata)`; that is a
decorator *factory*, and factories lose field typing entirely (§5b) — it would have
shipped untyped models. A base class needs no arguments at class-creation time
because `metadata` lives on the base, which sidesteps the factory problem.

This is also SQLAlchemy's own shape: `dataclass_transform` is applied to the
metaclass `DCTransformDeclarative` (`orm/decl_api.py:157-171`) and `DeclarativeBase`
declares `metaclass=DeclarativeAttributeIntercept` (`decl_api.py:640-643`).

Note this is not metaclass-vs-no-metaclass — rowform already has `ColumnMeta`
(`rowform/model.py:29`). The only question is who supplies it, and a base class
supplies it while also carrying `metadata`.

```python
@dataclass_transform()                 # on the METACLASS -- see 5b
class ModelMeta(type):
    def __getattribute__(cls, name):   # same shape as ColumnMeta, but
        ...                            # __column_exprs__ holds SA Columns
    def __clause_element__(cls):       # THE hook that makes sa.select(Model) work
        return type.__getattribute__(cls, "__table__")

class Base(metaclass=ModelMeta):
    metadata = MetaData()              # where Alembic's target_metadata points

class User(Base):
    __tablename__ = "users"
    id: Mapped[int]
    name: Mapped[str]
    is_active: Mapped[bool]
```

The metaclass builds both a real `Table` (for DDL/reflection/Alembic) and a plain
dataclass (for storage), and populates rowform's `__columns__` so the existing
hydrator codegen works untouched.

`Base.metadata` matters beyond tidiness: `target_metadata = Base.metadata` is the
single most-copied line in every Alembic `env.py`, so this is the parity that makes
§6 work without explanation.

**`sa.select(User)` works via `__clause_element__` on the metaclass.** SQLAlchemy's
coercion layer honours it; on a class it has to live on the metaclass. Verified
working (`/tmp/q_metaclass.py`): `select()`, `where()`, `limit()`, `join()`,
`select_from(Model)` and `func.count()` all treat the class as its `Table`;
`User.id > 100` is a genuine `BinaryExpression`; instances carry no
`_sa_instance_state`; the round trip hydrates `is_active` as a real `bool`.

### 5b. Typing (verified with the project's basedpyright)

With `dataclass_transform` on the metaclass:

| expression | inferred |
|---|---|
| `u.id` / `u.name` | `int` / `str` |
| `User.id` | `InstrumentedAttribute[int]` |
| `User.id > 100` | `ColumnElement[bool]` |
| `sa.select(User)` | `Select[Tuple[User]]` |
| `User(id="nope", ...)` | **error** (caught) |
| `User(id=1)` | **error: missing args** (caught) |

**Why the decorator route is rejected outright, not merely constrained:**

| route | `field.id` inferred |
|---|---|
| `dataclass_transform` on a metaclass + base class | **`int`** |
| `@sa_model(metadata)` — decorator factory | **`Any`** |

The factory form is the same `dataclass_transform` propagation gap recorded at
`docs/FINDINGS.md:378-380` for `@model(tablename=...)`. A bare decorator types fine,
but a bare decorator cannot receive `metadata` — so for this design the decorator
route is unusable, not just awkward.

Two residual notes:

1. `User.id` is declared `InstrumentedAttribute[int]` but is an `sa.Column` at
   runtime. Harmless (both are `ColumnOperators`, which is why the comparison works)
   but it is an inherited type-level fiction, from `Mapped.__get__`'s overloads in
   `orm/base.py`.
2. The generated `__init__` parameter type is `SQLCoreOperations[int] | int`, so
   passing a SQL expression where an `int` is meant typechecks. Inherited from
   `Mapped.__set__`; not worth fighting.

### 5b-i. Mixins and column order — two traps the base class invites

Both **verified**, and both need a decision before P3 ships.

**A plain mixin's fields are not `__init__` parameters.** Given
`class Comment(TimestampMixin, Base)` where `TimestampMixin` declares
`created_at: Mapped[int]`, basedpyright reports `c.created_at` as `int` but rejects
the constructor call:

```
error: No parameter named "created_at"  (reportCallIssue)
```

So the attribute is visible while the field is not constructible. Mixins therefore
need to be processed too (share the metaclass), or be documented as unsupported.

**Column order is MRO-derived, which is a silent migration hazard.** `get_type_hints`
merges base-class annotations *first*, regardless of MRO position:

```
class Comment(TimestampMixin, Base):  ['created_at', 'updated_at', 'id', 'body']
class Comment2(Base, TimestampMixin): ['created_at', 'updated_at', 'id', 'body']
own __annotations__ only            : ['id', 'body']
```

That order becomes **both** the `CREATE TABLE` column order and the positional
hydration order (§5c). So introducing a mixin silently moves its columns to the front
of the table, and Alembic autogenerate does not diff column *order* — the drift is
invisible. **Requirement for P3: pin column order explicitly and record it, rather
than inheriting `get_type_hints` order.**

### 5c. The hydrator must be planned from the statement, not the model

The original design generated the hydrator from the model's declaration order. That
is a silent-corruption hazard: `select(User.name, User.id)` yields a different column
order, and because the generated code unpacks positionally
(`for f0, f1, f2, f3, in rows`, `rowform/compile.py:90`) fields would be
mis-assigned with nothing to catch it.

Fix: **plan the hydrator from `stmt.selected_columns`.** Mirroring SQLAlchemy makes
this fall out naturally rather than needing an error path — a contiguous run of
selected columns that *is* a known table's column list becomes a model entity;
anything else is a scalar. So `select(User.name, User.id)` degrades to a
`(str, int)` tuple, which is exactly what SQLAlchemy returns for that query.

`rowform.compile.compile_join_hydrator` already consumes exactly this shape
(`("model", cls, nullable)` / `("column", py_type)` specs in select order), so the
work is the planner, not new codegen.

Design notes for the planner (**sketched in `/tmp/q_codegen.py`, NOT run — no part
of §5c is verified**):

- Compare columns **by identity**, never `==`. `Column.__eq__` builds a SQL
  expression; it does not compare.
- `nullable` for a model entity means "reached through an OUTER join", so the
  planner must walk `stmt.get_final_froms()` for `Join(isouter=True)` / `full=True`
  and mark tables on the right-hand side. `compile_join_hydrator` already
  implements the all-columns-NULL → `None` behaviour.
- Aliased tables (self-joins) have distinct `Column` objects, so a registry keyed by
  `Table` will miss them and degrade the entity to scalars. Correct, but a feature
  gap worth naming.
- Set `wrap=False` only for the single-whole-model case, matching
  `compile_join_hydrator`'s documented contract.

### 5d. Execution: compile once, run on the driver

```python
class CoreQuery:
    def __init__(self, stmt, dialect, converters):
        self._compiled = stmt.compile(dialect=dialect)          # once
        self.sql = self._compiled.string
        self._positional = dialect.positional
        self._keys = list(self._compiled.positiontup or [])
        self._entities, self._hydrate = hydrator_for(stmt, converters)

    def bind(self, **params):
        built = self._compiled.construct_params(params, escape_names=False)
        return tuple(built[k] for k in self._keys) if self._positional else built

    async def fetch_all(self, conn, **params):
        cur = await conn.execute(self.sql, self.bind(**params))
        return self._hydrate(await cur.fetchall())
```

Verified for the single-model case (`/tmp/usage_demo.py`): `create_all()` creates the
table, `Inspector` reflects it, one compiled statement serves different bind params,
and hydration produces real `bool`s.

### 5e. Sharp edges found

1. **Nullable columns break converter lookup.** A nullable column wants
   `int | None`, but the converter table is keyed by exact type
   (`converters.get(column.py_type)`, `compile.py:95`). `bool | None` will not match
   `bool`, so int→bool would silently not run. Normalise to the non-optional type for
   converter lookup, or make the table union-aware.
2. **`python_type` is not total.** It raises `NotImplementedError` for some types, and
   `Enum` resolves to bare `str`, losing the enum class. Needs an explicit override
   table for both the declaration path and the scalar-column path in §5c.
3. **Core emits `LIMIT ? OFFSET ?`** — an OFFSET nobody asked for, `0` supplied by
   `construct_params`. Harmless, but it is why the positional tuple must be built
   from `positiontup`, never from the params you think you passed.

---

## 6. What this retires, and what it costs

| SQLAlchemy feature | still available? | why |
|---|---|---|
| DDL (`metadata.create_all`) | yes, fully | DDL returns no rows; the result layer is not involved |
| Reflection (`Inspector`, `autoload_with=`) | yes, fully | cold-path catalog SELECTs through the stock result layer; approach 4 never touches the dialect |
| Alembic autogenerate | yes (**untested**, §7 R3) | needs `target_metadata` (a `MetaData`) + a connection; never sees the read path |
| `DeclarativeBase` | no — and not wanted | ORM; instruments classes and attaches `instance_state` |

`DeclarativeBase`'s only contribution to DDL/Alembic is `Base.metadata`, a `MetaData`
of implicitly-built `Table`s. §5a builds exactly that from `Mapped[]` annotations, so
Alembic receives identical input.

**No schema duplication.** An earlier draft of this plan had `Table` as the source of
truth with the model derived from it, and listed that duplication as the approach's
main cost. §5a removes it: the model declaration *is* the `Table` declaration, one
class, one set of field names. `benchmarks/shapes/flat.py` — where `User`,
`users_table`, `UserORM` and `UserDC` all describe the same four columns — is the
duplication this avoids, and is the natural first thing to collapse once §8 P3 ships.

---

## 7. Open risks (do not quote these as findings)

| id | risk | status | how to close |
|---|---|---|---|
| R1 | Every measurement uses an `int/str/str/bool` shape — the one case where sqlite needs no temporal processor | **unmeasured** | add a `DateTime` + `Numeric` + nullable column shape (§8 P1) |
| R2 | Approach 4's speed | **CLOSED — measured, §2g.** 0.98x rowform hoisted, 1.02x per-request; thesis holds | — |
| R3 | Alembic compatibility is read off its API contract; alembic is not even installed here | **untested** | spike: install, `alembic revision --autogenerate` against a bare `MetaData`, confirm sane output |
| R4 | The `/tmp` harness adds a constant `run_until_complete` per call, compressing absolute ratios vs `bench micro` | known | port contenders into the real harness (§8 P2); treat within-harness deltas as the reliable part |
| R5 | Per-request acquire, and whether SQLAlchemy's implicit BEGIN/COMMIT costs materially | **CLOSED — measured, §2g.** Answered negatively: `AUTOCOMMIT` was *slower*. But per-request acquire itself costs Core ~0.18 ms vs rowform's ~0.03–0.08 ms, which is why §5d executes on rowform's pool | — |
| R6 | The declaration layer (§5a) is skeletal in the prototype: no nullable handling, no `Enum`, no composite PK, `primary_key` inferred from the field being named `id`, one hardcoded `python type -> SA type` map, and no `mapped_column()`-style field specifier (which would reintroduce rowform's dataclass default-probe trap, `docs/FINDINGS.md` "The `@model` metaclass") | known | §8 P3 |
| R7 | **All of §5c (statement-driven hydrator planning) is unverified** — sketched in `/tmp/q_codegen.py` and never executed. It is the piece that makes arbitrary `select()` shapes safe, so it gates correctness, not just ergonomics | **unmeasured** | §8 P3; test matrix in P3 below |
| R8 | Aliased tables / self-joins degrade to scalar tuples instead of models (distinct `Column` identities miss a `Table`-keyed registry) | known gap | accept for v1, or key the registry on `__clause_element__` output per-`FromClause` |
| R9 | `sqlalchemy.orm` becomes an import dependency purely for the `Mapped` annotation — the ORM module this design otherwise avoids entirely | accepted (§5) | none; revisit only if import cost measures material |
| R10 | **Metaclass conflict.** A base class forces `ModelMeta` on every model, so combining it with `ABC`, `Protocol`, or any other custom metaclass raises `TypeError: metaclass conflict`. Unfixable, not merely awkward — a decorator would compose freely, but the decorator route is unusable for typing reasons (§5b) | accepted (§9) | document; offer no workaround |
| R11 | **MRO-derived column order** silently reorders `CREATE TABLE` when a mixin is added, and Alembic does not diff column order (§5b-i) | **verified hazard** | P3 must pin and record column order explicitly |
| R12 | **Mixin fields are not constructor parameters** — visible as attributes, rejected by `__init__` (§5b-i) | **verified** | P3: share the metaclass with mixins, or document mixins as unsupported |

---

## 8. Phases

Sequenced so the cheap disqualifiers run first.

**P1 — Widen the shape (gates everything).**
Add a shape to `benchmarks/shapes/` with a `DateTime`, a `Numeric`, and a nullable
column, per `PLAN.md` §4's *"measure more than one shape"*. Verify stock Core and the
bypass paths produce **identical** values, not just identical row counts — this is
where R1 either kills the bypass on sqlite or bounds it. Byte-identical JSON via
`harness/equivalence.py`.
*Verify:* equivalence gate passes for every variant, or the failure is documented.

**P2 — Measure approach 4 in the real harness.** *(partially done: §2g answers the
speed question in the throwaway harness; what remains is porting it into
`benchmarks/` and covering postgres.)*
Port these contenders into `benchmarks/micro/contenders.py` (they are currently
throwaway `/tmp` scripts):
- `core_compiler_only` — §5's `CoreQuery` on the raw driver
- `core_dialect_seam` — approach 3, for comparison
- `core_tuned` — `.all()` + no `.mappings()`, approach 2
- floors: driver→dicts, and driver + the *same* hydrator (§2f — both, always)

Register them for sqlite **and** postgres (`PLAN.md` §14a notes `bench micro` has no
postgres runner yet — that gap must close here, since §4a predicts the asyncpg result
differs qualitatively, not just numerically). Include a per-request-connection arm and
an `AUTOCOMMIT` arm for R5.
*Verify:* `just bench micro run` reports all variants with spread; ratios reproduce
§2e's ordering within the harness's own noise floor.

**P3 — Decide, then build only if P2 holds.**
If approach 4 lands at ≈1.0–1.1x rowform *and* P1 shows no correctness gap, build
`Base`/`ModelMeta` (§5a) + the §5c statement planner + `CoreQuery` as **the** library
surface (§1a — not an optional module). Close R6, R7, R8, R11, R12.

Two requirements that are not optional polish:

- **Pin column order** (R11). Do not inherit `get_type_hints` MRO order. Derive it
  deterministically, assert it against the `Table`, and fail loudly if a mixin would
  reorder an existing table — Alembic will not catch this.
- **Decide on mixins** (R12). Either give mixins the same metaclass so their fields
  become constructor parameters, or reject non-`Base` bases at class creation with a
  clear error. Silently-visible-but-not-constructible is the one outcome to avoid.

The planner is the correctness-critical piece (R7) and needs a test matrix, not a
demo. At minimum, each asserting both the entity plan *and* the hydrated values:

| statement | expected result shape |
|---|---|
| `select(Model)` | `[Model, ...]` |
| `select(Model.id, Model.name)` | `[(int, str), ...]` |
| `select(Model.name, Model.id)` | `[(str, int), ...]` — **not** a mis-assigned `Model` |
| `select(A, B).join(...)` | `[(A, B), ...]` |
| `select(A, B.title).join(...)` | `[(A, str), ...]` |
| `select(A, B).outerjoin(...)` | `[(A, B \| None), ...]` |
| `select(func.count())` | `[(int,)]` |
| `select(A).join(A_alias)` | documented degradation (R8) |

*Verify:* the matrix passes; §5a/§5b examples become tests; `just typecheck` and
`just test` clean.

**P4 — Alembic spike (independent of P1–P3).**
Close R3. Cheap and can run in parallel; if it fails, the entire motivation in §1
collapses and P3 should not ship.

**P4a — Retire the query builder (§1a).**
Delete `expr.py`, `query.py`, `dml.py`, `dialects.py` (4,453 LOC) and rewrite
`__init__.py`'s surface. Strip SQL generation out of the three engines, leaving
execute + pool + transaction. Do this *after* P3 proves the replacement works, never
before — the old builder is the oracle for the new one during P3.
*Verify:* `just lint` and `just typecheck` clean; no dangling imports; the §5c test
matrix still passes against the trimmed tree.

**P4b — Rework the tests.**
26 files / 5,885 LOC today, and most of it tests a query builder that no longer
exists. Triage rather than port:
- **Delete**: `test_expressions`, `test_query_sql`, `test_ctes`, `test_predicates`,
  `test_grouping`, `test_joins_sqlite`, `test_join_kinds`, `test_set_operations`,
  `test_aliases`, `test_conditions`, `test_update_from`, `test_upsert`, `test_dml*`,
  `test_dialect*` — these now test SQLAlchemy, which tests itself.
- **Delete outright**: `tests/sqlalchemy_ports/` — porting SQLAlchemy's compiler tests
  is pointless once its compiler *is* the compiler.
- **Keep and expand**: `test_hydrators`, `test_model`, `tests/typing/` — plus the new
  §5c planner matrix and §5b-i's mixin/ordering decisions.
- **Rewrite**: `test_engines_*`, `test_transactions_*`, `conftest.py` — same intent,
  new construction path. `conftest.py`'s hand-written `CREATE TABLE` strings should
  become `metadata.create_all()`, which is the first dividend of this whole plan.
*Verify:* `just test` green; coverage of the hydrator/planner is *higher* than today's,
since that is now the entire value proposition.

**P4c — Rework the benchmarks.**
The suite's framing partly collapses: "rowform vs SQLAlchemy Core" is incoherent when
the library *is* Core for SQL generation. Reframe the contender set as:
- new library vs **SQLAlchemy ORM** (still a real comparison — the instrumentation gap)
- new library vs **stock Core result layer** (`Row`/`CursorResult`) — the §2e measurement,
  now the headline claim rather than a footnote
- new library vs **current rowform** — the regression gate for §1a's thesis; keep a
  pinned pre-rewrite commit runnable for exactly this
- the floors from §2f, both of them, permanently

`benchmarks/shapes/*.py` collapses from four parallel declarations (`User`,
`users_table`, `UserORM`, `UserDC`) to two: the new model, and the ORM comparison.
*Verify:* `just bench micro run` reports the new contender set; the pinned-commit
comparison shows no regression, or the regression is documented.

**P4d — Rework the docs.**
- `README.md` — the tagline, feature list and architecture diagram all describe a
  query builder that is being deleted. Rewrite around "SQLAlchemy schema and SQL,
  compiled hydration, no instance state."
- `docs/FINDINGS.md` — keep the hydration/codegen findings (still load-bearing), the
  orjson trap, and the metaclass rationale. Mark the query-builder findings historical.
- `docs/BENCHMARKS.md` — 1,462 lines of numbers for a library that will no longer
  exist in that form. Do **not** delete: relabel as pre-rewrite history with the
  commit that reproduces them, then add the P4c results as the current section.
- `docs/METHODOLOGY.md` — unaffected in substance; gains P5's corrections.
*Verify:* no doc describes a deleted API; every retained number names the commit it
came from.

**P5 — Record the methodology corrections.**
`docs/METHODOLOGY.md` gains two entries, in its existing numbered style:
- **correction 9** — Core contenders charged for `for row in result` and `.mappings()`,
  idioms a tuned caller avoids; worth 0.27–0.37 ms/1000 rows. Same class as
  correction 8. Affects `contenders.py:191-199` and every published `vs Core` ratio.
- **correction 10** — a floor whose hydrator is slower than the contender's is not a
  floor (§2f). Strengthens the existing *"include a floor"* invariant with *"and the
  floor must do strictly less work, including object construction."*

Then update `docs/FINDINGS.md` with §2a's two retractions.

---

## 9. Accepted risks

- **SQLAlchemy becomes a hard, unconditional dependency** (§1a). There is no
  standalone mode. The queued column-metadata work (SQL types, primary keys,
  autoincrement, foreign keys, indexes/constraints) is retired wholesale rather than
  reduced — SQLAlchemy already has all of it.
- **72% of the current library is deleted** (§1a), including two of the three largest
  modules. Everything the query builder does well — the join-graph validation, the
  dialect-validated feature gating, the typed expression tree — goes with it. The bet
  is that SQLAlchemy's equivalents are good enough that none of that was the moat.
- **`docs/BENCHMARKS.md`'s central comparison becomes historical.** Every published
  "rowform vs Core" ratio describes a library that will no longer exist in that shape.
  Retained as pre-rewrite history against a named commit (P4d), not deleted — but it
  stops being the current claim.
- **A single declaration shape, and it is the new one.** `@model` with
  `Column[int] = Column(int)` is retired in favour of `class User(Base)` with
  `Mapped[int]` (§5a, §5b). One shape to document; existing callers get no
  compatibility path.
- **Metaclass conflict is accepted (R10).** Every model must use `ModelMeta`, so
  `class User(Base, ABC)` or combining with `Protocol` fails outright. Judged rare
  enough for data-carrying classes to be worth the typing win, but it is a real
  capability the decorator route would have kept.
- **Coupling to `compiled.positiontup` / `construct_params`.** Public but low-level.
  Cheaper than approach 3's coupling, and pinned by P2's tests.
- **sqlite temporal types stay a footgun** until P1 proves the converter tables
  cover them. This is the single most likely reason to abandon the plan.


---

## 10. What was built, and where the plan was wrong

Written after the fact. The phases above are complete; this section records the
delta, because a plan that turned out to be right about everything would not have
been worth writing down.

### Every risk closed

| id | resolution |
|---|---|
| R1 | **Fired, and it was the most valuable thing in this document.** 8 of 13 columns came back wrong on sqlite over a widened shape. The fix was not a bigger converter table but *deleting the concept*: each column's own `result_processor`, asked of the dialect-adapted type. Now permanently gated by a `wide` benchmark shape and `tests/test_types.py`, both against stock Core as the oracle. METHODOLOGY correction 11. |
| R2 | Closed by §2g, and reproduced in the real harness: 1.0515 ms against Core's 1.6337 on `flat`. |
| R3 | **Closed.** Alembic autogenerate works off `Base.metadata` with no `DeclarativeBase` — full `create_table` with FKs, indexes and unique constraints, plus drift detection. `tests/test_alembic.py`. |
| R4 | Closed by porting the contenders into `benchmarks/`; the `/tmp` harness is gone. |
| R5 | Closed by §2g, negatively. |
| R6 | Closed: nullable, `Enum`, composite PK, explicit types, renames, `ForeignKey`, `__table_args__`, defaults and `init=False` all work, and `mapped_column()` exists without reintroducing the default-probe trap. |
| R7 | **Closed.** The planner is built and is the most heavily tested part of the library (`tests/test_planner.py`, plus the same matrix end-to-end in `tests/test_engines.py` on two backends). |
| R8 | **Closed rather than accepted.** The plan proposed documenting self-joins as a degradation. Resolving declared columns *through the FromClause actually selected* makes an alias match like any other table, so `select(A, A_alias)` hydrates two models. |
| R9 | Accepted as planned. `sqlalchemy.orm` is imported for `Mapped` and nothing else. |
| R10 | Accepted as planned, and asserted as a test rather than left as prose. |
| R11 | Mitigated, not eliminated. Order is inherited-first and deterministic, recorded as `__column_order__`, and pinnable. `tests/test_alembic.py` asserts the hazard is real — that autogenerate reports *nothing* for a reordering — so the mitigation cannot quietly stop being needed. |
| R12 | **Closed, and better than either option offered.** The plan proposed "share the metaclass, or reject mixins". Sharing it turned out to be enough on its own: a mixin under the same base is a dataclass to the checker, so its fields are real constructor parameters. |

### Three things the plan did not know

**1. The hydrator cannot be built at compile time.** postgres
`Numeric.result_processor` *raises* without a DBAPI type code, so §5d's eager
`hydrator_for(stmt, converters)` is impossible. Hydrators are planned on the first
execute, from `cursor.description` — which asyncpg has to prepare a statement to
supply. Once per statement, then cached.

**2. Binding needs as much machinery as hydrating.** §5d's `bind()` was three
lines. The real one applies SQLAlchemy's bind processors (sqlite cannot bind a
`Decimal`, `UUID` or `datetime` at all), handles `IN` expansion — which *rewrites
the SQL string per call* — and carries the caller's own literals as
`extracted_parameters`. Without that last part a cached statement silently
executes the first caller's values, which is the single worst bug found during
the build.

**3. A raw pool does not get the dialect's connection setup.** SQLAlchemy's
asyncpg dialect registers json/jsonb codecs in `on_connect`, and its
`JSON.result_processor` then returns `None` because "the driver already did it".
Running on a raw pool, nothing had. The engine now runs the dialect's own codec
coroutines. **Generalises past this bug**: adopting a dialect's *type* contract
means adopting its *connection* contract too.

### One thing decided differently

§5c had a lone model unwrap (`[User, ...]`) but a lone scalar stay a 1-tuple
(`[(int,)]`), on the grounds of mirroring SQLAlchemy. That is two rules where one
will do — and, decisively, it is not expressible in the type system:
`select(User)` and `select(User.name)` are `Select[Tuple[User]]` and
`Select[Tuple[str]]`, distinguishable only by arity. Since §5b makes exact typing
the reason the declaration layer is a base class at all, arity now decides alone:
**one entity yields that entity, two or more yield a tuple**, and `fetch_all` is
overloaded to match exactly.

### What the deletion actually cost

4,580 lines of library and 9,100 lines of tests, against ~1,400 lines of new
library. The join-graph validation, the dialect-validated feature gating and the
typed expression tree all went, as §9 said they would. What came back is larger
than what the plan promised: not just DDL, reflection and Alembic, but every SQL
construct SQLAlchemy can build — window functions, CTEs, lateral joins, dialect
extensions — none of which anyone now has to write or test here.

The bet in §9 was "SQLAlchemy's equivalents are good enough that none of that was
the moat". The measured answer is that the moat was the 215 lines in
`compile.py`, and it is still there.
