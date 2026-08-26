"""Contenders for `bench micro`, one function each.

Every function has the same shape: `async def f(init: ContenderInit) ->
tuple[Target, Teardown]`. `target()` runs one unit of work and returns
response-ready JSON bytes (for the equivalence gate); `teardown()`
releases whatever the factory opened. `@contender(...)` is typed against exactly
that shape, so a factory that takes the wrong argument or returns the wrong thing
is a type error at the decorator rather than a runtime surprise.

**What is being compared changed with the rewrite.** "rowform vs SQLAlchemy Core"
is incoherent now that Core *is* the SQL generator on both sides — the same
`select()` compiles to the same string for every contender here. What remains,
and what this file is organised around:

* **vs the stock Core result layer** — `Row`/`CursorResult` against a compiled
  hydrator over the same driver. This is the headline claim now, not a footnote.
* **vs the SQLAlchemy ORM** — the instrumentation gap, still a real comparison.
  Registered twice, because stock declarative returns instrumented objects
  carrying loader state and `MappedAsDataclass` does not; comparing against only
  the first would overstate the win.
* **against two floors, permanently.** A driver-to-dicts floor bounds the
  whole stack, and a driver-plus-*the same hydrator* floor separates the engine's
  cost from the hydrator's. Keeping only the first is how an earlier run produced
  a "floor" slower than the thing it was bounding.

**Two claims, two spellings of rowform — never blended.** The plain `rowform`
rows make the *result-layer* claim, so they do the same work as every rival:
the statement goes in unprepared (paying rowform's structural cache-key per
call, as `conn.execute(stmt)` pays SQLAlchemy's) and the payload is built by
the same per-row pass the ORM rows pay. The `rowform (idiomatic)` rows
(`tags=("idiomatic",)`) make the *endpoint* claim — the code an application
would actually write: statement prepared once at startup, dataclasses handed
straight to orjson's C serializer. The delta between the two rows prices the
prepare-once and direct-serialization advantages explicitly, instead of
smuggling them into the result-layer ratio (which an earlier version of this
file did, in violation of its own rule below).

**Every non-floor contender builds its payload the same way**, with a
`{field: getattr(obj, field)}` comprehension over the shape's field list —
shared per shape (`_flat_objs`/`_join_objs`/`_wide_objs`) so the parity is
structural rather than a rule someone has to keep re-verifying. That is
not a style rule: the `MappedAsDataclass` rows used `dataclasses.asdict()`, which
deep-copies recursively, and on `wide` that cost more than the ORM work the row
was there to measure — 14 ms of a 17 ms cell, for byte-identical JSON. The row
registered to *avoid* overstating the win was carrying the largest handicap in
the file. If a contender needs a different payload builder — Core positional's
unpacking builders (cheaper than getattr: the generosity runs toward the
rival), `.mappings()`'s priced `str()` cast, the idiomatic rows' direct dumps —
the difference is deliberate and priced, never incidental.

**Every contender runs its read inside `BEGIN`...`COMMIT`**, because that is what
the code this library is measured against looks like — `async with session.begin():
session.execute(...)`. It is also the only way the comparison is honest: SQLAlchemy
autobegins on first statement and rolls back on release, so a `Core`/ORM contender
was always paying for a transaction, while `Engine.fetch_all()` off the engine opens
none. Left alone, part of rowform's margin was a weaker isolation guarantee billed
as row-layer speed. Measured cost of closing that gap: 0.711x -> 0.782x against Core
on sqlite, and 1.015x -> 1.134x against the raw asyncpg floor on postgres.

**On sqlite the floors send a literal `BEGIN`, because rowform does.** pysqlite only
implicitly begins before DML, so SQLAlchemy's `begin()` around a SELECT sends nothing
and stock Core's read stays in autocommit — but rowform applies SQLAlchemy's own
pysqlite recipe (`SqliteDriver.configure`), without which `begin_nested()`'s savepoint
lands outside its transaction. So rowform's read *is* in a transaction here and Core's
is not, and a floor that skipped the `BEGIN` was a round trip lighter than the thing it
bounded: it priced "the row layer" as the row layer plus a transaction. That is
correction 15's bug one backend over, and it survived here because the earlier reasoning
took "the contenders" to mean the SQLAlchemy ones. `_sqlite_txn_engine` gives the
same-plumbing floor rowform's two events, and the hand-rolled floors spell it on the
driver connection, which puts all of them at rowform's five driver round trips.

The asymmetry with Core is left standing rather than erased, because it is real: an
application writing `engine.begin()` on stock SQLAlchemy genuinely gets no transaction
on this backend. It is priced instead, from both sides — `rowform (no transaction)`
takes the guarantee off rowform, and `SQLAlchemy Core (positional, real transaction)`
puts it onto Core.

**On postgres the opposite holds**, and *every* floor there spells the transaction on
the driver connection with `conn.transaction()` — a real `BEGIN`/`COMMIT` on the wire —
because that is what the contenders send. `sa_conn.begin()` is not a substitute for it
even on the SQLAlchemy-pooled floor: SQLAlchemy autobegins *lazily*, emitting `BEGIN`
with the first statement routed through SQLAlchemy, and these floors deliberately await
the driver directly instead. It marks a transaction in Python and sends nothing. That
was live in `pg_flat_sa_plumbing_dict` through the 2026-08-15 sweep and is what made it
land below the raw-asyncpg floor; the audit that caught it (`log_statement=all`, counting
`BEGIN`/`COMMIT` per iteration for every contender in the cell) is the check to repeat
whenever a floor is added. The rule is "match whatever the contenders' transaction
actually costs on this backend", not "avoid `BEGIN`"; the sqlite spelling is a
consequence, not the principle.

Two exemptions. The mock contenders have no connection to begin on. And
`rowform (no transaction)` is registered deliberately without one, because
`Engine.fetch_all()` off the engine opens none and pricing that separately is the
point of the row — it is the one contender the rule above does not apply to.

**`wide` has no mock cell at all**, so it has no `hand-written dict (mock)` arm either.
That is not an oversight in the parsing floor: nothing else is registered under
`backend="mock"` for `wide`, and a cell with one contender compares it to nothing and
passes the equivalence gate vacuously. Adding a wide mock arm means adding the rowform
and SQLAlchemy siblings with it.

`bench micro` calls these factories directly — this is its whole registry. The
FastAPI load-test worker (`service/app.py`) is deliberately *not* a consumer: it
is hand-written so it profiles as real named functions instead of frames through
this file's closures.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

import orjson
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import asyncpg
from sqlalchemy.dialects.postgresql import psycopg as psycopg_dialect
from sqlalchemy.dialects.sqlite import aiosqlite
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.util import await_only

import rowform as rf

_SQLITE_DIALECT = aiosqlite.dialect()
_PG_DIALECT = asyncpg.dialect()
from benchmarks.harness.registry import ContenderInit, Target, Teardown, contender
from benchmarks.shapes.flat import User, UserDC, UserORM, users_table
from benchmarks.shapes.join import (
    AUTHOR_FIELDS,
    POST_FIELDS,
    Author,
    AuthorDC,
    AuthorORM,
    Post,
    PostDC,
    PostORM,
)
from benchmarks.shapes.wide import Event, EventDC, EventORM, Severity, events_table
from benchmarks.shapes.write import orm_params as write_orm_params
from benchmarks.shapes.write import orm_update as write_orm_update
from benchmarks.shapes.write import update_params as write_params
from benchmarks.shapes.write import update_stmt as write_update

FLAT_FIELDS = [str(c.name) for c in users_table.columns]
WIDE_FIELDS = [str(c.name) for c in events_table.columns]

#: One pool configuration for every contender that opens one, so a cell compares
#: result layers rather than pool settings.
#:
#: `4+0` rather than `1+3`, which is the same ceiling but not the same pool.
#: SQLAlchemy *closes* overflow connections on return while asyncpg's pool retains
#: them, so under `1+3` four concurrent checkouts reuse exactly one connection
#: across rounds and re-establish three, where asyncpg reuses all four — measured,
#: and on postgres that difference is a TCP connect plus auth on three quarters of
#: the traffic. Under `4+0` both retain four. `POOL_MAX` is the same ceiling for
#: the pools that are not SQLAlchemy's.
POOL = {"pool_size": 4, "max_overflow": 0}
POOL_MAX = POOL["pool_size"] + POOL["max_overflow"]


def _default(value: Any) -> Any:
    """The two things orjson will not serialize on its own here.

    `Decimal` it simply has no native path for. `uuid.UUID` it does — but that
    path is keyed on the *exact* type, and asyncpg hands back
    `asyncpg.pgproto.UUID`, which is a genuine `uuid.UUID` subclass carrying an
    identical value. Stock SQLAlchemy Core returns the same object (its asyncpg
    dialect sets `supports_native_uuid`, so `Uuid.result_processor` is None), so
    this is a serializer quirk rather than a hydration difference — and it is
    registered for every contender precisely so it stays one.
    """
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def dumps(payload: Any) -> bytes:
    return orjson.dumps(payload, default=_default)


def _sa_dsn(path: str) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _sqlite_txn_engine(path: str) -> Any:
    """A stock `AsyncEngine` whose `begin()` actually reaches sqlite.

    pysqlite begins implicitly before DML and never before a SELECT, so
    `sa_conn.begin()` around a read emits nothing and the connection stays in
    autocommit — while rowform sends a real `BEGIN`, because without one pysqlite
    puts `begin_nested()`'s savepoint outside the transaction and silently breaks
    it (`SqliteDriver.configure`). A floor that skips the BEGIN is a round trip
    lighter than the thing it bounds, which is correction 15's bug one backend
    over, and it made this floor price "the row layer" as a row layer plus a
    transaction.

    These are rowform's own two events, spelled the same way, so "same plumbing"
    covers the transaction and not just the pool.
    """
    engine = create_async_engine(_sa_dsn(path), **POOL)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_implicit_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _explicit_begin(conn: Any) -> None:
        await_only(conn.connection.driver_connection.execute("BEGIN"))

    return engine


def _sa_dsn_pg(dsn: str) -> str:
    """psycopg-style DSN (`postgresql://...?sslmode=disable`) -> the URL the
    asyncpg SQLAlchemy dialect wants: swap the driver prefix and drop the query
    string (that dialect forwards query params verbatim to `asyncpg.connect()`,
    which has no `sslmode` kwarg)."""
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1).split("?", 1)[0]


# --------------------------------------------------------------------------
# The statements. One per shape, shared by every contender of that shape, so
# no contender can accidentally be measured on a different query.
# --------------------------------------------------------------------------


def flat_stmt(limit: int, model: Any = User) -> Any:
    """`model` is deliberately untyped: the whole point is that the *same*
    statement is built against four different declarations of one table — the
    rowform model, the ORM one, and the dataclass ORM one — so no contender can
    be measured on a different query."""
    return (
        select(model)
        .where(model.is_active == True)
        .where(model.id > 100)
        .limit(limit)
    )


def join_stmt(limit: int, left: Any = Author, right: Any = Post) -> Any:
    return (
        select(left, right)
        .join(right, right.author_id == left.id)
        .where(left.is_active == True)
        .where(right.score > 100)
        .limit(limit)
    )


def wide_stmt(limit: int, model: Any = Event) -> Any:
    return (
        select(model)
        .where(model.seen == True)
        .where(model.id > 100)
        .limit(limit)
    )


# --------------------------------------------------------------------------
# Payload builders, written out per shape rather than driven by a field list.
#
# This is not a style choice. A generic `{f: v for f, v in zip(fields, row)}`
# builder costs a zip, a per-field lookup and a membership test per column, and
# the first version of this file used one everywhere — which made the "true
# floor" *slower than rowform*, tripping the suite's own tripwire: rowform
# appearing to beat the floor is always a bug in the floor. A floor must do
# strictly less work than every contender, and shared helper code that quietly
# does more is exactly how it stops doing so.
#
# They also read the row by *unpacking* it, exactly as the generated hydrator
# does, rather than by four subscripts. That is not micro-optimisation either:
# an asyncpg `Record` is more expensive to index than a tuple, and a floor that
# indexed four times where the hydrator unpacks once measured *slower than
# rowform* on postgres while measuring faster on sqlite. The floor has to be
# below the contender because it does less work, not because of how it happens
# to read its input.
#
# `_raw` variants coerce sqlite's 0/1 to bool; the others receive rows that have
# already been through SQLAlchemy's processors. Every one of them must produce
# byte-identical JSON — the equivalence gate checks it on every run.
# --------------------------------------------------------------------------


def _flat_raw(rows):
    return [
        {"id": a, "name": b, "email": c, "is_active": bool(d)} for a, b, c, d in rows
    ]


def _flat(rows):
    return [{"id": a, "name": b, "email": c, "is_active": d} for a, b, c, d in rows]


def _join_raw(rows):
    return [
        [
            {"id": a, "name": b, "email": c, "is_active": bool(d)},
            {"id": e, "author_id": f, "title": g, "score": h, "published": bool(i)},
        ]
        for a, b, c, d, e, f, g, h, i in rows
    ]


def _join(rows):
    return [
        [
            {"id": a, "name": b, "email": c, "is_active": d},
            {"id": e, "author_id": f, "title": g, "score": h, "published": i},
        ]
        for a, b, c, d, e, f, g, h, i in rows
    ]


def _wide_raw(rows):
    """sqlite hands back 0/1, strings and floats; these are the conversions
    SQLAlchemy's per-column processors would apply, written out by hand.

    Skipping them would not make a faster floor, it would make a *different
    answer* — 8 of these 9 columns come back wrong untouched, which is the whole
    point of this shape (`shapes/wide.py`). `Decimal` is built from a `%.3f`
    string rather than the float, because that is what `Numeric(12, 3)`'s
    processor does (`to_decimal_processor_factory`) and anything else disagrees
    in the last digits.
    """
    return [
        {
            "id": a,
            "label": b,
            "seen": bool(c),
            "at": dt.datetime.fromisoformat(d),
            "day": dt.date.fromisoformat(e),
            "amount": decimal.Decimal(f"{f:.3f}"),
            "severity": Severity[g],
            "trace": uuid.UUID(h),
            "note": i,
        }
        for a, b, c, d, e, f, g, h, i in rows
    ]


def _wide(rows):
    return [
        {
            "id": a,
            "label": b,
            "seen": c,
            "at": d,
            "day": e,
            "amount": f,
            "severity": g,
            "trace": h,
            "note": i,
        }
        for a, b, c, d, e, f, g, h, i in rows
    ]


# The equal-work payload pass every non-floor, non-idiomatic contender pays —
# one shared builder per shape, so "same payload builder" is enforced by
# construction rather than by review (see the module docstring's payload rule,
# which the rowform arms had silently drifted from before these existed).


def _flat_objs(objs):
    return [{f: getattr(u, f) for f in FLAT_FIELDS} for u in objs]


def _join_objs(pairs):
    return [
        [
            {f: getattr(a, f) for f in AUTHOR_FIELDS},
            {f: getattr(p, f) for f in POST_FIELDS},
        ]
        for a, p in pairs
    ]


def _wide_objs(objs):
    return [{f: getattr(e, f) for f in WIDE_FIELDS} for e in objs]


# ==========================================================================
# flat shape (`users`) — sqlite
# ==========================================================================


@contender(
    "rowform",
    backend="sqlite",
    shape="flat",
    description="The result-layer claim: compiled hydrator, equal work — unprepared "
    "statement, same payload pass as the ORM rows.",
)
async def flat_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (prepared)",
    backend="sqlite",
    shape="flat",
    tags=("decomposition",),
    description="Equal work except prepare-once — the middle rung that decomposes the "
    "idiomatic delta into cache-key vs serialization.",
)
async def flat_rowform_prepared(init: ContenderInit) -> tuple[Target, Teardown]:
    """The decomposition ladder, one variable per rung: `rowform` (unprepared,
    equal payload) minus this row is the structural cache-key cost; this row
    minus `rowform (idiomatic)` is the Python payload pass vs orjson's C
    dataclass path. Added because the recorded join/postgres gap between the
    equal-work and idiomatic rows (0.73x) bundled both and could not be read.
    """
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs(await conn.fetch_all(query)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="sqlite",
    shape="flat",
    tags=("idiomatic",),
    description="The endpoint claim: rowform as an app writes it — prepared once, "
    "dataclasses straight to orjson.",
)
async def flat_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    """The other family (module docstring): the delta between this row and
    plain `rowform` is what prepare-once plus direct C serialization are
    worth, priced instead of blended into the result-layer ratio."""
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose

# The one row that is deliberately *not* in a transaction, registered on `flat`
# only — the no-transaction cost is a property of the API, not of the shape, so
# pricing it once per backend is enough.


@contender(
    "rowform (no transaction)",
    backend="sqlite",
    shape="flat",
    description="`fetch_all()` straight off the engine — one statement, no transaction opened.",
)
async def flat_rowform_oneshot(init: ContenderInit) -> tuple[Target, Teardown]:
    """The cheaper, weaker read, priced rather than published as the headline.

    `Engine.fetch_all()` opens no transaction at all (`engine.py`), so a row
    measured this way is not comparable to contenders that all pay for one — it
    buys its margin partly with a weaker guarantee. Registering it next to the
    transactional row is what makes that a number instead of a footnote.
    """
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_flat_objs(await engine.fetch_all(stmt)))

    return target, sa_engine.dispose


# rowform ships two tracks over one connection, told apart by name: `fetch_all()`
# hands back hydrated objects, and `execute()` hands the same objects to
# SQLAlchemy's own `Result`. Registering both is the only way the "you pay per
# accessor, not per execute" claim is a measurement rather than an assertion —
# and the accessor is what varies, so the compat track is registered twice at
# arity one. Same statement, same hydrator, same one-shot checkout throughout;
# only the result layer differs, and the equivalence gate holds the payload
# byte-identical across all three.


@contender(
    "rowform compat (.scalars())",
    backend="sqlite",
    shape="flat",
    description="The compat track: rowform's rows inside SQLAlchemy's Result, taken as scalars.",
)
async def flat_rowform_compat_scalars(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs((await conn.execute(stmt)).scalars().all()))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.all())",
    backend="sqlite",
    shape="flat",
    description="The same Result taken as rows — one SQLAlchemy Row built per row.",
)
async def flat_rowform_compat_rows(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs([row[0] for row in (await conn.execute(stmt)).all()]))

    return target, sa_engine.dispose


@contender(
    "rowform (mock)",
    backend="mock",
    shape="flat",
    tags=("mapper-floor",),
    description="rowform's row-layer cost alone, via MockEngine — zero driver cost.",
)
async def flat_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """Unprepared, like the real `rowform` row it is the floor of — this is
    also what makes `engines/mock.py`'s "including the per-request cache-key
    lookup" claim true: a prepared `CoreQuery` short-circuits that lookup
    entirely, so the earlier prepared spelling could never have caught a
    cache-key regression."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle, FLAT_FIELDS)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_flat_objs(await engine.fetch_all(stmt)))

    return target, engine.sa_engine.dispose


