# ⚡ sqlom: Zero-Overhead Async Data Layer for Python

`sqlom` is a data access library concept for Python built for high-throughput HTTP services (FastAPI, Sanic, Granian). It aims to reduce ORM overhead by skipping session tracking, identity maps, and dynamic class reflection, pairing a **descriptor-driven query builder** with an **`asyncpg`-backed execution engine** that hydrates rows into `@dataclass(slots=True)`-style objects and serializes them via `orjson`.

It relies on pure Python plus existing C-extensions (`asyncpg` + `orjson`) rather than a custom Rust/FFI layer.

> **Status:** early, but no longer hypothetical. The core is implemented and benchmarked against both sqlite and a live PostgreSQL 16 under concurrent load; every number below comes from a script in [`benchmarks/`](benchmarks/) with results checked in. It is not packaged, not on PyPI, has no test suite, and has never run in production. Read [what none of this shows](docs/BENCHMARKS.md#8-what-none-of-this-shows) before believing any of it applies to your workload.

---

## 🎯 Key Features (proposed)

* **Compiled hydration:** A per-model `row -> object` function is code-generated once, so field stores are plain `STORE_ATTR` bytecode against a fixed slot rather than a `setattr()` loop. ~4.9x faster than a reflective loop in isolation.
* **Query builder:** A SQLAlchemy-Core-like builder (`User.id > 10`) built on descriptors.
* **Two schema styles:** a custom-metaclass model, or real stdlib `@dataclass(slots=True)` models that still support `User.id > 100`.
* **Slotted objects:** 73 B/instance vs 113 B for a `__dict__`-backed equivalent.
* **Async-first:** Native `asyncpg` pool integration — ~6x SQLAlchemy's async ORM under concurrent load, i.e. ~1 core to serve what the ORM needs ~6 cores for. Costs ~10-25% more CPU than doing no object mapping at all.
* **Postgres `json_agg` support:** Implemented (`Query.to_json_sql`), but **not the current focus** — see [If you only ever emit JSON](#if-you-only-ever-emit-json-use-the-database).

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

⚠️ **If you use this style, pass `sqlom.DATACLASS_DUMP_OPTION` to `orjson.dumps`.** orjson recognizes dataclasses natively and will silently *ignore* your `default=` hook for them — and its native path is slow for slotted classes ([details](docs/FINDINGS.md#the-orjson-dataclass-trap)). That flag (`orjson.OPT_PASSTHROUGH_DATACLASS`) routes them back to sqlom's compiled hook and is worth ~30% end-to-end.

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

### 3. DB-side JSON (not the focus yet)

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
2. **Compiled hydration.** A model's column layout is fixed and known once, so sqlom generates a specialized `rows -> [instance]` function per model (inspect it via `fn.__source__`). Field stores are written as plain attribute assignments so CPython 3.11's specializing interpreter can quicken them to `STORE_ATTR_SLOT`.
3. **Slotted storage.** Instances use `__slots__`, so attribute storage is a fixed-size array rather than a `__dict__` — 72 vs 113 bytes per object here. Note the tradeoff: this is also what forces orjson off its native dataclass fast path (see [the orjson dataclass trap](docs/FINDINGS.md#the-orjson-dataclass-trap)).
4. **Path (B) skips 2 and 3 entirely** by shaping in SQL. Implemented but parked; path (A) is the focus.

None of this makes the pipeline "zero-copy" — data still moves from the C-level tuple into Python object storage into JSON bytes. The claim to make is "fewer intermediate Python-level allocations than an ORM identity-map path," not "no copying happens." And per the profile, path (A)'s remaining cost is dominated by the driver materializing Python values, not by sqlom.

---

## 📊 Performance

Full results in **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**, engineering conclusions in
**[docs/FINDINGS.md](docs/FINDINGS.md)**, and — please — **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**,
which logs four published claims that turned out to be wrong and why.

### Throughput: ~6x SQLAlchemy's async ORM

Concurrent load against live PostgreSQL 16 over asyncpg, isolated, median of 3, c=8,
100 rows/request, client pinned to one core:

| Postgres cores | sqlom | async ORM | ratio |
|---|---|---|---|
| 1 | 4560 rps (0.217 ms CPU/req) | 741 rps (1.346) | 6.15x |
| 2 | 4111 rps (0.242) | 672 rps (1.484) | 6.12x |
| 3 | 3599 rps (0.278) | 701 rps (1.426) | 5.13x |

### With transport removed (sqlite, single-threaded, 100 rows/req)

No event loop, no pool, no TLS — the mapper's own cost:

| approach | CPU ms/req | req/s (1 thread) | vs. ORM |
|---|---|---|---|
| **sqlom** | **0.100** | **9997** | **7.4x** |
| SQLAlchemy Core | 0.437 | 2286 | 1.70x |
| SQLAlchemy ORM | 0.742 | 1346 | 1.0x |

The lead survives removing transport, so it is not an artifact of sockets masking
differences.

### Latency: ~6.3x on a single request

sqlite micro-benchmark, 1000 rows/response, median of 5 trials, all approaches
asserted to emit byte-identical JSON:

| approach | median | vs. ORM |
|---|---|---|
| sqlom compiled (per-row / batch) and `@model` + passthrough | 1.03–1.06 ms | **~6.3x** |
| `@model` dataclass, orjson native path | 1.29 ms | 5.2x |
| sqlom reflective (unoptimized) | 2.53 ms | 2.6x |
| SQLAlchemy 2.0 Core | 4.25 ms | 1.6x |
| SQLAlchemy 2.0 ORM | 6.65 ms | 1.0x |

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
- **There is no HTTP layer.** A real FastAPI/uvicorn stack adds per-request overhead
  that would compress these ratios. "Requests/sec for your API" is unmeasured.
- **Against Postgres, sqlom's generated code is only ~15% of client CPU** — 38% is the
  asyncio event loop, 19% the asyncpg fetch, 15% pool acquire/release. But that is a
  fact about *sockets*, not the mapper: profiled against in-process sqlite, transport
  turns out to be **53% of the Postgres cost** and sqlom's share rises to ~50%. Fix
  transport first for a remote DB; the mapper pays back directly for a local one.
- **The benchmark's loopback connection negotiates TLSv1.3**, which costs ~20% of
  client CPU. Ratios are unaffected (both sides pay it) but absolute throughput is
  understated: 5440 rps with `sslmode=disable` vs 4724 with it on.
- **Most of the remaining throughput is outside the mapper.** asyncpg's pool runs
  `RESET ALL` as a *second round trip* on every release (2.01 queries sent per
  request, verified). Fixing that plus `uvloop` is **1.61x** with no change to sqlom;
  holding connections instead of pooling reaches 2.36x. See
  [§6](docs/BENCHMARKS.md#6-acting-on-the-profile-24x-more-throughput-outside-the-mapper)
  — including why the pool fix is a behavioural tradeoff, not a free win.

### If you only ever emit JSON, use the database

`Query.to_json_sql` / `DatabaseEngine.fetch_json` push shaping and encoding into
Postgres and beat every object path by ~2.2x. Implemented but **parked** — it isn't
an object mapper. If that fits your endpoint, sqlom's object path is the wrong tool.

---

## 📜 License

MIT License. Free for open-source and commercial use.
