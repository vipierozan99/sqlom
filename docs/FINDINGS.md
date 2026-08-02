# What makes this fast, and what doesn't

Conclusions from building and measuring rowform. The numbers are in
[METHODOLOGY.md](METHODOLOGY.md); the reasoning is here.

Most of these were measured to settle an argument, and several came out against the
thing that seemed obvious. Those are the ones worth reading.

---

## The scaling model, which reframes everything else

A rowform client is one asyncio event loop under the GIL. It saturates **exactly one
core** and cannot use more — measured CPU utilization is 0.91–1.00 in every
configuration tested. Three consequences:

1. **Row-layer efficiency is not a latency feature, it is a core-count feature.** Per
   request it saves fractions of a millisecond. Per *core* it serves several times the
   requests of the async ORM, and on a fixed core budget that is the whole difference.
2. **Extra cores do nothing for one process, and can hurt.** Pinning a client to two
   cores instead of one cost ~30% more CPU per request (0.308 vs 0.217 ms): the loop
   migrates between cores and loses cache locality.
3. **You scale by adding processes, and it is linear.** 1 worker → 4398 rps; 2 workers
   on 2 cores → 8739 rps, per-worker throughput unchanged.

This resolves an apparent contradiction. Within one request, hydration is a small
share of wall clock, so making it several times faster buys much less end-to-end — the
driver dominates. But under saturation the binding constraint is *total CPU per
request*, and there the row layer's share is decisive. **Stage share governs latency;
total CPU governs throughput.** Both are true.

Corollary: because the client is the bottleneck in these benchmarks, the ratios measure
*client CPU efficiency*. A query heavy enough to make the database the bottleneck
compresses them toward 1.0.

---

## Where the CPU goes

Profiling the saturated path — client on one core, postgres on two, sampled — puts a
ceiling on how much the row layer can still matter:

| component | share of client CPU |
|---|---|
| asyncio loop dispatch + protocol/TLS | 38% |
| asyncpg `Connection.fetch` | 19% |
| asyncpg pool acquire/release | 15% |
| **rowform generated code** | **15%** |
| `orjson.dumps` | 7% |

Driving rowform's own code to zero would buy ~15%. Pool acquire/release alone costs as
much as all hydration, and neither it nor the event loop is row-layer work.

For the ORM the profile names the mechanism exactly: one `InstanceState.__init__` and
`new_instance` per row, one `InstrumentedAttribute.__get__` per field read, and
`orm/loading.py:_instance` as the single largest frame. That is the identity-map and
instrumentation cost this design skips, measured rather than asserted.

### With transport removed, the row layer *is* the cost

| rowform, 100 rows/request | CPU/req | rowform's share |
|---|---|---|
| postgres (asyncpg, pooled, TLS) | 0.215 ms | ~15% |
| sqlite (in-process) | 0.100 ms | ~50% |

Transport is **0.115 ms/req, 53% of the postgres figure** — slightly more than an
entire sqlite request costs end to end. So "the row layer is only 15% of CPU" was never
a statement about the row layer; it was a statement about sockets.

Both readings are true and they bound the work differently. Against a remote database,
optimizing hydration is capped at ~15% and transport comes first. Against a local or
embedded one, hydration is the dominant term and pays back directly.

### And with transport gone, the row layer is also done

Attacking every cost the sqlite profile named produced no reliable gain. Cursor reuse,
replacing `bool()` with a tuple index, and collapsing orjson's per-row callbacks each
measured 1.02–1.04x, they do not compose (all three stacked = 1.04x, the same as the
best alone), and across smaller runs they ranged 0.99–1.03x. Pushing the per-row loop
into sqlite3's C fetch loop via `row_factory` made it **slower** (0.99x): it trades an
interpreted loop for one Python *call* per row, and the call costs more than the
iteration it replaces.

The decomposition says why nothing was available:

| component | ms/req | share |
|---|---|---|
| sqlite3 fetch — driver creating Python values | 0.0654 | **64%** |
| JSON serialization | 0.0200 | 20% |
| object materialization | 0.0167 | **16%** |

Objects are 16% of the request. **Both ends are now measured and both say stop:**
against a remote database transport dominates; against a local one the row layer is
~50% of CPU but its *addressable* part is 16% and already at the floor.

