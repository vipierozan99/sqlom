# What actually makes this fast (and what doesn't)

Engineering conclusions from building and measuring rowform. Numbers are from
[BENCHMARKS.md](BENCHMARKS.md); the reasoning about *why* is here.

> **Read the scope note first.** rowform was rewritten around SQLAlchemy Core
> ([PLAN_CORE_COMPILER.md](PLAN_CORE_COMPILER.md)): Core now compiles every
> statement and owns the schema, and the in-house query builder — expressions,
> `Query`, DML, dialects — was deleted. This document predates that.
>
> | section | status |
> |---|---|
> | everything about **hydration and codegen** (§ "Code-generate the hydrator", "Batch the hydrator", "The `@model` metaclass", "The orjson dataclass trap") | **current and load-bearing.** This is the part that survived, and it is now the whole value proposition. |
> | everything about **where the CPU goes**, the scaling model, and what to optimise next | **current in substance.** The shares shift because SQL generation is no longer rowform's, but the conclusion — transport dominates, then the row layer — is unchanged. |
> | the **"vs Core" and "vs ORM" ratios** in the next section | **historical.** They compare a library that generated its own SQL against one that did not. Both sides now compile identically, so the comparison is purely the result layer — see the table in [README.md](../README.md) and correction 9 in [METHODOLOGY.md](METHODOLOGY.md), which found that every published Core ratio was inflated a second time. |
> | anything describing `@model`, `Column[int]`, `rf.select` or `Query` **as API** | **deleted.** The design *rationale* in those sections is still why the current metaclass looks the way it does; the syntax is gone. |
>
> Two retractions and three new corrections came out of the rewrite; they are at
> the bottom of this file under "Retracted".

---

## The bottom line, and how much it depends on the setup

| configuration | vs Core | vs ORM |
|---|---|---|
| **psycopg both sides, both default, via FastAPI** | **2.07x** | **3.33x** |
| psycopg both sides, both default, data layer | 2.67x | 4.28x |
| asyncpg, both tuned, via FastAPI | 2.73x | 4.80x |
| asyncpg, both tuned, data layer | 4.00x | 7.18x |

**Quote 3.3x.** That is the same driver on both sides, both libraries at their default
pool behaviour, measured through a real web stack — the claim with the fewest
assumptions behind it. The 7.18x headline required asyncpg (which SQLAlchemy cannot
use) *and* skipping the pool's session reset, so it belongs in a caveat, not a claim.

Two independent effects each cost about a third and compound: rowform's advantage partly
*was* the driver plus the skipped reset, and the web layer adds ~119 µs/request that
every route pays equally.

