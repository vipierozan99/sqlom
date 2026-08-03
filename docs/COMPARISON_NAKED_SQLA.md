# rowform vs naked-sqla

[naked-sqla](https://github.com/ManiMozaffar/naked-sqla) is the closest thing this
project has to a peer: same diagnosis — the ORM's identity map, dirty tracking and
implicit loading cost more than they are worth in a service — and a different cure.
This explains the two designs, then compares them on the four axes that matter, with
measurements taken on this machine at commit `d26bf92` against SQLAlchemy 2.0.51.

Everything below is reproducible. The performance numbers come from `bench micro`
with naked-sqla registered as a contender, so it runs the same SQL, the same payload
builder and the same equivalence gate as every other arm
(`benchmarks/micro/contenders.py`). The compatibility results come from probe scripts
whose cases are listed inline.

**Summary.** naked-sqla wins on breadth of SQLAlchemy surface accepted and on how
little you have to change to adopt it. rowform wins on speed by roughly an order of
magnitude in the row layer, and on saying no clearly where naked-sqla says nothing at
all. naked-sqla is unmaintained — one release, October 2024, and one of its four
headline features has been broken by upstream for every SQLAlchemy release since.

---

## The approaches

Read this section before the four axes. Almost everything measured below follows
mechanically from one decision made two different ways, and knowing which decision it
is turns the tables from a list of results into a list of consequences.

### The one decision: where to cut SQLAlchemy's read stack

SQLAlchemy's ORM read path is five layers:

| | layer | what it does |
|---|---|---|
| 1 | Declarative + `Mapper` | class → `Table`, instrumented attributes, and a `_prop_set` of mapped properties |
| 2 | `ORMCompileState` | `select(User)` → SQL, plus a list of `_QueryEntity` objects saying what the columns *mean* |
| 3 | `orm.loading.instances()` | `CursorResult` → objects: per-entity row processors, then identity map, post-load, dedupe |
| 4 | `CursorResult` / `Row` | Core's result layer: result metadata and per-column type processors |
| 5 | DBAPI cursor | raw tuples |

Both libraries agree on three things. Core owns the SQL and the schema — neither
generates SQL. The session's bookkeeping is what has to go. And — arrived at
independently, which is worth noting — `execute()` should return a **real**
`sqlalchemy.Result` rather than a lookalike: naked-sqla feeds upstream's
`ChunkedIteratorResult`, rowform feeds `IteratorResult`/`ChunkedIteratorResult`, so on
both sides `.scalars()`, `.mappings()`, `.unique()`, `Row` attribute access and
`NoResultFound` are upstream's implementations and cannot drift. That is the single
most valuable compatibility property either library has and they share it.

They differ in where the knife goes.

**naked-sqla cuts inside layer 3.** Layers 1 and 2 survive intact, the per-entity row
processors *within* 3 are upstream's (`_MapperEntity.row_processor`,
`_ColumnEntity.row_processor`, `_BundleEntity.row_processor`), and only 3's stateful
tail is deleted. `naked_sqla/om/loading.py` is a fork of upstream's with the identity
map, post-load hooks, dedupe and all but one populator bucket removed. Rows are
assembled with `mapper.class_manager.new_instance()`. Roughly 1 400 lines.

**rowform cuts above layer 1.** It replaces 1 with its own metaclass (a real `sa.Table`
plus a stdlib dataclass, no `Mapper`, no `_sa_instance_state`, no instrumented
attribute), replaces 2 with a positional planner over `stmt.selected_columns`, replaces
3 with a code generator, and skips `Row`/`CursorResult` construction in 4 — keeping
exactly one thing out of that layer: the per-column type processors, inlined. Roughly
3 200 lines.

### What follows from naked-sqla's cut

Because layers 1 and 2 survive, everything *computed by upstream* keeps working for
free: `Mapped`, `mapped_column`, `MappedAsDataclass`, `__mapper_args__`,
`sa.orm.aliased()`, `Bundle`, `composite()`, `column_property()`, label styles,
`aliased(User, cte)`, Alembic. That is the real win, and it is why the adoption story
is "change your session import". Sync and async both work almost for free, because the
only difference is which connection object gets passed in — the mapping code is
identical, which is why `session.py` and `asession.py` are near-mirrors.

Two things follow just as directly, and neither is incidental.

**It requires an ORM entity to exist.** `instances()` reads
`compile_state._entities`. A plain `select(sa_table)`, a `union()`, a
`select(literal(1))` or a `text()` compiles to a non-ORM compile state that has no such
attribute, so it does not produce a wrong answer — it produces an `AttributeError` from
inside the library. The cut sits *below* the layer that knows what a row means, so a
statement that never entered that layer has nowhere to land.

**Its savings are bounded by what it kept.** Per row it still pays
`mapper.class_manager.new_instance()`, a dict of populator closures, and one
`getter(row)` call per column, then assembles through `ChunkedIteratorResult`. What it
removed — identity-key computation, dirty tracking, post-load — is real bookkeeping,
but it is the minority of the ORM's per-row cost. That is a structural ceiling rather
than a tuning gap: the row processors *are* the expensive part, and they are the part
it deliberately reuses. It is why the measured row layer lands nearer the ORM than
Core.

