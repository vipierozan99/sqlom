"""A fixed-size aiosqlite pool, so the sqlite floors check out per request.

aiosqlite ships no pool, and the floors used to hold one connection for the whole
run. That made them the only contenders paying no checkout at all — and, because
aiosqlite gives every `Connection` its own worker thread, the only ones capped at
one concurrent statement while their comparators ran four. Two errors, both inside
the floor, pushing in opposite directions: you could not even sign the bias.

Deliberately the cheapest thing that still checks out: a queue of live
connections, a get and a put. A floor has to do strictly less work than
everything it bounds (`micro/contenders.py`), so this must stay far below
SQLAlchemy's ~0.4 ms checkout — and a queue round trip is microseconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite


class AiosqlitePool:
    """`size` connections, handed out one at a time and never closed on release."""

    __slots__ = ("_all", "_free")

    def __init__(self, connections: list[aiosqlite.Connection]) -> None:
        self._all = connections
        self._free: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        for conn in connections:
            self._free.put_nowait(conn)

    @classmethod
    async def open(cls, path: str, size: int) -> AiosqlitePool:
        return cls([await aiosqlite.connect(path) for _ in range(size)])

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._free.get()
        try:
            yield conn
        finally:
            self._free.put_nowait(conn)

    async def close(self) -> None:
        for conn in self._all:
            await conn.close()