@contender(
    "hand-written dict (mock)",
    backend="mock",
    shape="flat",
    shipped=False,
    tags=("mapper-floor", "floor"),
    description="The parsing floor: canned driver rows straight to dicts, no engine at all.",
)
async def flat_dict_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """What reading these rows costs if nothing at all sits between them and the
    payload — no engine, no connection, no pool, no transaction.

    The mock backend already cans the driver, so this is the only arm in the file
    where the number is purely "parse N rows into something serializable". Every
    other floor still has plumbing in it, however little. Registered here rather
    than as a sqlite contender because on a real backend it would be
    indistinguishable from the hand-rolled floor.
    """
    rows = init.handle

    async def target() -> bytes:
        return dumps(_flat_raw(rows))

    async def teardown() -> None:
        return None

    return target, teardown


@contender(
    "floor: hand-rolled (dict)",
    backend="sqlite",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The true floor: driver rows straight to dicts, no object construction.",
)
async def flat_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    # Imported here, not at module scope. `python -m benchmarks` loads locust,
    # which gevent-monkey-patches `threading.Thread` into a greenlet; aiosqlite
    # binding that instead of a real thread deadlocks its worker against the
    # asyncio loop, and the whole run hangs before its first result.
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    sql, params = _compiled(flat_stmt(init.limit), _SQLITE_DIALECT)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()
        return dumps(_flat_raw(rows))

    return target, pool.close


