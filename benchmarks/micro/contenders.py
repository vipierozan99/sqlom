"""Contenders for `bench micro` (PLAN.md §7 tiers 2-3), one function each.

Every function has the same shape: `async def f(init: ContenderInit) ->
tuple[Target, Teardown]`. `target()` runs one unit of work and returns
response-ready JSON bytes (for the equivalence gate — PLAN.md §4); `teardown()`
releases whatever the factory opened. `@contender(...)` is typed against exactly
that shape, so a factory that takes the wrong argument or returns the wrong thing
is a type error at the decorator rather than a runtime surprise.

**What is being compared changed with the rewrite.** "rowform vs SQLAlchemy Core"
is incoherent now that Core *is* the SQL generator on both sides — the same
`select()` compiles to the same string for every contender here. What remains,
and what this file is organised around (docs/PLAN_CORE_COMPILER.md §8 P4c):

* **vs the stock Core result layer** — `Row`/`CursorResult` against a compiled
  hydrator over the same driver. This is the headline claim now, not a footnote.
* **vs the SQLAlchemy ORM** — the instrumentation gap, still a real comparison.
  Registered twice, because stock declarative returns instrumented objects
  carrying loader state and `MappedAsDataclass` does not; comparing against only
  the first would overstate the win.
* **against two floors, permanently** (§2f). A driver-to-dicts floor bounds the
  whole stack, and a driver-plus-*the same hydrator* floor separates the engine's
  cost from the hydrator's. Keeping only the first is how an earlier run produced
  a "floor" slower than the thing it was bounding.

`bench micro` calls these factories directly — this is its whole registry. The
FastAPI load-test worker (`service/app.py`) is deliberately *not* a consumer: it
is hand-written so it profiles as real named functions instead of frames through
this file's closures.
"""

from __future__ import annotations

import decimal
import uuid
from dataclasses import asdict
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import rowform as rf
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
from benchmarks.shapes.wide import Event, EventDC, EventORM, events_table

FLAT_FIELDS = [str(c.name) for c in users_table.columns]
WIDE_FIELDS = [str(c.name) for c in events_table.columns]


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
# floor" *slower than rowform*, tripping PLAN.md §4's own tripwire ("when
# rowform appeared to beat the floor, that was the tripwire"). A floor must do
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
    engine = rf.SqliteEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

    return target, engine.close


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

    return target, engine.close


@contender(
    "raw aiosqlite + dict",
    backend="sqlite",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The true floor: driver rows straight to dicts, no object construction.",
)
async def flat_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    import aiosqlite

    conn = await aiosqlite.connect(init.handle)
    sql, params = _compiled(flat_stmt(init.limit), rf.SqliteEngine.dialect)

    async def target() -> bytes:
        cur = await conn.execute(sql, params)
        return dumps(_flat_raw(await cur.fetchall()))

    return target, conn.close


@contender(
    "raw aiosqlite + rowform hydrator",
    backend="sqlite",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The second floor (§2f): same driver, same hydrator, no engine — isolates engine cost.",
)
async def flat_raw_aiosqlite_hydrated(init: ContenderInit) -> tuple[Target, Teardown]:
    """Why two floors, always.

    A floor whose hydrator is *slower* than the contender's is not a floor. An
    earlier run used `User(**kwargs)` as the floor's row constructor while
    rowform used its generated one, and the floor came out slower than the thing
    it was bounding. Measuring both a dicts-only floor and a floor running the
    *same* hydrator separates "what does the engine cost" from "what does row
    construction cost", which is the only way either number means anything
    (docs/PLAN_CORE_COMPILER.md §2f, METHODOLOGY correction 10).
    """
    import aiosqlite

    conn = await aiosqlite.connect(init.handle)
    dialect = rf.SqliteEngine.dialect
    statement = flat_stmt(init.limit)
    sql, params = _compiled(statement, dialect)
    hydrate = _hydrator(statement, dialect, FLAT_FIELDS)

    async def target() -> bytes:
        cur = await conn.execute(sql, params)
        return dumps(hydrate(await cur.fetchall()))

    return target, conn.close


