"""Contenders for `bench micro` (PLAN.md §7 tiers 2-3), one file, one function
per contender — the old suite defined `Query(User).where(...)` roughly ten
times across `bench_sqlite.py`, `bench_sqlite_async.py`, `bench_final.py`,
`estimate_ceilings.py`, etc.; a later revision of this suite still had it
split across `contenders/flat.py` and `contenders/join.py`.

Every function has the same shape: `async def f(init: ContenderInit) ->
tuple[Target, Teardown]`. `target()` runs one unit of work and returns
response-ready JSON bytes (for the equivalence gate — PLAN.md §4);
`teardown()` releases whatever the factory opened. `@contender(...)` is typed
against exactly this shape (`ContenderFactory`), so a factory that takes the
wrong argument or returns the wrong thing is a type error at the decorator,
not a runtime surprise.

`bench micro` calls these factories directly — this is its whole registry.
The FastAPI load-test worker (`service/app.py`) is deliberately *not* a
consumer: it's hand-written, on purpose, so it profiles as real named
functions instead of frames through this file's `@contender` closures (see
its module docstring). `benchmarks/loadtests/` is the load-test registry's
own, independent source of truth (`benchmarks/load/registry.py`) and doesn't
import this module either.
"""

from __future__ import annotations

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import rowform as rf
from benchmarks.harness.registry import ContenderInit, Target, Teardown, contender
from benchmarks.shapes.flat import User, UserORM, users_table
from benchmarks.shapes.join import (
    AUTHOR_FIELDS,
    POST_FIELDS,
    Author,
    AuthorORM,
    Post,
    PostORM,
    authors_table,
    posts_table,
)


def _sa_dsn(path: str) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _sa_dsn_pg(dsn: str) -> str:
    """psycopg-style DSN (`postgresql://...?sslmode=disable`) -> the URL the
    asyncpg SQLAlchemy dialect wants: swap the driver prefix and drop the
    query string (that dialect forwards query params verbatim to
    `asyncpg.connect()`, which has no `sslmode` kwarg)."""
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1).split("?", 1)[0]


# --------------------------------------------------------------------------
# flat shape (`users`)
# --------------------------------------------------------------------------


def _flat_query(limit: int):
    return rf.select(User).where(User.is_active == True).where(User.id > 100).limit(limit)


@contender(
    "rowform", backend="sqlite", shape="flat",
    description="rowform's shipped sqlite path: compiled hydrator + compiled orjson hook.",
)
async def flat_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = rf.SqliteEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = _flat_query(init.limit)
    to_dict = rf.compile_json_default(User)

    async def target() -> bytes:
        rows = await engine.fetch_all(query)
        return orjson.dumps(rows, default=to_dict)

    return target, engine.close


@contender(
    "rowform (MockEngine)", backend="mock", shape="flat", tags=("mapper-floor",),
    description="rowform's mapper cost alone, via MockEngine — zero driver cost.",
)
async def flat_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """`init.handle` is precomputed rows (typically `harness.seed.flat_rows`
    filtered to `init.limit`) — see `benchmarks.engines.mock.MockEngine`."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle)
    query = _flat_query(init.limit)
    to_dict = rf.compile_json_default(User)

    async def target() -> bytes:
        rows = await engine.fetch_all(query)
        return orjson.dumps(rows, default=to_dict)

    async def teardown() -> None:
        return None

    return target, teardown


@contender(
    "raw aiosqlite + dict", backend="sqlite", shape="flat", shipped=False, tags=("floor",),
    description="Naive no-mapping baseline: dict(zip(names, row)) per row.",
)
async def flat_raw_aiosqlite(init: ContenderInit) -> tuple[Target, Teardown]:
    import aiosqlite

    conn = await aiosqlite.connect(init.handle)
    sql = (
        "SELECT id, name, email, is_active FROM users "
        "WHERE is_active = 1 AND id > 100 LIMIT ?"
    )
    names = ("id", "name", "email", "is_active")

    async def target() -> bytes:
        cur = await conn.execute(sql, (init.limit,))
        rows = await cur.fetchall()
        payload = [dict(zip(names, (r[0], r[1], r[2], bool(r[3])), strict=True)) for r in rows]
        return orjson.dumps(payload)

    return target, conn.close


@contender(
    "SQLAlchemy async Core (.mappings())", backend="sqlite", shape="flat",
    description="SQLAlchemy Core via .mappings() — orjson needs a per-key str() cast for it.",
)
async def flat_sa_core_mappings(init: ContenderInit) -> tuple[Target, Teardown]:
    """`.mappings()` yields `RowMapping`s keyed by `quoted_name` (a `str`
    subclass orjson refuses), so every row pays a `str()` cast per key — kept
    registered alongside the positional variant below rather than "corrected
    away" (PLAN.md §4: "price any workaround one contender needs")."""
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(init.limit)
    )

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return target, engine.dispose


