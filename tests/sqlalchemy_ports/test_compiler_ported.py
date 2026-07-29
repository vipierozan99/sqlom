"""Ported from SQLAlchemy's test/sql/test_compiler.py (SQL-generation subset
relevant to query building), adapted to rowform.

SQLAlchemy's `test_compiler.py` is ~8200 lines and covers far more than SELECT
generation: DDL/CREATE TABLE, custom type compilation, per-dialect quoting for a
dozen backends, sequences and column defaults, ORM-flavoured label styles, and
the raw bind-parameter machinery behind all of it. rowform has none of that surface
— no Table/MetaData/DDL, no reflection, no custom types, and only sqlite +
postgres to worry about — so most of the file does not apply.

What follows targets the same *ideas* `SelectTest` in that file exercises
(`test_table_select`, `test_where_multiple`, `test_conjunctions`,
`test_nested_conjunctions_short_circuit`, `test_joins`, `test_full_outer_join`,
`test_from_subquery`, `test_where_subquery`, `test_exists`, `test_scalar_select`,
`test_alias`, `test_distinct`, `test_limit_offset`, `test_calculated_columns`,
`test_compound_selects`, `test_orderby_groupby`), re-expressed against rowform's
`Query`/`Alias`/`Subquery`/predicate API and asserted against exact `.to_sql()`
output, rowform house style.

Skipped entirely, and why:
  * DDLTest, SchemaTest — CREATE TABLE / constraint / index DDL. rowform has no
    schema management at all.
  * CoercionTest, ResultMapTest — SQLAlchemy's internal type-coercion and
    result-column-name mapping machinery; there is no equivalent layer here.
  * test_cast*, custom-type sections — Numeric/Enum/etc. type compilation.
    rowform columns carry a plain `py_type`, not a compiled SQL type.
  * Dialect-specific quoting of reserved identifiers, Oracle/MSSQL/Firebird
    spellings, `test_paramstyles`, `test_anon_param_name_on_keys` — rowform only
    ever renders sqlite `?`, psycopg `%s`, or asyncpg `$n`, with no identifier
    quoting or anonymous-label generation.
  * LABEL_STYLE_* / `test_use_labels*` / `test_dupe_columns*` /
    `test_overlapping_labels*` — SQLAlchemy's auto-disambiguating label styles
    for ORM row mapping. rowform has no such mode; a caller names collisions away
    with `.label()`.
  * `test_for_update`, `test_hints`, `test_statement_hints`, `prefix_with` —
    locking clauses, optimizer hints and statement prefixes: no equivalent API.
  * BindParameterTest, CrudParamOverlapTest — SQLAlchemy's named/expanding
    bindparam mechanics and INSERT/UPDATE param-overlap handling; rowform binds
    everything positionally and DML is out of scope for this file (see
    test_dml.py).
  * KwargPropagationTest, ExecutionOptionsTest, StringifySpecialTest,
    UnsupportedTest, OmitFromStatementsTest — compiler-internals plumbing with
    no user-facing rowform equivalent.
  * `test_over_framespec`/`test_over_invalid_framespecs`/`test_over_within_group`
    and most window-function detail — already covered thoroughly in
    tests/test_expressions.py, not re-ported here.
  * CTE-specific compilation — already covered in tests/test_ctes.py.
  * `select(table1, table2)` with no join (an implicit cross join via a comma
    in FROM) — rowform's `join()` always requires an explicit ON clause linking
    the new source to one already in the query; there is no way to express an
    unconditional cross join, so that shape of test_table_select has no port.
"""

import pytest

from rowform import (
    Alias,
    Query,
    and_,
    case,
    cast,
    count,
    exists,
    false,
    literal,
    literal_column,
    not_,
    null,
    or_,
    true,
)
from tests.conftest import Author, Book, Tag

# --------------------------------------------------------------------------
# Basic SELECT rendering — test_table_select
# --------------------------------------------------------------------------


class TestBasicSelect:
    # Ported from test/sql/test_compiler.py::SelectTest.test_table_select (SQLAlchemy 2.0.51)
    def test_select_all_columns_of_one_model(self):
        sql, params = Query(Book).to_sql()
        assert sql == "SELECT id, author_id, title FROM t_books"
        assert params == ()

    # Ported from test/sql/test_compiler.py::SelectTest.test_table_select (SQLAlchemy 2.0.51)
    def test_select_specific_columns(self):
        sql, params = Query(Book.id, Book.title).to_sql()
        assert sql == "SELECT id, title FROM t_books"
        assert params == ()

    # Ported from test/sql/test_compiler.py::SelectTest.test_joins (SQLAlchemy 2.0.51)
    def test_select_mixes_model_and_column_across_a_join(self):
        sql, _ = (Query(Book, Author.name)
                  .join(Author, Book.author_id == Author.id)
                  .to_sql())
        assert sql == (
            "SELECT t_books.id, t_books.author_id, t_books.title, t_authors.name "
            "FROM t_books JOIN t_authors ON t_books.author_id = t_authors.id"
        )