@contender(
    "SQLAlchemy Core (positional)",
    backend="sqlite",
    shape="flat",
    description="The headline comparison: identical SQL, stock Row/CursorResult result layer.",
)
async def flat_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    alongside the positional variant rather than "corrected away" (PLAN.md §4:
    "price any workaround one contender needs")."""
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    after the first (PLAN.md §4: "audit what is inside each timed region")."""
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = flat_stmt(init.limit, UserORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = flat_stmt(init.limit, UserDC)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            return dumps([asdict(u) for u in users])

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

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            return dumps(_flat(result.all()))

    return target, engine.dispose


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

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            return dumps([{f: getattr(u, f) for f in FLAT_FIELDS} for u in users])

    return target, engine.dispose


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
    engine = rf.SqliteEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

    return target, engine.close


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

    return target, engine.close


@contender(
    "raw aiosqlite + dict",
    backend="sqlite",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The true floor: driver rows split into two dicts per row.",
)
async def join_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    import aiosqlite

    conn = await aiosqlite.connect(init.handle)
    sql, params = _compiled(join_stmt(init.limit), rf.SqliteEngine.dialect)

    async def target() -> bytes:
        cur = await conn.execute(sql, params)
        return dumps(_join_raw(await cur.fetchall()))

    return target, conn.close


@contender(
    "raw aiosqlite + rowform hydrator",
    backend="sqlite",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The second floor (§2f): same driver, same hydrator, no engine.",
)
async def join_raw_aiosqlite_hydrated(init: ContenderInit) -> tuple[Target, Teardown]:
    import aiosqlite

    conn = await aiosqlite.connect(init.handle)
    dialect = rf.SqliteEngine.dialect
    statement = join_stmt(init.limit)
    sql, params = _compiled(statement, dialect)
    hydrate = _hydrator(statement, dialect, AUTHOR_FIELDS + POST_FIELDS)

    async def target() -> bytes:
        cur = await conn.execute(sql, params)
        return dumps(hydrate(await cur.fetchall()))

    return target, conn.close


@contender(
    "SQLAlchemy Core (positional)",
    backend="sqlite",
    shape="join",
    description="Identical SQL, stock Row/CursorResult result layer.",
)
async def join_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = join_stmt(init.limit, AuthorORM, PostORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = join_stmt(init.limit, AuthorDC, PostDC)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            rows = (await session.execute(stmt)).all()
            return dumps([[asdict(a), asdict(p)] for a, p in rows])

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

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
    engine = rf.SqliteEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

    return target, engine.close


@contender(
    "SQLAlchemy Core (positional)",
    backend="sqlite",
    shape="wide",
    description="Identical SQL and identical processors, run through Row/CursorResult.",
)
async def wide_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = wide_stmt(init.limit, EventORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = wide_stmt(init.limit, EventDC)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            rows = (await session.execute(stmt)).scalars().all()
            return dumps([asdict(e) for e in rows])

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
    engine = rf.AsyncpgEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

    return target, engine.close


@contender(
    "raw asyncpg + dict",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The true floor: asyncpg Records straight to dicts.",
)
async def pg_flat_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
    import asyncpg

    pool = await asyncpg.create_pool(init.handle, min_size=1, max_size=4)
    assert pool is not None
    sql, params = _compiled(flat_stmt(init.limit), rf.AsyncpgEngine.dialect)

    async def target() -> bytes:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return dumps(_flat(rows))

    return target, pool.close


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres",
    shape="flat",
    description="Identical SQL, stock Row/CursorResult result layer, SQLAlchemy's own pool.",
)
async def pg_flat_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = flat_stmt(init.limit, UserORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
    engine = rf.AsyncpgEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

    return target, engine.close


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres",
    shape="join",
    description="Identical SQL, stock Row/CursorResult result layer.",
)
async def pg_join_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = join_stmt(init.limit, AuthorORM, PostORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
    engine = rf.AsyncpgEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        return dumps(await engine.fetch_all(query))

    return target, engine.close


@contender(
    "SQLAlchemy Core (positional)",
    backend="postgres",
    shape="wide",
    description="Identical SQL and processors, run through Row/CursorResult.",
)
async def pg_wide_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = wide_stmt(init.limit)

    async def target() -> bytes:
        async with engine.connect() as conn:
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
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = wide_stmt(init.limit, EventORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
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
