#!/usr/bin/env python3
"""FastAPI app exposing the same endpoint four ways, for end-to-end measurement.

Every benchmark before this drove the data layer directly, so all of them
overstate what a real service gains: a FastAPI/uvicorn request also pays routing,
ASGI plumbing and HTTP framing, and that cost is identical whichever mapper sits
underneath. This app exists to measure how much of the data-layer difference
survives the web layer.

Each endpoint returns byte-identical JSON via `Response`, bypassing FastAPI's own
`jsonable_encoder`, so the only difference between routes is the data layer.

Endpoints:
    /rowform       rowform, all optimizations (compiled hydrator + hook,
                 conditional reset)
    /core        SQLAlchemy 2.0 Core, tuned (AUTOCOMMIT, no pool reset)
    /core-fast   the same, shaping rows positionally rather than via .mappings()
    /orm         SQLAlchemy 2.0 ORM, tuned
    /orm-default SQLAlchemy 2.0 ORM as normally written
    /psy-rowform   rowform on psycopg3, DEFAULT pool behaviour
    /psy-core    SQLAlchemy Core on psycopg3, DEFAULT
    /psy-core-fast  the same, positional row shaping
    /psy-orm     SQLAlchemy ORM on psycopg3, DEFAULT
    /noop        returns a constant — the floor: routing + ASGI only, no database

Run (single core, Postgres pinned elsewhere):
    taskset -c 0 python3 -m uvicorn benchmarks.fastapi_app:app --port 8000 \
        --loop uvloop --http httptools --no-access-log
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orjson
from fastapi import FastAPI
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmarks.models import User, UserORM, users_table
from rowform import DatabaseEngine, PsycopgEngine, Query, compile_json_default

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"
SA_DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/rowform_bench"
# Same-driver, both-defaults variants: psycopg3 async on both sides, no pool
# tuning anywhere. See benchmarks/bench_psycopg.py.
PSY_CONNINFO = DSN
PSY_SA_DSN = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/rowform_bench?sslmode=disable"
LIMIT = int(os.environ.get("BENCH_LIMIT", "100"))
POOL = int(os.environ.get("BENCH_POOL", "10"))

app = FastAPI()
state = {}
JSON = "application/json"


def sa_engine(tuned):
    kwargs = {"pool_size": POOL, "max_overflow": 0, "connect_args": {"ssl": False}}
    if tuned:
        # Without this SQLAlchemy sends BEGIN + SELECT + ROLLBACK per request —
        # 3 statements against rowform's 1. See benchmarks/bench_final.py.
        kwargs["isolation_level"] = "AUTOCOMMIT"
        kwargs["pool_reset_on_return"] = None
    return create_async_engine(SA_DSN, **kwargs)


@app.on_event("startup")
async def startup():
    db = DatabaseEngine(dsn=DSN, conditional_reset=True, min_size=POOL, max_size=POOL)
    await db.connect()
    state["db"] = db
    state["query"] = (Query(User).where(User.is_active == True)
                      .where(User.id > 100).limit(LIMIT))
    state["to_dict"] = compile_json_default(User)

    state["core_engine"] = sa_engine(True)
    state["core_stmt"] = (select(users_table)
                          .where(users_table.c.is_active == True)
                          .where(users_table.c.id > 100).limit(LIMIT))

    state["orm_engine"] = sa_engine(True)
    state["orm_engine_default"] = sa_engine(False)
    state["orm_stmt"] = (select(UserORM)
                         .where(UserORM.is_active == True)
                         .where(UserORM.id > 100).limit(LIMIT))
    state["orm_cols"] = [str(c.name) for c in UserORM.__table__.columns]

    # --- psycopg3 on both sides, default pool behaviour on both ---
    psy = PsycopgEngine(PSY_CONNINFO, min_size=POOL, max_size=POOL)
    await psy.connect()
    state["psy"] = psy
    state["psy_core_engine"] = create_async_engine(PSY_SA_DSN, pool_size=POOL,
                                                   max_overflow=0)
    state["psy_orm_engine"] = create_async_engine(PSY_SA_DSN, pool_size=POOL,
                                                  max_overflow=0)


@app.on_event("shutdown")
async def shutdown():
    await state["db"].close()
    await state["psy"].close()
    for key in ("core_engine", "orm_engine", "orm_engine_default",
                "psy_core_engine", "psy_orm_engine"):
        await state[key].dispose()


@app.get("/noop")
async def noop():
    """Floor: how much of a request is FastAPI + uvicorn, with no database."""
    return Response(content=b"[]", media_type=JSON)


@app.get("/rowform")
async def read_rowform():
    rows = await state["db"].fetch_all(state["query"])
    return Response(content=orjson.dumps(rows, default=state["to_dict"]),
                    media_type=JSON)


@app.get("/core")
async def read_core():
    async with state["core_engine"].connect() as conn:
        result = await conn.execute(state["core_stmt"])
        payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/core-fast")
async def read_core_fast():
    """Core again, shaping rows positionally instead of through `.mappings()`.

    `.mappings()` yields `RowMapping`s whose keys are `quoted_name` (a `str`
    subclass) that orjson refuses, so every route above casts each key per row.
    That cast is not row shaping — it is the price of one Core API — and it measured
    at 2.6x Core's whole sqlite time. Zipping the flat row against names captured
    once is equally idiomatic Core and produces identical bytes, so this endpoint
    exists to keep every published Core ratio honest about which idiom it assumed.
    """
    names = state["orm_cols"]
    async with state["core_engine"].connect() as conn:
        result = await conn.execute(state["core_stmt"])
        payload = [dict(zip(names, row)) for row in result]
    return Response(content=orjson.dumps(payload), media_type=JSON)


async def _orm(engine):
    names = state["orm_cols"]
    async with AsyncSession(engine) as session:
        users = (await session.execute(state["orm_stmt"])).scalars().all()
        payload = [{n: getattr(u, n) for n in names} for u in users]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/orm")
async def read_orm():
    return await _orm(state["orm_engine"])


@app.get("/orm-default")
async def read_orm_default():
    return await _orm(state["orm_engine_default"])


# --------------------------------------------------------------------------
# Same driver (psycopg3 async), default pool behaviour on both sides. These are
# the fairest mapper-only comparison: nothing is tuned on either side, and both
# send BEGIN / SELECT / COMMIT-or-ROLLBACK per request.
# --------------------------------------------------------------------------


@app.get("/psy-rowform")
async def read_psy_rowform():
    rows = await state["psy"].fetch_all(state["query"])
    return Response(content=orjson.dumps(rows, default=state["to_dict"]),
                    media_type=JSON)


@app.get("/psy-core")
async def read_psy_core():
    async with state["psy_core_engine"].connect() as conn:
        result = await conn.execute(state["core_stmt"])
        payload = [{str(k): v for k, v in m.items()} for m in result.mappings()]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/psy-core-fast")
async def read_psy_core_fast():
    """The positional idiom on psycopg. See `/core-fast`."""
    names = state["orm_cols"]
    async with state["psy_core_engine"].connect() as conn:
        result = await conn.execute(state["core_stmt"])
        payload = [dict(zip(names, row)) for row in result]
    return Response(content=orjson.dumps(payload), media_type=JSON)


@app.get("/psy-orm")
async def read_psy_orm():
    return await _orm(state["psy_orm_engine"])
