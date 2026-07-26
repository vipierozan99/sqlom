# ⚡ sqlom: Zero-Overhead Async Data Layer for Python

`sqlom` is a data access library concept for Python built for high-throughput HTTP services (FastAPI, Sanic, Granian). It aims to reduce ORM overhead by skipping session tracking, identity maps, and dynamic class reflection, pairing a **descriptor-driven query builder** with an **`asyncpg`-backed execution engine** that hydrates rows into `@dataclass(slots=True)`-style objects and serializes them via `orjson`.

It relies on pure Python plus existing C-extensions (`asyncpg` + `orjson`) rather than a custom Rust/FFI layer.

> **Status:** early, but no longer hypothetical. The core is implemented and benchmarked against both sqlite and a live PostgreSQL 16 under concurrent load; every number below comes from a script in [`benchmarks/`](benchmarks/) with results checked in. It is not packaged, not on PyPI, has no test suite, and has never run in production. Read [What this still does not show](#what-this-still-does-not-show) before believing any of it applies to your workload.

---

## 🎯 Key Features (proposed)

* **Compiled hydration:** A per-model `row -> object` function is code-generated once, so field stores are plain `STORE_ATTR` bytecode against a fixed slot rather than a `setattr()` loop. ~4.9x faster than a reflective loop in isolation.
* **Query builder:** A SQLAlchemy-Core-like builder (`User.id > 10`) built on descriptors.
* **Two schema styles:** a custom-metaclass model, or real stdlib `@dataclass(slots=True)` models that still support `User.id > 100`.
* **Slotted objects:** 73 B/instance vs 113 B for a `__dict__`-backed equivalent.
* **Async-first:** Native `asyncpg` pool integration — ~6x SQLAlchemy's async ORM under concurrent load, i.e. ~1 core to serve what the ORM needs ~6 cores for. Costs ~10-25% more CPU than doing no object mapping at all.
* **Postgres `json_agg` support:** Implemented (`Query.to_json_sql`), but **not the current focus** — see [the note below](#-db-side-json-not-the-focus-yet).

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
3. **Slotted storage.** Instances use `__slots__`, so attribute storage is a fixed-size array rather than a `__dict__` — 72 vs 116 bytes per object here. Note the tradeoff: this is also what forces orjson off its native dataclass fast path (see [the orjson dataclass trap](#the-orjson-dataclass-trap)).
4. **Path (B) skips 2 and 3 entirely** by shaping in SQL. Implemented but parked; path (A) is the focus.

None of this makes the pipeline "zero-copy" — data still moves from the C-level tuple into Python object storage into JSON bytes. The claim to make is "fewer intermediate Python-level allocations than an ORM identity-map path," not "no copying happens." And per the profile, path (A)'s remaining cost is dominated by the driver materializing Python values, not by sqlom.

---

## 📊 Performance

[`benchmarks/bench_sqlite.py`](benchmarks/bench_sqlite.py) runs every approach against the *same* sqlite file through the *same* driver, so the database round trip is held roughly constant and the measured delta is the object-shaping path. 1,000 rows returned from a 200,000-row table, 300 iterations after 30 warmup, `time.perf_counter`, single process, no concurrency.

**Every approach is asserted to emit byte-identical JSON before timing starts.** That check matters — see the correction note below.

| approach | mean | median | p95 | resp/sec | vs. ORM |
|---|---|---|---|---|---|
| **sqlom compiled, batch hydrator** | 1.06 ms | 1.00 ms | 1.14 ms | 941 | **6.0x** |
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

### 🧊 DB-side JSON (not the focus yet)

For completeness: the `json_agg` path measures **0.48 ms / 13.5x vs ORM**, roughly 2.2x faster than the best object path, because it skips both object construction and Python-side serialization. It's excluded from the table above on purpose — it isn't an object mapper, and it's parked until the object path is settled. The benchmark still runs it (`sqlom DB-side JSON`) so the number stays honest.

### The object path is close to its floor

From [`benchmarks/profile_stages.py`](benchmarks/profile_stages.py), on the compiled pipeline:

| stage | cost | share |
|---|---|---|
| sqlite query + row fetch | 0.63 ms | **65%** |
| orjson serialization | 0.19 ms | 20% |
| hydration into objects | 0.12 ms | **12%** |

Two follow-up measurements pin down how little headroom is left:

- **`conn.execute(...)` on its own costs 0.005 ms.** Essentially the entire 65% is the driver materializing 4,000 C values into Python `int`/`str` objects. No amount of sqlom-side work touches it, and an object path fundamentally needs those objects.
- **Streaming the cursor instead of `fetchall()` is a wash** (0.715 vs 0.706 ms). `fetchall()` is already a C-level loop, so skipping the intermediate tuple list buys nothing.

So hydration — the thing "lean hydration" optimizes — is ~12% of a single request's wall-clock, and driving it to *zero* would still leave 85% of that cost. In isolation the compiled hydrator is genuinely **4.9x** faster than the reflective one (123 vs 601 ns/object), but within one sequential request that buys ~2.4x because hydration was never that request's bottleneck.

**This is a statement about latency, not throughput.** Under concurrent load the picture changes — see [below](#why-the-gap-widens-under-load), where total CPU per request, not per-request stage share, sets the ceiling.

### Under concurrent load, against real Postgres

[`benchmarks/bench_pg_load.py`](benchmarks/bench_pg_load.py) closes the two biggest gaps at once: a real asyncpg/Postgres round trip, and concurrency against a shared pool. Closed-loop — `c` worker tasks issue request-shaped queries back-to-back for 4 s; a "request" is query + materialize + produce JSON bytes. All contenders get an identically sized pool (10) and are checked for byte-identical output before timing.

⚠️ **The two tables immediately below are from the combined suite and are biased by contender ordering** — see [the artifacts section](#-two-measurement-artifacts-that-inflated-earlier-numbers) for corrected, isolated figures. They are kept because the *shape* (where each approach plateaus, how p99 degrades) is still informative; the absolute ratios are not. For the order-corrected figure and how it varies with core allocation, see [How it scales with cores](#how-it-scales-with-cores-it-doesnt-per-process) — it lands back at **~6x**, but for reasons worth reading.

**Throughput (req/s), 100 rows per request:**

| approach | c=1 | c=8 | c=32 | c=64 |
|---|---|---|---|---|
| raw asyncpg + codegen dict *(floor)* | 2391 | 4741 | 4232 | 4126 |
| **sqlom (compiled)** | **2249** | **4305** | **4135** | **4098** |
| raw asyncpg + `dict(Record)` | 2240 | 4168 | 3673 | 3835 |
| SQLAlchemy async Core | 790 | 1171 | 1033 | 1089 |
| SQLAlchemy async ORM | 610 | 720 | 726 | 698 |
| *sqlom vs ORM (biased — see below)* | *3.7x* | *6.0x* | *5.7x* | *5.9x* |

**Throughput (req/s), 1000 rows per request:**

| approach | c=1 | c=8 | c=32 |
|---|---|---|---|
| raw asyncpg + codegen dict *(floor)* | 783 | 979 | 945 |
| **sqlom (compiled)** | **672** | **837** | **783** |
| SQLAlchemy async Core | 211 | 213 | 213 |
| SQLAlchemy async ORM | 126 | 122 | 118 |
| *sqlom vs ORM (biased — see below)* | *5.3x* | *6.9x* | *6.6x* |

```
asyncpg 0.31.0   sqlalchemy 2.0.51   orjson 3.11.9   PostgreSQL 16.13
4 vCPU, pool_size=10, 200k-row table, Postgres on localhost
```

```bash
python3 benchmarks/bench_pg_load.py --seed-only          # create + seed the table
python3 benchmarks/bench_pg_load.py --limit 100 --concurrency 1,8,32,64 --duration 4
```

### Why the gap *widens* under load

An earlier revision of this README predicted the opposite — that a real network round trip would grow the driver's share and make Python-side overhead matter *less*. **That prediction was wrong.** Client-side CPU per request explains why:

| client CPU ms/request (100 rows, c=8) | value |
|---|---|
| raw asyncpg + codegen dict | 0.215 |
| sqlom (compiled) | 0.237 |
| SQLAlchemy async Core | 0.889 |
| SQLAlchemy async ORM | 1.458 |

The workload is **client-CPU-bound**, not latency-bound. Throughput tracks the inverse of CPU-per-request closely: the ORM burns ~6x sqlom's CPU and delivers ~1/6 the throughput. Concurrency doesn't hide Python overhead — it *converts* it into a throughput ceiling, because every core spent on identity-map bookkeeping is a core not spent serving another request.

This also reframes the earlier "hydration is only 12%" finding. That stays true of a single request's *latency*, but per-request stage share and throughput ceiling are different questions: under saturation what matters is total CPU per request.

### ⚠️ Two measurement artifacts that inflated earlier numbers

Both were found by trying to break the benchmark rather than trusting it, and both had been published here before being caught.

**1. Contender ordering inside one process.** The suite ran all contenders in a single process, in dict order, with sqlom first. That is not neutral — later contenders measured slower. At c=1 it produced a physically impossible result: sqlom appeared to beat the *no-object* baselines and to use less CPU than doing no mapping at all.

| c=1, pinned, 100 rows | in-suite | isolated (median of 3) |
|---|---|---|
| sqlom | 1095 rps | **848 rps** |
| raw asyncpg + codegen dict | 777 rps | **1237 rps** |
| raw asyncpg + `dict(Record)` | 667 rps | **1309 rps** |

Isolated, the hand-written baselines are faster than sqlom — as they must be. Use `--only` (one contender per process) plus `--repeat` for any number you intend to quote; the combined suite is for a quick side-by-side, not for publication. c=1 in particular is noisy (spreads of 15-20% across trials), so single runs there mean little.

**2. Giving the client more than one core — which made it *slower*.** A single asyncio event loop under the GIL saturates exactly one core and cannot use more. Measured `cpu_utilization` (CPU-seconds per wall-second) is **0.91-1.00 for every contender in every configuration** — nobody ever exceeds one core.

So an earlier revision of this section was wrong twice over. It pinned the client to *two* cores, which wastes one, and then attributed the resulting throughput drop to "removing CPU contention" and revised the headline down to 4.2x. Both parts were wrong: the drop came from the client losing cache locality as the loop migrated between two cores, plus Postgres having fewer cores than it did unpinned.

| client cores (sqlom, c=8) | CPU ms/req | throughput |
|---|---|---|
| 1 (pinned to core 0) | **0.217** | 4560 rps |
| 2 (pinned to cores 0,1) | 0.308 | 3168 rps |

Pinning to exactly **one** core costs ~30% less CPU per request than floating across two. The 4.2x figure was an artifact of that, not a real correction, and is retracted.

### How it scales with cores: it doesn't, per process

With the client correctly pinned to one core and Postgres given 1, 2, or 3 cores (isolated, median of 3, c=8, 100 rows):

| Postgres cores | sqlom | async ORM | ratio |
|---|---|---|---|
| 1 | 4560 rps (0.217 ms CPU) | 741 rps (1.346) | 6.15x |
| 2 | 4111 rps (0.242) | 672 rps (1.484) | 6.12x |
| 3 | 3599 rps (0.278) | 701 rps (1.426) | 5.13x |

Two things fall out:

- **The ratio is essentially independent of Postgres's core count** (5.1-6.2x, median ~6.1x). These queries are small indexed reads served from shared buffers; Postgres needs well under one core to sustain ~4500 of them per second, so the client is the binding constraint in every configuration. **~6x is the defensible headline**, with the spread as honest uncertainty.
- **Giving Postgres *more* cores slightly slowed the client** (0.217 → 0.278 ms CPU/req). That is not CPU-time contention — the client has its own dedicated core. It is shared L3 and memory bandwidth: a busier neighbour evicts the client's working set. sqlom feels this more than the ORM does, presumably because its working set is small enough for locality to matter.

**Extra cores are used by adding processes, not by any single mapper.** Scaling is linear, as it should be for independent event loops:

| sqlom workers (1 core each, Postgres on cores 2,3) | per-worker | total |
|---|---|---|
| 1 | 4398 rps | 4398 rps |
| 2 | 4415, 4324 rps | **8739 rps (1.99x)** |

Per-worker throughput is unchanged when a second worker is added, so the useful way to read the mapper's efficiency is **cores required for a target throughput**: ~4,400 req/s needs 1 core with sqlom and roughly 6 with SQLAlchemy's async ORM. On a fixed core budget that is the whole difference; it is not a latency claim.

```bash
bash benchmarks/pin_and_run.sh --db-cores 1,2,3 --client-cores 0 -- \
     --only sqlom --concurrency 8 --duration 4 --repeat 3
```

### ⚠️ Correction to an earlier number

An earlier revision of this README reported **3.5x vs ORM** for the reflective path. That number was inflated by an unfair comparison: sqlom emitted `"is_active":1` (sqlite's raw integer) while both SQLAlchemy variants emitted `"is_active":true`, so sqlom was skipping an int→bool coercion its competitors were paying for. With output equivalence enforced, the honest figure for that same approach is **~2.1-2.5x**. The benchmark now fails loudly rather than silently comparing different payloads.

### Optimizations measured, and what we learned

**Adopted:**

- **Codegen the hydrator per model** (`compile_hydrator`). Straight-line attribute stores instead of a `setattr()` loop: 601 → 148 ns/object.
- **Batch hydrator** (`compile_batch_hydrator`). Unpacking the row in the `for` statement (`for f0, f1, f2 in rows`) instead of subscripting beats per-row indexing: 148 → 123 ns/object. End-to-end it's within noise here, but it's free.
- **Codegen the orjson hook** (`compile_json_default`). A straight-line dict literal instead of a comprehension over the column map.
- **DB-side JSON** (`Query.to_json_sql`, `DatabaseEngine.fetch_json`). Implemented and fast, but parked — see the note above.

**Rejected, with reasons:**

- **attrs.** orjson does *not* serialize attrs classes natively (`TypeError: Type is not JSON serializable`), so it still needs a `default=` hook, and `attrs.asdict` is ~6x slower than a compiled dict literal (1507 vs 236 ns/object). Since sqlom bypasses `__init__` entirely via `object.__new__`, attrs' generated-init advantages don't apply — attribute read/write and construction measured identical to `dataclass(slots=True)` within noise. `@define` also adds a `__weakref__` slot by default, making instances 8 bytes larger. No remaining advantage for this use case.
- **Calling slot descriptors' `__set__` directly.** Intuitive, and 3.4x *slower*. CPython 3.11's specializing interpreter ([PEP 659](https://peps.python.org/pep-0659/)) quickens `obj.x = v` on a slotted class into `STORE_ATTR_SLOT`; going through `descr.__set__(obj, v)` is an ordinary method call that defeats that inline cache. Same reason `setattr()` loses.
- **All `__dict__` tricks** (`obj.__dict__ = {...}`, `__dict__.update(zip(...))`): 3.1-4.3x slower. In 3.11 instances use key-sharing dicts; materializing a real `__dict__` defeats `STORE_ATTR_INSTANCE_VALUE`.
- **Switching wholesale to non-slotted dataclasses.** orjson's native fast path reads `__dict__` directly, so a *non*-slotted dataclass serializes fastest of any object form (~92 vs ~182 ns/object). But measured end-to-end that is only a **5% win** (0.873 vs 0.913 ms) for **55% more memory** (113 vs 73 B/object). Worse, orjson's fast path dumps whatever is in `__dict__` minus underscore-prefixed keys — so a stray runtime attribute [leaks into the JSON](https://github.com/ijl/orjson/issues/83), whereas the slots path correctly filters on `__dataclass_fields__`. Faster, looser, hungrier; not worth it as a default. Available as `@model(slots=False)` if your workload is serialization-dominated and memory is free.
- **Streaming the cursor instead of `fetchall()`.** Feeding `conn.execute(...)` straight into the batch hydrator to avoid materializing an intermediate list of tuples measured 0.715 vs 0.706 ms — a wash. `fetchall()` is already a C-level loop, so there is nothing to reclaim.

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

- **No HTTP layer.** The load benchmark drives the data layer directly. A real FastAPI/uvicorn stack adds routing, validation, and ASGI overhead per request that would compress every ratio here — quite possibly a lot. Until that's measured, none of these numbers are "requests/sec for your API."
- **Postgres shares the box.** Even pinned to disjoint cores it shares L3 and memory bandwidth with the client, which measurably slows the client (0.217 → 0.278 ms CPU/req as Postgres goes from 1 to 3 cores). A remote database removes that but adds real network latency; neither is measured.
- **Postgres is barely loaded here.** These are small indexed reads from shared buffers, and Postgres sustains ~4500/s on well under one core. The client is the bottleneck in every configuration measured, which is precisely why the mapper's CPU cost shows up so clearly. A query heavy enough to make Postgres the bottleneck would compress every ratio here toward 1.0, and that regime is untested.
- **Only 4 cores total, and process scaling verified only to 2 workers.** 1 → 2 workers is linear (1.99x); whether that holds at 16 or 64 workers against one Postgres is unmeasured.
- **Loopback, not a network.** No real RTT, so the latency-bound regime — where slow client code hides behind network wait — is entirely untested. That regime is exactly where these ratios should shrink.
- **Narrow shape.** One flat 4-column table of small ints and short strings. No joins, no nested shaping, no wide rows, no large text/JSONB, no writes or transactions. Per-object costs amortize differently as column count grows.
- **No sampling profiler.** The stage breakdown is wall-clock timing of isolated stages, not `py-spy`/`pyinstrument` output; it attributes cost per stage, not per function.
- **Postgres `json_agg` output is not byte-identical** to orjson's (it emits `{"id" : 1}` with spaces), so DB-side JSON is excluded from the load comparison rather than being silently compared against different bytes.

---

## 📜 License

MIT License. Free for open-source and commercial use.
