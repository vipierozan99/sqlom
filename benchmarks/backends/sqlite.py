"""Ephemeral sqlite backend: a temp-file database, DDL, and seed data.

Independent of rowform's own `SqliteEngine` (`rowform/sqlite_engine.py`) — this
provisions the on-disk database that *any* contender (rowform, raw sqlite3,
SQLAlchemy) reads from, matching PLAN.md §7's tier-3 "real driver" contract.
"""

from __future__ import annotations

import sqlite3
import tempfile
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
        tmpdir = tempfile.TemporaryDirectory(prefix="rowform-bench-")
        instance = cls(path=str(Path(tmpdir.name) / "bench.sqlite3"), _tmpdir=tmpdir)
        instance._seed(shape, rows)
        return instance

    def _seed(self, shape: str, rows: int) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
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
            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
