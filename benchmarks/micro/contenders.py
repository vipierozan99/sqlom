"""Contenders for `bench micro`, one function each.

Every factory is `async def f(init: ContenderInit) -> (target, teardown)`:
`target()` runs one read and returns the JSON bytes the equivalence gate
compares, `teardown()` releases what the factory opened.

The rules every cell is built on:

* **Identical SQL.** One statement per shape (`flat_stmt`/`join_stmt`/`wide_stmt`),
  compiled by SQLAlchemy Core for every contender, so only the result layer varies.
* **Identical payload work.** Every non-floor contender builds its payload with the
  shared per-shape builder (`_flat_objs`/`_join_objs`/`_wide_objs`); the floors use
  the positional dict builders, which are cheaper — a floor must do strictly less.
* **Every read is inside `BEGIN`…`COMMIT`**, because that is what the application
  code being compared against looks like. The one exception is `rowform (one-shot)`,
  which prices `Engine.fetch_all()` on its own.
* **Floors send the transaction the contenders send on that backend.** On postgres
  that is a real `BEGIN`/`COMMIT` on the driver connection. On sqlite rowform sends a
  literal `BEGIN` (pysqlite's savepoints are broken without one — `SqliteDriver`),
  while stock SQLAlchemy sends nothing before a SELECT; the floors match rowform.

Two rowform spellings, never blended: `rowform` is the result-layer claim (statement
unprepared, same payload pass as the ORM rows); `rowform (idiomatic)` is the code an
application writes (prepared once, dataclasses straight to orjson).
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
from benchmarks.harness.registry import ContenderInit, Target, Teardown, contender
from benchmarks.shapes.flat import User, UserORM, users_table
from benchmarks.shapes.join import AUTHOR_FIELDS, POST_FIELDS, Author, AuthorORM, Post, PostORM
from benchmarks.shapes.wide import Event, EventORM, Severity, events_table

_SQLITE_DIALECT = aiosqlite.dialect()
_PG_DIALECT = asyncpg.dialect()

FLAT_FIELDS = [str(c.name) for c in users_table.columns]
WIDE_FIELDS = [str(c.name) for c in events_table.columns]

#: One pool configuration for every contender that opens one. `4+0` rather than
#: `1+3`: SQLAlchemy closes overflow connections on return, asyncpg's pool keeps
#: them, so only a zero-overflow pool is the same pool on both sides.
POOL = {"pool_size": 4, "max_overflow": 0}
POOL_MAX = POOL["pool_size"] + POOL["max_overflow"]


def _default(value: Any) -> Any:
    """`Decimal` has no orjson path; asyncpg's `UUID` subclass misses orjson's
    exact-type check. Registered for every contender so it stays a serializer
    quirk rather than a hydration difference."""
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def dumps(payload: Any) -> bytes:
    return orjson.dumps(payload, default=_default)


def _sa_dsn(path: str) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _sa_dsn_pg(dsn: str) -> str:
    """psycopg-style DSN -> the asyncpg SQLAlchemy URL (no query string: that
    dialect forwards it to `asyncpg.connect()`, which has no `sslmode`)."""
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1).split("?", 1)[0]


def _compiled(statement: Any, dialect: Any) -> tuple[str, Any]:
    """`(sql, params)` for a floor — built here so a floor never shares an
    engine's cache."""
    return rf.CoreQuery(statement, dialect).bind()


# --------------------------------------------------------------------------
# Statements. `model` is untyped on purpose: the same statement is built
# against the rowform model and the ORM one.
# --------------------------------------------------------------------------


def flat_stmt(limit: int, model: Any = User) -> Any:
    return select(model).where(model.is_active == True).where(model.id > 100).limit(limit)


def join_stmt(limit: int, left: Any = Author, right: Any = Post) -> Any:
    return (
        select(left, right)
        .join(right, right.author_id == left.id)
        .where(left.is_active == True)
        .where(right.score > 100)
        .limit(limit)
    )


def wide_stmt(limit: int, model: Any = Event) -> Any:
    return select(model).where(model.seen == True).where(model.id > 100).limit(limit)


# --------------------------------------------------------------------------
# Payload builders. The `_raw` variants apply the conversions sqlite's driver
# does not (0/1 -> bool, strings -> temporal types, ...), written out by hand;
# the plain ones take rows SQLAlchemy's processors already handled. Every
# builder must produce byte-identical JSON — the equivalence gate checks.
# --------------------------------------------------------------------------


