"""`SqliteEngine`'s pool sizing, and the public export surface.

aiosqlite ships no pool, so `rowform` provides one; `min_size`/`max_size` are the
same two knobs asyncpg's pool and `psycopg_pool` take. They used to collapse to
`max(min_size, max_size)`, so `max_size` was not a maximum and `min_size` had no
effect of its own. These tests pin both.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
import sqlalchemy as sa
from conftest import Author, seed

import rowform


@pytest.fixture
async def seeded(sqlite_path):
    """Schema and rows at `sqlite_path`, so the tests below can open their own
    differently-sized engines against it."""
    async with rowform.SqliteEngine(sqlite_path) as db:
        await seed(db)


class TestPoolSizing:
    async def test_min_size_is_opened_up_front(self, sqlite_path):
        async with rowform.SqliteEngine(sqlite_path, min_size=3, max_size=5) as db:
            assert len(db.pool._all) == 3

    async def test_it_grows_to_max_size_under_concurrency(self, seeded, sqlite_path):
        """Ten concurrent readers against min_size=1 must not serialise behind one
        connection, and must not open more than max_size."""
        async with rowform.SqliteEngine(sqlite_path, min_size=1, max_size=4) as db:
            await asyncio.gather(*(db.fetch_all(sa.select(Author)) for _ in range(10)))
            assert 1 < len(db.pool._all) <= 4

    async def test_it_does_not_grow_past_max_size(self, seeded, sqlite_path):
        async with rowform.SqliteEngine(sqlite_path, min_size=1, max_size=2) as db:
            await asyncio.gather(*(db.fetch_all(sa.select(Author)) for _ in range(25)))
            assert len(db.pool._all) <= 2

    async def test_a_fixed_size_pool_is_min_equals_max(self, seeded, sqlite_path):
        async with rowform.SqliteEngine(sqlite_path, min_size=2, max_size=2) as db:
            await asyncio.gather(*(db.fetch_all(sa.select(Author)) for _ in range(10)))
            assert len(db.pool._all) == 2

    async def test_connections_are_returned_not_leaked(self, seeded, sqlite_path):
        async with rowform.SqliteEngine(sqlite_path, min_size=2, max_size=2) as db:
            for _ in range(5):
                await db.fetch_all(sa.select(Author))
            assert db.pool._idle.qsize() == 2

    @pytest.mark.parametrize(
        ("min_size", "max_size"),
        [(4, 2), (1, 0), (-1, 5)],
    )
    def test_impossible_sizes_are_refused(self, sqlite_path, min_size, max_size):
        """The old `max(min, max)` accepted min_size=4, max_size=2 and quietly
        opened four connections."""
        with pytest.raises(rowform.ConfigurationError, match="pool sizes"):
            rowform.SqliteEngine(sqlite_path, min_size=min_size, max_size=max_size)


class TestPoolFailures:
    """Opening the pool can fail half-way, and what it opened has to be closed.

    `_open_pool` never returns in that case, so `engine.pool` is never assigned
    and nothing else holds a reference to the connections already made — a
    retried `connect()` would just accumulate file handles.
    """

    @staticmethod
    def _flaky_aiosqlite(monkeypatch, fail_on_pragma_after: int):
        """Stand in for `aiosqlite.connect`, failing a PRAGMA after N connections."""
        import aiosqlite

        opened: list[object] = []
        closed: list[object] = []

        class FakeConn:
            def __init__(self, index: int):
                self.index = index

            async def execute(self, sql, *args):
                if self.index >= fail_on_pragma_after and sql.startswith("PRAGMA"):
                    raise OSError("disk I/O error")

            async def close(self):
                closed.append(self)

        async def fake_connect(*args, **kwargs):
            conn = FakeConn(len(opened))
            opened.append(conn)
            return conn

        monkeypatch.setattr(aiosqlite, "connect", fake_connect)
        return opened, closed

    async def test_a_failing_pragma_closes_the_connection_it_opened(
        self, sqlite_path, monkeypatch
    ):
        opened, closed = self._flaky_aiosqlite(monkeypatch, fail_on_pragma_after=0)
        db = rowform.SqliteEngine(sqlite_path, min_size=1, max_size=2)
        with pytest.raises(OSError, match="disk I/O error"):
            await db.connect()
        assert len(opened) == 1
        assert closed == opened, "the connection whose PRAGMA failed was left open"

    async def test_a_failure_part_way_closes_the_earlier_connections(
        self, sqlite_path, monkeypatch
    ):
        opened, closed = self._flaky_aiosqlite(monkeypatch, fail_on_pragma_after=2)
        db = rowform.SqliteEngine(sqlite_path, min_size=4, max_size=4)
        with pytest.raises(OSError, match="disk I/O error"):
            await db.connect()
        assert len(opened) == 3  # two good, one that failed its PRAGMA
        assert len(closed) == 3, "connections opened before the failure leaked"
        assert db.pool is None


class TestExports:
    def test_asyncpg_engine_is_exported(self):
        assert "AsyncpgEngine" in rowform.__all__
        assert rowform.AsyncpgEngine.__name__ == "AsyncpgEngine"

    def test_star_import_reaches_it(self):
        namespace: dict = {}
        exec("from rowform import *", namespace)  # noqa: S102 -- exercising __all__
        assert "AsyncpgEngine" in namespace

    def test_importing_rowform_does_not_import_asyncpg(self):
        """The reason `AsyncpgEngine` is served by `__getattr__` at all: the
        dialect module imports the driver, so exporting it eagerly would make
        asyncpg a hard dependency of `import rowform`. A subprocess, because the
        test session has already imported asyncpg elsewhere."""
        code = "import rowform, sys; print('asyncpg' in sys.modules)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "False"

    def test_everything_in_all_resolves(self):
        for name in rowform.__all__:
            assert getattr(rowform, name) is not None, name
