"""Contenders for the flat (`users`) shape, sqlite tier (PLAN.md §7 tier 3).

Defined once and registered via `@contender` — the old suite defined this same
`Query(User).where(...)` roughly ten times across `bench_sqlite.py`,
`bench_sqlite_async.py`, `bench_final.py`, `estimate_ceilings.py`, etc.

Every factory takes `(path, limit)` and returns `(request, teardown)`:
`request()` runs one unit of work and returns response-ready JSON bytes (for
the equivalence gate — PLAN.md §4); `teardown()` releases whatever the
factory opened.
"""

from __future__ import annotations

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmarks.harness.registry import contender
from benchmarks.shapes.flat import User, UserORM, users_table
from rowform import Query, SqliteEngine, compile_json_default


def _rowform_query(limit: int):
    return Query(User).where(User.is_active == True).where(User.id > 100).limit(limit)


def _sa_dsn(path: str) -> str:
    return f"sqlite+aiosqlite:///{path}"


@contender(
    "rowform", backend="sqlite", shape="flat",
    description="rowform's shipped sqlite path: compiled hydrator + compiled orjson hook.",
)
async def make_rowform(path: str, limit: int):
    """rowform: compiled hydrator + compiled orjson hook, both built once —
    what a real service pays once per process, not once per request."""
    engine = SqliteEngine(path, min_size=1, max_size=4)
    await engine.connect()
    query = _rowform_query(limit)
    to_dict = compile_json_default(User)

    async def request():
        rows = await engine.fetch_all(query)
        return orjson.dumps(rows, default=to_dict)

    return request, engine.close


@contender(
    "rowform (MockEngine)", backend="mock", shape="flat", tags=("mapper-floor",),
    description="rowform's mapper cost alone, via MockEngine — zero driver cost.",
)
async def make_rowform_mock(rows: list[tuple], limit: int):
    """Tier 2 (PLAN.md §7): the mapper's floor, zero driver cost — see
    `benchmarks.engines.mock.MockEngine`. `rows` are precomputed by the
    caller (typically `harness.seed.flat_rows(limit)`); `limit` only sizes
    the SQL rowform generates, since the fake connection ignores it."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(rows)
    query = _rowform_query(limit)
    to_dict = compile_json_default(User)

    async def request():
        result = await engine.fetch_all(query)
        return orjson.dumps(result, default=to_dict)

    async def teardown():
        return None

    return request, teardown


async def rowform_stages(path: str, limit: int):
    """Stage decomposition for the rowform sqlite contender (PLAN.md §4:
    "stage decomposition prints sum-of-parts next to the measured whole and
    reports the residual"). Not a registered contender — `bench micro
    decompose` uses this directly to break `request()` into "fetch" (the
    driver round trip + hydration) and "serialize" (orjson), each timeable on
    its own with `harness.timing.best_of()`."""
    engine = SqliteEngine(path, min_size=1, max_size=4)
    await engine.connect()
    query = _rowform_query(limit)
    to_dict = compile_json_default(User)

    async def fetch():
        return await engine.fetch_all(query)

    cached_rows = await fetch()

    async def serialize():
        return orjson.dumps(cached_rows, default=to_dict)

    async def whole():
        rows = await engine.fetch_all(query)
        return orjson.dumps(rows, default=to_dict)

    return {"fetch": fetch, "serialize": serialize, "whole": whole}, engine.close


@contender(
    "raw aiosqlite + dict", backend="sqlite", shape="flat", shipped=False, tags=("floor",),
    description="Naive no-mapping baseline: dict(zip(names, row)) per row.",
)
async def make_raw_aiosqlite(path: str, limit: int):
    """Naive no-mapping baseline: `dict(zip(names, row))` per row — what you'd
    write by hand without a mapper."""
    import aiosqlite

    conn = await aiosqlite.connect(path)
    sql = (
        "SELECT id, name, email, is_active FROM users "
        "WHERE is_active = 1 AND id > 100 LIMIT ?"
    )
    names = ("id", "name", "email", "is_active")

    async def request():
        cur = await conn.execute(sql, (limit,))
        rows = await cur.fetchall()
        payload = [dict(zip(names, (r[0], r[1], r[2], bool(r[3])), strict=True)) for r in rows]
        return orjson.dumps(payload)

    return request, conn.close


@contender(
    "SQLAlchemy async Core (.mappings())", backend="sqlite", shape="flat",
    description="SQLAlchemy Core via .mappings() — orjson needs a per-key str() cast for it.",
)
async def make_sa_core_mappings(path: str, limit: int):
    """`.mappings()` yields `RowMapping`s keyed by `quoted_name` (a `str`
    subclass orjson refuses), so every row pays a `str()` cast per key — kept
    registered alongside the positional variant below rather than "corrected
    away" (PLAN.md §4: "price any workaround one contender needs")."""
    engine = create_async_engine(_sa_dsn(path))
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return request, engine.dispose


@contender(
    "SQLAlchemy async Core (positional)", backend="sqlite", shape="flat",
    description="SQLAlchemy Core, rows shaped positionally instead of via .mappings().",
)
async def make_sa_core_positional(path: str, limit: int):
    """Zipping the flat row against names captured once — equally idiomatic
    Core, without `.mappings()`'s per-key cast."""
    engine = create_async_engine(_sa_dsn(path))
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )
    names = [str(c.name) for c in users_table.columns]

    async def request():
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [dict(zip(names, row, strict=True)) for row in result]
        return orjson.dumps(payload)

    return request, engine.dispose


@contender(
    "SQLAlchemy async ORM", backend="sqlite", shape="flat",
    description="SQLAlchemy ORM, one Session per request.",
)
async def make_sa_orm(path: str, limit: int):
    """Fresh `Session` per request, bound to a per-request connection: hoisting
    the `Session` would let its identity map skip hydration on every request
    after the first (PLAN.md §4: "audit what is inside each timed region")."""
    engine = create_async_engine(_sa_dsn(path))
    stmt = (
        select(UserORM)
        .where(UserORM.is_active == True)
        .where(UserORM.id > 100)
        .limit(limit)
    )
    names = [str(c.name) for c in UserORM.__table__.columns]

    async def request():
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            payload = [{name: getattr(u, name) for name in names} for u in users]
        return orjson.dumps(payload)

    return request, engine.dispose
