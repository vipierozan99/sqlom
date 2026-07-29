"""Contenders for the join (`j_authors`/`j_posts`) shape, sqlite tier
(PLAN.md §7 tier 3). Kept separate from `contenders/flat.py` for the same
reason `shapes/join.py` is separate from `shapes/flat.py`.
"""

from __future__ import annotations

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmarks.harness.registry import contender
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
from rowform import Query, SqliteEngine, compile_json_default


def _rowform_query(limit: int):
    return (
        Query(Author, Post)
        .join(Post, Post.author_id == Author.id)
        .where(Author.is_active == True)
        .where(Post.score > 100)
        .limit(limit)
    )


def _sa_dsn(path: str) -> str:
    return f"sqlite+aiosqlite:///{path}"


@contender(
    "rowform", backend="sqlite", shape="join",
    description="rowform's shipped sqlite join path: compiled join hydrator + dispatching orjson hook.",
)
async def make_rowform(path: str, limit: int):
    """rowform: compiled join hydrator (built once by the engine, cached per
    query shape) + one dispatching orjson hook for the two entity types."""
    engine = SqliteEngine(path, min_size=1, max_size=4)
    await engine.connect()
    query = _rowform_query(limit)
    author_default = compile_json_default(Author)
    post_default = compile_json_default(Post)

    def _default(obj):
        return author_default(obj) if type(obj) is Author else post_default(obj)

    async def request():
        pairs = await engine.fetch_all(query)
        return orjson.dumps(
            [{"author": author, "post": post} for author, post in pairs], default=_default
        )

    return request, engine.close


async def rowform_stages(path: str, limit: int):
    """Stage decomposition for the rowform sqlite join contender — see
    `contenders.flat.rowform_stages`."""
    engine = SqliteEngine(path, min_size=1, max_size=4)
    await engine.connect()
    query = _rowform_query(limit)
    author_default = compile_json_default(Author)
    post_default = compile_json_default(Post)

    def _default(obj):
        return author_default(obj) if type(obj) is Author else post_default(obj)

    async def fetch():
        return await engine.fetch_all(query)

    cached_pairs = await fetch()

    async def serialize():
        return orjson.dumps(
            [{"author": a, "post": p} for a, p in cached_pairs], default=_default
        )

    async def whole():
        pairs = await engine.fetch_all(query)
        return orjson.dumps([{"author": a, "post": p} for a, p in pairs], default=_default)

    return {"fetch": fetch, "serialize": serialize, "whole": whole}, engine.close


@contender(
    "rowform (MockEngine)", backend="mock", shape="join", tags=("mapper-floor",),
    description="rowform's join mapper cost alone, via MockEngine — zero driver cost.",
)
async def make_rowform_mock(rows: list[tuple], limit: int):
    """Tier 2 (PLAN.md §7). `rows` here are the pre-joined `(author_cols +
    post_cols)` flat tuples `compile_join_hydrator` expects — see
    `benchmarks.engines.mock.MockEngine`."""
    from benchmarks.engines.mock import MockEngine

    engine = MockEngine(rows)
    query = _rowform_query(limit)
    author_default = compile_json_default(Author)
    post_default = compile_json_default(Post)

    def _default(obj):
        return author_default(obj) if type(obj) is Author else post_default(obj)

    async def request():
        pairs = await engine.fetch_all(query)
        return orjson.dumps(
            [{"author": author, "post": post} for author, post in pairs], default=_default
        )

    async def teardown():
        return None

    return request, teardown


@contender(
    "SQLAlchemy async Core (positional)", backend="sqlite", shape="join",
    description="SQLAlchemy Core, positional join shaping — .mappings() collides on the shared id column.",
)
async def make_sa_core_positional(path: str, limit: int):
    """Core's `.mappings()` collides here — both tables have an `id` column —
    so positional shaping isn't just the faster idiom (PLAN.md §4's "price
    any workaround"), it's the only one that works for a join at all."""
    engine = create_async_engine(_sa_dsn(path))
    stmt = (
        select(authors_table, posts_table)
        .join(posts_table, posts_table.c.author_id == authors_table.c.id)
        .where(authors_table.c.is_active == True)
        .where(posts_table.c.score > 100)
        .limit(limit)
    )
    n_author = len(AUTHOR_FIELDS)

    async def request():
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

    return request, engine.dispose


@contender(
    "SQLAlchemy async ORM", backend="sqlite", shape="join",
    description="SQLAlchemy ORM join, one Session per request.",
)
async def make_sa_orm(path: str, limit: int):
    engine = create_async_engine(_sa_dsn(path))
    stmt = (
        select(AuthorORM, PostORM)
        .join(PostORM, PostORM.author_id == AuthorORM.id)
        .where(AuthorORM.is_active == True)
        .where(PostORM.score > 100)
        .limit(limit)
    )

    async def request():
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

    return request, engine.dispose
