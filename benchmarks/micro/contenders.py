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

**Every contender builds its payload the same way**, with a
`{field: getattr(obj, field)}` comprehension over the shape's field list. That is
not a style rule: the `MappedAsDataclass` rows used `dataclasses.asdict()`, which
deep-copies recursively, and on `wide` that cost more than the ORM work the row
was there to measure — 14 ms of a 17 ms cell, for byte-identical JSON. The row
registered to *avoid* overstating the win was carrying the largest handicap in
the file. If a contender needs a different payload builder, the difference
belongs in the timed region of every contender or none.

**Every contender runs its read inside `BEGIN`...`COMMIT`**, because that is what
the code this library is measured against looks like — `async with session.begin():
session.execute(...)`. It is also the only way the comparison is honest: SQLAlchemy
autobegins on first statement and rolls back on release, so a `Core`/ORM contender
was always paying for a transaction, while `Engine.fetch_all()` off the engine opens
none. Left alone, part of rowform's margin was a weaker isolation guarantee billed
as row-layer speed. Measured cost of closing that gap: 0.711x -> 0.782x against Core
on sqlite, and 1.015x -> 1.134x against the raw asyncpg floor on postgres.

**On sqlite, the floors spell it with the DBAPI's `commit()` and never literal
`BEGIN`/`COMMIT` SQL.** They are not interchangeable *there*. pysqlite only implicitly
begins before DML, so SQLAlchemy's `begin()` around a SELECT emits no `BEGIN` and the
connection never enters a transaction — a floor issuing the SQL by hand *does* open one
(measured 0.4445 ms against 0.4182 ms) and would do strictly more work than the
contenders it bounds, which is the same "floor slower than the thing it bounds" bug
recorded below, arrived at from the other direction.

**On postgres the opposite holds**, and `pg_flat_raw_asyncpg` uses
`conn.transaction()` — a real `BEGIN`/`COMMIT` on the wire — for exactly the same
reason: there SQLAlchemy's `begin()` *does* emit one, so a floor that skipped it would
be the cheap side of the asymmetry instead of the expensive one. The rule is "match
whatever the contenders' transaction actually costs on this backend", not "avoid
`BEGIN`"; the sqlite spelling is a consequence, not the principle.

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
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import asyncpg
from sqlalchemy.dialects.sqlite import aiosqlite
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

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


# ==========================================================================
# flat shape (`users`) — sqlite
# ==========================================================================


@contender(
    "rowform",
    backend="sqlite",
    shape="flat",
    description="Core compiles, rowform hydrates: compiled hydrator into plain dataclasses.",
)
async def flat_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
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
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

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
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps((await conn.execute(query)).scalars().all())

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
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps([row[0] for row in (await conn.execute(query)).all()])

    return target, sa_engine.dispose


@contender(
    "rowform (mock)",
    backend="mock",
    shape="flat",
    tags=("mapper-floor",),
    description="rowform's row-layer cost alone, via MockEngine — zero driver cost.",
)
async def flat_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle, FLAT_FIELDS)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

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
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
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
            return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

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
            return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

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
            return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

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
    description="Two entities per row through one compiled hydrator, no per-entity call.",
)
async def join_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
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
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps([(a, p) for a, p in (await conn.execute(query)).all()])

    return target, sa_engine.dispose


@contender(
    "rowform (mock)",
    backend="mock",
    shape="join",
    tags=("mapper-floor",),
    description="rowform's join row-layer cost alone, via MockEngine — zero driver cost.",
)
async def join_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle, AUTHOR_FIELDS + POST_FIELDS)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

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
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
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
            return dumps(
                [
                    [
                        {f: getattr(a, f) for f in AUTHOR_FIELDS},
                        {f: getattr(p, f) for f in POST_FIELDS},
                    ]
                    for a, p in rows
                ]
            )

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
            return dumps(
                [
                    [
                        {f: getattr(a, f) for f in AUTHOR_FIELDS},
                        {f: getattr(p, f) for f in POST_FIELDS},
                    ]
                    for a, p in rows
                ]
            )

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
            return dumps(
                [
                    [
                        {f: getattr(a, f) for f in AUTHOR_FIELDS},
                        {f: getattr(p, f) for f in POST_FIELDS},
                    ]
                    for a, p in rows
                ]
            )

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
    description="Per-column processors from SQLAlchemy, inlined into generated code.",
)
async def wide_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
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
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps((await conn.execute(query)).scalars().all())

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
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
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
            return dumps([{f: getattr(e, f) for f in WIDE_FIELDS} for e in rows])

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
            return dumps([{f: getattr(e, f) for f in WIDE_FIELDS} for e in rows])

    return target, engine.dispose


