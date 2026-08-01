"""Ephemeral sqlite backend: a temp-file database, DDL, and seed data.

Independent of any contender's connection code — this provisions the on-disk
database that *any* of them (rowform, raw sqlite3, SQLAlchemy) reads from, so
every contender runs against a real driver.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy.dialects.sqlite import aiosqlite

from benchmarks.harness import seed as seed_module

_DIALECT = aiosqlite.dialect()


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
        """DDL and inserts both come from the shape's own models.

        The insert used to be a hand-written `INSERT INTO users VALUES (?,?,?,?)`
        per shape, with a hand-written `int(active)` to turn a bool into what
        sqlite stores. Compiling the statement instead means the bind processors
        do that — which matters far more for `wide`, where a `Decimal`, a `UUID`
        and a `datetime` cannot be bound to sqlite at all without them.
        """
        for statement in seed_module.ddl_for(shape, "sqlite"):
            conn.execute(statement)
        for table, data in seed_module.rows_for(shape, rows):
            conn.executemany(
                seed_module.insert_sql(table, _DIALECT),
                seed_module.bound_rows(table, data, _DIALECT),
            )

    def close(self) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