# --------------------------------------------------------------------------
# WHERE clause composition — test_where_multiple, plus operator/IN/LIKE/NULL
# rendering that test_compiler.py spreads across several sections
# --------------------------------------------------------------------------


class TestWhereClause:
    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_single_condition(self):
        sql, params = Query(Book).where(Book.title == "compilers").to_sql()
        assert sql == "SELECT id, author_id, title FROM t_books WHERE title = ?"
        assert params == ("compilers",)

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_multiple_where_calls_and_without_extra_parens(self):
        sql, params = (Query(Book).where(Book.id > 1).where(Book.author_id == 2)
                       .to_sql(placeholder="$"))
        assert sql.endswith("WHERE id > $1 AND author_id = $2")
        assert params == (1, 2)

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_comparison_operators_render_in_where(self):
        cases = [
            (Book.id == 5, "="),
            (Book.id != 5, "!="),
            (Book.id > 5, ">"),
            (Book.id >= 5, ">="),
            (Book.id < 5, "<"),
            (Book.id <= 5, "<="),
        ]
        for condition, op in cases:
            sql, params = Query(Book).where(condition).to_sql()
            assert sql == f"SELECT id, author_id, title FROM t_books WHERE id {op} ?"  # noqa: S608 -- expected-SQL literal for comparison, not executed
            assert params == (5,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_null_equality_renders_is_null(self):
        sql, params = Query(Book).where(Book.title == None).to_sql()
        assert sql.endswith("WHERE title IS NULL")
        assert params == ()

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_null_inequality_renders_is_not_null(self):
        sql, params = Query(Book).where(Book.title != None).to_sql()
        assert sql.endswith("WHERE title IS NOT NULL")
        assert params == ()

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_like_pattern(self):
        sql, params = Query(Book).where(Book.title.like("comp%")).to_sql()
        assert sql.endswith("WHERE title LIKE ?")
        assert params == ("comp%",)

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_multiple (SQLAlchemy 2.0.51)
    def test_in_list_of_values(self):
        sql, params = Query(Book).where(Book.id.in_([10, 11, 12])).to_sql(placeholder="$")
        assert sql.endswith("WHERE id IN ($1, $2, $3)")
        assert params == (10, 11, 12)

    # rowform-original test (no SQLAlchemy equivalent)
    def test_in_empty_list_renders_false_rather_than_invalid_sql(self):
        sql, params = Query(Book).where(Book.id.in_([])).to_sql()
        assert sql.endswith("WHERE FALSE")
        assert params == ()

    # rowform-original test (no SQLAlchemy equivalent)
    def test_between_equivalent_using_and_of_two_comparisons(self):
        # rowform has no dedicated BETWEEN operator; the equivalent is spelled with
        # and_() over two range comparisons.
        sql, params = (Query(Book).where(and_(Book.id >= 10, Book.id <= 12))
                       .to_sql(placeholder="$"))
        assert sql.endswith("WHERE (id >= $1 AND id <= $2)")
        assert params == (10, 12)


# --------------------------------------------------------------------------
# Boolean composition and its parenthesisation — test_conjunctions,
# test_nested_conjunctions_short_circuit
# --------------------------------------------------------------------------


class TestBooleanComposition:
    # Ported from test/sql/test_compiler.py::SelectTest.test_conjunctions (SQLAlchemy 2.0.51)
    def test_and_of_conditions(self):
        sql, params = (Query(Book).where(and_(Book.id > 1, Book.author_id == 2))
                       .to_sql(placeholder="$"))
        assert sql.endswith("WHERE (id > $1 AND author_id = $2)")
        assert params == (1, 2)

    # Ported from test/sql/test_compiler.py::SelectTest.test_conjunctions (SQLAlchemy 2.0.51)
    def test_or_of_conditions(self):
        sql, params = (Query(Book).where(or_(Book.id == 1, Book.id == 2))
                       .to_sql(placeholder="$"))
        assert sql.endswith("WHERE (id = $1 OR id = $2)")
        assert params == (1, 2)

    # Ported from test/sql/test_compiler.py::SelectTest.test_conjunctions (SQLAlchemy 2.0.51)
    def test_and_or_nesting_is_parenthesised(self):
        sql, params = (
            Query(Book)
            .where(and_(Book.author_id == 1, or_(Book.title == "a", Book.title == "b")))
            .to_sql(placeholder="$")
        )
        assert sql.endswith("WHERE (author_id = $1 AND (title = $2 OR title = $3))")
        assert params == (1, "a", "b")

    # Ported from test/sql/test_compiler.py::SelectTest.test_nested_conjunctions_short_circuit (SQLAlchemy 2.0.51)
    def test_same_operator_nesting_is_flattened(self):
        sql, params = (
            Query(Book).where(or_(Book.id == 1, or_(Book.id == 2, Book.id == 3)))
            .to_sql(placeholder="$")
        )
        assert sql.endswith("WHERE (id = $1 OR id = $2 OR id = $3)")
        assert params == (1, 2, 3)

    # Ported from test/sql/test_compiler.py::SelectTest.test_conjunctions (SQLAlchemy 2.0.51)
    def test_not_negation(self):
        sql, params = Query(Book).where(not_(Book.id == 1)).to_sql(placeholder="$")
        assert sql.endswith("WHERE NOT (id = $1)")
        assert params == (1,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_conjunctions (SQLAlchemy 2.0.51)
    def test_operator_overloads_and_or_invert(self):
        sql, _ = Query(Book).where((Book.id == 1) | (Book.id == 2)).to_sql(placeholder="$")
        assert sql.endswith("WHERE (id = $1 OR id = $2)")

        sql, _ = Query(Book).where(~(Book.id == 1)).to_sql(placeholder="$")
        assert sql.endswith("WHERE NOT (id = $1)")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_python_bitwise_precedence_trap_without_parens(self):
        # `&`/`|` bind tighter than the comparison operators in Python, so an
        # un-parenthesised `Book.id > 1 & Book.id < 9` does not compare and then
        # AND — it tries `1 & Book.id` first. ColumnExpr has no __and__/__rand__
        # (only Predicate does), so this fails loudly rather than silently
        # producing the wrong SQL. This is the same trap SQLAlchemy has and for
        # the same reason; the fix is always to parenthesise each comparison.
        with pytest.raises(TypeError):
            Book.id > 1 & Book.id < 9  # noqa: B015


# --------------------------------------------------------------------------
# JOIN / LEFT / FULL OUTER JOIN rendering — test_joins, test_full_outer_join
# --------------------------------------------------------------------------


class TestJoins:
    # Ported from test/sql/test_compiler.py::SelectTest.test_joins (SQLAlchemy 2.0.51)
    def test_inner_join(self):
        sql, _ = Query(Book, Tag).join(Tag, Tag.book_id == Book.id).to_sql()
        assert sql == (
            "SELECT t_books.id, t_books.author_id, t_books.title, "
            "t_tags.id, t_tags.book_id, t_tags.label "
            "FROM t_books JOIN t_tags ON t_tags.book_id = t_books.id"
        )

    # Ported from test/sql/test_compiler.py::SelectTest.test_joins (SQLAlchemy 2.0.51)
    def test_left_outer_join(self):
        sql, _ = Query(Book, Tag).outer_join(Tag, Tag.book_id == Book.id).to_sql()
        assert "LEFT OUTER JOIN t_tags ON t_tags.book_id = t_books.id" in sql

    # Ported from test/sql/test_compiler.py::SelectTest.test_full_outer_join (SQLAlchemy 2.0.51)
    def test_full_outer_join(self):
        sql, _ = Query(Author, Book).full_join(Book, Book.author_id == Author.id).to_sql()
        assert "FULL OUTER JOIN t_books ON t_books.author_id = t_authors.id" in sql

    # Ported from test/sql/test_compiler.py::SelectTest.test_joins (SQLAlchemy 2.0.51)
    def test_three_way_join_chain(self):
        sql, _ = (
            Query(Author, Book, Tag)
            .join(Book, Book.author_id == Author.id)
            .join(Tag, Tag.book_id == Book.id)
            .to_sql()
        )
        assert sql == (
            "SELECT t_authors.id, t_authors.name, t_authors.active, "
            "t_books.id, t_books.author_id, t_books.title, "
            "t_tags.id, t_tags.book_id, t_tags.label "
            "FROM t_authors "
            "JOIN t_books ON t_books.author_id = t_authors.id "
            "JOIN t_tags ON t_tags.book_id = t_books.id"
        )

    # Ported from test/sql/test_compiler.py::SelectTest.test_joins (SQLAlchemy 2.0.51)
    def test_join_combined_with_or_where(self):
        sql, params = (
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .where(or_(Author.name == "ada", Book.title == "compilers"))
            .to_sql(placeholder="$")
        )
        assert sql.endswith(
            "WHERE (t_authors.name = $1 OR t_books.title = $2)"
        )
        assert params == ("ada", "compilers")

    # Ported from test/sql/test_compiler.py::SelectTest.test_joins (SQLAlchemy 2.0.51)
    def test_self_join_distinguishes_both_sides_in_where(self):
        mgr = Alias(Author, "mgr")
        sql, params = (
            Query(Author, mgr)
            .join(mgr, Author.id == mgr.id)
            .where(and_(Author.active == True, mgr.name == "ada"))
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT t_authors.id, t_authors.name, t_authors.active, "
            "mgr.id, mgr.name, mgr.active "
            "FROM t_authors JOIN t_authors AS mgr ON t_authors.id = mgr.id "
            "WHERE (t_authors.active = $1 AND mgr.name = $2)"
        )
        assert params == (True, "ada")


# --------------------------------------------------------------------------
# Subqueries in FROM, IN, EXISTS and scalar comparisons — test_from_subquery,
# test_where_subquery, test_exists, test_scalar_select
# --------------------------------------------------------------------------


class TestSubqueries:
    # Ported from test/sql/test_compiler.py::SelectTest.test_from_subquery (SQLAlchemy 2.0.51)
    def test_subquery_in_from(self):
        sub = (Query(Book.author_id, count().label("n"))
               .group_by(Book.author_id)
               .subquery("counts"))
        sql, params = Query(sub.author_id, sub.n).to_sql()
        assert sql == (
            "SELECT author_id, n FROM "
            "(SELECT author_id, count(*) AS n FROM t_books GROUP BY author_id) "
            "AS counts"
        )
        assert params == ()

    # Ported from test/sql/test_compiler.py::SelectTest.test_from_subquery (SQLAlchemy 2.0.51)
    def test_subquery_column_referenced_in_where(self):
        sub = (Query(Book.author_id, count().label("n"))
               .group_by(Book.author_id)
               .subquery("counts"))
        sql, params = Query(sub.author_id, sub.n).where(sub.n > 1).to_sql(placeholder="$")
        assert sql == (
            "SELECT author_id, n FROM "
            "(SELECT author_id, count(*) AS n FROM t_books GROUP BY author_id) "
            "AS counts WHERE n > $1"
        )
        assert params == (1,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_where_subquery (SQLAlchemy 2.0.51)
    def test_in_subquery(self):
        sql, params = (
            Query(Book)
            .where(Book.id.in_(Query(Tag.book_id).where(Tag.label == "classic")))
            .to_sql(placeholder="$")
        )
        assert sql.endswith("WHERE id IN (SELECT book_id FROM t_tags WHERE label = $1)")
        assert params == ("classic",)

    # Ported from test/sql/test_compiler.py::SelectTest.test_exists (SQLAlchemy 2.0.51)
    def test_exists_subquery(self):
        sql, _ = (
            Query(Book)
            .where(exists(Query(Tag.id).correlate(Book).where(Tag.book_id == Book.id)))
            .to_sql()
        )
        assert sql == (
            "SELECT id, author_id, title FROM t_books WHERE EXISTS "
            "(SELECT t_tags.id FROM t_tags WHERE t_tags.book_id = t_books.id)"
        )

    # Ported from test/sql/test_compiler.py::SelectTest.test_exists (SQLAlchemy 2.0.51)
    def test_not_exists_subquery(self):
        sql, _ = (
            Query(Book)
            .where(~exists(Query(Tag.id).correlate(Book).where(Tag.book_id == Book.id)))
            .to_sql()
        )
        assert "NOT EXISTS (SELECT t_tags.id FROM t_tags WHERE t_tags.book_id = t_books.id)" in sql

    # Ported from test/sql/test_compiler.py::SelectTest.test_scalar_select (SQLAlchemy 2.0.51)
    def test_scalar_subquery_in_comparison(self):
        sql, _ = (
            Query(Book).where(Book.id > Query(count(Tag.id)).scalar_subquery())
            .to_sql()
        )
        assert sql == (
            "SELECT id, author_id, title FROM t_books "
            "WHERE id > (SELECT count(id) FROM t_tags)"
        )


# --------------------------------------------------------------------------
# CASE expressions — no direct test_compiler.py section covers CASE (its
# grammar is exercised via ORM/type tests there); ported against rowform's own
# `case()` since it is squarely SQL-generation.
# --------------------------------------------------------------------------


class TestCaseExpression:
    # rowform-original test (no SQLAlchemy equivalent)
    def test_case_when_then_else(self):
        sql, params = (
            Query(Book.id, case((Book.author_id == 1, "ada"), else_="other"))
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT id, CASE WHEN author_id = $1 THEN $2 ELSE $3 END FROM t_books"
        )
        assert params == (1, "ada", "other")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_case_multiple_whens_no_else(self):
        sql, params = (
            Query(Book.id, case((Book.author_id == 1, "a"), (Book.author_id == 2, "b")))
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT id, CASE WHEN author_id = $1 THEN $2 "
            "WHEN author_id = $3 THEN $4 END FROM t_books"
        )
        assert params == (1, "a", 2, "b")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_case_with_column_values(self):
        sql, params = (
            Query(Book.id, case((Book.author_id == 1, Book.title), else_=Book.id))
            .to_sql()
        )
        assert sql == (
            "SELECT id, CASE WHEN author_id = ? THEN title ELSE id END FROM t_books"
        )
        assert params == (1,)


# --------------------------------------------------------------------------
# Label and alias rendering — test_alias, test_label_comparison
# --------------------------------------------------------------------------


class TestLabelsAndAlias:
    # Ported from test/sql/test_compiler.py::SelectTest.test_label_comparison_one (SQLAlchemy 2.0.51)
    def test_label_renders_as_alias_in_the_select_list(self):
        sql, _ = Query(Book.title.label("book_title")).to_sql()
        assert sql == "SELECT title AS book_title FROM t_books"

    # Ported from test/sql/test_compiler.py::SelectTest.test_alias (SQLAlchemy 2.0.51)
    def test_table_alias_renames_the_from_clause(self):
        au = Alias(Author, "au")
        sql, _ = Query(au).to_sql()
        assert sql == "SELECT id, name, active FROM t_authors AS au"

    # Ported from test/sql/test_compiler.py::SelectTest.test_alias (SQLAlchemy 2.0.51)
    def test_alias_join_where_order_limit_together(self):
        mgr = Alias(Author, "mgr")
        sql, params = (
            Query(Author.name, mgr.name.label("manager_name"))
            .join(mgr, Author.id == mgr.id)
            .where(mgr.active == True)
            .order_by(Author.name)
            .limit(5)
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT t_authors.name, mgr.name AS manager_name "
            "FROM t_authors JOIN t_authors AS mgr ON t_authors.id = mgr.id "
            "WHERE mgr.active = $1 ORDER BY t_authors.name LIMIT $2"
        )
        assert params == (True, 5)

    # Ported from test/sql/test_compiler.py::SelectTest.test_alias_nesting_subquery (SQLAlchemy 2.0.51)
    def test_subquery_alias_exposes_labelled_output_columns(self):
        sub = Query(Book.id, Book.title.label("book_title")).subquery("bt")
        sql, _ = Query(sub.id, sub.book_title).to_sql()
        assert sql == (
            "SELECT id, book_title FROM "
            "(SELECT id, title AS book_title FROM t_books) AS bt"
        )


# --------------------------------------------------------------------------
# DISTINCT / LIMIT / OFFSET — test_distinct, test_limit_offset
# --------------------------------------------------------------------------


class TestDistinctLimitOffset:
    # Ported from test/sql/test_compiler.py::SelectTest.test_distinct (SQLAlchemy 2.0.51)
    def test_distinct_alone(self):
        sql, _ = Query(Book.author_id).distinct().to_sql()
        assert sql == "SELECT DISTINCT author_id FROM t_books"

    # Ported from test/sql/test_compiler.py::SelectTest.test_distinct (SQLAlchemy 2.0.51)
    def test_distinct_with_where(self):
        sql, params = (
            Query(Book.author_id).distinct().where(Book.title.like("c%"))
            .to_sql(placeholder="$")
        )
        assert sql == "SELECT DISTINCT author_id FROM t_books WHERE title LIKE $1"
        assert params == ("c%",)

    # Ported from test/sql/test_compiler.py::SelectTest.test_limit_offset (SQLAlchemy 2.0.51)
    def test_limit_only(self):
        sql, params = Query(Book).limit(3).to_sql(placeholder="$")
        assert sql.endswith("LIMIT $1")
        assert params == (3,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_limit_offset (SQLAlchemy 2.0.51)
    def test_offset_only(self):
        sql, params = Query(Book).offset(2).to_sql(placeholder="$")
        assert sql.endswith("OFFSET $1")
        assert params == (2,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_limit_offset (SQLAlchemy 2.0.51)
    def test_limit_and_offset_together(self):
        sql, params = (
            Query(Book).order_by(Book.id).limit(2).offset(1).to_sql(placeholder="$")
        )
        assert sql == "SELECT id, author_id, title FROM t_books ORDER BY id LIMIT $1 OFFSET $2"
        assert params == (2, 1)

    # Ported from test/sql/test_compiler.py::SelectTest.test_limit_offset (SQLAlchemy 2.0.51)
    def test_limit_zero_is_allowed(self):
        assert Query(Book).limit(0).to_sql()[1] == (0,)


# --------------------------------------------------------------------------
# Row locking — dialect/postgresql/test_compiler.py's test_for_update.
# Postgres-only: sqlite has no locking clause at all (same status as
# DELETE ... USING, README §11) — these check SQL generation only.
# --------------------------------------------------------------------------


class TestForUpdate:
    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_plain_for_update(self):
        sql, params = Query(Author).where(Author.id == 7).with_for_update().to_sql(
            placeholder="$"
        )
        assert sql == "SELECT id, name, active FROM t_authors WHERE id = $1 FOR UPDATE"
        assert params == (7,)

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_nowait(self):
        sql, _ = Query(Author).with_for_update(nowait=True).to_sql()
        assert sql.endswith("FOR UPDATE NOWAIT")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_skip_locked(self):
        sql, _ = Query(Author).with_for_update(skip_locked=True).to_sql()
        assert sql.endswith("FOR UPDATE SKIP LOCKED")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_read_renders_for_share(self):
        sql, _ = Query(Author).with_for_update(read=True).to_sql()
        assert sql.endswith("FOR SHARE")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_read_and_nowait(self):
        sql, _ = Query(Author).with_for_update(read=True, nowait=True).to_sql()
        assert sql.endswith("FOR SHARE NOWAIT")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_key_share_and_nowait_renders_for_no_key_update(self):
        sql, _ = Query(Author).with_for_update(key_share=True, nowait=True).to_sql()
        assert sql.endswith("FOR NO KEY UPDATE NOWAIT")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_key_share_and_read_and_nowait_renders_for_key_share(self):
        sql, _ = (
            Query(Author)
            .with_for_update(key_share=True, read=True, nowait=True)
            .to_sql()
        )
        assert sql.endswith("FOR KEY SHARE NOWAIT")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_of_a_single_source(self):
        sql, _ = Query(Author).with_for_update(of=Author).to_sql()
        assert sql.endswith("FOR UPDATE OF t_authors")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_of_several_sources_with_nowait(self):
        sql, _ = (
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .with_for_update(read=True, nowait=True, of=[Author, Book])
            .to_sql()
        )
        assert sql.endswith("FOR SHARE OF t_authors, t_books NOWAIT")

    # Ported from test/dialect/postgresql/test_compiler.py::CompileTest.test_for_update (SQLAlchemy 2.0.51)
    def test_key_share_of_source_with_skip_locked(self):
        sql, _ = (
            Query(Author)
            .with_for_update(key_share=True, skip_locked=True, of=Author)
            .to_sql()
        )
        assert sql.endswith("FOR NO KEY UPDATE OF t_authors SKIP LOCKED")


# --------------------------------------------------------------------------
# Arithmetic expression rendering — test_calculated_columns
# --------------------------------------------------------------------------


class TestArithmeticExpressions:
    # Ported from test/sql/test_compiler.py::SelectTest.test_calculated_columns (SQLAlchemy 2.0.51)
    def test_addition_and_multiplication_combo(self):
        sql, params = Query((Book.id + 1) * 2).to_sql(placeholder="$")
        assert sql == "SELECT ((id + $1) * $2) FROM t_books"
        assert params == (1, 2)

    # Ported from test/sql/test_compiler.py::SelectTest.test_calculated_columns (SQLAlchemy 2.0.51)
    def test_string_concat_in_select(self):
        sql, params = Query(Book.title.concat(" (book)")).to_sql(placeholder="$")
        assert sql == "SELECT (title || $1) FROM t_books"
        assert params == (" (book)",)

    # Ported from test/sql/test_compiler.py::SelectTest.test_calculated_columns (SQLAlchemy 2.0.51)
    def test_arithmetic_expression_in_where(self):
        sql, params = Query(Book).where(Book.id % 2 == 0).to_sql(placeholder="$")
        assert sql.endswith("WHERE (id % $1) = $2")
        assert params == (2, 0)


class TestCast:
    """Ported from test_compiler.py's test_cast: rowform's `cast()` takes a
    plain SQL type-name string rather than a type object (see cast()'s
    docstring), since rowform has no type system to instantiate one from."""

    # Ported from test/sql/test_compiler.py::SelectTest.test_cast (SQLAlchemy 2.0.51)
    def test_cast_function_form(self):
        sql, params = Query(cast(Book.id, "numeric")).to_sql(placeholder="$")
        assert sql == "SELECT CAST(id AS numeric) FROM t_books"
        assert params == ()

    # Ported from test/sql/test_compiler.py::SelectTest.test_cast (SQLAlchemy 2.0.51)
    def test_cast_method_form_matches_the_function(self):
        function_form, _ = Query(cast(Book.id, "numeric")).to_sql()
        method_form, _ = Query(Book.id.cast("numeric")).to_sql()
        assert function_form == method_form

    # Ported from test/sql/test_compiler.py::SelectTest.test_cast (SQLAlchemy 2.0.51)
    def test_cast_with_type_parameters(self):
        sql, _ = Query(cast(Book.id, "numeric(12, 9)")).to_sql()
        assert sql == "SELECT CAST(id AS numeric(12, 9)) FROM t_books"

    # Ported from test/sql/test_compiler.py::SelectTest.test_cast (SQLAlchemy 2.0.51)
    def test_cast_a_literal_value(self):
        # Unlike SQLAlchemy's select(cast(1234, Text)), rowform always needs a
        # real table in FROM (see Query()'s own "no table to select from"
        # error) — there is no bare-value SELECT with no source.
        sql, params = Query(Book.id, cast(1234, "text")).to_sql(placeholder="$")
        assert sql == "SELECT id, CAST($1 AS text) FROM t_books"
        assert params == (1234,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_cast (SQLAlchemy 2.0.51)
    def test_cast_used_in_a_comparison(self):
        sql, params = (
            Query(Book).where(Book.id.cast("text") == "7").to_sql(placeholder="$")
        )
        assert sql.endswith('WHERE CAST(id AS text) = $1')
        assert params == ("7",)

    # rowform-original test (no SQLAlchemy equivalent)
    def test_cast_rejects_a_fragment_as_the_type_name(self):
        with pytest.raises(ValueError, match="not a valid SQL type name"):
            cast(Book.id, "text); DROP TABLE t_books; --")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_cast_end_to_end(self, run_query):
        rows = run_query(Query(cast(Book.id, "text")).where(Book.id == 10))
        assert rows == [("10",)]


class TestLiteralsAndKeywords:
    """`literal()`, `true()`, `false()`, `null()` — standalone value/keyword
    wrappers, needed only where a bare Python value isn't already accepted
    (as a whole SELECT-list entry on its own)."""

    # Ported from test/sql/test_compiler.py::SelectTest.test_literal (SQLAlchemy 2.0.51)
    def test_literal_as_a_selected_value(self):
        sql, params = Query(Book.id, literal(1)).to_sql(placeholder="$")
        assert sql == "SELECT id, $1 FROM t_books"
        assert params == (1,)

    # rowform-original test (no SQLAlchemy equivalent)
    def test_literal_declares_its_py_type_from_the_value_by_default(self):
        assert literal("x").py_type is str
        assert literal(1).py_type is int
        assert literal(1, py_type=float).py_type is float

    # rowform-original test (no SQLAlchemy equivalent)
    def test_true_false_null_render_as_bare_keywords(self):
        sql, params = Query(Book.id, true(), false(), null()).to_sql()
        assert sql == "SELECT id, TRUE, FALSE, NULL FROM t_books"
        assert params == ()

    # rowform-original test (no SQLAlchemy equivalent)
    def test_true_and_false_end_to_end(self, run_query):
        rows = run_query(Query(Book.id, true(), false()).where(Book.id == 10))
        assert rows == [(10, 1, 0)]  # sqlite has no bool type; 1/0 on the wire

    # rowform-original test (no SQLAlchemy equivalent)
    def test_null_used_in_a_case_arm(self):
        sql, params = Query(case((Book.id > 10, null()), else_="x")).to_sql(
            placeholder="$"
        )
        assert sql == "SELECT CASE WHEN id > $1 THEN NULL ELSE $2 END FROM t_books"
        assert params == (10, "x")


class TestLiteralColumn:
    """`literal_column()` — a raw SQL fragment inserted verbatim, the "I know
    what I'm doing" escape hatch (unlike `cast()`'s type-name regex or
    `sql_function()`'s identifier regex, there is no validation at all
    here). Ported from test/sql/test_compiler.py's general usage of
    literal_column() as a plain value/column stand-in throughout that file
    (e.g. lines 322-481), adapted to rowform idiom rather than any single test
    function — SQLAlchemy has no isolated "test literal_column renders
    verbatim" test of its own, since it is used as a building block
    everywhere else in that file instead.
    """

    # rowform-original test (no SQLAlchemy equivalent)
    def test_renders_verbatim_in_a_select_list(self):
        sql, params = Query(Book.id, literal_column("count(*) + 1")).to_sql()
        assert sql == "SELECT id, count(*) + 1 FROM t_books"
        assert params == ()

    # rowform-original test (no SQLAlchemy equivalent)
    def test_composes_with_ordinary_comparison_operators(self):
        sql, params = (
            Query(Book).where(literal_column("1") == Book.id).to_sql(placeholder="$")
        )
        assert sql == "SELECT id, author_id, title FROM t_books WHERE 1 = id"
        assert params == ()

    # rowform-original test (no SQLAlchemy equivalent)
    def test_composes_with_arithmetic(self):
        # literal_column() alone selects nothing to name a FROM table from
        # (sources() is empty) — pair it with a real column, same as any
        # other from-less expression like count().
        sql, _ = Query(Book.id, literal_column("1") + literal_column("2")).to_sql()
        assert sql == "SELECT id, (1 + 2) FROM t_books"

    # rowform-original test (no SQLAlchemy equivalent)
    def test_py_type_is_declarable(self):
        assert literal_column("count(*)", py_type=int).py_type is int
        assert literal_column("count(*)").py_type is None

    # rowform-original test (no SQLAlchemy equivalent)
    def test_no_validation_at_all_unlike_cast_or_sql_function(self):
        # Deliberately accepts anything, including something that would be
        # rejected everywhere else in this library that takes a fragment.
        weird = literal_column("; DROP TABLE t_books; --")
        sql, _ = Query(Book.id, weird).to_sql()
        assert "DROP TABLE" in sql  # exactly the point: nothing stops this

    # rowform-original test (no SQLAlchemy equivalent)
    def test_an_unjoined_table_reference_is_not_caught(self):
        # The deliberate cost of the escape hatch: sources() returns nothing,
        # so unlike a real ColumnExpr this is never validated against the
        # join graph — contrast with tests/sqlalchemy_ports/
        # test_from_linter_ported.py, where every *real* column reference to
        # an unjoined table raises immediately.
        query = Query(Author).where(literal_column("t_books.title") == "x")
        sql, params = query.to_sql()  # does not raise
        assert sql == "SELECT id, name, active FROM t_authors WHERE t_books.title = ?"
        assert params == ("x",)


# --------------------------------------------------------------------------
# GROUP BY / HAVING / ORDER BY combinations — test_orderby_groupby
# --------------------------------------------------------------------------


class TestGroupByHavingOrderBy:
    # Ported from test/sql/test_compiler.py::SelectTest.test_orderby_groupby (SQLAlchemy 2.0.51)
    def test_group_by_then_order_by(self):
        sql, _ = (
            Query(Book.author_id, count().label("n"))
            .group_by(Book.author_id)
            .order_by(Book.author_id)
            .to_sql()
        )
        assert sql == (
            "SELECT author_id, count(*) AS n FROM t_books "
            "GROUP BY author_id ORDER BY author_id"
        )

    # Ported from test/sql/test_compiler.py::SelectTest.test_orderby_groupby (SQLAlchemy 2.0.51)
    def test_having_after_group_by(self):
        sql, params = (
            Query(Book.author_id, count().label("n"))
            .group_by(Book.author_id)
            .having(count() > 1)
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT author_id, count(*) AS n FROM t_books "
            "GROUP BY author_id HAVING count(*) > $1"
        )
        assert params == (1,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_orderby_groupby (SQLAlchemy 2.0.51)
    def test_order_by_calls_accumulate_rather_than_replace(self):
        sql, _ = (
            Query(Book).order_by(Book.author_id).order_by(Book.id, descending=True)
            .to_sql()
        )
        assert sql.endswith("ORDER BY author_id, id DESC")


# --------------------------------------------------------------------------
# UNION / INTERSECT / EXCEPT — test_compound_selects
# --------------------------------------------------------------------------


class TestCompoundSelects:
    # Ported from test/sql/test_compiler.py::SelectTest.test_compound_selects (SQLAlchemy 2.0.51)
    def test_union_of_two_selects(self):
        sql, params = (
            Query(Author.name).where(Author.active == True)
            .union(Query(Author.name).where(Author.active == False))
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT name FROM t_authors WHERE active = $1 "
            "UNION "
            "SELECT name FROM t_authors WHERE active = $2"
        )
        assert params == (True, False)

    # Ported from test/sql/test_compiler.py::SelectTest.test_compound_selects (SQLAlchemy 2.0.51)
    def test_union_all_keeps_duplicates(self):
        sql, _ = Query(Author.name).union_all(Query(Author.name)).to_sql()
        assert "UNION ALL" in sql

    # Ported from test/sql/test_compiler.py::SelectTest.test_compound_selects (SQLAlchemy 2.0.51)
    def test_except_removes_matching_rows(self):
        sql, params = (
            Query(Author.name)
            .except_(Query(Author.name).where(Author.active == False))
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT name FROM t_authors "
            "EXCEPT "
            "SELECT name FROM t_authors WHERE active = $1"
        )
        assert params == (False,)

    # Ported from test/sql/test_compiler.py::SelectTest.test_compound_selects (SQLAlchemy 2.0.51)
    def test_compound_with_order_by_limit_offset(self):
        sql, params = (
            Query(Author.name).union(Query(Author.name))
            .order_by("name").limit(3).offset(1)
            .to_sql(placeholder="$")
        )
        assert sql == (
            "SELECT name FROM t_authors "
            "UNION "
            "SELECT name FROM t_authors "
            "ORDER BY name LIMIT $1 OFFSET $2"
        )
        assert params == (3, 1)