# ==========================================================================
# naked-sqla (https://github.com/ManiMozaffar/naked-sqla) — sqlite
#
# The other "ORM without the ORM" library, and the closest thing to a peer this
# suite has. It reuses SQLAlchemy's *ORM* declarations and compile state and
# replaces only `orm.loading.instances`, dropping the identity map, dirty
# tracking and post-load hooks; rowform replaces the declaration too and
# generates a hydrator per statement shape. Registering it here is what turns
# "which approach costs less" into a measurement.
#
# It reads through its own `AsyncSessionFactory`, which is `engine.begin()` plus
# a commit, so it satisfies this file's every-contender-in-a-transaction rule
# without special casing. The models are `UserORM`/`AuthorORM`/`EventORM` — its
# whole input is a stock declarative class, so it shares the ORM contenders'
# declarations rather than adding a fifth.
#
# Skipped, not failed, when the package is absent: it is in the `bench`
# dependency group, and a `uv sync` without that group must still be able to run
# `bench micro`.
# ==========================================================================

#: Declared `Any` rather than left to inference: the factories below are defined
#: only when this is not None, but that narrowing does not reach inside them, so
#: the inferred `type[...] | None` would read as an optional call at every use.
_NakedFactory: Any
try:
    from naked_sqla.om.asession import AsyncSessionFactory as _NakedFactory
except ImportError:  # pragma: no cover -- optional contender
    _NakedFactory = None


if _NakedFactory is not None:

    @contender(
        "naked-sqla",
        backend="sqlite",
        shape="flat",
        description="naked-sqla: SQLAlchemy's ORM row processors, no identity map or session state.",
    )
    async def flat_naked_sqla(init: ContenderInit) -> tuple[Target, Teardown]:
        engine = create_async_engine(_sa_dsn(init.handle), **POOL)
        db = _NakedFactory(engine)
        stmt = flat_stmt(init.limit, UserORM)

        async def target() -> bytes:
            async with db.begin() as session:
                users = (await session.scalars(stmt)).all()
                return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

        return target, engine.dispose

    @contender(
        "naked-sqla (DC)",
        backend="sqlite",
        shape="flat",
        description="naked-sqla over a MappedAsDataclass model — the same shape rowform returns.",
    )
    async def flat_naked_sqla_dc(init: ContenderInit) -> tuple[Target, Teardown]:
        engine = create_async_engine(_sa_dsn(init.handle), **POOL)
        db = _NakedFactory(engine)
        stmt = flat_stmt(init.limit, UserDC)

        async def target() -> bytes:
            async with db.begin() as session:
                users = (await session.scalars(stmt)).all()
                return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

        return target, engine.dispose

    @contender(
        "naked-sqla",
        backend="sqlite",
        shape="join",
        description="naked-sqla, two entities per row.",
    )
    async def join_naked_sqla(init: ContenderInit) -> tuple[Target, Teardown]:
        engine = create_async_engine(_sa_dsn(init.handle), **POOL)
        db = _NakedFactory(engine)
        stmt = join_stmt(init.limit, AuthorORM, PostORM)

        async def target() -> bytes:
            async with db.begin() as session:
                rows = (await session.execute(stmt)).all()
                return dumps(
                    [
                        [
                            {f: getattr(a, f) for f in AUTHOR_FIELDS},
                            {f: getattr(p, f) for f in POST_FIELDS},
                        ]
                        for a, p in rows
                    ]
                )

        return target, engine.dispose

    @contender(
        "naked-sqla (mock)",
        backend="mock",
        shape="flat",
        tags=("mapper-floor",),
        description="naked-sqla's hydration cost alone, via mock_sqlalchemy_engine.",
    )
    async def flat_naked_sqla_mock(init: ContenderInit) -> tuple[Target, Teardown]:
        """The cell that answers "which mapper is cheaper", with the driver gone.

        Same seam and same hoisted checkout as `SQLAlchemy ORM (mock)`, so those
        two are directly comparable — both go through a real `CursorResult` and
        differ only in what turns it into objects. Reading it against
        `rowform (mock)` carries the caveat in `engines/mock.py`: rowform's mock
        cans the driver one layer higher, so the two floors bound their own
        libraries rather than pricing one against the other.
        """
        from naked_sqla.om.asession import AsyncSession as NakedSession

        from benchmarks.engines.mock import mock_sqlalchemy_engine

        engine = mock_sqlalchemy_engine(FLAT_FIELDS, init.handle)
        stmt = flat_stmt(init.limit, UserORM)
        conn = await engine.connect()

        async def target() -> bytes:
            users = (await NakedSession(conn).scalars(stmt)).all()
            return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

        async def teardown() -> None:
            await conn.close()
            await engine.dispose()

        return target, teardown

    @contender(
        "naked-sqla (mock)",
        backend="mock",
        shape="join",
        tags=("mapper-floor",),
        description="naked-sqla's join hydration cost alone — zero driver cost.",
    )
    async def join_naked_sqla_mock(init: ContenderInit) -> tuple[Target, Teardown]:
        from naked_sqla.om.asession import AsyncSession as NakedSession

        from benchmarks.engines.mock import mock_sqlalchemy_engine

        engine = mock_sqlalchemy_engine(AUTHOR_FIELDS + POST_FIELDS, init.handle)
        stmt = join_stmt(init.limit, AuthorORM, PostORM)
        conn = await engine.connect()

        async def target() -> bytes:
            rows = (await NakedSession(conn).execute(stmt)).all()
            return dumps(
                [
                    [
                        {f: getattr(a, f) for f in AUTHOR_FIELDS},
                        {f: getattr(p, f) for f in POST_FIELDS},
                    ]
                    for a, p in rows
                ]
            )

        async def teardown() -> None:
            await conn.close()
            await engine.dispose()

        return target, teardown

    @contender(
        "naked-sqla",
        backend="sqlite",
        shape="wide",
        description="naked-sqla over the widened shape — the same per-column processors.",
    )
    async def wide_naked_sqla(init: ContenderInit) -> tuple[Target, Teardown]:
        engine = create_async_engine(_sa_dsn(init.handle), **POOL)
        db = _NakedFactory(engine)
        stmt = wide_stmt(init.limit, EventORM)

        async def target() -> bytes:
            async with db.begin() as session:
                rows = (await session.scalars(stmt)).all()
                return dumps([{f: getattr(e, f) for f in WIDE_FIELDS} for e in rows])

        return target, engine.dispose