---

## Concurrency and uvloop both pay exactly the idle fraction

| | sqlite (in-process) | postgres (socket) |
|---|---|---|
| client utilization at c=1 | **1.00** | **0.64** |
| concurrency gain, c=1 → 32 | **1.00x** | **2.0–2.5x** |
| uvloop gain | 1.02x (noise) | 1.05–1.26x |
| thread offload (`aiosqlite`) | 0.60–0.79x | n/a |

**One number predicts both columns: the fraction of a request spent waiting.** At c=1
the postgres client is 0.64 utilized — a third of the core idle on the socket — and
concurrency reclaims precisely that, reaching 0.99 by c=4 with throughput roughly
doubled, then flattening because there is no idle left. sqlite is already 1.00 at c=1,
so there is nothing to reclaim and every variant lands within noise of a plain
synchronous loop. You can read the concurrency payoff off the utilization column before
running a sweep.

uvloop follows the same rule because it is an **I/O layer, not a faster asyncio**: it
pays where there are sockets and does nothing where there are none. Its gain also
*shrinks* once the pool reset is removed (1.22x → 1.07x at c=8), because both reduce
work per round trip and partly overlap — and the same script gave 1.11x and 1.22x in
two sessions, so treat single-figure uvloop claims as ±10%.

**Do not reach for `aiosqlite` to "make it async".** It offloads to a worker thread, so
it is not single-threaded, and it runs at **0.60–0.79x**: every request pays a thread
handoff and GIL round trip to overlap a wait that was never there.

**The determining question is never the row layer or the loop implementation; it is
whether the request waits on anything.** For an embedded database, that means
synchronous calls plus process-level parallelism.

---

## The pool sends a second query

> [!NOTE]
> **History.** This was measured when rowform ran `asyncpg.create_pool` itself.
> The pool is SQLAlchemy's now and `conditional_reset` is gone with it, so the
> knob described below no longer exists — what survives is the finding, which is
> about asyncpg's pool and still true of anyone using it directly. SQLAlchemy's
> equivalent is `pool_reset_on_return`, on the engine you hand to `rf.Engine`;
> `PLAN_SQLA_API.md` §2b prices it at ~0.11 ms of the checkout.

asyncpg's `PoolConnectionHolder.release()` calls `Connection.reset()`, which executes
`SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;` as its own round
trip. Measured via `_protocol.queries_count`, a pooled request sends **2.01 queries**
against 1.00 with a no-op reset — so half the server round trips in the default
configuration are cleanup, worth 20–30% of throughput.

`conditional_reset=True` was the engine default and got **1.23x against the no-op's
1.24x**: the whole benefit with the semantics intact. Compiled reads cannot leave
session state behind, so those connections were provably clean; `acquire()` and
`transaction()` marked a connection dirty and its release paid the full reset.

Two routes that *don't* work, measured first:

- **Moving the reset to acquire** gains nothing. It is still a separate round trip, and
  where it happens is irrelevant.
- **Batching it with the query via psycopg3 pipeline mode** is 4x worse at c=8 (771 vs
  3309 rps). An *empty* pipeline — no statements queued at all — costs **221 µs**,
  while the reset it would absorb costs 176 µs. The overhead is a fixed per-pipeline
  cost 1.3x larger than the thing it removes, not a per-statement one; reusing cursors
  changes nothing.

The inverse is the useful rule: **pipelining pays when its fixed cost is amortised over
many statements.** psycopg3's `executemany` is built on pipeline mode and is 5.0x
faster than looping `execute()` over 100 INSERTs for exactly that reason. Paying
pipeline setup per request to save one round trip is the opposite trade.
`DISCARD ALL` as a single-statement reset is disqualified outright: it includes
`DEALLOCATE ALL` and breaks the prepared-statement cache.

The residual gap between a no-op reset (1.30x) and holding a connection per worker
(1.69x) is the pool's Python-side cost — `PoolAcquireContext`, holder juggling,
acquire/release futures — which removing the round trip does not address.

---

## Code-generate the hydrator