@contender(
    "SQLAlchemy async Core (positional)", backend="sqlite", shape="flat",
    description="SQLAlchemy Core, rows shaped positionally instead of via .mappings().",
)
async def flat_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(init.limit)
    )
    names = [str(c.name) for c in users_table.columns]

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [dict(zip(names, row, strict=True)) for row in result]
        return orjson.dumps(payload)

    return target, engine.dispose


@contender(
    "SQLAlchemy async ORM", backend="sqlite", shape="flat",
    description="SQLAlchemy ORM, one Session per request.",
)
async def flat_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    """Fresh `Session` per request, bound to a per-request connection: hoisting
    the `Session` would let its identity map skip hydration on every request
    after the first (PLAN.md §4: "audit what is inside each timed region")."""
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = (
        select(UserORM)
        .where(UserORM.is_active == True)
        .where(UserORM.id > 100)
        .limit(init.limit)
    )
    names = [str(c.name) for c in UserORM.__table__.columns]

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            payload = [{name: getattr(u, name) for name in names} for u in users]
        return orjson.dumps(payload)

    return target, engine.dispose


# --------------------------------------------------------------------------
# join shape (`j_authors` x `j_posts`)
# --------------------------------------------------------------------------


def _join_query(limit: int):
    return (
        rf.select(Author, Post)
        .join(Post, Post.author_id == Author.id)
        .where(Author.is_active == True)
        .where(Post.score > 100)
        .limit(limit)
    )


@contender(
    "rowform", backend="sqlite", shape="join",
    description="rowform's shipped sqlite join path: compiled join hydrator + dispatching orjson hook.",
)
async def join_rowform(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = rf.SqliteEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = _join_query(init.limit)
    author_default = rf.compile_json_default(Author)
    post_default = rf.compile_json_default(Post)

    def _default(obj):
        return author_default(obj) if type(obj) is Author else post_default(obj)

    async def target() -> bytes:
        pairs = await engine.fetch_all(query)
        return orjson.dumps(
            [{"author": author, "post": post} for author, post in pairs], default=_default
        )

    return target, engine.close


@contender(
    "rowform (MockEngine)", backend="mock", shape="join", tags=("mapper-floor",),
    description="rowform's join mapper cost alone, via MockEngine — zero driver cost.",
)
async def join_rowform_mock(init: ContenderInit) -> tuple[Target, Teardown]:
    """`init.handle` is the pre-joined `(author_cols + post_cols)` flat tuples
    `compile_join_hydrator` expects — see `benchmarks.engines.mock.MockEngine`."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(init.handle)
    query = _join_query(init.limit)
    author_default = rf.compile_json_default(Author)
    post_default = rf.compile_json_default(Post)

    def _default(obj):
        return author_default(obj) if type(obj) is Author else post_default(obj)

    async def target() -> bytes:
        pairs = await engine.fetch_all(query)
        return orjson.dumps(
            [{"author": author, "post": post} for author, post in pairs], default=_default
        )

    async def teardown() -> None:
        return None

    return target, teardown


@contender(
    "SQLAlchemy async Core (positional)", backend="sqlite", shape="join",
    description="SQLAlchemy Core, positional join shaping — .mappings() collides on the shared id column.",
)
async def join_sa_core_positional(init: ContenderInit) -> tuple[Target, Teardown]:
    """Core's `.mappings()` collides here — both tables have an `id` column —
    so positional shaping isn't just the faster idiom (PLAN.md §4's "price
    any workaround"), it's the only one that works for a join at all."""
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = (
        select(authors_table, posts_table)
        .join(posts_table, posts_table.c.author_id == authors_table.c.id)
        .where(authors_table.c.is_active == True)
        .where(posts_table.c.score > 100)
        .limit(init.limit)
    )
    n_author = len(AUTHOR_FIELDS)

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [
                {
                    "author": dict(zip(AUTHOR_FIELDS, row[:n_author], strict=True)),
                    "post": dict(zip(POST_FIELDS, row[n_author:], strict=True)),
                }
                for row in result
            ]
        return orjson.dumps(payload)

    return target, engine.dispose


@contender(
    "SQLAlchemy async ORM", backend="sqlite", shape="join",
    description="SQLAlchemy ORM join, one Session per request.",
)
async def join_sa_orm(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn(init.handle))
    stmt = (
        select(AuthorORM, PostORM)
        .join(PostORM, PostORM.author_id == AuthorORM.id)
        .where(AuthorORM.is_active == True)
        .where(PostORM.score > 100)
        .limit(init.limit)
    )

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            pairs = (await session.execute(stmt)).all()
            payload = [
                {
                    "author": {name: getattr(a, name) for name in AUTHOR_FIELDS},
                    "post": {name: getattr(p, name) for name in POST_FIELDS},
                }
                for a, p in pairs
            ]
        return orjson.dumps(payload)

    return target, engine.dispose


# --------------------------------------------------------------------------
# flat shape (`users`), postgres backend
# --------------------------------------------------------------------------


@contender(
    "rowform", backend="postgres", shape="flat",
    description="rowform's shipped postgres path: compiled hydrator + compiled orjson hook.",
)
async def flat_rowform_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = rf.PsycopgEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = _flat_query(init.limit)
    to_dict = rf.compile_json_default(User)

    async def target() -> bytes:
        rows = await engine.fetch_all(query)
        return orjson.dumps(rows, default=to_dict)

    return target, engine.close


@contender(
    "raw asyncpg + dict", backend="postgres", shape="flat", shipped=False, tags=("floor",),
    description="Naive no-mapping baseline: dict(record) per row.",
)
async def flat_raw_asyncpg(init: ContenderInit) -> tuple[Target, Teardown]:
    import asyncpg

    pool = await asyncpg.create_pool(init.handle, min_size=1, max_size=4)
    sql = "SELECT id, name, email, is_active FROM users WHERE is_active AND id > 100 LIMIT $1"

    async def target() -> bytes:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, init.limit)
        return orjson.dumps([dict(r) for r in rows])

    return target, pool.close