#### Why three ORM features fail silently, in one diff

This is the most important thing to understand about the design, and it is visible in
about fifteen lines. Upstream's `_instance_processor` loop
(`sqlalchemy/orm/loading.py:887`) has six populator buckets, explicit handling for the
deferred/raise-load sentinels, and an `else` branch:

```python
for prop in props:
    if prop in quick_populators:
        col = quick_populators[prop]
        if col is _DEFER_FOR_STATE:   ...        # deferred column
        elif col is _SET_DEFERRED_EXPIRED: ...
        elif col is _RAISE_FOR_STATE: ...        # lazy="raise"
        else:
            getter = result._getter(col, False)
            ...
    else:
        todo.append(prop)            # relationships and loader strategies
```

naked-sqla's version (`naked_sqla/om/loading.py:187`) has one bucket, no sentinel
handling, and no `else`:

```python
cached_populators = {"quick": []}
for prop in props:
    if prop in quick_populators:
        col = quick_populators[prop]
        getter = result._getter(col, False)
        ...
```

Every mapped property that is not a plain column present in the result falls off the
end of the loop — no processor, no error, no note. That single omission is the whole
explanation for four separate findings in the compatibility section:

* a **`relationship()`** is never populated, so the attribute falls through to the
  class-level lazy loader on an instance that was never given an identity key; a
  transient object has nothing to load from, so it reads as `[]`;
* **`selectinload()`** is a loader option consumed in the missing `todo` pass, so it is
  accepted, emits no second query, and yields `[]`;
* **polymorphic loading** lives in the sub-mapper recursion this version does not
  have, so it always instantiates the mapper the entity named — a `dog` row becomes an
  `Animal`;
* **`deferred=True`** is the one case where "ignore" becomes a crash instead of
  silence: the property *is* in `quick_populators`, but its value is a sentinel rather
  than a `Column`, and with upstream's sentinel branches gone that sentinel reaches
  `result._getter()`, where the lookup raises `IndexError` out of
  `sqlalchemy/engine/cursor.py`.

None of this is sloppiness — it is the cut. Keeping layer 1 means every ORM declaration
remains *expressible*; gutting layer 3 means only some of those declarations have
anything that runs. **The gap between "declarable" and "loadable" is where the silent
failures live, and it exists by construction.**

### What follows from rowform's cut

Removing layer 1 means there is no `Mapper`, so `sa.orm.aliased()` cannot work — it
inspects for one — and there is no entity system for a `Bundle` to resolve through.
Hence `rf.alias()`, and hence a `Bundle` degrading to a plain tuple. You re-declare
your models. The metaclass costs `ABC` and `Protocol` composition.

What that buys is that nothing sits between a driver tuple and the object. Layer 3
becomes a function `exec()`'d once per statement shape (`compile.py`):

```python
for f0, f1, f2, in rows:      # one UNPACK_SEQUENCE per row
    o0 = _new(_c0)            # object.__new__, no __init__ dispatch
    o0.id = f0                # plain STORE_ATTR — PEP 659 quickens it
    o0.active = _p2(f2)       # a call only where a processor exists
```

No populator dict, no per-column closure call, no `Row`, no `CursorResult`. The
generated source is attached to the function as `__source__`, so the codegen stays
inspectable rather than magic.

And because rowform owns the declaration layer, it cannot be handed a declaration it
will not load. There is no `relationship()`, no loader options, no polymorphic mapping;
`Mapped[Kid]` is a `DeclarationError` at class-creation time. The failure mode above is
unreachable rather than defended against — with one live counterexample,
`Mapped[list[X]]` silently becoming a `JSON()` column, which is the same species of bug
and is recorded as a defect below.

### The two questions they answer differently

**"What is a row?"** naked-sqla: *whatever the mapper says*. The entity list comes from
`ORMCompileState` and the mapping is authoritative. rowform: *whatever the statement
selected*, positionally — `planner.py` walks `stmt.selected_columns`, and a contiguous
run that is exactly some model's full column list becomes that model (compared by
identity, since `Column.__eq__` builds SQL rather than comparing); anything else is a
scalar.

That one difference accounts for most of the compatibility table in both directions.
rowform can hydrate a bare `Table`, a CTE, a union or a literal because it never needs
a mapper; naked-sqla cannot. Conversely naked-sqla gets `aliased(User, cte)` for free
where rowform needs `rf.alias(of=cte)` to *assert* that a CTE's columns are a model's —
and then validates the assertion, refusing an extra column rather than mis-assigning
fields. It is also why rowform's result shape is decided by arity: that is the only
rule the type system can express, so `select(User.name, User.id)` returns `(str, int)`
in that order and `fetch_all` can be typed exactly (`planner.py:41`).

