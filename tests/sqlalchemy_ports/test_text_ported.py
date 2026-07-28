"""Ported from SQLAlchemy's test/sql/test_text.py (SQLAlchemy 2.0.51) —
`text()` as a raw SQL fragment, adapted to sqlom.

sqlom's `text()` differs from SQLAlchemy's in one structural way worth
stating up front: SQLAlchemy's `text()` participates in its clause-element
graph (it can be a FROM source via `select_from(text(...))`, contributes to
`_result_columns`, etc.) — sqlom's is deliberately much smaller: a
`Predicate`/`Expression` usable as a whole WHERE clause or a plain value,
with `sources()` returning nothing (see `TextClause`'s docstring). So this
file ports the SELECT-composition and bindparam tests that map onto that
narrower shape, and skips:

* `test_text_adds_to_result_map` — sqlom has no `_result_columns`/type-map
  machinery to inspect.
* `test_select_composition_one`/`_two`'s `select_from(text(...))` form —
  sqlom's `select_from()` (see test_select_ported.py) takes models/aliases,
  not raw text, since a source needs `__columns__` for everything else in
  the query to resolve columns against. `literal_column()` in a SELECT list
  and `text()` in WHERE are ported separately, just not combined with a
  text-based FROM.
* `test_typing_construction`/the `type_=`/`checkparams`/`literal_binds=`
  machinery — sqlom's `bindparams()` takes plain values, no type objects,
  and always binds as a parameter (never inlines a literal into the SQL
  text) — see cast()'s docstring for why sqlom has no type-object system.
* Positional `bindparam(...)` arguments to `.bindparams()` — ported instead
  in test_bindparam_ported.py once `bindparam()` itself exists (the next
  feature added after this one); this file only exercises the `**kwargs`
  form, which needs no `bindparam()` class of its own.
"""

import pytest

from sqlom import Query, text
from tests.conftest import Author, Book


# Ported from test/sql/test_text.py::CompileTest.test_basic (SQLAlchemy 2.0.51)
def test_basic():
    clause, params = text("select * from foo where lala = bar").to_sql("?")
    assert clause == "select * from foo where lala = bar"
    assert params == ()


# Ported from test/sql/test_text.py::SelectCompositionTest.test_select_composition_two (SQLAlchemy 2.0.51)
def test_select_composition_with_add_columns_and_two_text_where_clauses():
    # literal_column() stands in for SQLAlchemy's bare column("column2") —
    # unlike SQLAlchemy, sqlom always needs a real FROM table (there is no
    # bare `select()` with nothing implying one), so the first column is a
    # real one (Author.id), which also supplies t_authors as the source.
    from sqlom import literal_column

    stmt = (
        Query(Author.id)
        .add_columns(literal_column("column2"))
        .where(text("column1=12"))
        .where(text("column2=19"))
        .order_by(literal_column("column1"))
    )
    sql, params = stmt.to_sql()
    assert sql == (
        "SELECT id, column2 FROM t_authors WHERE column1=12 AND "
        "column2=19 ORDER BY column1"
    )
    assert params == ()


# Ported from test/sql/test_text.py::BindParamTest.test_kw (SQLAlchemy 2.0.51)
def test_bindparams_kw_form():
    clause = text("select * from foo where lala=:bar and hoho=:whee")
    clause = clause.bindparams(bar=4, whee=7)
    sql, params = clause.to_sql("?")
    assert sql == "select * from foo where lala=? and hoho=?"
    assert params == (4, 7)


# sqlom-original test (no SQLAlchemy equivalent)
def test_bindparams_can_be_supplied_across_two_calls():
    clause = text("select * from foo where lala=:bar and hoho=:whee")
    clause = clause.bindparams(bar=4)
    clause = clause.bindparams(whee=7)
    sql, params = clause.to_sql("?")
    assert sql == "select * from foo where lala=? and hoho=?"
    assert params == (4, 7)


# sqlom-original test (no SQLAlchemy equivalent)
def test_missing_bindparam_raises_a_clear_error():
    with pytest.raises(ValueError, match=r"references :whee"):
        text("select * from foo where lala=:bar and hoho=:whee").bindparams(
            bar=4
        ).to_sql("?")


# sqlom-original test (no SQLAlchemy equivalent) — the one practical case
# from SQLAlchemy's test_bindparam_detection that actually matters in real
# SQL; see this file's module docstring for why the rest of that test's
# escaping-edge-case matrix isn't ported.
def test_double_colon_cast_syntax_is_not_mistaken_for_a_bindparam():
    # Postgres cast syntax (col::type) must survive untouched — the token
    # pattern requires a `:` not itself preceded by one.
    clause, params = text("id::text = :val").bindparams(val="7").to_sql("?")
    assert clause == "id::text = ?"
    assert params == ("7",)


# sqlom-original test (no SQLAlchemy equivalent)
def test_text_as_a_where_clause_in_a_real_query():
    sql, params = Query(Author).where(text("id = 1")).to_sql()
    assert sql == "SELECT id, name, active FROM t_authors WHERE id = 1"
    assert params == ()


# sqlom-original test (no SQLAlchemy equivalent)
def test_text_combined_with_an_ordinary_where_clause_numbers_params_in_order():
    # The one case that would catch a placeholder-ordering bug: a text()
    # bindparam and a normal where() clause in the same statement.
    query = (
        Query(Book)
        .where(text("author_id = :aid").bindparams(aid=1))
        .where(Book.id > 5)
    )
    sql, params = query.to_sql(placeholder="$")
    assert sql == (
        "SELECT id, author_id, title FROM t_books WHERE author_id = $1 "
        "AND id > $2"
    )
    assert params == (1, 5)


# sqlom-original test (no SQLAlchemy equivalent)
def test_text_used_as_a_selected_value():
    # A Predicate is also a plain Expression (Predicate extends
    # Expression[bool]), so text() composes as a value too, not just a
    # whole WHERE clause.
    sql, params = Query(Book.id, text("1")).to_sql()
    assert sql == "SELECT id, 1 FROM t_books"
    assert params == ()


# sqlom-original test (no SQLAlchemy equivalent)
def test_text_end_to_end(db):
    query = Query(Author).where(text("active = 1")).order_by(Author.id)
    sql, params = query.to_sql()
    rows = db.execute(sql, params).fetchall()
    assert [row[1] for row in rows] == ["ada", "brian", "dan"]

    parametrized = Query(Author).where(
        text("id = :id").bindparams(id=1)
    )
    sql, params = parametrized.to_sql()
    rows = db.execute(sql, params).fetchall()
    assert rows == [(1, "ada", 1)]
