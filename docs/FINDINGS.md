# What actually makes this fast (and what doesn't)

Engineering conclusions from building and measuring sqlom. Numbers are from
[BENCHMARKS.md](BENCHMARKS.md); the reasoning about *why* is here.

---

## The scaling model, which reframes everything else

A sqlom client is one asyncio event loop under the GIL. It saturates **exactly one
core** and cannot use more — measured CPU utilization is 0.91–1.00 in every
configuration tested. Three consequences:

1. **A mapper's efficiency is not a latency feature, it is a core-count feature.**
   Per request, sqlom saves fractions of a millisecond. Per *core*, it serves ~6x
   the requests of SQLAlchemy's async ORM. On a fixed core budget that is the whole
   difference: ~4,400 req/s needs 1 core with sqlom, roughly 6 with the ORM.
2. **Extra cores do nothing for a single process, and can hurt.** Pinning a client
   to two cores instead of one cost ~30% more CPU per request (0.308 vs 0.217 ms) —
   the event loop migrates between cores and loses cache locality.
3. **You scale by adding processes, and it is linear.** 1 worker → 4398 rps;
   2 workers on 2 cores → 8739 rps (1.99x), per-worker throughput unchanged.

This also resolves an apparent contradiction. Within one request, hydration is only
~12% of wall-clock, so making it 4.9x faster buys only ~2.4x end-to-end (2.53 ms
reflective → 1.03-1.06 ms compiled) — the driver dominates. But under saturation the binding constraint is *total CPU per
request*, and there the mapper's share is decisive. **Stage share governs latency;
total CPU governs throughput.** Both statements are true and they are not in
conflict.

Corollary worth stating plainly: because the client is the bottleneck in this
benchmark, the ~6x is a measure of *client CPU efficiency*. A query heavy enough to
make Postgres the bottleneck would compress it toward 1.0.

---

## Where the CPU actually goes (profiled)