@contender(
    "floor: hand-rolled (hydrator)",
    backend="sqlite",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The second floor: same driver, same hydrator, no engine — isolates engine cost.",
)
async def flat_raw_aiosqlite_hydrated(init: ContenderInit) -> tuple[Target, Teardown]:
    """Why two floors, always.

    A floor whose hydrator is *slower* than the contender's is not a floor. An
    earlier run used `User(**kwargs)` as the floor's row constructor while
    rowform used its generated one, and the floor came out slower than the thing
    it was bounding. Measuring both a dicts-only floor and a floor running the
    *same* hydrator separates "what does the engine cost" from "what does row
    construction cost", which is the only way either number means anything
.
    """
    # Imported here, not at module scope. `python -m benchmarks` loads locust,
    # which gevent-monkey-patches `threading.Thread` into a greenlet; aiosqlite
    # binding that instead of a real thread deadlocks its worker against the
    # asyncio loop, and the whole run hangs before its first result.
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    dialect = _SQLITE_DIALECT
    statement = flat_stmt(init.limit)
    sql, params = _compiled(statement, dialect)
    hydrate = _hydrator(statement, dialect, FLAT_FIELDS)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()
        return dumps(hydrate(rows))

    return target, pool.close


# The third floor, and the one that answers the adoption question.
#
# The other two hand-roll the plumbing as well as the rows, so the gap between
# them and `rowform` is mostly SQLAlchemy's pool and transaction — measured at a
# fixed ~0.41 ms per request, the same on `flat` and on `wide`, which is what
# gives it away as per-request overhead rather than row-layer work. That is a
# real cost, but it is one an application on SQLAlchemy is *already paying*
# before rowform enters the picture, so charging it to the row layer answers a
# question nobody has.
#
# This floor holds the plumbing constant instead: SQLAlchemy's pool, SQLAlchemy's
# transaction, the same compiled statement, and hand-written dicts where rowform
# runs its hydrator. What separates it from `rowform` is the row layer and
# nothing else — measured at +0.034 ms on flat and -0.033 ms on wide, i.e. zero
# within noise. Keep all three: this one prices the abstraction, the other two
# bound the stack.