@contender(
    "SQLAlchemy async Core (.mappings())", backend="postgres", shape="flat",
    description="SQLAlchemy Core via .mappings() — orjson needs a per-key str() cast for it.",
)
async def flat_sa_core_mappings_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(init.limit)
    )

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
        return orjson.dumps(payload)

    return target, engine.dispose


@contender(
    "SQLAlchemy async Core (positional)", backend="postgres", shape="flat",
    description="SQLAlchemy Core, rows shaped positionally instead of via .mappings().",
)
async def flat_sa_core_positional_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(init.limit)
    )
    names = [str(c.name) for c in users_table.columns]

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [dict(zip(names, row, strict=True)) for row in result]
        return orjson.dumps(payload)

    return target, engine.dispose


@contender(
    "SQLAlchemy async ORM", backend="postgres", shape="flat",
    description="SQLAlchemy ORM, one Session per request.",
)
async def flat_sa_orm_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = (
        select(UserORM)
        .where(UserORM.is_active == True)
        .where(UserORM.id > 100)
        .limit(init.limit)
    )
    names = [str(c.name) for c in UserORM.__table__.columns]

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            users = (await session.execute(stmt)).scalars().all()
            payload = [{name: getattr(u, name) for name in names} for u in users]
        return orjson.dumps(payload)

    return target, engine.dispose


# --------------------------------------------------------------------------
# join shape (`j_authors` x `j_posts`), postgres backend
# --------------------------------------------------------------------------


@contender(
    "rowform", backend="postgres", shape="join",
    description="rowform's shipped postgres join path: compiled join hydrator + dispatching orjson hook.",
)
async def join_rowform_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = rf.PsycopgEngine(init.handle, min_size=1, max_size=4)
    await engine.connect()
    query = _join_query(init.limit)
    author_default = rf.compile_json_default(Author)
    post_default = rf.compile_json_default(Post)

    def _default(obj):
        return author_default(obj) if type(obj) is Author else post_default(obj)

    async def target() -> bytes:
        pairs = await engine.fetch_all(query)
        return orjson.dumps(
            [{"author": author, "post": post} for author, post in pairs], default=_default
        )

    return target, engine.close


@contender(
    "SQLAlchemy async Core (positional)", backend="postgres", shape="join",
    description="SQLAlchemy Core, positional join shaping — .mappings() collides on the shared id column.",
)
async def join_sa_core_positional_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = (
        select(authors_table, posts_table)
        .join(posts_table, posts_table.c.author_id == authors_table.c.id)
        .where(authors_table.c.is_active == True)
        .where(posts_table.c.score > 100)
        .limit(init.limit)
    )
    n_author = len(AUTHOR_FIELDS)

    async def target() -> bytes:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            payload = [
                {
                    "author": dict(zip(AUTHOR_FIELDS, row[:n_author], strict=True)),
                    "post": dict(zip(POST_FIELDS, row[n_author:], strict=True)),
                }
                for row in result
            ]
        return orjson.dumps(payload)

    return target, engine.dispose


@contender(
    "SQLAlchemy async ORM", backend="postgres", shape="join",
    description="SQLAlchemy ORM join, one Session per request.",
)
async def join_sa_orm_pg(init: ContenderInit) -> tuple[Target, Teardown]:
    engine = create_async_engine(_sa_dsn_pg(init.handle))
    stmt = (
        select(AuthorORM, PostORM)
        .join(PostORM, PostORM.author_id == AuthorORM.id)
        .where(AuthorORM.is_active == True)
        .where(PostORM.score > 100)
        .limit(init.limit)
    )

    async def target() -> bytes:
        async with AsyncSession(engine) as session:
            pairs = (await session.execute(stmt)).all()
            payload = [
                {
                    "author": {name: getattr(a, name) for name in AUTHOR_FIELDS},
                    "post": {name: getattr(p, name) for name in POST_FIELDS},
                }
                for a, p in pairs
            ]
        return orjson.dumps(payload)

    return target, engine.dispose