**"Where does type conversion happen?"** Both answer "SQLAlchemy", which is the part a
hand-rolled mapper usually gets wrong and neither does. But:

* naked-sqla goes through `result._getter(col, False)` on a real `CursorResult`, so
  conversion is Core's metadata plus its type processors, as a closure call **per column
  per row**;
* rowform asks `column.type._cached_result_processor(dialect, coltype)` **once**, at
  hydrator-build time, and inlines the result into the generated source — and where the
  processor is `None`, which is most columns on asyncpg, the field compiles to a bare
  store with no call at all.

Identical answers; a different number of function calls, by a factor of the row count.
That is the mechanism behind the order-of-magnitude row-layer difference, not a
micro-optimisation. It is also why rowform builds the hydrator lazily on first execute
rather than at compile time: it needs the DBAPI type code from `cursor.description`,
and postgres `Numeric.result_processor` *raises* without one.

### Where each library's "no" lives

naked-sqla's boundary is implicit — it is wherever upstream's compile state and the
`"quick"` bucket happen to reach. Outside it you get SQLAlchemy's internal exceptions
(`AttributeError: attributes`, `AssertionError`, `IndexError`) or nothing at all. It
has two error classes of its own.

rowform's boundary is a place it can stand on. Because it owns the declaration layer it
refuses at class creation (`DeclarationError`), at plan time (`PlanError`), and at
execute (`StatementError: this statement produces no rows…` for a bare `text()`) — a
named hierarchy, plus an `observer` hook and DEBUG logging that carries the generated
source.

That is the maintainability argument in design terms rather than in test counts:
**owning a layer is what gives you a vocabulary to say no in.** Wrapping one leaves you
speaking upstream's error messages about internals your caller never touched.

### The maintenance shape of each bet

Both libraries read SQLAlchemy's private surface heavily and neither is safe. The
*kind* differs, and it matters more than the count:

* naked-sqla depends on the ORM's **structure** — entity classes, populator
  dictionaries, compile-state attributes, the path/registry system,
  `Column._make_proxy`. That is SQLAlchemy's most actively developed and most
  internally coupled subsystem, and the dependency is a **fork**: upstream refactors
  have to be mirrored, not merely tolerated. `_make_proxy` gaining two required
  arguments is exactly this failure, and it is what broke views in 2.0.36.
* rowform depends on the **compiler and result plumbing** — `_generate_cache_key`,
  `construct_params`, `positiontup`, `_bind_processors`,
  `_process_parameters_for_postcompile`, `_cached_result_processor`,
  `SimpleResultMetaData`. Narrower, and every one is a **leaf call** rather than a
  structure it must stay in step with. It is also the surface every dialect exercises,
  so it moves slowly in practice.

A fork has to be re-synced; a set of calls only has to keep resolving. Neither
justifies going unpinned, which is separately why rowform caps at `<2.1` with a weekly
canary against SQLAlchemy `main`.

### Which design is better positioned for what

naked-sqla's cut has one genuine long-run advantage: **it inherits improvements**.
Anything the ORM's entity system learns — a new bundle type, a new label style, a new
construct legal inside `select()` — works there without a line of code, where rowform
would have to implement it. If SQLAlchemy ever exposed a supported "load without a
session" entry point, naked-sqla collapses into a thin shim and rowform does not.

rowform's cut has the advantage that it is **finished**. The set of things it must
understand is "columns, and which runs of them are a model", and that set does not grow
with SQLAlchemy's ORM. The price is that its hardest 150 lines write Python at runtime,
so correctness has to come from an oracle suite rather than from reading the code —
which is the honest inversion of naked-sqla's position, where every line is readable
and the risk is in what upstream does next.

---

## 1. SQLAlchemy compatibility

### Where naked-sqla is clearly ahead

**Your models are unchanged.** They are stock `DeclarativeBase` classes, so
`Mapped`, `mapped_column`, `MappedAsDataclass`, `__mapper_args__`,
`sa.orm.aliased()`, `sa.orm.Bundle`, `composite()`, `column_property()`, Alembic's
`target_metadata` and every ORM idiom for *declaring* a schema keep working verbatim.
rowform requires you to re-declare the model on `rf.Base` — `rf.mapped_column`
instead of `mapped_column`, `rf.alias()` instead of `sa.orm.aliased()` — and its
metaclass makes `class User(Base, ABC)` a `TypeError`.

**It has a sync API.** `naked_sqla/om/session.py` mirrors the async one. rowform
refuses a sync `Engine` outright:

```
ConfigurationError: rf.Engine wraps a SQLAlchemy AsyncEngine, got Engine.
```

**Wider Python support.** naked-sqla is `>=3.9`; rowform is `>=3.11` (it depends on
`dataclass_transform` and on 3.11's specialising interpreter for the generated
stores).

**No version cap.** naked-sqla declares `sqlalchemy>=2.0.0`. rowform pins
`>=2.0.18,<2.1` deliberately, because it reads a dozen private compiler and result
internals; a caller who wants 2.1 has to wait for a release. See the *Maintainability*
section for why the unbounded declaration is worse than it looks.