@contender(
    "floor: on SQLAlchemy (dict)",
    backend="sqlite",
    shape="flat",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, hand-written dicts — the abstraction floor.",
)
async def flat_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = _sqlite_txn_engine(init.handle)
    sql, params = _compiled(flat_stmt(init.limit), _SQLITE_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            # The driver connection, not the adapter's cursor: statements are
            # awaited from a real coroutine rather than through `greenlet_spawn`,
            # which is what rowform does and what this floor has to match to be
            # measuring the row layer and not the execution path.
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with sa_conn.begin():
                cur = await driver_conn.execute(sql, params)
                rows = await cur.fetchall()
        return dumps(_flat_raw(rows))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="sqlite",
    shape="flat",
    description="The headline comparison: identical SQL, stock Row/CursorResult result layer.",
)
async def flat_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_flat(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy Core (positional, real transaction)",
    backend="sqlite",
    shape="flat",
    tags=("decomposition",),
    description="Core with SQLAlchemy's own pysqlite recipe applied — the same read, "
    "this time actually inside a transaction.",
)
async def flat_sa_core_positional_txn(init: ContenderInit) -> tuple[Target, Teardown]:
    """The row that keeps the sqlite comparison symmetric.

    `engine.begin()` sends nothing to pysqlite before a SELECT, so stock Core's
    read runs in autocommit: no snapshot shared by two statements, and a
    `begin_nested()` savepoint that lands outside the transaction its writes open.
    Both are defects SQLAlchemy documents and declines to fix by default, with the
    recipe `_sqlite_txn_engine` applies. rowform applies it — so on this backend
    rowform pays a round trip for a guarantee stock Core does not provide, and the
    published sqlite ratios were reading that as row-layer cost.

    Neither headline row is doctored to hide it. `rowform (no transaction)` prices
    the guarantee off rowform's side, this prices it onto Core's, and the table
    then orders both libraries at both guarantees rather than asserting that the
    difference between them is speed.
    """
    engine = _sqlite_txn_engine(init.handle)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_flat(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy Core (.mappings())",
    backend="sqlite",
    shape="flat",
    description="Core via .mappings() — orjson needs a per-key str() cast for it.",
)
async def flat_sa_core_mappings(init: ContenderInit) -> tuple[Target, Teardown]:
    """`.mappings()` yields `RowMapping`s keyed by `quoted_name` (a `str` subclass
    orjson refuses), so every row pays a `str()` cast per key — kept registered
    alongside the positional variant rather than "corrected away", because a
    workaround one contender needs has to be priced, not hidden."""
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps([{str(k): v for k, v in m.items()} for m in result.mappings()])

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="sqlite",
    shape="flat",
    description="SQLAlchemy ORM, one Session per request.",
)
async def flat_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    """Fresh `Session` per request, bound to a per-request connection: hoisting
    the `Session` would let its identity map skip hydration on every request
    after the first — what is inside each timed region gets audited."""
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit, UserORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            users = (await session.execute(stmt)).scalars().all()
            return dumps(_flat_objs(users))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM (DC)",
    backend="sqlite",
    shape="flat",
    description="SQLAlchemy ORM (MappedAsDataclass) — the closest ORM shape to what rowform builds.",
)
async def flat_sa_orm_dc(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit, UserDC)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            users = (await session.execute(stmt)).scalars().all()
            return dumps(_flat_objs(users))

    return target, engine.dispose


@contender(
    "SQLAlchemy Core (positional) (mock)",
    backend="mock",
    shape="flat",
    tags=("mapper-floor",),
    description="Core's result-layer cost alone, via mock_sqlalchemy_engine — zero driver cost.",
)
async def flat_sa_core_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.engines.mock import mock_sqlalchemy_engine

    engine = mock_sqlalchemy_engine(FLAT_FIELDS, init.handle)
    stmt = flat_stmt(init.limit)
    # Hoisted, because rowform's mock overrides `_connection` to yield nothing —
    # the ~0.4 ms checkout is the cost this instrument exists to exclude, and
    # leaving it inside the timed region here made the ratio a checkout
    # comparison as much as a row-layer one.
    conn = await engine.connect()

    async def target() -> bytes:
        result = await conn.execute(stmt)
        return dumps(_flat(result.all()))

    async def teardown() -> None:
        await conn.close()
        await engine.dispose()

    return target, teardown


@contender(
    "SQLAlchemy ORM (mock)",
    backend="mock",
    shape="flat",
    tags=("mapper-floor",),
    description="The ORM's hydration cost alone, via mock_sqlalchemy_engine — zero driver cost.",
)
async def flat_sa_orm_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.engines.mock import mock_sqlalchemy_engine

    engine = mock_sqlalchemy_engine(FLAT_FIELDS, init.handle)
    stmt = flat_stmt(init.limit, UserORM)
    # `bind=conn` rather than `bind=engine`: a fresh Session per request (so the
    # identity map is fresh, as in production) over a connection checked out
    # once, so the excluded cost is the same one rowform's mock excludes.
    conn = await engine.connect()

    async def target() -> bytes:
        async with AsyncSession(bind=conn) as session:
            users = (await session.execute(stmt)).scalars().all()
            return dumps(_flat_objs(users))

    async def teardown() -> None:
        await conn.close()
        await engine.dispose()

    return target, teardown


# ==========================================================================
# join shape (`j_authors` x `j_posts`) — sqlite
# ==========================================================================