# ==========================================================================
# postgres
# ==========================================================================


@contender(
    "rowform",
    backend="postgres",
    shape="flat",
    description="Core compiles, rowform's asyncpg pool executes, compiled hydrator shapes.",
)
async def pg_flat_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
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
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

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
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps((await conn.execute(query)).scalars().all())

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
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps([row[0] for row in (await conn.execute(query)).all()])

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
    "floor: on SQLAlchemy (dict)",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor", "same-plumbing"),
    description="Same pool, same transaction, hand-written dicts — the abstraction floor.",
)
async def pg_flat_sa_plumbing_dict(init: ContenderInit) -> tuple[Target, Teardown]:
    """Worth more here than on sqlite. The transaction the plumbing opens is two
    real round trips on postgres where on sqlite it is Python overhead only, so
    this is the arm that keeps that cost out of the row-layer number."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    sql, params = _compiled(flat_stmt(init.limit), _PG_DIALECT)

    async def target() -> bytes:
        async with sa_engine.connect() as sa_conn:
            driver_conn: Any = (await sa_conn.get_raw_connection()).driver_connection
            async with sa_conn.begin():
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
            return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

    return target, engine.dispose


@contender(
    "rowform",
    backend="postgres",
    shape="join",
    description="Two entities per row through one compiled hydrator, on asyncpg.",
)
async def pg_join_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
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
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps([(a, p) for a, p in (await conn.execute(query)).all()])

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
            return dumps(
                [
                    [
                        {f: getattr(a, f) for f in AUTHOR_FIELDS},
                        {f: getattr(p, f) for f in POST_FIELDS},
                    ]
                    for a, p in rows
                ]
            )

    return target, engine.dispose


@contender(
    "rowform",
    backend="postgres",
    shape="wide",
    description="The widened shape where asyncpg decodes natively and most processors are None.",
)
async def pg_wide_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
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
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps((await conn.execute(query)).scalars().all())

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
            return dumps([{f: getattr(e, f) for f in WIDE_FIELDS} for e in rows])

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
