"""The FastAPI worker `bench load`/`bench service run`/`bench profile load`
drive traffic at — plain, hand-written routes, one real `async def` per case.

This is deliberately *not* generated from `bench micro`'s contender registry
(`harness/registry.py`, `contenders.py`) the way an earlier revision of this
file did. That was convenient to write once, but a flamegraph full of
`registry.contender.<locals>.decorator.<locals>.route`/`<locals>.target`
frames is much harder to read than a real, named function you can set a
breakpoint in — and this file exists specifically to be profiled
(`bench profile load` attaches py-spy/austin to it). The duplication with
`contenders.py` (the same queries, the same hydration) is accepted on
purpose, not an oversight: these two files answer different questions (one
isolates the mapper in-process, this one is the actual HTTP+driver path
under concurrent load) and sharing code between them is what made this file
confusing to profile in the first place.

`limit` is a query parameter (`?limit=N`), read per request — not baked into
a query built once at startup — the same way a hand-written endpoint would
do it. Each route acquires its connection from a pool set up once in
`lifespan()` and stored on `app.state`.

Configured from one environment variable, `BENCH_HANDLE` (the sqlite db path)
— `launch.py` starts this as a uvicorn subprocess per worker, and env vars
are the plumbing that survives a subprocess boundary without needing a
config file.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import aiosqlite
import asyncpg
import orjson
from fastapi import FastAPI, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import rowform as rf
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

DB_PATH = os.environ.get("BENCH_HANDLE", "")
PG_DSN = os.environ.get("BENCH_PG_DSN", "")
JSON = "application/json"
DEFAULT_LIMIT = 1000


def _sa_dsn(path: str) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _sa_dsn_pg(dsn: str) -> str:
    """psycopg-style DSN (`postgresql://...?sslmode=disable`) -> the URL the
    asyncpg SQLAlchemy dialect wants: swap the driver prefix and drop the
    query string (that dialect forwards query params verbatim to
    `asyncpg.connect()`, which has no `sslmode` kwarg)."""
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1).split("?", 1)[0]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Every pool this worker will hand out connections from, opened once.
    Postgres pools are only opened if `BENCH_PG_DSN` is set — `bench service
    run` doesn't provision postgres, so its worker leaves `app.state.pg_*` as
    `None` and never serves a `/postgres-*` route."""
    app.state.rowform = rf.SqliteEngine(DB_PATH, min_size=1, max_size=4)
    await app.state.rowform.connect()
    app.state.aiosqlite = await aiosqlite.connect(DB_PATH)
    app.state.sa_engine = create_async_engine(_sa_dsn(DB_PATH))

    app.state.pg_rowform = None
    app.state.pg_asyncpg = None
    app.state.pg_sa_engine = None
    if PG_DSN:
        app.state.pg_rowform = rf.PsycopgEngine(PG_DSN, min_size=1, max_size=4)
        await app.state.pg_rowform.connect()
        app.state.pg_asyncpg = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=4)
        app.state.pg_sa_engine = create_async_engine(_sa_dsn_pg(PG_DSN))
    try:
        yield
    finally:
        await app.state.rowform.close()
        await app.state.aiosqlite.close()
        await app.state.sa_engine.dispose()
        if app.state.pg_rowform is not None:
            await app.state.pg_rowform.close()
        if app.state.pg_asyncpg is not None:
            await app.state.pg_asyncpg.close()
        if app.state.pg_sa_engine is not None:
            await app.state.pg_sa_engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.get("/noop")
async def noop() -> Response:
    """The floor: routing + ASGI only, no database (PLAN.md §7)."""
    return Response(content=b"[]", media_type=JSON)


# --------------------------------------------------------------------------
# flat shape (`users`)
# --------------------------------------------------------------------------


