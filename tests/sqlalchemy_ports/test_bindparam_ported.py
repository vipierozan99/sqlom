"""Ported from SQLAlchemy's test/sql/test_query.py and test/sql/test_text.py
(SQLAlchemy 2.0.51) — `bindparam()` as a genuinely deferred, reusable
parameter: a statement compiled once, with different values supplied per
call afterward, rather than sqlom's usual "every value binds immediately at
build time" (see `BindParameter`'s docstring in sqlom/expr.py).

Skipped:

* `test_bindparam_detection`'s exhaustive escaping/edge-case matrix
  (backslash-escaped colons, `$`/`_` inside identifier-like tokens, colons
  directly adjacent with no separator) — SQLAlchemy's real token detector is
  considerably more conservative than sqlom's (a `:` not itself preceded by
  another `:`, full stop). sqlom's is deliberately the simpler rule stated
  in `TextClause`'s docstring, not a byte-for-byte match of SQLAlchemy's;
  only the one practical case that actually matters in real SQL — Postgres
  `::` cast syntax not being mistaken for a bindparam — is ported (already
  covered in test_text_ported.py).
* `test_select_from_bindparam`'s `TypeDecorator` — no type system.
* `test_typing_construction` (type_= inference) — see test_text_ported.py's
  module docstring for why.

`bind_params()`/`has_deferred_params()` and the engine/transaction
`**overrides` integration have no SQLAlchemy test to port directly against
(SQLAlchemy resolves bindparams as part of `Connection.execute()`, an
entirely different execution model) — those are sqlom-original tests,
marked as such below.
"""

import pytest

from sqlom import (
    Insert,
    Query,
    bind_params,
    bindparam,
    has_deferred_params,
    text,
)
from tests.conftest import Author, Book


class TestDeferredRendering:
    def test_renders_a_normal_placeholder_and_defers_the_value(self):
        sql, params = Query(Author).where(Author.id == bindparam("id")).to_sql()
        assert sql == "SELECT id, name, active FROM t_authors WHERE id = ?"
        assert has_deferred_params(params)

    def test_bind_params_resolves_the_override(self):
        _, params = Query(Author).where(Author.id == bindparam("id")).to_sql()
        assert bind_params(params, id=1) == (1,)
        assert bind_params(params, id=2) == (2,)

    def test_a_default_value_is_used_when_not_overridden(self):
        _, params = Query(Author).where(Author.id == bindparam("id", 5)).to_sql()
        assert bind_params(params) == (5,)

    def test_a_default_value_is_still_overridable(self):
        # Matches SQLAlchemy: a value given at construction is a default,
        # not a fixed value — bindparam("id", 5) can still be overridden.
        _, params = Query(Author).where(Author.id == bindparam("id", 5)).to_sql()
        assert bind_params(params, id=9) == (9,)

    def test_missing_value_raises_a_clear_error(self):
        _, params = Query(Author).where(Author.id == bindparam("id")).to_sql()
        with pytest.raises(ValueError, match="missing value.*id"):
            bind_params(params)

    def test_has_deferred_params_is_false_with_no_bindparam_at_all(self):
        _, params = Query(Author).where(Author.id == 1).to_sql()
        assert not has_deferred_params(params)


# Ported from test/sql/test_query.py::QueryTest.test_repeated_bindparams (SQLAlchemy 2.0.51)
class TestRepeatedBindparam:
    def test_the_same_bindparam_used_twice_resolves_from_one_override(self):
        from sqlom import and_, or_

        param = bindparam("name")
        query = Query(Author).where(
            or_(Author.name == param, Author.name == param)
        )
        sql, params = query.to_sql()
        assert sql == (
            "SELECT id, name, active FROM t_authors WHERE "
            "(name = ? OR name = ?)"
        )
        assert bind_params(params, name="ada") == ("ada", "ada")

    def test_end_to_end_reusing_the_same_compiled_query_with_different_values(
        self, db
    ):
        # The actual premise: one compiled query, executed twice, with a
        # different bound value each time — no re-rendering in between.
        query = Query(Author.name).where(Author.id == bindparam("id"))
        sql, params = query.to_sql()

        first = db.execute(sql, bind_params(params, id=1)).fetchall()
        assert first == [("ada",)]

        second = db.execute(sql, bind_params(params, id=2)).fetchall()
        assert second == [("brian",)]


class TestCachingIsGenuinelyOnce:
    """sqlom-original tests (no SQLAlchemy equivalent) — proving the actual
    premise of a deferred parameter: the SQL is compiled once, and only the
    params tuple changes per resolution."""

    def test_to_sql_returns_the_same_cached_objects_on_repeat_calls(self):
        query = Query(Author).where(Author.id == bindparam("id"))
        sql1, params1 = query.to_sql()
        sql2, params2 = query.to_sql()
        assert sql1 is sql2
        assert params1 is params2

    def test_the_same_cached_params_resolve_differently_each_time(self):
        query = Query(Author).where(Author.id == bindparam("id"))
        _, params = query.to_sql()
        assert bind_params(params, id=1) == (1,)
        assert bind_params(params, id=2) == (2,)
        assert bind_params(params, id=3) == (3,)


class TestInsertRowValuesFix:
    """sqlom-original test (no SQLAlchemy equivalent) — regression test for
    the gap found while designing bindparam(): Insert._render()'s row loop
    previously treated every value as an opaque literal, so an Expression
    (bindparam() included) given as a VALUES value was spliced straight into
    the params tuple as an object rather than rendered."""

    def test_bindparam_as_an_insert_value(self):
        stmt = Insert(Author).values(name=bindparam("nm", "default"))
        sql, params = stmt.to_sql()
        assert sql == "INSERT INTO t_authors (name) VALUES (?)"
        assert bind_params(params) == ("default",)
        assert bind_params(params, nm="ada") == ("ada",)

    def test_bindparam_alongside_an_ordinary_insert_value(self):
        stmt = Insert(Book).values(
            author_id=1, title=bindparam("title")
        )
        sql, params = stmt.to_sql(placeholder="$")
        assert sql == "INSERT INTO t_books (author_id, title) VALUES ($1, $2)"
        assert bind_params(params, title="new book") == (1, "new book")


# Ported from test/sql/test_text.py::BindParamTest.test_positional_plus_kw (SQLAlchemy 2.0.51)
class TestBindparamWithText:
    def test_positional_bindparam_object_in_text_bindparams(self):
        clause = text("select * from foo where lala=:bar and hoho=:whee")
        clause = clause.bindparams(bindparam("bar", 4), whee=7)
        sql, params = clause.to_sql("?")
        assert sql == "select * from foo where lala=? and hoho=?"
        assert params == (4, 7)


class TestOutOfScope:
    """Explicitly documented, not silently discovered: bindparam() cannot
    back LIMIT/OFFSET, since Query.limit()/.offset() reject anything that
    isn't a plain int immediately."""

    def test_limit_rejects_a_bindparam(self):
        with pytest.raises(TypeError, match="takes an int"):
            Query(Author).limit(bindparam("n"))

    def test_offset_rejects_a_bindparam(self):
        with pytest.raises(TypeError, match="takes an int"):
            Query(Author).offset(bindparam("n"))
