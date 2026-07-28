"""Dialect-aware validation for four features that were previously
documentation-only "Postgres-only" claims with zero code-level enforcement:
`ilike()`, `Query.with_for_update()`, `Delete.using()`, and
`on_conflict_do_nothing()`/`on_conflict_do_update()`'s `constraint=`.

Opt-in only: every check here fires *only* when an explicit `dialect=` is
passed to `to_sql()` and that dialect doesn't support the feature. With no
dialect (the long-standing default, exercised by every pre-existing test in
this suite), behaviour is completely unchanged — these four features already
had their own dialect-less tests elsewhere (tests/test_expressions.py,
tests/test_engines_pg.py, tests/test_update_from.py, tests/test_upsert.py);
this file only adds the new, opt-in raise.

sqlom-original tests (no SQLAlchemy equivalent) — SQLAlchemy's own dialect
system compiles each of these differently per dialect rather than raising
(e.g. ILIKE against a non-Postgres dialect silently becomes `LOWER(x) LIKE
LOWER(y)`); sqlom has no per-dialect SQL rewriting, only a yes/no check, so
there is no directly corresponding upstream test to port.
"""

import pytest

from sqlom import POSTGRES, SQLITE, Column, Delete, Insert, ModelMeta, Query
from tests.conftest import Author, Book


class Tagged(metaclass=ModelMeta):
    __tablename__ = "t_tagged_dv"

    id = Column(int)
    name = Column(str)


class TestIlike:
    # sqlom-original test (no SQLAlchemy equivalent)
    def test_raises_on_sqlite(self):
        with pytest.raises(ValueError, match="ilike\\(\\) is not supported on sqlite"):
            Query(Author).where(Author.name.ilike("a%")).to_sql(dialect=SQLITE)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_works_on_postgres(self):
        sql, params = (
            Query(Author).where(Author.name.ilike("a%")).to_sql(dialect=POSTGRES)
        )
        assert sql == "SELECT id, name, active FROM t_authors WHERE name ILIKE $1"
        assert params == ("a%",)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_unchanged_with_no_dialect(self):
        sql, params = Query(Author).where(Author.name.ilike("a%")).to_sql()
        assert sql == "SELECT id, name, active FROM t_authors WHERE name ILIKE ?"
        assert params == ("a%",)


class TestWithForUpdate:
    # sqlom-original test (no SQLAlchemy equivalent)
    def test_raises_on_sqlite(self):
        with pytest.raises(
            ValueError, match="with_for_update\\(\\) is not supported on sqlite"
        ):
            Query(Author).with_for_update().to_sql(dialect=SQLITE)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_works_on_postgres(self):
        sql, _ = Query(Author).with_for_update().to_sql(dialect=POSTGRES)
        assert sql == "SELECT id, name, active FROM t_authors FOR UPDATE"

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_unchanged_with_no_dialect(self):
        sql, _ = Query(Author).with_for_update().to_sql()
        assert sql == "SELECT id, name, active FROM t_authors FOR UPDATE"


class TestDeleteUsing:
    def _stmt(self):
        return Delete(Book).using(Author).where(Author.id == Book.author_id)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_raises_on_sqlite(self):
        with pytest.raises(ValueError, match="using\\(\\) is not supported on sqlite"):
            self._stmt().to_sql(dialect=SQLITE)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_works_on_postgres(self):
        sql, _ = self._stmt().to_sql(dialect=POSTGRES)
        assert sql == (
            "DELETE FROM t_books USING t_authors WHERE t_authors.id = "
            "t_books.author_id"
        )

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_unchanged_with_no_dialect(self):
        sql, _ = self._stmt().to_sql()
        assert sql == (
            "DELETE FROM t_books USING t_authors WHERE t_authors.id = "
            "t_books.author_id"
        )


class TestOnConflictConstraint:
    def _stmt(self):
        return Insert(Tagged).values(name="x").on_conflict_do_nothing(
            constraint="tagged_name_key"
        )

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_raises_on_sqlite(self):
        with pytest.raises(ValueError, match="constraint= is not supported on sqlite"):
            self._stmt().to_sql(dialect=SQLITE)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_works_on_postgres(self):
        sql, _ = self._stmt().to_sql(dialect=POSTGRES)
        assert sql == (
            "INSERT INTO t_tagged_dv (name) VALUES ($1) "
            "ON CONFLICT ON CONSTRAINT tagged_name_key DO NOTHING"
        )

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_unchanged_with_no_dialect(self):
        sql, _ = self._stmt().to_sql()
        assert sql == (
            "INSERT INTO t_tagged_dv (name) VALUES (?) "
            "ON CONFLICT ON CONSTRAINT tagged_name_key DO NOTHING"
        )

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_index_elements_form_is_unaffected_on_sqlite(self):
        # Only constraint= is dialect-gated — naming the column(s) directly
        # works everywhere, on both dialects.
        stmt = Insert(Tagged).values(name="x").on_conflict_do_nothing(Tagged.name)
        sql, _ = stmt.to_sql(dialect=SQLITE)
        assert sql == "INSERT INTO t_tagged_dv (name) VALUES (?) ON CONFLICT (name) DO NOTHING"
