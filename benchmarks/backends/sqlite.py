"""Ephemeral sqlite backend: a temp-file database, DDL, and seed data.

Independent of rowform's own `SqliteEngine` (`rowform/sqlite_engine.py`) — this
provisions the on-disk database that *any* contender (rowform, raw sqlite3,
SQLAlchemy) reads from, matching PLAN.md §7's tier-3 "real driver" contract.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.harness import seed as seed_module


@dataclass(slots=True)
class EphemeralSqlite:
    path: str
    _tmpdir: TemporaryDirectory | None = field(default=None, repr=False)

    @classmethod
    def create(cls, shape: str, rows: int) -> EphemeralSqlite:
        return cls._provision([shape], rows)

    @classmethod
    def create_all_shapes(cls, rows: int) -> EphemeralSqlite:
        """Seed every shape into one file — for `service/app.py`, whose
        routes cover both shapes unconditionally (it isn't parameterized by
        `--shape` the way `bench micro`'s registry-driven paths are), so
        either one being un-seeded would 500 the moment it's hit."""
        return cls._provision(seed_module.SHAPES, rows)

    @classmethod
    def _provision(cls, shapes: Sequence[str], rows: int) -> EphemeralSqlite:
        tmpdir = tempfile.TemporaryDirectory(prefix="rowform-bench-")
        instance = cls(path=str(Path(tmpdir.name) / "bench.sqlite3"), _tmpdir=tmpdir)
        conn = sqlite3.connect(instance.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            for shape in shapes:
                instance._seed(conn, shape, rows)
            conn.commit()
        finally:
            conn.close()
        return instance

    def _seed(self, conn: sqlite3.Connection, shape: str, rows: int) -> None:
        for statement in seed_module.ddl_for(shape, "sqlite"):
            conn.execute(statement)
        if shape == "flat":
            data = seed_module.flat_rows(rows)
            conn.executemany(
                "INSERT INTO users VALUES (?, ?, ?, ?)",
                [(i, name, email, int(active)) for i, name, email, active in data],
            )
        else:
            authors, posts = seed_module.join_rows(rows)
            conn.executemany(
                "INSERT INTO j_authors VALUES (?, ?, ?, ?)",
                [(i, name, email, int(active)) for i, name, email, active in authors],
            )
            conn.executemany(
                "INSERT INTO j_posts VALUES (?, ?, ?, ?, ?)",
                [(i, author_id, title, score, int(published))
                 for i, author_id, title, score, published in posts],
            )

    def close(self) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