Profiling the saturated path — client on one core, Postgres on two, sampled so
instrumentation doesn't distort it — puts a ceiling on how much the mapper can still
matter ([BENCHMARKS §5b](BENCHMARKS.md#5b-where-sqloms-0225-msreq-goes-sampled)):

| component | share of client CPU |
|---|---|
| asyncio loop dispatch + protocol/TLS | 38% |
| asyncpg `Connection.fetch` | 19% |
| asyncpg pool acquire/release | 15% |
| **sqlom generated code** (hydrate + dict build) | **15%** |
| `orjson.dumps` | 7% |

**sqlom's own code is ~15% of the CPU it is competing on.** Driving it to zero would
buy ~15% throughput; the larger remaining targets are the event loop and pool
handling, neither of which is mapper work. Notably pool acquire/release alone costs
as much as all hydration.

This is the throughput analogue of the latency finding in §2: there the driver was
65% of a single request's wall clock, here the loop and driver are ~72% of its CPU.
Both say the same thing from different directions — **the mapper stopped being the
bottleneck some time ago**, and the 6x over the ORM comes from the ORM spending
1.29 ms/req where sqlom spends 0.22, not from sqlom's remaining 0.03 ms of headroom.

For the ORM the profile names the mechanism exactly: 40,000 `InstanceState.__init__`
and `new_instance` calls per 800 requests (one per row), 160,000
`InstrumentedAttribute.__get__` calls (one per field read), and `orm/loading.py:
_instance` as the single largest frame. That is the identity-map and instrumentation
cost this design set out to skip, measured rather than asserted.

One incidental discovery worth carrying: the benchmark's loopback connection was
negotiating **TLSv1.3**, which costs ~20% of client CPU. Ratios are unaffected since
both sides pay it, but absolute throughput in §4 is understated by ~25%.

### With transport removed, the mapper *is* the cost

Profiling the same request shape against in-process sqlite — no event loop, no pool,
no TLS ([BENCHMARKS §7](BENCHMARKS.md#7-profiled-sqlite-run-the-mapper-with-transport-removed))
— inverts the picture:

| sqlom, 100 rows/request | CPU/req | sqlom's share of it |
|---|---|---|
| Postgres (asyncpg, pooled, TLS) | 0.215 ms | ~15% |
| sqlite (in-process) | 0.100 ms | ~50% |

Transport is **0.115 ms/req, 53% of the Postgres figure** — slightly more than an
entire sqlite request costs end to end. So "the mapper is only 15% of CPU" was never a
statement about the mapper; it was a statement about sockets. Remove them and sqlom's
generated code becomes about half of what is left, with ~30% in the sqlite3 driver and
~15% in orjson.

Both readings are true and they bound the work differently:

- **Against a remote database**, optimizing the mapper is capped at ~15%. Fix
  transport first.
- **Against a local or embedded database**, the mapper is the dominant term and
  hydration work pays back directly.

sqlom still leads by 7.4x over the async ORM and 4.4x over Core in the sqlite
configuration, so the advantage is not an artifact of transport masking differences —
it survives having transport removed.

### And with transport gone, the mapper is also done

Attacking every cost the sqlite profile named
([BENCHMARKS §8](BENCHMARKS.md#8-what-is-left-in-the-sqlite-path-essentially-nothing))
produced no reliable gain. Cursor reuse, replacing `bool()` with a tuple index, and
collapsing orjson's 80,000 per-row callbacks into a single comprehension each measured
1.02–1.04x, they do not compose (all three stacked = 1.04x, the same as the best one
alone), and across smaller runs they ranged 0.99–1.03x — i.e. noise.

Pushing the per-row loop into sqlite3's C fetch loop via `row_factory` made it
**slower** (0.99x): it trades an interpreted loop for one Python *call* per row, and
the call costs more than the iteration it replaces.

The decomposition says why nothing was available:

| component | ms/req | share |
|---|---|---|
| sqlite3 fetch — driver creating Python values | 0.0654 | **64%** |
| JSON serialization | 0.0200 | 20% |
| object materialization | 0.0167 | **16%** |

Objects are 16% of the request. A mapper with free hydration and free serialization
could reach 1.20x (the no-objects line); the absolute floor is 1.56x. So the remaining
levers are all outside what a mapper controls: return narrower rows, skip objects for
JSON-only endpoints (1.20x), give up slots for orjson's native path (1.08x at +55%
memory), or push shaping into SQL (~2.2x — the largest, and why the `json_agg` path
stays implemented).

**Taken with the previous section, both ends are now measured and both say stop.**
Against a remote database the mapper is ~15% of CPU and transport dominates; against a
local one the mapper is ~50% of CPU but its *addressable* part is 16% and already at
the floor. Further hydration micro-optimization is not where any remaining time is.

### A native rewrite is the worst return on effort measured

Two hypotheticals, bounded in
[BENCHMARKS §9](BENCHMARKS.md#9-two-hypotheticals-a-native-object-builder-and-rust):

| hypothetical | bound | evidence |
|---|---|---|
| driver builds slotted objects directly | ≤1.51x | analytical; the one real implementation achieves **0.57x** |
| Rust mapper returning Python objects | ≤1.51x | same ceiling — value creation dominates |
| Rust returning JSON, no Python objects | ~1.9x | measured; already available in SQL today |
| transport fixes (pool + uvloop) | **1.61x** | measured, deployable |

The binding constraint is that **creating a Python value costs ~109 ns and there are
four per row** — 42% of a 100-row request. Any API that hands back objects whose
fields are Python values pays that no matter what language builds them. A native
builder can only remove the row tuple and the interpreted loop: ≤1.51x, and less in
practice since it still allocates and writes slots.

The empirical test is the striking part. `psqlpy` is a Rust/tokio-postgres driver
whose `as_class()` constructs Python instances from Rust — exactly the hypothetical —
and after controlling for pool policy and TLS it runs at **0.57x** of asyncpg plus
sqlom's codegen'd Python hydrator. Its Rust object construction is even slower than
its own dict path, which suggests `cls(**kwargs)` rather than direct slot writes.

Two conclusions worth separating:

1. **Not "Rust is slow".** asyncpg is mature, heavily tuned Cython with
   binary-protocol codecs; psqlpy is younger. Implementation maturity dominated
   language choice by a wide margin.
2. **"The driver builds the objects" is not automatically a win** — it depends how.
   The ≤1.51x ceiling assumes a builder that writes slots directly.

Neither hypothetical beats what is already available without writing any Rust: 1.61x
from fixing the pool and event loop, or ~1.9x by pushing shaping into SQL.

### What to optimize next, in order

Acting on the profile ([BENCHMARKS §6](BENCHMARKS.md#6-acting-on-the-profile-24x-more-throughput-outside-the-mapper))
found 2.36x more throughput without touching the mapper at all:

| lever | gain | deployable? |
|---|---|---|
| `create_pool(reset=<no-op>)` | 1.30x | with care — see caveat |
| holding a connection per worker | 1.69x | rarely |
| `sslmode=disable` (local DB only) | 1.15x | environment-specific |
| `uvloop` | 1.11x | yes, trivially |
| uvloop + no-reset + no-TLS | **1.61x** | yes, with the caveat |

**Why the pool is slow: it sends a second query.** asyncpg's
`PoolConnectionHolder.release()` calls `Connection.reset()`, which executes
`SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;` as its own round
trip. Measured via `_protocol.queries_count`: a pooled request sends **2.01 queries**,
versus 1.00 with `reset=` a no-op or a held connection. So half the server round trips
in the default configuration are cleanup.

The caveat matters: skipping `RESET ALL` lets session state leak between requests
(`SET`, temp tables, cursors, `LISTEN`, advisory locks). It is safe only while no
handler touches session state, and it fails silently the day one does.

The remaining gap between no-reset (1.30x) and holding a connection (1.69x) is the
pool's Python-side cost — `PoolAcquireContext`, holder juggling, acquire/release
futures — which removing the round trip does not address.

**So the order of work is: fix the pool, adopt uvloop, then optimize the mapper.**
After the first two, sqlom's generated code goes from ~15% of client CPU to roughly a
quarter of what remains — it becomes the largest single item, which it was not before.

---

## Adopted optimizations

### Code-generate the hydrator per model

A model's column layout is fixed and known once, so `compile_hydrator` builds a
specialized `row -> instance` function whose field stores are ordinary attribute
assignments. 601 → 148 ns/object versus a reflective `setattr` loop.

Field stores are written as plain `obj.x = v` **on purpose**. CPython 3.11's
specializing interpreter ([PEP 659](https://peps.python.org/pep-0659/)) quickens
that into `STORE_ATTR_SLOT` on a slotted class. Anything that looks more clever
defeats the inline cache:

| construction strategy | ns/object (10 fields) | vs. best |
|---|---|---|
| codegen `object.__new__` + tuple-unpack into locals | 157 | 1.00x |
| `cls(*row)` with a generated `__init__` | 167 | 1.06x |
| codegen `object.__new__` + `obj.f = row[i]` | 181 | 1.15x |
| `__new__` + `obj.__dict__ = {...}` literal | 480 | 3.06x |
| codegen direct slot-descriptor `__set__` calls | 533 | **3.39x** |
| `object.__new__` + `setattr()` loop | 787 | 5.01x |

Two counterintuitive entries: calling the slot descriptor's `__set__` directly is
**3.4x slower** than `obj.x = v` (it is an ordinary method call, so no inline
cache), and every `__dict__` trick loses because 3.11 instances use key-sharing
dicts that get materialized into real ones.

### Batch the hydrator

`compile_batch_hydrator` unpacks the row in the `for` statement itself
(`for f0, f1, f2 in rows`) and binds `list.append` once outside the loop: 148 → 123
ns/object in isolation.

**End-to-end this is not measurable**, and the honest reading is that it does not
matter for a JSON endpoint: 25 ns/object over 1,000 rows is 0.025 ms against a
~1.05 ms pipeline, so batch and per-row are a statistical tie in
[§1](BENCHMARKS.md#1-sqlite-micro-benchmark-single-request-latency) with their order
flipping between runs. It is kept because it is free and it matters more as rows
grow or when objects are built without being serialized.

### Code-generate the orjson hook

`compile_json_default` emits a straight-line dict literal with the keys baked in,
rather than a comprehension over the column map. Cached lazily on the class as
`__json_default__` via the metaclass's `__getattr__`.

### Two model styles, no performance difference

The README originally claimed stdlib `@dataclass(slots=True)` could not carry a
class-level query descriptor. That is false. The `__slots__`-vs-class-variable
collision only bites because both names live on *the class*; attribute lookup on a
class consults `type(cls).__mro__` first, and **a data descriptor on the metaclass
wins over the class's own entry**. `@dataclass(slots=True)` rebuilds the class via
`cls.__class__(...)`, preserving a custom metaclass, so the two compose:

```python
User.id      # -> ColumnExpr  (metaclass data descriptor wins)
user.id      # -> 1           (plain slot read)
```

Both styles measure the same (72 B/object; 1.06 vs 1.05 ms — a tie). Pick on ergonomics —
the dataclass style gets `asdict`, `replace`, `==`, `repr`, pattern matching.

---

## The orjson dataclass trap

Worth its own section because it is silent and expensive.

orjson has serialized `dataclasses.dataclass` **natively since 3.0, with no opt-in
flag** (`OPT_SERIALIZE_DATACLASS` is literally `0` in 3.11.9, kept only for API
compatibility). The consequence: **orjson ignores your `default=` hook for anything
that is a dataclass.**

And its native path has no fast route for slotted ones. It calls
`PyObject_GetAttr(obj, "__dict__")`; for `slots=True` that raises, so orjson clears
the error and takes a `#[cold]` fallback that walks `__dataclass_fields__` with two
`getattr` calls per field. Roughly 300 ns/object of the penalty is just provoking
and clearing an `AttributeError` on every instance.

| shape | 4 fields | 10 fields | vs. dict (4f) |
|---|---|---|---|
| plain `dict` | 68 ns | 137 ns | 1.00x |
| `@dataclass` no slots (native fast path) | 91 ns | 182 ns | 1.34x |
| `@dataclass(slots=True)` + `OPT_PASSTHROUGH_DATACLASS` + compiled hook | 182 ns | 377 ns | 2.67x |
| `@dataclass(slots=True)` (native fallback) | 421 ns | 672 ns | **6.16x** |

**If your models are slotted dataclasses, pass
`sqlom.DATACLASS_DUMP_OPTION`** (= `orjson.OPT_PASSTHROUGH_DATACLASS`) to route them
back to the compiled hook. That one flag is worth ~2.3x on the serialization step
and ~30% end-to-end.

Sources: [orjson README](https://github.com/ijl/orjson#dataclass) ·
[`dataclass.rs`](https://github.com/ijl/orjson/blob/master/src/serialize/per_type/dataclass.rs) ·
[CHANGELOG](https://github.com/ijl/orjson/blob/master/CHANGELOG.md) ·
[issue #83](https://github.com/ijl/orjson/issues/83)

---

## Rejected, with reasons

### attrs

orjson does **not** serialize attrs classes natively — `TypeError: Type is not JSON
serializable` for both `@define` and `@define(slots=False)`. So attrs still needs a
`default=` hook, and `attrs.asdict` is ~6x slower than a compiled dict literal
(1507 vs 236 ns/object).

Since sqlom bypasses `__init__` entirely via `object.__new__`, attrs' generated-init
advantages don't apply. Attribute read/write and construction measured identical to
`dataclass(slots=True)` within noise. `@define` also adds a `__weakref__` slot by
default, making instances 8 bytes larger (set `weakref_slot=False` for parity).

**No remaining advantage for this use case, and orjson support is a strict argument
for stdlib dataclasses.**

### Non-slotted dataclasses as the default

Tempting, because orjson's native fast path reads `__dict__` directly and makes them
the fastest object form to serialize (91 vs 182 ns/object). But end-to-end that is
only a **5% win** (0.873 vs 0.913 ms) for **55% more memory** (113 vs 72 B/object).

Worse, orjson's fast path dumps whatever is in `__dict__` minus underscore-prefixed
keys, so a stray runtime attribute
[leaks into the JSON](https://github.com/ijl/orjson/issues/83); the slots path
correctly filters on `__dataclass_fields__`. Faster, looser, hungrier. Available as
`@model(slots=False)` for serialization-dominated workloads.

### Streaming the cursor instead of `fetchall()`

Feeding `conn.execute(...)` straight into the batch hydrator to avoid materializing
an intermediate list of tuples: 0.715 vs 0.706 ms — a wash. `fetchall()` is already
a C-level loop.

### Further hydration micro-optimization

`conn.execute()` alone costs 0.005 ms of a 0.97 ms request; essentially all of the
65% "query + fetch" stage is the driver creating Python `int`/`str` objects. An
object path needs those objects, so that cost is not addressable from sqlom.

---

## Honest limits on the claim

- **The ~6x is over SQLAlchemy's ORM, not over writing it yourself.** Hand-written
  asyncpg with `dict(record)` reaches 3877 rps against sqlom's 3168 in the same
  isolated configuration. Against a competent hand-rolled loop, sqlom *loses*
  slightly; it costs +10–25% CPU over building no objects at all. The value is
  ergonomics at near-hand-written cost, not beating hand-written code.
- **Nothing scales past ~c=8 on a 4-vCPU box**, and p99 degrades sharply (2.8 ms at
  c=8 → 86 ms at c=64) as requests queue on a 10-connection pool. That is pool and
  core saturation, not a property of any mapper.
- **DB-side JSON (`json_agg`) beats every object path** by ~2.2x, because it skips
  both object construction and Python serialization. It is implemented
  (`Query.to_json_sql`, `DatabaseEngine.fetch_json`) but parked — it is not an
  object mapper. If your endpoint only ever emits JSON, it is the faster answer and
  sqlom's object path is the wrong tool.
