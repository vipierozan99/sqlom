# ⚡ sqlom: Zero-Overhead Async Data Layer for Python

`sqlom` is a data access library concept for Python built for high-throughput HTTP services (FastAPI, Sanic, Granian). It aims to reduce ORM overhead by skipping session tracking, identity maps, and dynamic class reflection, pairing a **descriptor-driven query builder** with an **`asyncpg`-backed execution engine** that hydrates rows into `@dataclass(slots=True)`-style objects and serializes them via `orjson`.

It relies on pure Python plus existing C-extensions (`asyncpg` + `orjson`) rather than a custom Rust/FFI layer.

> **Status:** this is a design proposal, not a released or benchmarked library. Numbers and code below are illustrative until validated against a real implementation.

---

## 🎯 Key Features (proposed)

* **Postgres `json_agg` support:** Push row shaping *and* JSON encoding into Postgres instead of doing it in Python. Listed first because it measured as by far the biggest win — see [Performance](#-performance).
* **Compiled hydration:** A per-model `row -> object` function is code-generated once, so field stores are plain `STORE_ATTR` bytecode against a fixed slot rather than a `setattr()` loop. ~4.9x faster than the reflective path *in isolation* (but see the honest framing below — hydration is only ~12% of the pipeline).
* **Query builder:** A SQLAlchemy-Core-like builder (`User.id > 10`) built on descriptors.
* **Two schema styles:** a custom-metaclass model, or real stdlib `@dataclass(slots=True)` models that still support `User.id > 100`.
* **Async-first:** Native `asyncpg` pool integration.

---

## 📦 Installation

```bash
pip install sqlom
```

---

## 🛠️ Usage Example

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

**Static typing caveat:** because `User.id` returns a `ColumnExpr` (not an `int`) at the class level, pyright/mypy will not natively infer `User.id > 100` as a valid, typed comparison without a plugin or `.pyi` stub that teaches the type checker about the descriptor's dual return type. This is the same category of problem as building first-class typing for a field-name descriptor pattern — don't advertise "full IDE autocomplete + static type inference" until that stub/plugin actually exists and is verified against `mypy --strict` and `pyright` in CI.

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

⚠️ **If you use this style, pass `sqlom.DATACLASS_DUMP_OPTION` to `orjson.dumps`.** orjson recognizes dataclasses natively and will silently *ignore* your `default=` hook for them — and its native path is slow for slotted classes (details below). That flag (`orjson.OPT_PASSTHROUGH_DATACLASS`) routes them back to sqlom's compiled hook and is worth ~30% end-to-end.

### 2. Query & Serialization (FastAPI)

```python
from fastapi import FastAPI
from fastapi.responses import Response
import orjson
from sqlom import Query, DatabaseEngine, User

app = FastAPI()
db = DatabaseEngine(dsn="postgresql://user:pass@localhost/db")

@app.on_event("startup")
async def startup():
    await db.connect()

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

### 3. Skip the objects entirely

For a read-only endpoint that only ever becomes JSON, materializing Python objects is pure overhead. `fetch_json` pushes row shaping *and* JSON encoding into Postgres (`json_agg` / `json_build_object`) and hands back bytes ready for the response body — by a wide margin the fastest path measured here:

```python
@app.get("/users")
async def get_users():
    query = Query(User).where(User.is_active == True).where(User.id > 100).limit(100)
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
2. **Compiled hydration.** A model's column layout is fixed and known once, so sqlom generates a specialized `rows -> [instance]` function per model (inspect it via `fn.__source__`). Field stores are written as plain attribute assignments so CPython 3.11's specializing interpreter can quicken them to `STORE_ATTR_SLOT`.
3. **Slotted storage.** Instances use `__slots__`, so attribute storage is a fixed-size array rather than a `__dict__` — 72 vs 116 bytes per object here. Note the tradeoff: this is also what forces orjson off its native dataclass fast path (see [the orjson dataclass trap](#the-orjson-dataclass-trap)).
4. **Or skip 2 and 3 entirely.** Path (B) does the shaping in SQL and never builds a Python object.

None of this makes the pipeline "zero-copy" — data still moves from the C-level tuple into Python object storage into JSON bytes. The claim to make is "fewer intermediate Python-level allocations than an ORM identity-map path," not "no copying happens." And per the profile, path (A)'s remaining cost is dominated by the driver, not by sqlom.

---

## 📊 Performance

[`benchmarks/bench_sqlite.py`](benchmarks/bench_sqlite.py) runs every approach against the *same* sqlite file through the *same* driver, so the database round trip is held roughly constant and the measured delta is the object-shaping path. 1,000 rows returned from a 200,000-row table, 300 iterations after 30 warmup, `time.perf_counter`, single process, no concurrency.

**Every approach is asserted to emit byte-identical JSON before timing starts.** That check matters — see the correction note below.

| approach | mean | median | p95 | resp/sec | vs. ORM |
|---|---|---|---|---|---|
| **sqlom DB-side JSON** (`json_agg`, no Python objects) | 0.48 ms | 0.46 ms | 0.57 ms | 2106 | **13.5x** |
| sqlom compiled, batch hydrator | 1.06 ms | 1.00 ms | 1.14 ms | 941 | 6.0x |
| `@model` dataclass + `OPT_PASSTHROUGH_DATACLASS` | 1.10 ms | 1.03 ms | 1.24 ms | 912 | 5.8x |
| sqlom compiled, per-row hydrator | 1.13 ms | 1.05 ms | 1.27 ms | 886 | 5.7x |
| `@model` dataclass, orjson native path | 1.51 ms | 1.31 ms | 2.24 ms | 664 | 4.3x |
| sqlom reflective (`hydrate()` + `as_dict()`) | 3.08 ms | 2.58 ms | 4.66 ms | 325 | 2.1x |
| SQLAlchemy 2.0 Core | 4.14 ms | 4.05 ms | 4.89 ms | 241 | 1.5x |
| SQLAlchemy 2.0 ORM | 6.39 ms | 5.40 ms | 15.54 ms | 156 | 1.0x (baseline) |

```
python_version: 3.11.15   platform: Linux-6.18.5-x86_64-with-glibc2.39
sqlalchemy_version: 2.0.51   orjson_version: 3.11.9   attrs_version: 26.1.0
```

Reproduce (from the repo root; `sqlom` needs no install, the script adds it to `sys.path`):

```bash
pip install sqlalchemy orjson
python3 benchmarks/bench_sqlite.py --rows 200000 --limit 1000 --iterations 300 --warmup 30
python3 benchmarks/profile_stages.py    # stage-by-stage breakdown
```

### ⚠️ Correction to an earlier number

An earlier revision of this README reported **3.5x vs ORM** for the reflective path. That number was inflated by an unfair comparison: sqlom emitted `"is_active":1` (sqlite's raw integer) while both SQLAlchemy variants emitted `"is_active":true`, so sqlom was skipping an int→bool coercion its competitors were paying for. With output equivalence enforced, the honest figure for that same approach is **~2.1-2.5x**. The benchmark now fails loudly rather than silently comparing different payloads.

### Where the time actually goes

From [`benchmarks/profile_stages.py`](benchmarks/profile_stages.py), on the compiled pipeline:

| stage | cost | share |
|---|---|---|
| sqlite query + row fetch | 0.63 ms | **65%** |
| orjson serialization | 0.19 ms | 20% |
| hydration into objects | 0.12 ms | **12%** |

This reframes the library's premise. Hydration — the thing "lean hydration" optimizes — is only ~12% of the pipeline. Driving it to *zero* would still leave 85% of the current cost in place. In isolation the compiled hydrator is genuinely **4.9x** faster than the reflective one (123 vs 601 ns/object), but end-to-end that buys ~2.4x because hydration was never the bottleneck. Over a real network to Postgres the query/fetch share should grow, making hydration matter *less*, not more.

That is exactly why DB-side JSON wins: it removes the object step *and* the Python serialization step at once.

### Optimizations measured, and what we learned

**Adopted:**

- **Codegen the hydrator per model** (`compile_hydrator`). Straight-line attribute stores instead of a `setattr()` loop: 601 → 148 ns/object.
- **Batch hydrator** (`compile_batch_hydrator`). Unpacking the row in the `for` statement (`for f0, f1, f2 in rows`) instead of subscripting beats per-row indexing: 148 → 123 ns/object. End-to-end it's within noise here, but it's free.
- **Codegen the orjson hook** (`compile_json_default`). A straight-line dict literal instead of a comprehension over the column map.
- **DB-side JSON** (`Query.to_json_sql`, `DatabaseEngine.fetch_json`). The single biggest win, ~2.2x over the best Python-object path.

**Rejected, with reasons:**

- **attrs.** orjson does *not* serialize attrs classes natively (`TypeError: Type is not JSON serializable`), so it still needs a `default=` hook, and `attrs.asdict` is ~6x slower than a compiled dict literal (1507 vs 236 ns/object). Since sqlom bypasses `__init__` entirely via `object.__new__`, attrs' generated-init advantages don't apply — attribute read/write and construction measured identical to `dataclass(slots=True)` within noise. `@define` also adds a `__weakref__` slot by default, making instances 8 bytes larger. No remaining advantage for this use case.
- **Calling slot descriptors' `__set__` directly.** Intuitive, and 3.4x *slower*. CPython 3.11's specializing interpreter ([PEP 659](https://peps.python.org/pep-0659/)) quickens `obj.x = v` on a slotted class into `STORE_ATTR_SLOT`; going through `descr.__set__(obj, v)` is an ordinary method call that defeats that inline cache. Same reason `setattr()` loses.
- **All `__dict__` tricks** (`obj.__dict__ = {...}`, `__dict__.update(zip(...))`): 3.1-4.3x slower. In 3.11 instances use key-sharing dicts; materializing a real `__dict__` defeats `STORE_ATTR_INSTANCE_VALUE`.
- **Switching wholesale to non-slotted dataclasses.** orjson's native fast path reads `__dict__` directly, so a *non*-slotted dataclass serializes fastest of any object form (~92 vs ~182 ns/object). But it costs ~60% more memory per instance (116 vs 72 bytes), and orjson's fast path dumps whatever is in `__dict__` minus underscore-prefixed keys — so a stray runtime attribute [leaks into the JSON](https://github.com/ijl/orjson/issues/83), whereas the slots path correctly filters on `__dataclass_fields__`. Faster but looser; not worth it as a default.

### The orjson dataclass trap

Worth stating separately because it is silent and costly. orjson has serialized `dataclasses.dataclass` natively since 3.0 — no opt-in flag (`OPT_SERIALIZE_DATACLASS` is literally `0` in 3.11.9, kept only for API compatibility). The consequence is that **orjson ignores your `default=` hook for anything that is a dataclass.**

And its native path has no fast route for slotted ones. It does `PyObject_GetAttr(obj, "__dict__")`; for `slots=True` that raises, so orjson clears the error and takes a `#[cold]` fallback that walks `__dataclass_fields__` with two `getattr` calls per field. Measured cost, single-object dumps:

| shape | ns/object | vs. dict |
|---|---|---|
| plain `dict` | 204 | 1.0x |
| `@dataclass` (no slots) — native fast path | 259 | 1.3x |
| `@dataclass(slots=True)` — native fallback | 810 | **4.0x** |
| `@dataclass(slots=True)` + `OPT_PASSTHROUGH_DATACLASS` + compiled hook | 182 | 0.9x |

Roughly 300 ns/object of that penalty is just orjson raising and clearing an `AttributeError` on `__dict__` for every instance. So if your models are slotted dataclasses, pass `sqlom.DATACLASS_DUMP_OPTION` (= `orjson.OPT_PASSTHROUGH_DATACLASS`) to route them to the compiled hook. That single flag is the difference between the 4th and 6th rows of the main table.

Sources: [orjson README](https://github.com/ijl/orjson#dataclass) · [`dataclass.rs`](https://github.com/ijl/orjson/blob/master/src/serialize/per_type/dataclass.rs) · [orjson CHANGELOG](https://github.com/ijl/orjson/blob/master/CHANGELOG.md) · [issue #83](https://github.com/ijl/orjson/issues/83) · [attrs: why not dataclasses](https://www.attrs.org/en/stable/why.html)

### What this still does not show

- **It's sqlite, not Postgres.** No network round trip, no protocol parsing, no connection pool. `DatabaseEngine` (asyncpg) is implemented but not yet benchmarked against a live server, so none of the numbers above are Postgres numbers.
- **No concurrency.** Single process, single connection, no event-loop contention — so nothing here speaks to throughput under load, which is the actual claim a "high-throughput HTTP services" library needs to make.
- **Narrow shape.** One flat 4-column table, 1,000-row responses. No joins, no nested `json_agg` shaping, no wide rows, no large text/JSONB columns. Per-object costs amortize differently as column count grows.
- **No sampling profiler.** The stage breakdown is wall-clock timing of isolated stages, not `py-spy`/`pyinstrument` output; it attributes cost per stage, not per function.

---

## 📜 License

MIT License. Free for open-source and commercial use.