@app.get("/sqlite-flat-rowform")
async def sqlite_flat_rowform(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    query = rf.select(User).where(User.is_active == True).where(User.id > 100).limit(limit)
    rows = await app.state.rowform.fetch_all(query)
    return Response(
        content=orjson.dumps(rows, default=rf.json_default, option=rf.DATACLASS_DUMP_OPTION),
        media_type=JSON,
    )


@app.get("/sqlite-flat-raw-aiosqlite-dict")
async def sqlite_flat_raw_aiosqlite_dict(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    sql = "SELECT id, name, email, is_active FROM users WHERE is_active = 1 AND id > 100 LIMIT ?"
    cur = await app.state.aiosqlite.execute(sql, (limit,))
    rows = await cur.fetchall()
    payload = [
        {
            "id": r[0],
            "name": r[1],
            "email": r[2],
            "is_active": bool(r[3]),
        }
        for r in rows
    ]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/sqlite-flat-sqlalchemy-async-core-mappings")
async def sqlite_flat_sqlalchemy_async_core_mappings(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )
    async with app.state.sa_engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/sqlite-flat-sqlalchemy-async-core-positional")
async def sqlite_flat_sqlalchemy_async_core_positional(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )
    async with app.state.sa_engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [
            {
                "id": row[0],
                "name": row[1],
                "email": row[2],
                "is_active": bool(row[3]),
            }
            for row in result
        ]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/sqlite-flat-sqlalchemy-async-orm")
async def sqlite_flat_sqlalchemy_async_orm(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    stmt = select(UserORM).where(UserORM.is_active == True).where(UserORM.id > 100).limit(limit)
    names = [str(c.name) for c in UserORM.__table__.columns]
    async with AsyncSession(app.state.sa_engine) as session:
        users = (await session.execute(stmt)).scalars().all()
        payload = [{name: getattr(u, name) for name in names} for u in users]
    return Response(content=orjson.dumps(payload), media_type=JSON)


# --------------------------------------------------------------------------
# join shape (`j_authors` x `j_posts`)
# --------------------------------------------------------------------------


@app.get("/sqlite-join-rowform")
async def sqlite_join_rowform(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    query = (
        rf
        .select(Author, Post)
        .join(Post, Post.author_id == Author.id)
        .where(Author.is_active == True)
        .where(Post.score > 100)
        .limit(limit)
    )
    pairs = await app.state.rowform.fetch_all(query)
    payload = [{"author": author, "post": post} for author, post in pairs]
    return Response(
        content=orjson.dumps(payload, default=rf.json_default, option=rf.DATACLASS_DUMP_OPTION),
        media_type=JSON,
    )


@app.get("/sqlite-join-sqlalchemy-async-core-positional")
async def sqlite_join_sqlalchemy_async_core_positional(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(authors_table, posts_table)
        .join(posts_table, posts_table.c.author_id == authors_table.c.id)
        .where(authors_table.c.is_active == True)
        .where(posts_table.c.score > 100)
        .limit(limit)
    )

    async with app.state.sa_engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [
            {
                "author": {
                    "id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "is_active": bool(row[3]),
                },
                "post": {
                    "id": row[4],
                    "author_id": row[5],
                    "title": row[6],
                    "score": row[7],
                    "published": bool(row[8]),
                },
            }
            for row in result
        ]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/sqlite-join-sqlalchemy-async-orm")
async def sqlite_join_sqlalchemy_async_orm(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    stmt = (
        select(AuthorORM, PostORM)
        .join(PostORM, PostORM.author_id == AuthorORM.id)
        .where(AuthorORM.is_active == True)
        .where(PostORM.score > 100)
        .limit(limit)
    )
    async with AsyncSession(app.state.sa_engine) as session:
        pairs = (await session.execute(stmt)).all()
        payload = [
            {
                "author": {name: getattr(a, name) for name in AUTHOR_FIELDS},
                "post": {name: getattr(p, name) for name in POST_FIELDS},
            }
            for a, p in pairs
        ]
    return Response(content=orjson.dumps(payload), media_type=JSON)


# --------------------------------------------------------------------------
# flat shape (`users`), postgres backend
# --------------------------------------------------------------------------


@app.get("/postgres-flat-rowform")
async def postgres_flat_rowform(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    query = rf.select(User).where(User.is_active == True).where(User.id > 100).limit(limit)
    rows = await app.state.pg_rowform.fetch_all(query)
    return Response(
        content=orjson.dumps(rows, default=rf.json_default, option=rf.DATACLASS_DUMP_OPTION),
        media_type=JSON,
    )


@app.get("/postgres-flat-raw-asyncpg-dict")
async def postgres_flat_raw_asyncpg_dict(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    sql = "SELECT id, name, email, is_active FROM users WHERE is_active AND id > 100 LIMIT $1"
    async with app.state.pg_asyncpg.acquire() as conn:
        rows = await conn.fetch(sql, limit)
    payload = [dict(r) for r in rows]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/postgres-flat-sqlalchemy-core-mappings")
async def postgres_flat_sqlalchemy_core_mappings(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )
    async with app.state.pg_sa_engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/postgres-flat-sqlalchemy-core-positional")
async def postgres_flat_sqlalchemy_core_positional(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(users_table)
        .where(users_table.c.is_active == True)
        .where(users_table.c.id > 100)
        .limit(limit)
    )
    async with app.state.pg_sa_engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [
            {
                "id": row[0],
                "name": row[1],
                "email": row[2],
                "is_active": bool(row[3]),
            }
            for row in result
        ]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/postgres-flat-sqlalchemy-orm")
async def postgres_flat_sqlalchemy_orm(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = select(UserORM).where(UserORM.is_active == True).where(UserORM.id > 100).limit(limit)
    names = [str(c.name) for c in UserORM.__table__.columns]
    async with AsyncSession(app.state.pg_sa_engine) as session:
        users = (await session.execute(stmt)).scalars().all()
        payload = [{name: getattr(u, name) for name in names} for u in users]
    return Response(content=orjson.dumps(payload), media_type=JSON)


# --------------------------------------------------------------------------
# join shape (`j_authors` x `j_posts`), postgres backend
# --------------------------------------------------------------------------


@app.get("/postgres-join-rowform")
async def postgres_join_rowform(limit: int = Query(default=DEFAULT_LIMIT)) -> Response:
    query = (
        rf
        .select(Author, Post)
        .join(Post, Post.author_id == Author.id)
        .where(Author.is_active == True)
        .where(Post.score > 100)
        .limit(limit)
    )
    pairs = await app.state.pg_rowform.fetch_all(query)
    payload = [{"author": author, "post": post} for author, post in pairs]
    return Response(
        content=orjson.dumps(payload, default=rf.json_default, option=rf.DATACLASS_DUMP_OPTION),
        media_type=JSON,
    )


@app.get("/postgres-join-sqlalchemy-core-positional")
async def postgres_join_sqlalchemy_core_positional(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(authors_table, posts_table)
        .join(posts_table, posts_table.c.author_id == authors_table.c.id)
        .where(authors_table.c.is_active == True)
        .where(posts_table.c.score > 100)
        .limit(limit)
    )
    async with app.state.pg_sa_engine.connect() as conn:
        result = await conn.execute(stmt)
        payload = [
            {
                "author": {
                    "id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "is_active": bool(row[3]),
                },
                "post": {
                    "id": row[4],
                    "author_id": row[5],
                    "title": row[6],
                    "score": row[7],
                    "published": bool(row[8]),
                },
            }
            for row in result
        ]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/postgres-join-sqlalchemy-orm")
async def postgres_join_sqlalchemy_orm(
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    stmt = (
        select(AuthorORM, PostORM)
        .join(PostORM, PostORM.author_id == AuthorORM.id)
        .where(AuthorORM.is_active == True)
        .where(PostORM.score > 100)
        .limit(limit)
    )
    async with AsyncSession(app.state.pg_sa_engine) as session:
        pairs = (await session.execute(stmt)).all()
        payload = [
            {
                "author": {name: getattr(a, name) for name in AUTHOR_FIELDS},
                "post": {name: getattr(p, name) for name in POST_FIELDS},
            }
            for a, p in pairs
        ]
    return Response(content=orjson.dumps(payload), media_type=JSON)