### Where rowform is ahead

Both libraries accept `Executable` in `execute()`. Only one of them can execute one.

| statement | naked-sqla | rowform |
|---|---|---|
| `select(plain_sa_Table)` | `AttributeError: attributes` | works |
| `select(literal(1))` | `AttributeError: attributes` | works |
| `union(select(U.id), select(U.id))` | `AttributeError: 'CompoundSelectCompileState' object has no attribute 'attributes'` | works |
| `text("select …")` | `AssertionError` | refused by name: `StatementError: this statement produces no rows…` |
| `text(…).columns(…)` | not tested | works |

naked-sqla's session requires an ORM entity somewhere in the statement, because
`context.orm_execute_statement` asserts a compile state and `instances()` reads
`compile_state._entities`. Anything Core-only crashes with a bare `AttributeError`
from inside its internals — no named error, no message. In practice that means a
codebase adopting it keeps a second execution path for its Core queries.

**Streaming.** naked-sqla has none. `execution_options={"yield_per": N}` produces
`AsyncMethodRequired: Can't use the AsyncConnection.execute() method with a
server-side cursor` — its `orm_execute_statement` calls `conn.execute`, never
`conn.stream`. rowform has `fetch_iter(stmt, chunk=N)` over a server-side cursor.

**`insert().returning(Model)` does not hydrate in naked-sqla.** It hands back a raw
row where `update()` and `delete()` hand back entities:

```
insert().returning(Model)  -> int:  (2, 'new')
insert().returning(cols)   -> int:  (3, 'n3')
update().returning(Model)  -> U:    (<U object …>,)
delete().returning(Model)  -> U:    (<U object …>,)
```

This reproduces on naked-sqla's own pinned SQLAlchemy 2.0.35, so it is an original
bug rather than upstream drift.

### The part that should decide it for an existing application

rowform's second goal is that a SQLAlchemy application adopts it *one query at a
time*, inside the session it already has. `db.connect(bind=session)` runs on that
session's connection, sees its uncommitted writes and rolls back with it. That is a
tested, documented, first-class entry point.

naked-sqla can do the same thing, and does not advertise it:
`NkSession(await sa_session.connection())` works — its `AsyncSession` takes an
`AsyncConnection`. Both libraries passed the probe case "read inside a foreign
`AsyncSession` transaction". So this is a documentation gap on naked-sqla's side, not
a capability gap.

### The ORM features that are declarable but not loaded