@contender(
    "rowform",
    backend="sqlite",
    shape="join",
    description="The result-layer claim at arity two: one compiled hydrator, equal work.",
)
async def join_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (prepared)",
    backend="sqlite",
    shape="join",
    tags=("decomposition",),
    description="Equal work except prepare-once, at arity two — see the flat twin.",
)
async def join_rowform_prepared(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs(await conn.fetch_all(query)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="sqlite",
    shape="join",
    tags=("idiomatic",),
    description="The endpoint claim at arity two: prepared once, object pairs straight to orjson.",
)
async def join_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the flat twin."""
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.all())",
    backend="sqlite",
    shape="join",
    description="The compat track at arity two, where a Row is what the accessor returns.",
)
async def join_rowform_compat(init: ContenderInit) -> tuple[Target, Teardown]:
    """`.scalars()` has no meaning here — it would take the `Author` and drop the
    `Post` — so rows are the idiomatic compat spelling at arity two, and the Row
    per row is unavoidable rather than opt-in."""
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs([(a, p) for a, p in (await conn.execute(stmt)).all()]))

    return target, sa_engine.dispose


@contender(
    "rowform (mock)",
    backend="mock",
    shape="join",
    tags=("mapper-floor",),
    description="rowform's join row-layer cost alone, via MockEngine — zero driver cost.",
)
async def join_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """Unprepared, like the real row it is the floor of — see the flat mock."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle, AUTHOR_FIELDS + POST_FIELDS)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_join_objs(await engine.fetch_all(stmt)))

    return target, engine.sa_engine.dispose


@contender(
    "hand-written dict (mock)",
    backend="mock",
    shape="join",
    shipped=False,
    tags=("mapper-floor", "floor"),
    description="The parsing floor at arity two: canned rows split into two dicts each.",
)
async def join_dict_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the flat twin."""
    rows = init.handle

    async def target() -> bytes:
        return dumps(_join_raw(rows))

    async def teardown() -> None:
        return None

    return target, teardown


@contender(
    "floor: hand-rolled (dict)",
    backend="sqlite",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The true floor: driver rows split into two dicts per row.",
)
async def join_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    # Imported here, not at module scope. `python -m benchmarks` loads locust,
    # which gevent-monkey-patches `threading.Thread` into a greenlet; aiosqlite
    # binding that instead of a real thread deadlocks its worker against the
    # asyncio loop, and the whole run hangs before its first result.
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    sql, params = _compiled(join_stmt(init.limit), _SQLITE_DIALECT)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()
        return dumps(_join_raw(rows))

    return target, pool.close


@contender(
    "floor: hand-rolled (hydrator)",
    backend="sqlite",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The second floor: same driver, same hydrator, no engine.",
)
async def join_raw_aiosqlite_hydrated(init: ContenderInit) -> tuple[Target, Teardown]:
    # Imported here, not at module scope. `python -m benchmarks` loads locust,
    # which gevent-monkey-patches `threading.Thread` into a greenlet; aiosqlite
    # binding that instead of a real thread deadlocks its worker against the
    # asyncio loop, and the whole run hangs before its first result.
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    dialect = _SQLITE_DIALECT
    statement = join_stmt(init.limit)
    sql, params = _compiled(statement, dialect)
    hydrate = _hydrator(statement, dialect, AUTHOR_FIELDS + POST_FIELDS)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()
        return dumps(hydrate(rows))

    return target, pool.close


@contender(
    "floor: on SQLAlchemy (dict)",
    backend="sqlite",
    shape="join",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, hand-written dicts — the abstraction floor.",
)
async def join_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the flat twin for why this floor exists alongside the hand-rolled ones."""
    sa_engine = _sqlite_txn_engine(init.handle)
    sql, params = _compiled(join_stmt(init.limit), _SQLITE_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with sa_conn.begin():
                cur = await driver_conn.execute(sql, params)
                rows = await cur.fetchall()
        return dumps(_join_raw(rows))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="sqlite",
    shape="join",
    description="Identical SQL, stock Row/CursorResult result layer.",
)
async def join_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_join(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="sqlite",
    shape="join",
    description="SQLAlchemy ORM, two entities per row, one Session per request.",
)
async def join_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = join_stmt(init.limit, AuthorORM, PostORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            rows = (await session.execute(stmt)).all()
            return dumps(_join_objs(rows))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM (DC)",
    backend="sqlite",
    shape="join",
    description="SQLAlchemy ORM (MappedAsDataclass), two entities per row.",
)
async def join_sa_orm_dc(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = join_stmt(init.limit, AuthorDC, PostDC)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            rows = (await session.execute(stmt)).all()
            return dumps(_join_objs(rows))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM (mock)",
    backend="mock",
    shape="join",
    tags=("mapper-floor",),
    description="The ORM's join hydration cost alone — zero driver cost.",
)
async def join_sa_orm_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.engines.mock import mock_sqlalchemy_engine

    engine = mock_sqlalchemy_engine(AUTHOR_FIELDS + POST_FIELDS, init.handle)
    stmt = join_stmt(init.limit, AuthorORM, PostORM)
    # See the flat mock: the checkout is excluded on both sides or neither.
    conn = await engine.connect()

    async def target() -> bytes:
        async with AsyncSession(bind=conn) as session:
            rows = (await session.execute(stmt)).all()
            return dumps(_join_objs(rows))

    async def teardown() -> None:
        await conn.close()
        await engine.dispose()

    return target, teardown


# ==========================================================================
# wide shape (`w_events`) — sqlite
#
# The shape where correctness costs something: 8 of 9 columns need a
# per-column processor on sqlite, against 1 of 4 in `flat`.
# ==========================================================================


@contender(
    "rowform",
    backend="sqlite",
    shape="wide",
    description="The result-layer claim where processors dominate: equal work, unprepared.",
)
async def wide_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_wide_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="sqlite",
    shape="wide",
    tags=("idiomatic",),
    description="The endpoint claim over the widened shape: prepared once, direct to orjson.",
)
async def wide_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the flat twin."""
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.scalars())",
    backend="sqlite",
    shape="wide",
    description="The compat track over the widened shape — same processors, SQLAlchemy's Result.",
)
async def wide_rowform_compat(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_wide_objs((await conn.execute(stmt)).scalars().all()))

    return target, sa_engine.dispose


@contender(
    "floor: hand-rolled (dict)",
    backend="sqlite",
    shape="wide",
    shipped=False,
    tags=("floor",),
    description="The true floor where correctness costs: hand-written per-column conversion into dicts.",
)
async def wide_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    """`flat` is the shape where bypassing `Row` looks free — one `bool()` is the
    whole conversion cost, so a floor there barely has to do anything and the
    hydrator's margin over it says little. Here 8 of 9 columns need a processor,
    which makes this the pair that actually prices the compiled hydrator against
    hand-written code doing the same conversions."""
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool  # see the flat floor

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    sql, params = _compiled(wide_stmt(init.limit), _SQLITE_DIALECT)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()
        return dumps(_wide_raw(rows))

    return target, pool.close


@contender(
    "floor: hand-rolled (hydrator)",
    backend="sqlite",
    shape="wide",
    shipped=False,
    tags=("floor",),
    description="The second floor: same driver, same hydrator, no engine — processors included.",
)
async def wide_raw_aiosqlite_hydrated(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool  # see the flat floor

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    dialect = _SQLITE_DIALECT
    statement = wide_stmt(init.limit)
    sql, params = _compiled(statement, dialect)
    hydrate = _hydrator(statement, dialect, WIDE_FIELDS)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()
        return dumps(hydrate(rows))

    return target, pool.close


@contender(
    "floor: on SQLAlchemy (dict)",
    backend="sqlite",
    shape="wide",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, hand-written per-column conversion into dicts.",
)
async def wide_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    """The most informative cell of the three: plumbing held constant *and* eight
    of nine columns needing a processor, so what is left between this and
    `rowform` is the compiled hydrator against hand-written conversions."""
    sa_engine = _sqlite_txn_engine(init.handle)
    sql, params = _compiled(wide_stmt(init.limit), _SQLITE_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with sa_conn.begin():
                cur = await driver_conn.execute(sql, params)
                rows = await cur.fetchall()
        return dumps(_wide_raw(rows))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="sqlite",
    shape="wide",
    description="Identical SQL and identical processors, run through Row/CursorResult.",
)
async def wide_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_wide(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="sqlite",
    shape="wide",
    description="SQLAlchemy ORM over the widened shape, one Session per request.",
)
async def wide_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = wide_stmt(init.limit, EventORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            rows = (await session.execute(stmt)).scalars().all()
            return dumps(_wide_objs(rows))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM (DC)",
    backend="sqlite",
    shape="wide",
    description="SQLAlchemy ORM (MappedAsDataclass) over the widened shape.",
)
async def wide_sa_orm_dc(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = wide_stmt(init.limit, EventDC)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            rows = (await session.execute(stmt)).scalars().all()
            return dumps(_wide_objs(rows))

    return target, engine.dispose


# ==========================================================================
# postgres
# ==========================================================================


@contender(
    "rowform",
    backend="postgres",
    shape="flat",
    description="The result-layer claim on asyncpg: equal work — unprepared, same payload pass.",
)
async def pg_flat_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (prepared)",
    backend="postgres",
    shape="flat",
    tags=("decomposition",),
    description="Equal work except prepare-once, on asyncpg — see the sqlite flat twin.",
)
async def pg_flat_rowform_prepared(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs(await conn.fetch_all(query)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="postgres",
    shape="flat",
    tags=("idiomatic",),
    description="The endpoint claim on asyncpg: prepared once, dataclasses straight to orjson.",
)
async def pg_flat_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the sqlite flat twin."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform (no transaction)",
    backend="postgres",
    shape="flat",
    description="`fetch_all()` straight off the engine — no BEGIN/COMMIT round trip.",
)
async def pg_flat_rowform_oneshot(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the sqlite twin. The gap is wider here: on postgres the transaction
    the other contenders open is two real round trips, where on sqlite it is
    Python overhead only."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_flat_objs(await engine.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.scalars())",
    backend="postgres",
    shape="flat",
    description="The compat track on asyncpg, taken as scalars.",
)
async def pg_flat_rowform_compat_scalars(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs((await conn.execute(stmt)).scalars().all()))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.all())",
    backend="postgres",
    shape="flat",
    description="The same Result taken as rows — one SQLAlchemy Row built per row.",
)
async def pg_flat_rowform_compat_rows(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs([row[0] for row in (await conn.execute(stmt)).all()]))

    return target, sa_engine.dispose


@contender(
    "floor: hand-rolled (dict)",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The true floor: asyncpg Records straight to dicts.",
)
async def pg_flat_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
    import asyncpg

    pool = await asyncpg.create_pool(init.handle, min_size=POOL_MAX, max_size=POOL_MAX)
    assert pool is not None
    sql, params = _compiled(flat_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(sql, *params)
        return dumps(_flat(rows))

    return target, pool.close


@contender(
    "floor: hand-rolled (no pool reset)",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor", "decomposition"),
    description="The hand-rolled floor with `asyncpg.Pool`'s release-time reset query "
    "switched off — prices that round trip on its own.",
)
async def pg_flat_raw_asyncpg_no_reset(init: ContenderInit) -> tuple[Target, Teardown]:
    """`floor: hand-rolled (dict)` minus one thing. `asyncpg.Pool.release()` calls
    `Connection.reset()`, which executes `SELECT pg_advisory_unlock_all(); CLOSE
    ALL; UNLISTEN *; RESET ALL;` — a server round trip per request that
    SQLAlchemy's pool does not make (its `reset_on_return='rollback'` is a no-op
    through the asyncpg adapter with no transaction open, and these floors commit
    inside the `async with`). Passing `reset=` replaces that call, so this row
    minus the hand-rolled one is the round trip and nothing else, and what is
    left over the `no pool` floor is asyncpg's acquire/release machinery. Without
    it the two pooled floors are not comparable: the pool ratio was charging
    asyncpg for session hygiene SQLAlchemy skips (correction 8)."""
    import asyncpg

    async def _no_reset(conn: Any) -> None:
        """Replaces `Connection.reset()`; `_reset()` still runs before it, and
        only emits `ROLLBACK` when a transaction is open, which it is not here."""

    pool = await asyncpg.create_pool(
        init.handle, min_size=POOL_MAX, max_size=POOL_MAX, reset=_no_reset
    )
    assert pool is not None
    sql, params = _compiled(flat_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(sql, *params)
        return dumps(_flat(rows))

    return target, pool.close


@contender(
    "floor: no pool (dict)",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor", "decomposition"),
    description="One dedicated asyncpg connection, no pool at all — isolates the "
    "checkout cost the other two floors disagree about.",
)
async def pg_flat_no_pool(init: ContenderInit) -> tuple[Target, Teardown]:
    """The 2026-08-15 sweep put the same-plumbing floor *below* the raw-asyncpg
    floor — SQLAlchemy's checkout apparently cheaper than asyncpg's own pool.
    The two differ in pool AND everything around it, so neither number
    attributes the gap. This floor removes the pool entirely, which *bounds*
    the cost of going through each pooled floor's pool (same statement, same
    payload) without attributing it — each pooled floor still differs by more
    than its pool, in opposite directions:

    - `pg_flat_sa_plumbing_dict` also builds a SQLAlchemy `Connection` and awaits
      `get_raw_connection()` per request, so its distance is an *upper* bound on
      SQLAlchemy's checkout rather than the checkout alone. (It matches this
      floor's transaction spelling now; that it did not was correction 15.)
    - `pg_flat_raw_asyncpg` pays `asyncpg.Pool.release()` -> `Connection.reset()`,
      a `RESET ALL`-family server round trip per request that neither this floor
      nor SQLAlchemy's pool makes, so its distance is not pool overhead alone —
      `pg_flat_raw_asyncpg_no_reset` above splits it into machinery and that
      round trip.

    See METHODOLOGY.md's "Reading the floors" for what the assembled ladder says."""
    import asyncpg

    conn = await asyncpg.connect(init.handle)
    sql, params = _compiled(flat_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with conn.transaction():
            rows = await conn.fetch(sql, *params)
        return dumps(_flat(rows))

    async def teardown() -> None:
        await conn.close()

    return target, teardown


@contender(
    "floor: on SQLAlchemy (dict)",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, hand-written dicts — the abstraction floor.",
)
async def pg_flat_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    """Worth more here than on sqlite. The transaction is two real round trips on
    postgres where on sqlite it is Python overhead only, so this is the arm that
    keeps that cost out of the row-layer number.

    The transaction is opened on the *driver* connection, not with
    `sa_conn.begin()`. SQLAlchemy autobegins lazily — it emits `BEGIN` when the
    first statement goes through SQLAlchemy — and this floor deliberately
    bypasses that, awaiting the driver directly (see the sqlite twin's comment).
    So `sa_conn.begin()` marked a transaction in Python and never sent one:
    verified against `log_statement=all`, the read went out as a bare `SELECT`
    in autocommit while every contender this floor bounds sent
    `BEGIN`/`SELECT`/`COMMIT`. A floor two round trips light, which is
    correction 10 from the other side, and the actual reason the 2026-08-15 sweep
    saw it come out *below* the raw-asyncpg floor."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    sql, params = _compiled(flat_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with driver_conn.transaction():
                rows = await driver_conn.fetch(sql, *params)
        return dumps(_flat(rows))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres",
    shape="flat",
    description="Identical SQL, stock Row/CursorResult result layer, SQLAlchemy's own pool.",
)
async def pg_flat_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_flat(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy Core (.mappings())",
    backend="postgres",
    shape="flat",
    description="Core via .mappings() — the per-key str() cast orjson needs.",
)
async def pg_flat_sa_core_mappings(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps([{str(k): v for k, v in m.items()} for m in result.mappings()])

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="postgres",
    shape="flat",
    description="SQLAlchemy ORM, one Session per request.",
)
async def pg_flat_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = flat_stmt(init.limit, UserORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            users = (await session.execute(stmt)).scalars().all()
            return dumps(_flat_objs(users))

    return target, engine.dispose


@contender(
    "floor: hand-rolled (dict)",
    backend="postgres",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The true floor at arity two: asyncpg Records split into two dicts per row.",
)
async def pg_join_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
    """The join column's strongest claim (the ORM ratio) had no postgres floor
    under it — 'idiomatic is ~13% above its floor' was an extrapolation from
    sqlite, not a measurement, the same shape of blind spot the wide shape
    once was."""
    import asyncpg

    pool = await asyncpg.create_pool(init.handle, min_size=POOL_MAX, max_size=POOL_MAX)
    assert pool is not None
    sql, params = _compiled(join_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(sql, *params)
        return dumps(_join(rows))

    return target, pool.close


@contender(
    "floor: on SQLAlchemy (dict)",
    backend="postgres",
    shape="join",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, two hand-written dicts per row — the "
    "abstraction floor at arity two.",
)
async def pg_join_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the flat twin for why this floor exists alongside the hand-rolled one,
    including why the transaction is opened on the driver connection rather than
    with `sa_conn.begin()` — this floor was written with the latter and inherited
    correction 15's bug (a floor sending no `BEGIN`) before it was ever recorded."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    sql, params = _compiled(join_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with driver_conn.transaction():
                rows = await driver_conn.fetch(sql, *params)
        return dumps(_join(rows))

    return target, sa_engine.dispose


@contender(
    "rowform",
    backend="postgres",
    shape="join",
    description="The result-layer claim at arity two on asyncpg: equal work.",
)
async def pg_join_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (prepared)",
    backend="postgres",
    shape="join",
    tags=("decomposition",),
    description="Equal work except prepare-once, arity two on asyncpg — the cell the "
    "recorded 0.73x idiomatic delta demanded.",
)
async def pg_join_rowform_prepared(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs(await conn.fetch_all(query)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="postgres",
    shape="join",
    tags=("idiomatic",),
    description="The endpoint claim at arity two on asyncpg: prepared once, direct to orjson.",
)
async def pg_join_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the sqlite flat twin."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.all())",
    backend="postgres",
    shape="join",
    description="The compat track at arity two on asyncpg — see the sqlite join note.",
)
async def pg_join_rowform_compat(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs([(a, p) for a, p in (await conn.execute(stmt)).all()]))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres",
    shape="join",
    description="Identical SQL, stock Row/CursorResult result layer.",
)
async def pg_join_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_join(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="postgres",
    shape="join",
    description="SQLAlchemy ORM, two entities per row, one Session per request.",
)
async def pg_join_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = join_stmt(init.limit, AuthorORM, PostORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            rows = (await session.execute(stmt)).all()
            return dumps(_join_objs(rows))

    return target, engine.dispose


@contender(
    "rowform",
    backend="postgres",
    shape="wide",
    description="The result-layer claim where asyncpg decodes natively: equal work.",
)
async def pg_wide_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_wide_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="postgres",
    shape="wide",
    tags=("idiomatic",),
    description="The endpoint claim over the widened shape on asyncpg: prepared once, direct to orjson.",
)
async def pg_wide_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the sqlite flat twin."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform compat (.scalars())",
    backend="postgres",
    shape="wide",
    description="The compat track over the widened shape on asyncpg.",
)
async def pg_wide_rowform_compat(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_wide_objs((await conn.execute(stmt)).scalars().all()))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres",
    shape="wide",
    description="Identical SQL and processors, run through Row/CursorResult.",
)
async def pg_wide_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_wide(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="postgres",
    shape="wide",
    description="SQLAlchemy ORM over the widened shape.",
)
async def pg_wide_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = wide_stmt(init.limit, EventORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            rows = (await session.execute(stmt)).scalars().all()
            return dumps(_wide_objs(rows))

    return target, engine.dispose


# --------------------------------------------------------------------------
# The two floors need what an engine would otherwise hold for them: a compiled
# statement, and a hydrator. Built here rather than imported from a contender
# so a floor never accidentally shares an engine's caching.
# --------------------------------------------------------------------------


def _compiled(statement: Any, dialect: Any) -> tuple[str, Any]:
    """`(sql, params)` for a statement with no runtime bind parameters."""
    return rf.CoreQuery(statement, dialect).bind()


def _hydrator(statement: Any, dialect: Any, fields: list[str]) -> Any:
    """The same generated hydrator the engine would build, with the description
    a driver reporting no type codes would give (which is sqlite's)."""
    return rf.compile_hydrator(rf.plan(statement), dialect, [None] * len(fields))


# ==========================================================================
# postgres through psycopg3 — `backend="postgres-psycopg"`
#
# **Why its own backend group rather than more rows in `postgres`.** Both the
# equivalence gate and the `vs rowform` column are per group. Sharing one would
# price rowform-on-psycopg against rowform-on-asyncpg and print the driver
# difference as a row-layer result — the exact confusion corrections 12 and 14
# were about. Here every row is psycopg, so `vs rowform` means what it means in
# every other cell: the row layer, at equal driver.
#
# psycopg is the driver two silent-wrongness bugs needed to be found
# (`docs/PLAN_SQLA_API.md` §8a) and the only one with pipeline mode, and it had
# never been measured. It is also the one whose connection is transactional in
# its own right, which removes one row from the set: there is no
# `rowform (no transaction)` here, because a psycopg connection that is not in
# autocommit opens a transaction on its first statement whatever rowform does.
# ==========================================================================

_PSYCOPG_DIALECT = psycopg_dialect.dialect()


def _sa_dsn_psycopg(dsn: str) -> str:
    """psycopg-style DSN -> the URL SQLAlchemy's psycopg dialect wants.

    The query string stays, unlike `_sa_dsn_pg`: `sslmode` is libpq's own
    spelling and psycopg speaks libpq, so it is understood rather than forwarded
    to a driver that has no such keyword.
    """
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


async def _psycopg_pool(dsn: str) -> Any:
    """psycopg's own pool at the shared ceiling, opened and waited for.

    `open()` returns before the connections exist, so a floor that skipped
    `wait()` would pay its first few checkouts inside the timed window.
    """
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(dsn, min_size=POOL_MAX, max_size=POOL_MAX, open=False)
    await pool.open()
    await pool.wait()
    return pool


# --- flat -----------------------------------------------------------------


@contender(
    "rowform",
    backend="postgres-psycopg",
    shape="flat",
    description="The result-layer claim on psycopg: equal work — unprepared, same payload pass.",
)
async def psy_flat_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="postgres-psycopg",
    shape="flat",
    tags=("idiomatic",),
    description="The endpoint claim on psycopg: prepared once, dataclasses straight to orjson.",
)
async def psy_flat_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres-psycopg",
    shape="flat",
    description="Identical SQL on the same driver, stock Row/CursorResult result layer.",
)
async def psy_flat_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_flat((await conn.execute(stmt)).all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="postgres-psycopg",
    shape="flat",
    description="SQLAlchemy ORM on psycopg, one Session per request.",
)
async def psy_flat_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    stmt = flat_stmt(init.limit, UserORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            return dumps(_flat_objs((await session.execute(stmt)).scalars().all()))

    return target, engine.dispose


@contender(
    "floor: hand-rolled (dict)",
    backend="postgres-psycopg",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The true floor: psycopg tuples straight to dicts, psycopg's own pool.",
)
async def psy_flat_raw(init: ContenderInit) -> tuple[Target, Teardown]:
    """`conn.transaction()` rather than psycopg's implicit begin, so the BEGIN is
    in this file where a reader can see it — the floors sending what the
    contenders send is correction 15's whole subject."""
    pool = await _psycopg_pool(init.handle)
    sql, params = _compiled(flat_stmt(init.limit), _PSYCOPG_DIALECT)

    async def target() -> bytes:
        async with pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
        return dumps(_flat(rows))

    return target, pool.close


@contender(
    "floor: on SQLAlchemy (dict)",
    backend="postgres-psycopg",
    shape="flat",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, hand-written dicts — the abstraction floor.",
)
async def psy_flat_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    """SQLAlchemy's pool and checkout, psycopg's cursor, no result layer — so
    rowform minus this row is the row layer and nothing else.

    The transaction is opened on the driver connection, as in the asyncpg twin
    and for the same reason: `sa_conn.begin()` marks one in Python and sends
    nothing until SQLAlchemy itself routes a statement, which this floor never
    lets it do.
    """
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    sql, params = _compiled(flat_stmt(init.limit), _PSYCOPG_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with driver_conn.transaction():
                cursor = await driver_conn.execute(sql, params)
                rows = await cursor.fetchall()
        return dumps(_flat(rows))

    return target, sa_engine.dispose


# --- join -----------------------------------------------------------------


@contender(
    "rowform",
    backend="postgres-psycopg",
    shape="join",
    description="The result-layer claim at arity two on psycopg: equal work.",
)
async def psy_join_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="postgres-psycopg",
    shape="join",
    tags=("idiomatic",),
    description="The endpoint claim at arity two on psycopg: prepared once, direct to orjson.",
)
async def psy_join_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres-psycopg",
    shape="join",
    description="Identical SQL at arity two, stock Row/CursorResult result layer.",
)
async def psy_join_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_join((await conn.execute(stmt)).all()))

    return target, engine.dispose