def _flat_raw(rows):
    return [{"id": a, "name": b, "email": c, "is_active": bool(d)} for a, b, c, d in rows]


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
    """`Decimal` from a `%.3f` string rather than the float, because that is what
    `Numeric(12, 3)`'s processor does and anything else disagrees in the last digits."""
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


# The equal-work payload pass every non-floor contender pays.


def _flat_objs(objs):
    return [{f: getattr(u, f) for f in FLAT_FIELDS} for u in objs]


def _join_objs(pairs):
    return [
        [{f: getattr(a, f) for f in AUTHOR_FIELDS}, {f: getattr(p, f) for f in POST_FIELDS}]
        for a, p in pairs
    ]


def _wide_objs(objs):
    return [{f: getattr(e, f) for f in WIDE_FIELDS} for e in objs]


# ==========================================================================
# sqlite
# ==========================================================================


@contender(
    "floor: raw driver (dict)",
    backend="sqlite",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The floor: driver rows straight to dicts, no SQLAlchemy.",
)
async def flat_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    # Imported here: `python -m benchmarks load` gevent-patches threading, and
    # aiosqlite bound to a greenlet deadlocks against the loop.
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
    "rowform",
    backend="sqlite",
    shape="flat",
    description="The result-layer claim: unprepared statement, same payload pass as the ORM rows.",
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
    "rowform (idiomatic)",
    backend="sqlite",
    shape="flat",
    tags=("idiomatic",),
    description="The endpoint claim: prepared once, dataclasses straight to orjson.",
)
async def flat_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform (one-shot)",
    backend="sqlite",
    shape="flat",
    description="`Engine.fetch_all()` off the engine: a direct pool checkout, no scope.",
)
async def flat_rowform_oneshot(init: ContenderInit) -> tuple[Target, Teardown]:
    """The API's single-read form. It skips the transaction every other row
    pays for, so it is priced on its own rather than compared as an equal."""
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_flat_objs(await engine.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "rowform (mock)",
    backend="mock",
    shape="flat",
    description="rowform's row layer alone, canned driver rows — the perf gate's tripwire.",
)
async def flat_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """Unprepared, so the per-request cache-key lookup is inside the timed region."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle, FLAT_FIELDS)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_flat_objs(await engine.fetch_all(stmt)))

    return target, engine.sa_engine.dispose


@contender(
    "SQLAlchemy Core",
    backend="sqlite",
    shape="flat",
    description="Identical SQL, stock Row/CursorResult result layer.",
)
async def flat_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return dumps(_flat(result.all()))

    return target, engine.dispose


@contender(
    "SQLAlchemy ORM",
    backend="sqlite",
    shape="flat",
    description="SQLAlchemy ORM, one Session per request.",
)
async def flat_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    """A fresh Session per request: a hoisted one would serve the identity map
    instead of hydrating."""
    engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    stmt = flat_stmt(init.limit, UserORM)

    async def target() -> bytes:
        async with AsyncSession(engine) as session, session.begin():
            users = (await session.execute(stmt)).scalars().all()
            return dumps(_flat_objs(users))

    return target, engine.dispose


@contender(
    "floor: raw driver (dict)",
    backend="sqlite",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The floor at arity two: driver rows split into two dicts per row.",
)
async def join_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.harness.aiosqlite_pool import AiosqlitePool  # see the flat floor

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
    "rowform (idiomatic)",
    backend="sqlite",
    shape="join",
    tags=("idiomatic",),
    description="The endpoint claim at arity two: prepared once, object pairs straight to orjson.",
)
async def join_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform (mock)",
    backend="mock",
    shape="join",
    description="rowform's join row layer alone, canned driver rows.",
)
async def join_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle, AUTHOR_FIELDS + POST_FIELDS)
    stmt = join_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_join_objs(await engine.fetch_all(stmt)))

    return target, engine.sa_engine.dispose


@contender(
    "SQLAlchemy Core",
    backend="sqlite",
    shape="join",
    description="Identical SQL, stock Row/CursorResult result layer.",
)
async def join_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
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
    "floor: raw driver (dict)",
    backend="sqlite",
    shape="wide",
    shipped=False,
    tags=("floor",),
    description="The floor where correctness costs: hand-written per-column conversion into dicts.",
)
async def wide_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    """8 of 9 columns need a conversion on sqlite; skipping one is a different
    answer, not a faster floor."""
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
    sa_engine = create_async_engine(_sa_dsn(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core",
    backend="sqlite",
    shape="wide",
    description="Identical SQL and identical processors, run through Row/CursorResult.",
)
async def wide_sa_core(init: ContenderInit) -> tuple[Target, Teardown]:
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


# ==========================================================================
# postgres (asyncpg)
# ==========================================================================


@contender(
    "floor: raw driver (dict)",
    backend="postgres",
    shape="flat",
    shipped=False,
    tags=("floor",),
    description="The floor: asyncpg Records straight to dicts, no SQLAlchemy.",
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
    "rowform",
    backend="postgres",
    shape="flat",
    description="The result-layer claim on asyncpg: unprepared, same payload pass.",
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
    "rowform (idiomatic)",
    backend="postgres",
    shape="flat",
    tags=("idiomatic",),
    description="The endpoint claim on asyncpg: prepared once, dataclasses straight to orjson.",
)
async def pg_flat_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(flat_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "rowform (one-shot)",
    backend="postgres",
    shape="flat",
    description="`Engine.fetch_all()` off the engine: a direct pool checkout, no BEGIN/COMMIT.",
)
async def pg_flat_rowform_oneshot(init: ContenderInit) -> tuple[Target, Teardown]:
    """See the sqlite twin. Worth more here: the transaction it skips is two
    real round trips rather than two worker-thread hops."""
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    stmt = flat_stmt(init.limit)

    async def target() -> bytes:
        return dumps(_flat_objs(await engine.fetch_all(stmt)))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core",
    backend="postgres",
    shape="flat",
    description="Identical SQL, stock Row/CursorResult result layer, on asyncpg.",
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
    "SQLAlchemy ORM",
    backend="postgres",
    shape="flat",
    description="SQLAlchemy ORM on asyncpg, one Session per request.",
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
    "floor: raw driver (dict)",
    backend="postgres",
    shape="join",
    shipped=False,
    tags=("floor",),
    description="The floor at arity two: asyncpg Records split into two dicts per row.",
)
async def pg_join_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
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
    "rowform",
    backend="postgres",
    shape="join",
    description="The result-layer claim at arity two on asyncpg.",
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
    "rowform (idiomatic)",
    backend="postgres",
    shape="join",
    tags=("idiomatic",),
    description="The endpoint claim at arity two on asyncpg.",
)
async def pg_join_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(join_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core",
    backend="postgres",
    shape="join",
    description="Identical SQL, stock Row/CursorResult result layer, on asyncpg.",
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
    description="SQLAlchemy ORM on asyncpg, two entities per row.",
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
    "floor: raw driver (dict)",
    backend="postgres",
    shape="wide",
    shipped=False,
    tags=("floor",),
    description="The floor over the widened shape: asyncpg decodes every column natively.",
)
async def pg_wide_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
    """No hand conversions here: asyncpg returns the right Python object for
    every column, except `Enum`, which SQLAlchemy maps by name."""
    import asyncpg

    pool = await asyncpg.create_pool(init.handle, min_size=POOL_MAX, max_size=POOL_MAX)
    assert pool is not None
    sql, params = _compiled(wide_stmt(init.limit), _PG_DIALECT)

    def build(rows):
        return [
            {
                "id": a,
                "label": b,
                "seen": c,
                "at": d,
                "day": e,
                "amount": f,
                "severity": Severity[g],
                "trace": h,
                "note": i,
            }
            for a, b, c, d, e, f, g, h, i in rows
        ]

    async def target() -> bytes:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(sql, *params)
        return dumps(build(rows))

    return target, pool.close


@contender(
    "rowform",
    backend="postgres",
    shape="wide",
    description="The result-layer claim over the widened shape on asyncpg.",
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
    description="The endpoint claim over the widened shape on asyncpg.",
)
async def pg_wide_rowform_idiomatic(init: ContenderInit) -> tuple[Target, Teardown]:
    sa_engine = create_async_engine(_sa_dsn_pg(init.handle), **POOL)
    engine = rf.Engine(sa_engine)
    query = engine.prepare(wide_stmt(init.limit))

    async def target() -> bytes:
        async with engine.begin() as conn:
            return dumps(await conn.fetch_all(query))

    return target, sa_engine.dispose


@contender(
    "SQLAlchemy Core",
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
