"""The `Dialect` abstraction itself: `SQLITE`/`POSTGRES` singletons, their
`is_distinct_from_sql()` spellings, and `current_dialect()`'s contextvar
behaviour outside of any render.

rowform-original tests (no SQLAlchemy equivalent) — this module doesn't exist
in SQLAlchemy, whose dialect system is a full compiler/visitor hierarchy.
rowform's is deliberately a much smaller "common core + a few overridable
flags/methods" — see rowform/dialects.py's module docstring for why.
"""

from rowform import POSTGRES, SQLITE, Dialect, PostgresDialect, SqliteDialect
from rowform.dialects import current_dialect


class TestSingletons:
    # rowform-original test (no SQLAlchemy equivalent)
    def test_sqlite_and_postgres_are_shared_instances(self):
        assert SqliteDialect() is not SQLITE
        assert PostgresDialect() is not POSTGRES

    # rowform-original test (no SQLAlchemy equivalent)
    def test_names_and_default_placeholders(self):
        assert SQLITE.name == "sqlite"
        assert SQLITE.default_placeholder == "?"
        assert POSTGRES.name == "postgres"
        assert POSTGRES.default_placeholder == "$"

    # rowform-original test (no SQLAlchemy equivalent)
    def test_generic_base_dialect_is_permissive(self):
        base = Dialect()
        assert base.supports_ilike
        assert base.supports_for_update
        assert base.supports_delete_using
        assert base.supports_on_conflict_constraint


class TestSupportsFlags:
    # rowform-original test (no SQLAlchemy equivalent)
    def test_postgres_supports_everything(self):
        assert POSTGRES.supports_ilike
        assert POSTGRES.supports_for_update
        assert POSTGRES.supports_delete_using
        assert POSTGRES.supports_on_conflict_constraint

    # rowform-original test (no SQLAlchemy equivalent)
    def test_sqlite_supports_none_of_the_postgres_only_features(self):
        assert not SQLITE.supports_ilike
        assert not SQLITE.supports_for_update
        assert not SQLITE.supports_delete_using
        assert not SQLITE.supports_on_conflict_constraint


class TestIsDistinctFromSql:
    # Ported from test/sql/test_operators.py::IsDistinctFromTest.test_is_distinct_from_postgresql (SQLAlchemy 2.0.51)
    def test_postgres_uses_the_ansi_keyword(self):
        assert POSTGRES.is_distinct_from_sql("a", "b", False) == "a IS DISTINCT FROM b"
        assert (
            POSTGRES.is_distinct_from_sql("a", "b", True)
            == "a IS NOT DISTINCT FROM b"
        )

    # Ported from test/sql/test_operators.py::IsDistinctFromTest.test_is_distinct_from_sqlite (SQLAlchemy 2.0.51)
    def test_sqlite_uses_is_is_not(self):
        # sqlite has no IS DISTINCT FROM keyword at all; its own IS/IS NOT
        # are already null-safe, so they are the direct equivalent —
        # inverted, since "IS" means "same" and "distinct" means "different".
        assert SQLITE.is_distinct_from_sql("a", "b", False) == "a IS NOT b"
        assert SQLITE.is_distinct_from_sql("a", "b", True) == "a IS b"


class TestCurrentDialect:
    # rowform-original test (no SQLAlchemy equivalent)
    def test_none_outside_any_render(self):
        assert current_dialect() is None