Measured with every optimization in this repo applied to rowform *and* the equivalent
applied to SQLAlchemy — including `isolation_level="AUTOCOMMIT"`, without which
SQLAlchemy sends 3 statements per request (`BEGIN`/`SELECT`/`ROLLBACK`) against
rowform's 1. SQLAlchemy's own tuning is worth 1.10-1.15x, so the gap is not an artifact
of leaving it misconfigured
([BENCHMARKS §13](BENCHMARKS.md#13-bottom-line-rowform-vs-sqlalchemy-both-tuned-with-and-without-fastapi)).

**FastAPI + uvicorn costs 121 µs/request and every route pays it**, which compresses
the ratio by about a third. Above that floor the data layers cost 292 µs (rowform),
1003 µs (Core) and 1856 µs (ORM) per request. The tail matters more than the mean:
rowform's p99 is 4.9 ms against the ORM's 65 ms.

Practical reading: on a fixed core budget a JSON read endpoint serves ~4.8x more
requests, and no data layer can exceed the 8297 rps framework floor — so the cheaper
the query, the less the mapper matters.

**The ratios survive an independent load generator.** Re-running the both-default
comparison under locust — different HTTP client, different concurrency model, no
shared code — gives 2.13x Core and 3.29x ORM against the published 2.07x/3.33x, and
matches rowform's absolute throughput to 0.1%. Concurrency is confirmed by socket counts
read from `/proc/net/tcp` and by Little's Law, which lands on the requested in-flight
count to two decimal places
([§15](BENCHMARKS.md#15-auditing-the-load-generator-itself)). The one thing locust
*cannot* measure is the framework floor: on a single core it saturates at ~5400 rps,
below `/noop`'s real throughput, so it reports the floor 37% low. A standard tool is
not automatically the more trustworthy one — it has to have the headroom.

## The scaling model, which reframes everything else

A rowform client is one asyncio event loop under the GIL. It saturates **exactly one
core** and cannot use more — measured CPU utilization is 0.91–1.00 in every
configuration tested. Three consequences:

1. **A mapper's efficiency is not a latency feature, it is a core-count feature.**
   Per request, rowform saves fractions of a millisecond. Per *core*, it serves ~6x
   the requests of SQLAlchemy's async ORM. On a fixed core budget that is the whole
   difference: ~4,400 req/s needs 1 core with rowform, roughly 6 with the ORM.
2. **Extra cores do nothing for a single process, and can hurt.** Pinning a client
   to two cores instead of one cost ~30% more CPU per request (0.308 vs 0.217 ms) —
   the event loop migrates between cores and loses cache locality.
3. **You scale by adding processes, and it is linear.** 1 worker → 4398 rps;
   2 workers on 2 cores → 8739 rps (1.99x), per-worker throughput unchanged.

This also resolves an apparent contradiction. Within one request, hydration is only
~12% of wall-clock, so making it 4.9x faster buys only ~2.3x end-to-end (3.56 ms
reflective → 1.39-1.55 ms compiled) — the driver dominates. But under saturation the binding constraint is *total CPU per
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
matter ([BENCHMARKS §5b](BENCHMARKS.md#5b-where-rowforms-0225-msreq-goes-sampled)):

| component | share of client CPU |
|---|---|
| asyncio loop dispatch + protocol/TLS | 38% |
| asyncpg `Connection.fetch` | 19% |
| asyncpg pool acquire/release | 15% |
| **rowform generated code** (hydrate + dict build) | **15%** |
| `orjson.dumps` | 7% |

**rowform's own code is ~15% of the CPU it is competing on.** Driving it to zero would
buy ~15% throughput; the larger remaining targets are the event loop and pool
handling, neither of which is mapper work. Notably pool acquire/release alone costs
as much as all hydration.

This is the throughput analogue of the latency finding in §2: there the driver was
65% of a single request's wall clock, here the loop and driver are ~72% of its CPU.
Both say the same thing from different directions — **the mapper stopped being the
bottleneck some time ago**, and the 6x over the ORM comes from the ORM spending
1.29 ms/req where rowform spends 0.22, not from rowform's remaining 0.03 ms of headroom.

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

| rowform, 100 rows/request | CPU/req | rowform's share of it |
|---|---|---|
| Postgres (asyncpg, pooled, TLS) | 0.215 ms | ~15% |
| sqlite (in-process) | 0.100 ms | ~50% |

Transport is **0.115 ms/req, 53% of the Postgres figure** — slightly more than an
entire sqlite request costs end to end. So "the mapper is only 15% of CPU" was never a
statement about the mapper; it was a statement about sockets. Remove them and rowform's
generated code becomes about half of what is left, with ~30% in the sqlite3 driver and
~15% in orjson.

Both readings are true and they bound the work differently:

- **Against a remote database**, optimizing the mapper is capped at ~15%. Fix
  transport first.
- **Against a local or embedded database**, the mapper is the dominant term and
  hydration work pays back directly.

rowform still leads by 7.4x over the async ORM and 4.4x over Core in the sqlite
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

### Concurrency and uvloop both pay exactly the idle fraction

The same matrix run on both backends
([§10](BENCHMARKS.md#10-asyncio-concurrency-and-uvloop-on-the-sqlite-path),
[§11](BENCHMARKS.md#11-the-same-matrix-on-postgres-concurrency-and-uvloop-both-matter)):

| | sqlite (in-process) | Postgres (socket) |
|---|---|---|
| client utilization at c=1 | **1.00** | **0.64** |
| concurrency gain, c=1 → 32 | **1.00x** | **2.0-2.5x** |
| uvloop gain | 1.02x (noise) | 1.05-1.26x |
| thread offload (`aiosqlite`) | 0.60-0.79x | n/a |

**One number predicts both columns: the fraction of a request spent waiting.** At c=1
the Postgres client is 0.64 utilized — a third of the core idle on the socket — and
concurrency reclaims precisely that, reaching 0.99 by c=4 with throughput roughly
doubled, then flattening because there is no idle left. sqlite is already 1.00 at c=1,
so there is nothing to reclaim and every variant lands at 0.93-1.05x of a plain
synchronous loop. You can read the concurrency payoff off the utilization column before
running a sweep.

uvloop follows the same rule because it is an **I/O layer, not a faster asyncio**: it
pays where there are sockets (1.05-1.26x) and does nothing where there are none. Two
qualifications worth carrying: its gain *shrinks* once the pool RESET is removed
(1.22x → 1.07x at c=8) because both optimizations reduce work per round trip and
partly overlap; and the same script gave 1.11x in one session and 1.22x in another, so
treat single-figure uvloop claims as ±10%.

A coroutine wrapper on a synchronous call is nearly free (one `await asyncio.sleep(0)`
measures 1.4-2.4 µs), so keep it for API symmetry if you want it.

**But do not reach for `aiosqlite` to "make it async":** it offloads to a worker
thread, so it is not single-threaded, and it runs at **0.60-0.79x** — every request
pays a thread handoff and GIL round trip to overlap a wait that was never there.
(This is the one place uvloop helps, 0.60x → 0.73x, because thread handoff goes
through `call_soon_threadsafe` and the self-pipe.)

**The determining question is never the mapper or the loop implementation; it is
whether the request waits on anything.** For an embedded database the right
architecture is synchronous calls plus process-level parallelism. For a networked one,
concurrency plus the pool fix plus uvloop compound to **3.68x** (1888 → 6951 rps) with
the mapper untouched — which remains the largest lever found anywhere in this repo.

### A native rewrite is the worst return on effort measured

Two hypotheticals, bounded in
[BENCHMARKS §9](BENCHMARKS.md#9-two-hypotheticals-a-native-object-builder-and-rust):

| hypothetical | bound | evidence |
|---|---|---|
| driver builds slotted objects directly | ≤1.42x | analytical; the one real implementation achieves **0.57x** |
| Rust mapper returning Python objects | ≤1.42x | same ceiling — value creation dominates |
| Rust returning JSON, no Python objects | ~1.9x | measured; already available in SQL today |
| transport fixes (pool + uvloop) | **1.61x** | measured, deployable |

The binding constraint is that **creating a Python value costs ~109 ns and there are
four per row** — 42% of a 100-row request. Any API that hands back objects whose
fields are Python values pays that no matter what language builds them. A native
builder can only remove the row tuple and the interpreted loop: ≤1.42x, and less in
practice since it still allocates and writes slots.

The empirical test is the striking part. `psqlpy` is a Rust/tokio-postgres driver
whose `as_class()` constructs Python instances from Rust — exactly the hypothetical —
and after controlling for pool policy and TLS it runs at **0.57x** of asyncpg plus
rowform's codegen'd Python hydrator. Its Rust object construction is even slower than
its own dict path, which suggests `cls(**kwargs)` rather than direct slot writes.

Two conclusions worth separating:

1. **Not "Rust is slow".** asyncpg is mature, heavily tuned Cython with
   binary-protocol codecs; psqlpy is younger. Implementation maturity dominated
   language choice by a wide margin.
2. **"The driver builds the objects" is not automatically a win** — it depends how.
   The ≤1.42x ceiling assumes a builder that writes slots directly.

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

**And it is fixable without changing behaviour** — `conditional_reset=True` is now the
engine default and gets **1.23x against the no-op's 1.24x**, i.e. the whole benefit with
the semantics intact. `fetch_all`/`fetch_json` run only generated SELECTs, which cannot
leave session state behind, so those connections are provably clean; `engine.acquire()`
marks a connection dirty and its release pays the full reset. Two routes that *don't*
work were measured first
([BENCHMARKS §12](BENCHMARKS.md#12-fixing-the-pool-reset-without-changing-behaviour)):
moving the reset to acquire gains nothing (it is still a separate round trip — where it
happens is irrelevant), and batching it with the query via psycopg3's pipeline mode is
**4x worse at c=8** (771 vs 3309 rps) for a reason worth knowing: an *empty* pipeline —
no statements queued at all — costs **221 µs**, while the reset it would absorb costs
176 µs as a second round trip. The overhead is a fixed per-pipeline cost 1.3x larger
than the thing it removes, not a per-statement one (reusing cursors changes nothing).
The extended protocol also forces the 4-statement reset to be split into 4 prepared
statements, since pipeline mode rejects multi-statement strings.

The inverse is the useful rule: **pipelining pays when its fixed cost is amortised over
many statements.** psycopg3's `executemany` is built on pipeline mode and is 5.0x faster
than looping `execute()` over 100 INSERTs (229 vs 1137 µs/statement) for exactly that
reason. Paying pipeline setup per request to save one statement's round trip is the
opposite trade. `DISCARD ALL` as a single-statement reset is disqualified
outright: it includes `DEALLOCATE ALL` and breaks the prepared-statement cache.

The remaining gap between no-reset (1.30x) and holding a connection (1.69x) is the
pool's Python-side cost — `PoolAcquireContext`, holder juggling, acquire/release
futures — which removing the round trip does not address.

**So the order of work is: fix the pool, adopt uvloop, then optimize the mapper.**
After the first two, rowform's generated code goes from ~15% of client CPU to roughly a
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

### The `@model` metaclass

Stdlib `@dataclass(slots=True)` *can* carry a class-level query descriptor -- the
mechanism below is what let `@model` support `slots=True` as an opt-in even
though the default is `slots=False`. The `__slots__`-vs-class-variable collision
people expect only bites because both names would live on *the class*;
attribute lookup on a class consults `type(cls).__mro__` first, and **a data
descriptor on the metaclass wins over the class's own entry**. `@dataclass(...)`
rebuilds the class via `cls.__class__(...)`, preserving a custom metaclass, so
the two compose regardless of `slots`:

```python
User.id  # -> ColumnExpr  (metaclass data descriptor wins)
user.id  # -> 1           (plain attribute read)
```

`model()` goes one step further than installing a descriptor: the `Column(int)`
class-body value is read once for its `py_type` and then discarded rather than
kept as a real class attribute. So there's no shadow-named storage field (no
`_rf_id`) and nothing to collide with `__slots__` in the first place — `id` is a
real, public-named field, and `Column.__get__`/`__set__` never run. That imposes
one build-order constraint: `@dataclass` discovers field defaults via
`getattr(cls, field_name, MISSING)`, so the metaclass's `ColumnExpr`-returning
interception can only switch on *after* `dataclass()` runs — turn it on before,
and the default probe sees a `ColumnExpr` and raises `ValueError: mutable
default ... is not allowed`.

Being public-named means `dataclasses.fields()`/`asdict()`, `__repr__` and
`__init__` all come from `@dataclass` unmodified, with no re-keying. One typing
gap remains: only the bare `@model` form gets full field-specifier typing, not
`@model(tablename=...)` — pyright doesn't propagate the `dataclass_transform`
synthesis through the intermediate closure for that call shape.

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

**`@model` defaults to `slots=False`** specifically to land on the fast
native-dict row above with zero orjson options or hooks required — see
["Non-slotted by default"](#non-slotted-by-default) below. `@model(slots=True)`
remains available for smaller instances, but rowform ships no hook to route
serialization back to the fast path for it, so a slotted model pays the 6.16x
row above; only worth it if the memory savings matter more than serialization
cost for your workload.

Sources: [orjson README](https://github.com/ijl/orjson#dataclass) ·
[`dataclass.rs`](https://github.com/ijl/orjson/blob/master/src/serialize/per_type/dataclass.rs) ·
[CHANGELOG](https://github.com/ijl/orjson/blob/master/CHANGELOG.md) ·
[issue #83](https://github.com/ijl/orjson/issues/83)

### Non-slotted by default

`@model` builds a plain, non-slotted `@dataclass` unless you pass
`slots=True`. This used to be the rejected option (see history below); the
calculus changed once the compiled `compile_json_default` hook was dropped for
being unneeded complexity — without it, a slotted default's only path to fast
serialization requires a per-model compiled hook the library no longer ships,
so it would pay the 6.16x row above by default. Landing on orjson's native
fast path with zero hooks was worth more than the memory savings.

**Caveat:** orjson's fast path dumps whatever is in the instance's `__dict__`
minus underscore-prefixed keys. A stray attribute set post-construction
(`user.session_token = "..."`) silently
[leaks into the JSON](https://github.com/ijl/orjson/issues/83) — the slotted
path can't do this, since it filters through `__dataclass_fields__`. If your
code sets extra attributes on model instances after construction, either keep
those private with a leading underscore, or opt into `@model(slots=True)`.

**Previously rejected, for the record:** the earlier measurement gave
non-slotted only a 5% end-to-end win (0.873 vs 0.913 ms) for 55% more memory
(113 vs 72 B/object) — not worth the correctness caveat above, or so it
seemed. That comparison assumed the slotted path would use the compiled hook
(2.67x row) rather than the native fallback (6.16x row); once the hook was
removed, non-slotted stopped being a marginal call and became the clear
default.

---

## Rejected, with reasons

### attrs

orjson does **not** serialize attrs classes natively — `TypeError: Type is not JSON
serializable` for both `@define` and `@define(slots=False)`. So attrs still needs a
`default=` hook, and `attrs.asdict` is ~6x slower than a compiled dict literal
(1507 vs 236 ns/object).

Since rowform bypasses `__init__` entirely via `object.__new__`, attrs' generated-init
advantages don't apply. Attribute read/write and construction measured identical to
`dataclass(slots=True)` within noise. `@define` also adds a `__weakref__` slot by
default, making instances 8 bytes larger (set `weakref_slot=False` for parity).

**No remaining advantage for this use case, and orjson support is a strict argument
for stdlib dataclasses.**

### Streaming the cursor instead of `fetchall()`

Feeding `conn.execute(...)` straight into the batch hydrator to avoid materializing
an intermediate list of tuples: 0.715 vs 0.706 ms — a wash. `fetchall()` is already
a C-level loop.

### Further hydration micro-optimization

`conn.execute()` alone costs 0.005 ms of a 0.97 ms request; essentially all of the
65% "query + fetch" stage is the driver creating Python `int`/`str` objects. An
object path needs those objects, so that cost is not addressable from rowform.

---

## Honest limits on the claim

- **The ~6x is over SQLAlchemy's ORM, not over writing it yourself.** Hand-written
  asyncpg with `dict(record)` reaches 3877 rps against rowform's 3168 in the same
  isolated configuration. Against a competent hand-rolled loop, rowform *loses*
  slightly; it costs +10–25% CPU over building no objects at all. The value is
  ergonomics at near-hand-written cost, not beating hand-written code.
- **Nothing scales past ~c=8 on a 4-vCPU box**, and p99 degrades sharply (2.8 ms at
  c=8 → 86 ms at c=64) as requests queue on a 10-connection pool. That is pool and
  core saturation, not a property of any mapper.
- **DB-side JSON (`json_agg`) beats every object path** by ~2.2x, because it skips
  both object construction and Python serialization. It is implemented
  (`Query.to_json_sql`, `DatabaseEngine.fetch_json`) but parked — it is not an
  object mapper. If your endpoint only ever emits JSON, it is the faster answer and
  rowform's object path is the wrong tool.


---

## Retracted

Three predictions that measurement contradicted, and one that measurement
confirmed but for a different reason than expected. Recorded because each was
acted on, or nearly was.

### Collapsing sqlite's extra round trip is not a win

`SqliteEngine._fetch_rows` does `execute` then `fetchall` — two thread handoffs —
where SQLAlchemy's aiosqlite adapter buffers both in one. Predicted that
collapsing it via `aiosqlite.execute_fetchall()` would be free throughput.

Measured the opposite: 1 round trip = 1.0702 ms, 2 round trips = 1.0366 ms.
**Do not act on this.**

### Hydrating off the adapter's row deque is not a win

SQLAlchemy's asyncpg adapter buffers rows in a `deque`, and `cursor.fetchall()`
copies it to a list. Predicted that hydrating straight off the deque would save
the copy. Measured *slower*: 1.0947 ms against 1.0115 ms.

### `exec_driver_sql` to skip compilation is not a win

Predicted that bypassing the compile-cache lookup would show up. Measured a
wash-to-slightly-worse (1.0308 vs 1.0115) — exactly as a ~0.001 ms cache hit
predicts. **There was never anything there to win**, which is the useful part:
the compile step had been assumed expensive for long enough to be worth
measuring, and it is not.

### `AUTOCOMMIT` does not pay for itself on the read path

SQLAlchemy's implicit `BEGIN`/`COMMIT` around a pooled read was assumed to be a
material per-request cost. Setting `isolation_level="AUTOCOMMIT"` measured
*slower*: 1.2193 ms against 1.1944 ms.

The per-request cost that *is* real is pool acquisition — SQLAlchemy's costs
~0.18 ms against rowform's ~0.03–0.08 ms, which is the whole reason the current
design has Core compile and rowform's engine execute.

---

## New, from the rewrite

### orjson's native UUID path is exact-type

orjson serializes `uuid.UUID` natively, but the check is on the exact type.
asyncpg returns `asyncpg.pgproto.UUID`, a genuine `uuid.UUID` *subclass* holding
an identical value — and orjson falls through to `default=` for it. Stock
SQLAlchemy Core returns the same object (its asyncpg dialect sets
`supports_native_uuid`, so `Uuid.result_processor` is `None`), so this is a
serializer quirk rather than a hydration difference. It still has to be handled,
and handled *identically for every contender*, or it reads as one.

### A hand-written converter table cannot be right

The retired `SQLITE_CONVERTERS = {bool: bool}` was keyed by exact Python type.
Two things follow, and both are unfixable without abandoning the approach:

* A nullable column wants `bool | None`, which never matches `bool`, so
  conversion silently did not run.
* `type.python_type` is not total — it raises for some types, and `Enum` resolves
  to bare `str`, losing the enum class.

Asking each *column* for its own `result_processor` has neither problem: the
lookup is per-column rather than per-type, so nullability is irrelevant, and the
dialect supplies the answer so `python_type` is never consulted on the read path
at all.

### Compiling with the cache key is not optional

SQLAlchemy's structural cache key deliberately ignores literal values — that is
what makes compiling once worthwhile. The consequence is that a cached compiled
statement holds whichever literals the *first* caller had, so
`insert(...).values(id=51)` executed against a query compiled from `values(id=50)`
inserts 50 again, silently.

The fix is SQLAlchemy's own: compile *with* the cache key, and pass the current
statement's `CacheKey.bindparams` as `extracted_parameters` on every call. Without
the first half, the second raises.