# --- wide -----------------------------------------------------------------


@contender(
    "rowform",
    backend="postgres-psycopg",
    shape="wide",
    description="The result-layer claim where processors dominate, on psycopg: equal work.",
)
async def psy_wide_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_wide_objs(await conn.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (idiomatic)",
    backend="postgres-psycopg",
    shape="wide",
    tags=("idiomatic",),
    description="The endpoint claim over the widened shape on psycopg.",
)
async def psy_wide_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres-psycopg",
    shape="wide",
    description="Identical SQL and identical processors, run through Row/CursorResult.",
)
async def psy_wide_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_psycopg(init.handle), **POOL)
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(_wide((await conn.execute(stmt)).all()))

    return target, engine.dispose


# ==========================================================================
# The streamed read — `fetch_iter` and its two SQLAlchemy counterparts
#
# `fetch_iter` is the other half of the read API and had never been timed. It is
# not a faster `fetch_all`: it exists so a result larger than memory can be read
# at all, and what it costs *per chunk* is the number a caller sizing `chunk=`
# needs. Registered on `flat` at both backends, because the three drivers stream
# through three different primitives — `fetchmany` on a sqlite cursor, a portal on
# asyncpg, a `DECLARE`d cursor on psycopg — and a per-chunk cost is exactly where
# those differ.
#
# **Chunked at `CHUNK`, on every arm.** The buffered rows above read 1000 rows in
# one round trip; these read them in ten. Comparing a ten-round-trip read against
# a one-round-trip read *between* arms would be measuring the chunk size, so the
# only comparison these rows support is with each other — which is why they are
# grouped in this section and say so.
#
# Payloads are byte-identical to the buffered rows in the same cell, so the
# equivalence gate covers streaming against `fetch_all` for free: a chunk boundary
# that lost or duplicated a row would fail the gate rather than the eye.
# ==========================================================================

