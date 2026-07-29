"""The single FastAPI app (PLAN.md §9/§11), routes generated from the
contender registry instead of hand-written per approach — the old
`fastapi_app.py` wrote one route per contender by hand and only ever
exercised Postgres.

Configured from environment variables rather than CLI args: `launch.py` starts
this as a uvicorn subprocess per worker, and env vars are the plumbing that
survives a subprocess boundary without needing a config file.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response

import benchmarks.contenders  # noqa: F401 -- registration side-effects
from benchmarks.harness import registry

JSON = "application/json"


def build_app(backend: str, shape: str, handle: str, limit: int) -> FastAPI:
    """One FastAPI app exposing every registered contender for
    `backend`/`shape` as its own route, at `/{spec.slug}` (e.g.
    `/sqlite-flat-rowform`), plus `/noop` — the floor: routing + ASGI only,
    no database (PLAN.md §7). The slug (not a locally-recomputed name-only
    one) is the route path so it can never drift from `bench contenders
    list`'s idea of the same contender.
    """
    specs = registry.select(backend=backend, shape=shape)
    if not specs:
        raise ValueError(f"no contenders registered for backend={backend!r} shape={shape!r}")

    state: dict[str, tuple[Callable[[], Awaitable[bytes]], Callable[[], Awaitable[None]]]] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for spec in specs:
            state[spec.slug] = await spec.factory(handle, limit)
        try:
            yield
        finally:
            for _, teardown in state.values():
                await teardown()

    fastapi_app = FastAPI(lifespan=lifespan)

    @fastapi_app.get("/noop")
    async def noop() -> Response:
        return Response(content=b"[]", media_type=JSON)

    def _make_route(slug: str):
        async def route() -> Response:
            request, _ = state[slug]
            return Response(content=await request(), media_type=JSON)

        return route

    for spec in specs:
        fastapi_app.add_api_route(f"/{spec.slug}", _make_route(spec.slug), methods=["GET"])

    return fastapi_app


# uvicorn imports this module and looks up `app` at the string target
# "benchmarks.service.app:app" — built eagerly from env vars set by
# `launch.py`, since each worker is its own subprocess with its own env.
app = build_app(
    backend=os.environ.get("BENCH_BACKEND", "sqlite"),
    shape=os.environ.get("BENCH_SHAPE", "flat"),
    handle=os.environ.get("BENCH_HANDLE", ""),
    limit=int(os.environ.get("BENCH_LIMIT", "1000")),
)
