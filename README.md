# ⚡ sqlom: Zero-Overhead Async Data Layer for Python

`sqlom` is a data access library concept for Python built for high-throughput HTTP services (FastAPI, Sanic, Granian). It aims to reduce ORM overhead by skipping session tracking, identity maps, and dynamic class reflection, pairing a **descriptor-driven query builder** with an **`asyncpg`-backed execution engine** that hydrates rows into `@dataclass(slots=True)`-style objects and serializes them via `orjson`.

It relies on pure Python plus existing C-extensions (`asyncpg` + `orjson`) rather than a custom Rust/FFI layer.

> **Status:** this is a design proposal, not a released or benchmarked library. Numbers and code below are illustrative until validated against a real implementation.

---

## 🎯 Key Features (proposed)

* **Lean hydration:** Positional tuple unpacking from `asyncpg` into slotted objects, avoiding per-row `__dict__` allocation and identity-map bookkeeping.
* **Query builder:** A SQLAlchemy-Core-like builder (`User.id > 10`) built on descriptors.
* **Descriptor-based schema definition:** Define tables with typed class attributes.
* **Async-first:** Native `asyncpg` pool integration.
* **Postgres `json_agg` support:** Push nested/relational shaping to Postgres instead of doing it in Python, to avoid N+1 queries.

---

## 📦 Installation

```bash
pip install sqlom
```

---

## 🛠️ Usage Example

### 1. Define Your Schema

This is the part worth being honest about: **you cannot combine stdlib `@dataclass(slots=True)` with a metaclass-injected query-builder descriptor of the same name.** Two real conflicts:

1. `@dataclass` only turns *annotated* class variables into fields. `id = Column(int)` with no annotation is invisible to it.
2. `slots=True` generates a `__slots__` entry per field. A class-level attribute (like a `Column` descriptor) sharing the same name as a slot raises `ValueError: 'id' in __slots__ conflicts with class variable`. You can't have `User.id` be both a slot and a live descriptor that returns a query expression at class-access time.

The fix is to stop delegating to `@dataclass` and have the metaclass own both slot generation *and* the descriptor protocol, storing instance values under a shadow name:

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

    # orjson (3.x) serializes dataclasses natively — no option flag required.
    # If User isn't a stdlib dataclass (see above), you'll need a `default=`
    # callback or `__getstate__`-style hook so orjson knows how to read it.
    json_bytes = orjson.dumps(users)

    return Response(content=json_bytes, media_type="application/json")
```

---

## 🏗️ Architecture Under the Hood

```
[ PostgreSQL ] ──(asyncpg C-driver)──> [ C-tuples ]
                                            │
                                            ▼ (positional unpacking)
[ Response (JSON) ] <──(orjson)── [ Slotted object ]
```

1. **Descriptor expressions.** `User.id > 100` evaluates `Column.__get__` at class scope, returning a `ColumnExpr` node rather than doing a Python-level comparison — this gives the query builder a queryable AST without needing SQLAlchemy-style instrumentation.
2. **Positional hydration.** Column layout is inspected once per query shape; rows are unpacked positionally instead of built via keyword/dict mapping, which avoids one layer of dict lookups per row.
3. **Slotted storage.** Because instances use `__slots__`, attribute storage is a fixed-size array rather than a `__dict__`, which is generally cheaper to allocate and read.

None of this makes the pipeline "zero-copy" — data still moves from the C-level tuple into Python object storage into JSON bytes. The claim to make is "fewer intermediate Python-level allocations than an ORM identity-map path," not "no copying happens."

---

## 📊 Performance

A first micro-benchmark is checked in at [`benchmarks/bench_sqlite.py`](benchmarks/bench_sqlite.py). It isolates the Python-side hydration + JSON-serialization path — the specific thing this README's architecture claims about — by running all three approaches against the *same* sqlite file through the *same* driver, so the database round trip is held roughly constant:

1. **sqlom** — positional hydration into a slotted object (`sqlom.hydrate`), then `orjson.dumps(objs, default=sqlom.as_dict)`.
2. **SQLAlchemy 2.0 Core** — `select()`, `.mappings()`, then `orjson.dumps` over plain dicts.
3. **SQLAlchemy 2.0 ORM** — `Session.execute(...).scalars().all()` (full identity-map insertion + instrumented attributes), then a manual dict comprehension before `orjson.dumps`.

Each request-shaped query returns 1,000 rows out of a 200,000-row table, timed over 300 iterations after 30 warmup iterations (`time.perf_counter`, wall-clock, single-process, no concurrency). Results from this repo's environment, checked in at [`benchmarks/results/sqlite_latest.json`](benchmarks/results/sqlite_latest.json):

| approach | mean | median | p95 | responses/sec | vs. ORM |
|---|---|---|---|---|---|
| sqlom (positional hydration) | 1.92 ms | 1.82 ms | 2.30 ms | 520 | **3.5x** |
| SQLAlchemy Core | 4.17 ms | 4.00 ms | 5.24 ms | 240 | 1.6x |
| SQLAlchemy ORM | 6.69 ms | 5.55 ms | 16.65 ms | 150 | 1.0x (baseline) |

```
python_version: 3.11.15
platform: Linux-6.18.5-x86_64-with-glibc2.39
sqlalchemy_version: 2.0.51
orjson_version: 3.11.9
```

Reproduce with (from the repo root, `sqlom` needs no install — the script adds it to `sys.path`):

```bash
pip install sqlalchemy orjson
python3 benchmarks/bench_sqlite.py --rows 200000 --limit 1000 --iterations 300 --warmup 30
```

**What this does and doesn't show:**

- It's a real, reproducible measurement — not an invented number — but it's sqlite, single-process, single-connection. It shows the *hydration/serialization* delta is real and directionally matches the architecture's claim (fewer intermediate Python-level allocations than an ORM identity-map path).
- It does **not** yet cover: a real asyncpg/Postgres round trip (network + protocol overhead may compress or amplify this gap differently than sqlite's in-process driver), concurrent load against a connection pool, or `py-spy`/`pyinstrument` profiling to show *where* inside each path the time actually goes.
- `sqlom.engine.DatabaseEngine` (asyncpg-backed) exists in this repo for that follow-up benchmark, but running it needs a live Postgres instance and isn't part of this checked-in result yet.

---

## 📜 License

MIT License. Free for open-source and commercial use.