#: Ten chunks over the 1000-row read. Small enough that per-chunk overhead is
#: visible, large enough not to become a round-trip benchmark.
CHUNK = 100


@contender(
    "rowform (fetch_iter)",
    backend="sqlite",
    shape="flat",
    tags=("streaming",),
    description=f"The streamed read: {CHUNK} rows per chunk, hydrated per chunk.",
)
async def flat_rowform_stream(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            users = [user async for user in conn.fetch_iter(stmt, chunk=CHUNK)]
        return dumps(_flat_objs(users))

    return target, sa_engine.dispose


@contender(
    "rowform compat (stream())",
    backend="sqlite",
    shape="flat",
    tags=("streaming",),
    description="The same chunks through SQLAlchemy's AsyncResult, taken as scalars.",
)
async def flat_rowform_stream_compat(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.stream(stmt, chunk=CHUNK)
            users = await result.scalars().all()
        return dumps(_flat_objs(users))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (stream())",
    backend="sqlite",
    shape="flat",
    tags=("streaming",),
    description=f"Core's own streamed read at yield_per={CHUNK}, taken in partitions.",
)
async def flat_sa_core_stream(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit).execution_options(yield_per=CHUNK)

    async def target() -> bytes:
        rows: list[Any] = []
        async with engine.begin() as conn:
            result = await conn.stream(stmt)
            async for partition in result.partitions(CHUNK):
                rows.extend(partition)
        return dumps(_flat(rows))

    return target, engine.dispose


@contender(
    "rowform (fetch_iter)",
    backend="postgres",
    shape="flat",
    tags=("streaming",),
    description=f"The streamed read on asyncpg: a portal, {CHUNK} rows per chunk.",
)
async def pg_flat_rowform_stream(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            users = [user async for user in conn.fetch_iter(stmt, chunk=CHUNK)]
        return dumps(_flat_objs(users))

    return target, sa_engine.dispose


@contender(
    "rowform compat (stream())",
    backend="postgres",
    shape="flat",
    tags=("streaming",),
    description="The same portal through SQLAlchemy's AsyncResult, taken as scalars.",
)
async def pg_flat_rowform_stream_compat(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.stream(stmt, chunk=CHUNK)
            users = await result.scalars().all()
        return dumps(_flat_objs(users))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (stream())",
    backend="postgres",
    shape="flat",
    tags=("streaming",),
    description=f"Core's own server-side cursor at yield_per={CHUNK}, in partitions.",
)
async def pg_flat_sa_core_stream(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = flat_stmt(init.limit).execution_options(yield_per=CHUNK)

    async def target() -> bytes:
        rows: list[Any] = []
        async with engine.begin() as conn:
            result = await conn.stream(stmt)
            async for partition in result.partitions(CHUNK):
                rows.extend(partition)
        return dumps(_flat(rows))

    return target, engine.dispose
# write shape (`users`, updated by primary key) — the `execute_many` cell
#
# The write path had no benchmark at all: `execute_many` is how an application
# applies a batch, and nothing here priced it against SQLAlchemy's own
# executemany or against the driver's. Every arm sends the *same* N parameter
# sets through the same compiled UPDATE, inside one transaction.
#
# Why an idempotent UPDATE rather than an INSERT, and what that leaves
# unmeasured (`copy_in`): `shapes/write.py`.
#
# The payload is the parameter-set count, so the equivalence gate here proves
# only that every arm attempted the same batch — bytes cannot show that rows
# changed. What shows it is `tests/test_bench_write_parity.py`, which runs each
# contender and reads the table back.
# ==========================================================================


def _batch(count: int) -> bytes:
    return dumps({"updated": count})


@contender(
    "rowform execute_many",
    backend="sqlite",
    shape="write",
    description="One compiled UPDATE, N parameter sets, inside one transaction.",
)
async def write_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = write_update()
    params = write_params(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            await conn.execute_many(stmt, params)
        return _batch(len(params))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (executemany)",
    backend="sqlite",
    shape="write",
    description="Identical UPDATE and parameter sets through Core's executemany.",
)
async def write_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = write_update()
    params = write_params(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            await conn.execute(stmt, params)
        return _batch(len(params))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM (bulk update)",
    backend="sqlite",
    shape="write",
    description="The ORM's own bulk UPDATE by primary key, one Session per batch.",
)
async def write_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = write_orm_update()
    params = write_orm_params(init.limit)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(stmt, params)
        return _batch(len(params))

    return target, engine.dispose


@contender(
    "floor: hand-rolled (executemany)",
    backend="sqlite",
    shape="write",
    shipped=False,
    tags=("floor",),
    description="aiosqlite's own executemany over the same compiled SQL — the floor.",
)
async def write_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool

    pool = await AiosqlitePool.open(init.handle, POOL_MAX)
    sql, binds = _compiled_many(write_update(), _SQLITE_DIALECT, write_params(init.limit))

    async def target() -> bytes:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN")
            await conn.executemany(sql, binds)
            await conn.commit()
        return _batch(len(binds))

    return target, pool.close


@contender(
    "rowform execute_many",
    backend="postgres",
    shape="write",
    description="One compiled UPDATE, N parameter sets, inside one transaction.",
)
async def pg_write_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = write_update()
    params = write_params(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            await conn.execute_many(stmt, params)
        return _batch(len(params))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core (executemany)",
    backend="postgres",
    shape="write",
    description="Identical UPDATE and parameter sets through Core's executemany.",
)
async def pg_write_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = write_update()
    params = write_params(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            await conn.execute(stmt, params)
        return _batch(len(params))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM (bulk update)",
    backend="postgres",
    shape="write",
    description="The ORM's own bulk UPDATE by primary key, one Session per batch.",
)
async def pg_write_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    stmt = write_orm_update()
    params = write_orm_params(init.limit)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(stmt, params)
        return _batch(len(params))

    return target, engine.dispose


@contender(
    "floor: hand-rolled (executemany)",
    backend="postgres",
    shape="write",
    shipped=False,
    tags=("floor",),
    description="asyncpg's own executemany over the same compiled SQL — the floor.",
)
async def pg_write_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
    import asyncpg

    pool = await asyncpg.create_pool(init.handle, min_size=POOL_MAX, max_size=POOL_MAX)
    assert pool is not None
    sql, binds = _compiled_many(write_update(), _PG_DIALECT, write_params(init.limit))

    async def target() -> bytes:
        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany(sql, binds)
        return _batch(len(binds))

    return target, pool.close


def _compiled_many(statement: Any, dialect: Any, params: list[dict[str, Any]]) -> tuple[str, list]:
    """`(sql, one bound row per parameter set)` — what a driver's `executemany`
    takes. The SQL is compiled once, as an engine would, and each set is bound
    through the same processors rather than by hand (`harness/seed.bound_rows`
    does this for the seeder, for the same reason)."""
    query = rf.CoreQuery(statement, dialect)
    return query.sql, [query.bind(row)[1] for row in params]