This is the sharpest edge in naked-sqla, and it follows directly from "your models
are unchanged": every ORM mapping feature is *declarable*, and only some of them
work. The mechanism is the missing `else` branch in its `_instance_processor` — see
[Why three ORM features fail silently](#why-three-orm-features-fail-silently-in-one-diff);
what follows is what that costs, one model per feature so each result is attributable:

| declared on a stock ORM model | naked-sqla |
|---|---|
| `composite()` | **works** |
| `column_property(<SQL expr>)` | **works** |
| `mapped_column(deferred=True)` | **raises** `IndexError: list index out of range`, from inside `sqlalchemy/engine/cursor.py` |
| `relationship()`, attribute read | **silently returns `[]`** |
| `.options(selectinload(...))` | **silently returns `[]`** — the option is accepted, no second query is emitted |
| joined/single-table polymorphic identity | **silently returns the base class** — a row whose discriminator says `dog` hydrates as `Animal` |

The three "silently" rows are the problem, and the comparison to make is against stock
SQLAlchemy rather than against nothing: under SQLAlchemy asyncio, touching an unloaded
`relationship()` raises `MissingGreenlet`. Under naked-sqla it returns an empty
collection. An explicit `selectinload()` — the documented way to eager-load under
asyncio — is accepted and does nothing at all. Neither is documented as unsupported;
the README says relationships "are not supported", which is true, but "not supported"
here means "reads as empty", not "raises".

rowform cannot reach these failure modes because it cannot express them: there is no
`relationship()`, no loader options, no polymorphic mapping, and `Mapped[Kid]` raises
`DeclarationError: no SQLAlchemy type registered for <class 'Kid'>`. It has one
analogous sharp edge, found while writing this: **`Mapped[list[Kid]]` is silently
accepted as a `JSON()` column**, because `DEFAULT_TYPE_MAP` maps `list` to JSON and
the element type is never inspected. Someone porting `kids: Mapped[list[Child]]` off
the ORM gets a JSON column instead of an error. That is a real defect and worth
fixing.

### Compatibility probe, in full

Twenty cases, each written the natural way for each library
(`select` shapes, DML, transactions, type fidelity):

| case | naked-sqla | rowform |
|---|---|---|
| `select(Model)` | pass | pass |
| `select(Model.col)` | pass | pass |
| `select(Model.a, Model.b)` | pass | pass |
| `select(A, B).join()` | pass | pass |
| `outerjoin` → `None` for the unmatched entity | pass | pass |
| `select(func.count())` | pass | pass |
| self-join through an alias | pass | pass |
| entity out of a CTE | pass | pass |
| `orm.Bundle` | pass | **fail** — degrades to a plain tuple |
| `select(Model, expr)` | pass | pass |
| `insert().returning(Model)` | **fail** — raw row | pass |
| `update().returning(Model)` | pass | pass |
| `DateTime`/`Numeric`/`Enum`/`UUID` fidelity on sqlite | pass | pass |
| `.mappings()` on the returned `Result` | pass | pass |
| streaming / chunked read | **fail** — no streaming | pass |
| read inside a foreign `AsyncSession` transaction | pass | pass |
| `begin_nested()` savepoint | pass | pass |
| `executemany` | pass | pass |
| sync (non-async) API | pass | **fail** — async only |
| `relationship()` attribute access | **fail** — silently `[]` | **fail** — cannot be declared |

Both return a real `sqlalchemy.Result` from `execute()`, so `.scalars()`,
`.mappings()`, `.tuples()`, `.unique()` and `NoResultFound` are upstream's
implementation on both sides. That is the single most important compatibility
property either library has, and they share it.

**Verdict on axis 1.** naked-sqla accepts a wider *declaration* surface and a wider
runtime (sync, 3.9+). rowform accepts a wider *statement* surface and fails loudly
where naked-sqla fails silently. If your models are already ORM models and your
queries all name entities, naked-sqla is a smaller diff. If your codebase mixes Core
and ORM statements, or leans on relationships, naked-sqla will surprise you — twice
quietly.

---

## 2. Developer experience

### Reading a result

naked-sqla is SQLAlchemy's idiom, exactly:

```python
async with db.begin() as session:
    users = (await session.scalars(sa.select(User))).all()        # list[User]
    rows  = (await session.execute(sa.select(User, Post))).all()  # list[Row[tuple[User, Post]]]
```

Two steps — execute, then choose an accessor — and typed the way SQLAlchemy types it:
`scalars(TypedReturnsRows[Tuple[_T]]) -> ScalarResult[_T]`. Nothing new to learn, and
`session.tuples()` / `session.scalars()` are pleasant shorthands.

rowform offers both, told apart by name:

```python
users = await conn.fetch_all(sa.select(User))                     # list[User]
pairs = await conn.fetch_all(sa.select(User, Post).join(Post))    # list[tuple[User, Post]]
name  = await conn.fetch_one(sa.select(User.name).limit(1))       # str | None
users = (await conn.execute(sa.select(User))).scalars().all()     # SQLAlchemy's way
```

`fetch_all` is overloaded on arity, so one selected entity gives that entity and two
or more give a tuple, with no `.scalars()` and no cast. `fetch_one` returns
`T | None`. Fewer keystrokes and one less concept per read; the cost is that it is
rowform's vocabulary, not SQLAlchemy's — which is exactly why `execute()` exists
beside it.

### Typing

Both ship `py.typed`. Both are checked in CI (pyright standard mode for naked-sqla,
basedpyright for rowform).

The difference is what the types *prove*. naked-sqla inherits SQLAlchemy's
`TypedReturnsRows` plumbing and adds overloads on top; it is as precise as
SQLAlchemy's own `Session.execute`, which is to say good. It also leaks private
SQLAlchemy names into its public signatures — `_CoreAnyExecuteParams` and
`_CoreKnownExecutionOptions` appear in every `execute`/`scalars`/`tuples` overload —
so its public API is annotated in terms of symbols upstream is free to move.

rowform's read overloads are the reason its API is shaped the way it is: arity alone
decides the result shape *because* that is the only rule expressible in the type
system (`planner.py:41`). And the typing is tested rather than asserted:
`tests/typing/positive.py` (299 lines) must check clean, and
`tests/typing/negative.py` (214 lines) is a file of deliberate mistakes each carrying
a `# pyright: ignore` that `reportUnnecessaryTypeIgnoreComment = "error"` verifies is
still necessary. If a bad call stops being a type error, the suite fails. naked-sqla
has no equivalent.

### Declaring

naked-sqla: nothing to declare, you already have models. This is a genuine and large
DX win, and it is the whole reason its adoption story is "change your session import".

rowform: one class does three jobs, and the payoff is that instances are plain
dataclasses — `repr()`, `==`, `dataclasses.fields()`, `orjson.dumps(user)` with no
encoder, `frozen=`/`kw_only=`/`slots=` through class keywords. naked-sqla's instances
are ORM instances: they carry `_sa_instance_state`, so `orjson.dumps` needs help, and
`MappedAsDataclass` is the closest it gets to a plain object.

### Errors and observability

naked-sqla has a two-class error hierarchy (`BaseNakedSQLAException`, plus
`InvalidSessionState` and `UnknownEntity`). Everything else surfaces as whatever
SQLAlchemy raised — including the `AttributeError`/`AssertionError`/`IndexError`
cases in the tables above, which is where the DX cost of "accept any `Executable`"
lands.

rowform has a named hierarchy (`RowformError` and six subclasses) and an `observer`
hook called after every statement with SQL, elapsed time and row count, plus DEBUG
logging that carries the generated hydrator source. That last one matters more than it
sounds: the codegen is the part a reader cannot see, and `hydrate.__source__` makes it
inspectable.

### Documentation

naked-sqla has a published mkdocs site with an mkdocstrings API reference, a
migration guide and a why-page arguing the design. Its docstrings are long,
example-rich, and pleasant. rowform has ~141 KB of markdown in `docs/` — a guide, an
API reference, a benchmark methodology with a log of thirteen wrong published claims,
and a findings document — but no rendered site.

For a reader deciding whether to adopt, naked-sqla's docs are friendlier; rowform's
are more complete and considerably more candid about their own numbers.

**Verdict on axis 2.** naked-sqla for smallest possible change and familiarity.
rowform for precision — exact row types without accessor calls, tested types, named
errors, an observability hook. Call it a draw weighted by which you value.

---

## 3. Maintainability

### Size

| | naked-sqla | rowform |
|---|---|---|
| library | 1 394 lines | 3 204 lines |
| tests | 585 lines, 4 files, **9 tests** | 4 840 lines, 22 files, **783 tests** |
| benchmark harness | 1 script, 295 lines | a CLI with shapes, floors, isolation, equivalence gate |
| coverage gate | none | `--cov-fail-under=90` in CI |

naked-sqla is smaller, and for a library that is mostly a fork of upstream code that
is the right shape. But 9 tests for a row layer is thin: `select(Model)`,
`select(Model.col)`, two entities, one view case, and two tests that exist to
demonstrate ORM misbehaviour. There is no type-fidelity test, no transaction test, no
test for the DML-returning asymmetry the probe found, and no test that a Core-only
statement is rejected rather than crashing.

rowform's suite is large because the design demands it: engine and transaction tests
run against both sqlite and PostgreSQL from one parametrised fixture (the two differ
exactly where hydration is most exposed), the row path is checked against Core as an
oracle over Hypothesis-generated statements, and there is an Alembic autogenerate
test. That is the cost of generating code: you cannot read the hydrator to know it is
right, so it has to be verified against something.

### Reliance on SQLAlchemy's private surface

Both libraries do it. [The maintenance shape of each
bet](#the-maintenance-shape-of-each-bet) argues why the two dependencies are different
in kind — a fork of the ORM's structure against a set of leaf calls into the compiler.
This is the evidence for it, plus how each project manages the bet.

naked-sqla imports `ORMCompileState`, `FromStatement`, `_MapperEntity`,
`_ColumnEntity`, `_BundleEntity`, `_QueryEntity`, `SimpleResultMetaData`,
`ChunkedIteratorResult`, `_CoreAnyExecuteParams`, `_CoreKnownExecutionOptions`, and
calls `cursor._raw_all_rows()`, `result._getter()`, `mapper._prop_set`,
`mapper.class_manager.new_instance()`, `compile_state._has_mapper_entities`,
`compile_state._entities`, `path.get(...)`, `Column._make_proxy()`,
`_columns._populate_separate_keys()`. It declares `sqlalchemy>=2.0.0` with no ceiling
and no canary.

rowform calls `_cached_result_processor`, `_generate_cache_key`, `construct_params`,
`_process_parameters_for_postcompile`, `_bind_processors`, `escaped_bind_names`,
`positiontup`, `_limit_clause`, `_returning`, `SimpleResultMetaData`,
`IteratorResult._source_supports_scalars`, `util.await_only`, and the asyncpg
adapter's `_started`/`_start_transaction`. It declares `sqlalchemy>=2.0.18,<2.1`,
documents the exact list in `pyproject.toml` next to the pin, runs a weekly canary
job against SQLAlchemy's `main` branch that is deliberately *not*
`continue-on-error`, and runs a `floor` CI job that resolves every dependency to its
declared minimum so the floor stays a fact rather than a guess.

The consequence is measurable. **naked-sqla's view support has been broken since
SQLAlchemy 2.0.36** (October 2024, days after its only release):

```
$ pip install naked-sqla sqlalchemy==2.0.36   # …through 2.0.51
TypeError: Column._make_proxy() missing 2 required positional arguments:
           'primary_key' and 'foreign_keys'
```

Bisected: 2.0.35 passes, 2.0.36 through 2.0.51 fail at import time of any module
declaring a view. Because the dependency is unbounded, `pip install naked-sqla`
today gives you this. Views are one of its four advertised features.

A second casualty on 2.0.51: `test_complicated_update_map_incorrectly_in_sqlalchemy`
fails, because it asserts that SQLAlchemy's ORM returns stale data after a complicated
update — and upstream has since fixed that. The library's *motivating* example no
longer reproduces. That is not naked-sqla's fault, but a maintained project would have
noticed.

Running its suite at head:

```
$ pytest                      # SQLAlchemy 2.0.35 (its lockfile)
9 passed

$ pytest                      # SQLAlchemy 2.0.51
ERROR tests/test_view_failed.py  - TypeError: Column._make_proxy() …
1 failed, 6 passed
```

### Activity

| | naked-sqla | rowform |
|---|---|---|
| last commit | 2024-10-12 | active |
| PyPI releases | one, `0.1.0`, 2024-10-12 | not published yet |
| CHANGELOG | none | none |
| LICENSE | MIT in package metadata, no `LICENSE` file | **neither** |
| CI | tox py3.9–3.11, ruff, pyright | lint/typecheck, 3.11–3.14 matrix, PostgreSQL 16 service, dependency-floor job, weekly SQLAlchemy-`main` canary, benchmark workflow |
| CI Python vs declared | declares `>=3.9`, tests 3.9–3.11 (tox lists 3.12; the workflow matrix does not) | tests every version it declares |

### rowform's side of the ledger, stated plainly

* **Not released.** No PyPI package, no LICENSE file, never run in production. Its
  own README says so. naked-sqla is `pip install`-able today, and that is a real
  advantage even in its current state.
* **Deliberately capped at `<2.1`**, so a SQLAlchemy 2.1 adopter is blocked until
  someone raises it with the suite green.
* **Nearly 2.5× the code**, and the hardest 150 lines of it are a code generator that
  `exec()`s a string. That is a maintenance liability that no amount of test coverage
  removes; it is defended by coverage, an oracle test, and `__source__` on the
  generated function, not eliminated.
* **A metaclass per model**, so `ABC` and `Protocol` combinations are a `TypeError`.
* **Async only, 3.11+.**
* The `Mapped[list[X]] -> JSON()` silent acceptance found above.

**Verdict on axis 3.** rowform, decisively, and not because it is bigger. naked-sqla
is a fork of upstream internals with no ceiling, no canary, 9 tests, and 22 months of
upstream releases it has never been run against — one of which broke a headline
feature. The design would be defensible with a maintainer; without one, "thin layer
over SQLAlchemy's private ORM internals" is the riskiest possible shape.

---

## 4. Performance

`bench micro`, sqlite, 200 000-row table, 1 000 rows per read, 500 iterations after
100 warmup, 3 trials, **one contender per process**, GC off, pinned to CPUs 0–3.
Every arm runs identical SQL compiled by Core, builds its payload with the same
`{field: getattr(obj, field)}` comprehension, and reads inside `BEGIN`…`COMMIT`. The
equivalence gate holds every arm's JSON byte-identical before timing starts — it
passed for naked-sqla on all three shapes, which is also a correctness check.

Medians in ms, lower is better. `x` is against rowform.

| | flat | join | wide | | flat | join | wide |
|---|---|---|---|---|---|---|---|
| floor: hand-rolled dicts *(no SQLAlchemy)* | 1.6327 | 2.5211 | 4.9733 | | 0.72x | 0.71x | 0.84x |
| floor: same pool + transaction → dicts | 1.8825 | 3.0230 | 5.3567 | | 0.83x | 0.85x | 0.90x |
| **rowform** `fetch_all()` | **2.2694** | **3.5620** | **5.9523** | | **1.00x** | **1.00x** | **1.00x** |
| rowform `execute().scalars()` | 2.3563 | — | 5.9755 | | ~1.04x | — | ~1.00x |
| SQLAlchemy Core (positional) | 2.4046 | 3.7411 | 6.1401 | | ~1.06x | ~1.05x | ~1.03x |
| SQLAlchemy Core (`.mappings()`) | 5.1558 | — | — | | 2.27x | — | — |
| **naked-sqla** | **5.0945** | **9.6531** | **10.1110** | | **2.24x** | **2.71x** | **1.70x** |
| naked-sqla (`MappedAsDataclass`) | 4.8673 | — | — | | 2.14x | — | — |
| SQLAlchemy ORM | 8.6512 | 14.2168 | 14.2166 | | 3.81x | 3.99x | 2.39x |
| SQLAlchemy ORM (`MappedAsDataclass`) | 8.4415 | 13.9849 | 14.2163 | | 3.72x | 3.93x | 2.53x |

Worst trial-to-trial spread across these cells: 20.0% (Core on `flat`); most are
under 10%. Ratios within ±10% of each other are marked `~` and are not ordered.

**These absolutes are not comparable to the table in `README.md`.** That one was taken
on a different machine with 1 500 iterations; this one is a 4-core box with 500. The
*ratios* between arms are what carries over, and rowform's ratios against Core and the
ORM here (~1.06x, 3.8x on flat) are close to the published ones (~1.08x, 4.4x), which
is the check that this run is measuring the same thing.

Three readings.

**naked-sqla is faster than the ORM, by less than it claims.** Its README says
"nearly twice as fast as ORMs". Measured: **1.70x on flat, 1.47x on join, 1.41x on
wide**. Roughly right on the simplest shape, optimistic on the others.

**naked-sqla is slower than the SQLAlchemy Core it is a "thin layer" on** — 2.1x on
flat, 2.6x on join, 1.65x on wide. Core's `Row`/`CursorResult` hands back untyped
tuples rather than objects, so this is not a like-for-like product; but rowform hands
back *objects* and ties with Core, so the gap is not the price of object construction.

**rowform is 1.7–2.7x naked-sqla end to end.** The margin is smallest on `wide`,
which is the shape full of `DateTime`/`Numeric`/`Enum`/`Uuid` columns where
per-column type processors dominate and both sides run the same ones — which is
exactly why `wide` is in the table.

### With the driver removed

End-to-end numbers understate the difference, because on sqlite most of a read is not
the row layer. The `mock` backend cans the driver at the DBAPI seam and leaves
everything above it running for real. This is the cell that answers "which mapper is
cheaper":

| row layer alone, per 1 000 rows | flat | join |
|---|---|---|
| hand-written dicts *(parsing floor)* | 0.1874 | 0.4361 |
| **rowform** | **0.2748** | **0.5770** |
| SQLAlchemy Core (positional) | 0.4590 | — |
| **naked-sqla** | **2.7340** | **6.3373** |
| SQLAlchemy ORM | 4.0664 | 7.9547 |

So the honest characterisation of naked-sqla's row layer:

* **1.49x cheaper than the ORM's** on flat, 1.26x on join. That is what removing the
  identity map, dirty tracking and post-load hooks buys — real, and much smaller than
  the end-to-end ratio against the ORM suggests, because the ORM's remaining cost is
  the row processors and instance construction that naked-sqla keeps.
* **6.0x more expensive than stock Core's** on flat. It sits much closer to the ORM
  than to Core.
* **10.0x rowform's** on flat, **11.0x** on join.

An order of magnitude is not a tuning difference; it is the cut, and this cell is where
[the approaches](#the-approaches) show up as a number. naked-sqla pays per row for
`new_instance()`, a populator dict and a `getter(row)` call per column, then assembles
through `ChunkedIteratorResult`. rowform pays one `UNPACK_SEQUENCE` per row and one
quickened `STORE_ATTR` per field, with the type processor inlined only where the column
needs one. Both get their conversion from the same place; one of them asks per row and
the other asked once.

Caveat, from `benchmarks/engines/mock.py`: rowform's mock cans the driver one layer
higher than SQLAlchemy's, so `rowform (mock)` and the SQLAlchemy-side mocks are each
a floor for their own library rather than a strict head-to-head. The
naked-sqla-vs-ORM comparison in that table *is* strict — same seam, same hoisted
checkout, differing only in what turns the `CursorResult` into objects.

**Verdict on axis 4.** rowform, by a wide margin, and the margin is structural. On
PostgreSQL with asyncpg the end-to-end gap would narrow further (transport is a larger
share, and most asyncpg columns need no processor at all) — that was not re-measured
here and should not be assumed.

---

## Choosing

**Use naked-sqla if** you have an existing SQLAlchemy ORM codebase, you want the
identity map and dirty tracking gone tomorrow with a one-line import change, you need
sync or Python 3.9, and 1.4–1.7x off the ORM is enough. Then read the silent-failure
table twice — if the codebase declares `relationship()` anywhere, or executes any
Core-only statement, budget for that. And pin `sqlalchemy<2.0.36` if you use its
views, or vendor the 20 lines of `view.py` and fix `_make_proxy`.

**Use rowform if** the read path is the bottleneck and 10x in the row layer is worth
re-declaring your models for; if you want exact row types without accessor calls; or
if you want a library that refuses what it cannot do instead of returning `[]`. Then
weigh that it is unreleased, unlicensed, async-only, 3.11+, capped below SQLAlchemy
2.1, and that its fastest 150 lines write Python at runtime.

**Use neither if** your reads are small and local. Both libraries trade the unit of
work — insert ordering, write batching, knowing what changed — for row-layer speed,
and at low row counts against a nearby database the ORM's cost is not what your
latency is made of.

---

## Reproducing

```bash
uv sync --all-extras --all-groups     # installs naked-sqla into the bench group

for s in flat join wide; do
  just bench micro run --shape=$s --backend=sqlite --rows=200000 --limit=1000 \
    --iterations=500 --warmup=100 --trials=3 --isolate --pin=0,1,2,3 --record
done
for s in flat join; do
  just bench micro run --shape=$s --backend=mock --limit=1000 \
    --iterations=500 --warmup=100 --trials=3 --isolate --pin=0,1,2,3 --record
done
```

Recorded runs: `benchmarks/results/runs/2026-08-03T07-3{0,2}*_ad6bd3d` (flat, join)
and `…_d26bf92` (wide, and both mock cells), all `quotable=True`. The compatibility
probes are not committed; their cases are listed verbatim in the tables above and each
is a dozen lines of `sa.select(...)` through both libraries.

The naked-sqla checkout used was `6b29842` ("Add purpose to documentation"), its
`main` at the time of writing.