A model's column layout is fixed and known once, so `compile_hydrator` builds a
specialized `row -> instance` function whose field stores are ordinary attribute
assignments. 601 → 148 ns/object against a reflective `setattr` loop.

Field stores are written as plain `obj.x = v` **on purpose**. CPython's specializing
interpreter ([PEP 659](https://peps.python.org/pep-0659/)) quickens that into
`STORE_ATTR_SLOT` on a slotted class, and anything cleverer defeats the inline cache:

| construction strategy | ns/object (10 fields) | vs best |
|---|---|---|
| codegen `object.__new__` + tuple-unpack into locals | 157 | 1.00x |
| `cls(*row)` with a generated `__init__` | 167 | 1.06x |
| codegen `object.__new__` + `obj.f = row[i]` | 181 | 1.15x |
| `__new__` + `obj.__dict__ = {...}` literal | 480 | 3.06x |
| codegen direct slot-descriptor `__set__` calls | 533 | **3.39x** |
| `object.__new__` + `setattr()` loop | 787 | 5.01x |

Two counterintuitive rows: calling the slot descriptor's `__set__` directly is **3.4x
slower** than `obj.x = v`, because it is an ordinary method call with no inline cache;
and every `__dict__` trick loses, because instances use key-sharing dicts that get
materialized into real ones.

**Batching helps in isolation and not end to end.** Unpacking in the `for` statement
itself and binding `list.append` once outside the loop is 148 → 123 ns/object, but 25
ns/object over 1000 rows is 0.025 ms against a ~1.05 ms pipeline. It is kept because it
is free, and it matters more as rows grow or when objects are built without being
serialized.

### Why the metaclass

The `__slots__`-vs-class-variable collision people expect only bites because both
names would live on *the class*. Attribute lookup on a class consults
`type(cls).__mro__` first, so **a data descriptor on the metaclass wins over the
class's own entry** — which is what lets `User.id` be the `sa.Column` while `user.id`
is the `int`:

```python
User.id   # -> sa.Column   (metaclass data descriptor wins)
user.id   # -> 1           (plain attribute read)
```

`dataclasses.dataclass(...)` rebuilds the class via `cls.__class__(...)`, preserving a
custom metaclass, so the two compose with or without `slots=True`. One build-order
constraint follows: `@dataclass` discovers field defaults via
`getattr(cls, name, MISSING)`, so the metaclass's interception can only switch on
*after* `dataclass()` runs — turn it on before and the default probe raises
`ValueError: mutable default ... is not allowed`.

Fields stay public-named, so `dataclasses.fields()`, `asdict()`, `__repr__` and
`__init__` all come from `@dataclass` unmodified with no re-keying.

---

## The orjson dataclass trap

Worth its own section because it is silent and expensive.

orjson has serialized `dataclasses.dataclass` **natively since 3.0, with no opt-in
flag** (`OPT_SERIALIZE_DATACLASS` is literally `0`, kept for API compatibility). So
**orjson ignores your `default=` hook for anything that is a dataclass.**

And its native path has no fast route for slotted ones. It calls
`PyObject_GetAttr(obj, "__dict__")`; for `slots=True` that raises, so orjson clears the
error and takes a `#[cold]` fallback walking `__dataclass_fields__` with two `getattr`
calls per field. Roughly 300 ns/object is just provoking and clearing an
`AttributeError` on every instance.

| shape | 4 fields | 10 fields | vs dict (4f) |
|---|---|---|---|
| plain `dict` | 68 ns | 137 ns | 1.00x |
| `@dataclass` no slots (native fast path) | 91 ns | 182 ns | 1.34x |
| `@dataclass(slots=True)` + `OPT_PASSTHROUGH_DATACLASS` + compiled hook | 182 ns | 377 ns | 2.67x |
| `@dataclass(slots=True)` (native fallback) | 421 ns | 672 ns | **6.16x** |

### Non-slotted by default

Models are plain non-slotted dataclasses unless you pass `slots=True`, specifically to
land on the native-dict row above with zero orjson options or hooks.

This used to be the rejected option. The calculus changed when the compiled
`default=` hook was dropped for being unneeded complexity: without it, a slotted
default's only path to fast serialization needs a per-model hook the library no longer
ships, so it would pay the 6.16x row. The earlier measurement gave non-slotted only a
5% end-to-end win (0.873 vs 0.913 ms) for 55% more memory (113 vs 72 B/object) — not
worth it, on the assumption that the slotted path would use the hook (2.67x) rather
than the native fallback (6.16x). Once the hook was gone, non-slotted stopped being
marginal.

**Caveat:** orjson's fast path dumps whatever is in the instance's `__dict__` minus
underscore-prefixed keys, so an attribute set post-construction
(`user.session_token = "..."`) silently
[leaks into the JSON](https://github.com/ijl/orjson/issues/83). The slotted path cannot
do this, since it filters through `__dataclass_fields__`. If your code sets extra
attributes on instances, keep them underscore-prefixed or opt into `slots=True`.

Sources: [orjson README](https://github.com/ijl/orjson#dataclass) ·
[`dataclass.rs`](https://github.com/ijl/orjson/blob/master/src/serialize/per_type/dataclass.rs) ·
[issue #83](https://github.com/ijl/orjson/issues/83)

### orjson's native UUID path is exact-type

orjson serializes `uuid.UUID` natively, but the check is on the exact type. asyncpg
returns `asyncpg.pgproto.UUID`, a genuine `uuid.UUID` *subclass* holding an identical
value — and orjson falls through to `default=` for it. Stock SQLAlchemy Core returns the
same object (its asyncpg dialect sets `supports_native_uuid`, so
`Uuid.result_processor` is `None`), so this is a serializer quirk rather than a
hydration difference. It still has to be handled *identically for every contender*, or
it reads as one.

---

## A native rewrite is the worst return on effort measured

| hypothetical | bound | evidence |
|---|---|---|
| driver builds slotted objects directly | ≤1.42x | analytical; the one real implementation achieves **0.57x** |
| Rust row layer returning Python objects | ≤1.42x | same ceiling — value creation dominates |
| Rust returning JSON, no Python objects | ~1.9x | measured; also available in SQL today |
| transport fixes (pool + uvloop) | **1.61x** | measured, deployable |

The binding constraint is that **creating a Python value costs ~109 ns and there are
four per row** — 42% of a 100-row request. Any API handing back objects whose fields are
Python values pays that no matter what language builds them. A native builder can only
remove the row tuple and the interpreted loop.

The empirical test is the striking part. `psqlpy` is a Rust/tokio-postgres driver whose
`as_class()` constructs Python instances from Rust — exactly the hypothetical — and
after controlling for pool policy and TLS it runs at **0.57x** of asyncpg plus rowform's
generated Python hydrator. Its Rust object construction is even slower than its own dict
path, which suggests `cls(**kwargs)` rather than direct slot writes.

Two conclusions worth separating: this is **not "Rust is slow"** — asyncpg is mature,
heavily tuned Cython with binary-protocol codecs, and implementation maturity dominated
language choice by a wide margin. And **"the driver builds the objects" is not
automatically a win**; the ≤1.42x ceiling assumes a builder that writes slots directly.

---

## Rejected, with reasons

**attrs.** orjson does not serialize attrs classes natively (`TypeError: Type is not
JSON serializable` for both `@define` and `@define(slots=False)`), so attrs needs a
`default=` hook, and `attrs.asdict` is ~6x slower than a compiled dict literal (1507 vs
236 ns/object). Since hydration bypasses `__init__` entirely via `object.__new__`,
attrs' generated-init advantages do not apply, and attribute access measured identical
to `dataclass(slots=True)`. No remaining advantage here, and orjson support is a strict
argument for stdlib dataclasses.

**Streaming the cursor instead of `fetchall()`.** Feeding rows straight into the batch
hydrator to avoid an intermediate list: 0.715 vs 0.706 ms, a wash. `fetchall()` is
already a C-level loop.

**Further hydration micro-optimization.** `conn.execute()` alone costs 0.005 ms of a
0.97 ms request; essentially all of the 65% "query + fetch" stage is the driver creating
Python `int`/`str` objects. An object path needs those objects, so that cost is not
addressable here.

---

## Predictions that measurement contradicted

Recorded because each was acted on, or nearly was.

**Collapsing sqlite's extra round trip is not a win.** `execute` then `fetchall` is two
thread handoffs where SQLAlchemy's aiosqlite adapter buffers both in one. Predicted free
throughput; measured the opposite — 1 round trip 1.0702 ms, 2 round trips 1.0366 ms.

**Hydrating off the adapter's row deque is not a win.** SQLAlchemy's asyncpg adapter
buffers rows in a `deque` and `fetchall()` copies it to a list. Predicted saving the
copy; measured *slower*, 1.0947 against 1.0115 ms.

**Skipping the compile cache is not a win.** Predicted that bypassing the cache lookup
would show up. Measured a wash-to-slightly-worse (1.0308 vs 1.0115) — exactly as a
~0.001 ms cache hit predicts. **There was never anything there to win**, which is the
useful part: the compile step had been assumed expensive long enough to be worth
measuring.

**`AUTOCOMMIT` does not pay for itself on the read path.** SQLAlchemy's implicit
`BEGIN`/`COMMIT` around a pooled read was assumed to be a material per-request cost.
Setting `isolation_level="AUTOCOMMIT"` measured *slower*: 1.2193 against 1.1944 ms.

> [!NOTE]
> **Half of this aged badly, in both directions.** The transaction *is* a material
> per-request cost — the benchmark suite later found that adding one to rowform's
> engine-level read, which opened none, moved it by more than the row layer does. What
> `AUTOCOMMIT` measured was not "no transaction", it was a different transaction mode,
> which is a much smaller change than the one that matters. And the pool figures quoted
> here (~0.18 ms against ~0.03–0.08 ms) were taken with the benchmark CLI importing
> locust, whose `gevent.monkey.patch_all()` moved every timing by ~30%; they are
> withdrawn. See `PLAN_SQLA_API.md` §2 and `RUNS.md` 2026-08-02.

**A hand-written converter table cannot be right.** The retired
`SQLITE_CONVERTERS = {bool: bool}` was keyed by exact Python type, and two things follow
that are unfixable without abandoning the approach: a nullable column wants
`bool | None`, which never matches `bool`, so conversion silently did not run; and
`type.python_type` is not total — it raises for some types, and `Enum` resolves to bare
`str`, losing the enum class. Asking each *column* for its own `result_processor` has
neither problem, because the lookup is per-column rather than per-type and the dialect
supplies the answer.

**Compiling with the cache key is not optional.** SQLAlchemy's structural cache key
deliberately ignores literal values — that is what makes compiling once worthwhile. The
consequence is that a cached compiled statement holds whichever literals the *first*
caller had, so `insert(...).values(id=51)` run against a statement compiled from
`values(id=50)` inserts 50 again, silently. The fix is SQLAlchemy's own: compile *with*
the cache key and pass the current statement's `CacheKey.bindparams` as
`extracted_parameters` on every call. Without the first half, the second raises.

---

## Honest limits on the claim

- **The advantage is over SQLAlchemy's ORM, not over writing it yourself.** Hand-written
  asyncpg with `dict(record)` reaches 3877 rps against 3168 in the same isolated
  configuration. Against a competent hand-rolled loop rowform *loses* slightly, costing
  +10–25% CPU over building no objects at all. The value is ergonomics at
  near-hand-written cost, not beating hand-written code. The micro suite has since put a
  number on the row layer alone: the compiled hydrator costs **+5.7–12.6%** over
  hand-written dict-building across three shapes, and — the part worth knowing — that
  overhead does not grow as the conversions get expensive. One column needing a `bool()`
  and eight needing `DateTime`/`Numeric`/`Enum`/`Uuid` parsing both cost about the same
  percentage, which is what inlining the processors into generated code buys.
- **Nothing scales past ~c=8 on a 4-vCPU box**, and p99 degrades sharply (2.8 ms at c=8
  → 86 ms at c=64) as requests queue on a 10-connection pool. That is pool and core
  saturation, not a property of any row layer.
- **DB-side JSON (`json_agg`) beats every object path** by ~2.2x, because it skips both
  object construction and Python serialization. It is not implemented here — this is an
  object mapper — but if an endpoint only ever emits JSON, it is the faster answer and
  this is the wrong tool.
