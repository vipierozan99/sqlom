"""Ported from SQLAlchemy's test/sql/test_operators.py and test/sql/test_functions.py
(subset relevant to sqlom's operator/function surface), adapted to sqlom.

Skipped: custom/user-defined types and TypeDecorator comparator overrides
(sqlom has no type system to hook into); dialect-specific operators (MySQL/
Oracle/MSSQL rendering, `%` paramstyle escaping, floor-division-as-FLOOR()
rewriting) since sqlom targets Postgres/sqlite generically and does not do
per-dialect operator rewriting; JSON/ARRAY/HSTORE indexing operators (Postgres
extension types sqlom does not model); ORM-level `Comparator` customization,
`GenericFunction` subclassing and the function registry (no ORM, and
`func.*` here is a thin passthrough, not a registry); ANSI `IS DISTINCT FROM`
(no equivalent method exists — see the gap note below: sqlite and Postgres
spell null-safe equality completely differently — bare `IS`/`IS NOT` versus
`IS [NOT] DISTINCT FROM` — and `to_sql()` renders one dialect-agnostic
string, so there is no single fragment that is valid on both); pickling of
expression trees; `VALUES` constructs since sqlom has no derived-table VALUES
clause; and precedence tests that rely on SQLAlchemy's
precedence-aware compiler omitting "unnecessary" parens — sqlom always
parenthesises `BinaryOp`/`BooleanClause`/`Not`, so there is no
precedence-dependent rendering to assert on the sqlom side (only the *Python*
operator-precedence trap around `&`/`|` remains observable, and is covered
below).
"""

import pytest

from sqlom import (
    Query,
    and_,
    avg,
    count,
    func,
    max_,
    min_,
    not_,
    or_,
    sql_function,
    sum_,
    tuple_,
)
from tests.conftest import Author, Book, assert_dialect_sql


def where_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" WHERE ", 1)[1], params


def select_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" FROM ")[0].removeprefix("SELECT "), params


# --------------------------------------------------------------------------
# Comparison operators
# --------------------------------------------------------------------------


class TestComparisonOperators:
    @pytest.mark.parametrize("expression,expected", [
        (Book.id == 5, "id = ?"),
        (Book.id != 5, "id != ?"),
        (Book.id > 5, "id > ?"),
        (Book.id >= 5, "id >= ?"),
        (Book.id < 5, "id < ?"),
        (Book.id <= 5, "id <= ?"),
    ])
    def test_operators_render(self, expression, expected):
        clause, params = expression.to_sql("?")
        assert clause == expected
        assert params == (5,)

    def test_value_on_the_left_reflects_onto_the_column(self):
        # sqlom only defines the comparison dunders on Expression, not their
        # reflected counterparts on plain values. Python's own operator
        # protocol supplies the rest: when `int.__gt__` can't handle a
        # ColumnExpr it falls back to the reflected method, which for `>`/`<`
        # swaps operands (`5 > x` tries `x.__lt__(5)`), and for `==`/`!=`
        # does not (`5 == x` tries `x.__eq__(5)`, still `x` first).
        assert (5 == Book.id).to_sql("?") == ("id = ?", (5,))
        assert (5 != Book.id).to_sql("?") == ("id != ?", (5,))
        assert (5 > Book.id).to_sql("?") == ("id < ?", (5,))
        assert (5 < Book.id).to_sql("?") == ("id > ?", (5,))
        assert (5 >= Book.id).to_sql("?") == ("id <= ?", (5,))
        assert (5 <= Book.id).to_sql("?") == ("id >= ?", (5,))

    @pytest.mark.parametrize("op,expected", [
        ("==", "id = author_id"),
        ("!=", "id != author_id"),
        (">", "id > author_id"),
        (">=", "id >= author_id"),
        ("<", "id < author_id"),
        ("<=", "id <= author_id"),
    ])
    def test_column_to_column_comparison_binds_nothing(self, op, expected):
        condition = eval(f"Book.id {op} Book.author_id")  # noqa: S307
        clause, params = condition.to_sql("?")
        assert clause == expected
        assert params == ()


# --------------------------------------------------------------------------
# NULL / IS / IS NOT / LIKE
# --------------------------------------------------------------------------


class TestNullAndIsOperators:
    def test_is_none_and_is_not_none(self):
        assert Book.title.is_(None).to_sql("?") == ("title IS NULL", ())
        assert Book.title.is_not(None).to_sql("?") == ("title IS NOT NULL", ())

    def test_is_matches_eq_none_and_is_not_matches_ne_none(self):
        # SQLAlchemy's `.is_()`/`.is_not()` are just spellings of `==`/`!=`
        # against NULL; sqlom keeps that equivalence but only for None.
        assert Book.title.is_(None).to_sql("?") == (Book.title == None).to_sql("?")  # noqa: E711
        assert Book.title.is_not(None).to_sql("?") == (Book.title != None).to_sql("?")  # noqa: E711

    @pytest.mark.parametrize("bad", [True, False, 0, "x"])
    def test_is_only_accepts_none(self, bad):
        # Unlike SQLAlchemy, which lets `.is_()` take TRUE/FALSE literals too
        # (`c.is_(True)` -> "x IS true"), sqlom restricts it to None: SQL `IS`
        # otherwise wants a literal, not a bound parameter, and a value
        # comparison already has a correct spelling in `==`.
        with pytest.raises(TypeError, match="only supports None"):
            Book.title.is_(bad)

    @pytest.mark.parametrize("bad", [True, False, 0, "x"])
    def test_is_not_only_accepts_none(self, bad):
        with pytest.raises(TypeError, match="only supports None"):
            Book.title.is_not(bad)

    def test_like(self):
        clause, params = Book.title.like("%algo%").to_sql("?")
        assert clause == "title LIKE ?"
        assert params == ("%algo%",)

    def test_like_used_in_a_where_clause(self):
        clause, params = where_of(Query(Book).where(Book.title.like("a%")))
        assert clause == "title LIKE $1"
        assert params == ("a%",)


class TestIsDistinctFrom:
    """`IS DISTINCT FROM` / `IS NOT DISTINCT FROM` — null-safe (in)equality,
    added alongside the multi-dialect Dialect system since sqlite and
    Postgres spell this completely differently (sqlite has no `DISTINCT
    FROM` keyword at all)."""

    # Ported from test/sql/test_operators.py::IsDistinctFromTest.test_is_distinct_from_postgresql (SQLAlchemy 2.0.51)
    def test_is_distinct_from_on_postgres(self):
        assert_dialect_sql(
            Query(Book).where(Book.id.is_distinct_from(1)),
            postgres="SELECT id, author_id, title FROM t_books "
                     "WHERE id IS DISTINCT FROM $1",
            params=(1,),
        )

    # Ported from test/sql/test_operators.py::IsDistinctFromTest.test_is_distinct_from_sqlite (SQLAlchemy 2.0.51)
    def test_is_distinct_from_on_sqlite(self):
        assert_dialect_sql(
            Query(Book).where(Book.id.is_distinct_from(1)),
            sqlite="SELECT id, author_id, title FROM t_books WHERE id IS NOT ?",
            params=(1,),
        )

    # Ported from test/sql/test_operators.py::IsDistinctFromTest.test_is_not_distinct_from_postgresql (SQLAlchemy 2.0.51)
    def test_is_not_distinct_from_on_postgres(self):
        assert_dialect_sql(
            Query(Book).where(Book.id.is_not_distinct_from(1)),
            postgres="SELECT id, author_id, title FROM t_books "
                     "WHERE id IS NOT DISTINCT FROM $1",
            params=(1,),
        )

    # Ported from test/sql/test_operators.py::IsDistinctFromTest.test_is_not_distinct_from_sqlite (SQLAlchemy 2.0.51)
    def test_is_not_distinct_from_on_sqlite(self):
        assert_dialect_sql(
            Query(Book).where(Book.id.is_not_distinct_from(1)),
            sqlite="SELECT id, author_id, title FROM t_books WHERE id IS ?",
            params=(1,),
        )

    # sqlom-original test (no SQLAlchemy equivalent) — SQLAlchemy has no
    # dialect-less default to fall back on either, but it never needs one:
    # its compiler always has a dialect (defaulting to "generic ANSI"),
    # where sqlom's to_sql() is dialect-less unless a dialect is explicitly
    # given. This is the one predicate that requires one.
    def test_raises_without_a_dialect(self):
        with pytest.raises(ValueError, match="needs a dialect"):
            Query(Book).where(Book.id.is_distinct_from(1)).to_sql()
        with pytest.raises(ValueError, match="needs a dialect"):
            Query(Book).where(Book.id.is_not_distinct_from(1)).to_sql()

    # sqlom-original test (no SQLAlchemy equivalent) — proves the null-safe
    # semantics actually execute correctly against real NULLs, the exact
    # case plain ==/!= get wrong. No fixture column is ever NULL, so an
    # outer join supplies one: author "dan" (id 4) has no books, so
    # Book.id is NULL for that row only.
    def test_is_distinct_from_end_to_end_on_sqlite(self, db):
        from sqlom import SQLITE

        distinct = (
            Query(Author.name, Book.id)
            .outer_join(Book, Book.author_id == Author.id)
            .where(Book.id.is_distinct_from(None))
            .order_by(Author.id)
        )
        sql, params = distinct.to_sql(dialect=SQLITE)
        rows = db.execute(sql, params).fetchall()
        # "dan" (NULL book.id) is correctly excluded — NULL is not distinct
        # from NULL, unlike `!= NULL`, which would exclude every row.
        assert rows == [("ada", 10), ("ada", 11), ("brian", 12), ("carol", 13)]

        not_distinct = (
            Query(Author.name, Book.id)
            .outer_join(Book, Book.author_id == Author.id)
            .where(Book.id.is_not_distinct_from(None))
            .order_by(Author.id)
        )
        sql, params = not_distinct.to_sql(dialect=SQLITE)
        rows = db.execute(sql, params).fetchall()
        # Only "dan" — the one row where book.id really is NULL — matches,
        # unlike `== NULL`, which would match zero rows.
        assert rows == [("dan", None)]


# --------------------------------------------------------------------------
# IN / NOT IN
# --------------------------------------------------------------------------


class TestLikeIlikeBetween:
    """`.like()`/`.ilike()`/`.between()` — SQLAlchemy's `ColumnOperators`
    names, added to close the naming gap so these read as a drop-in
    substitute for the same calls against a SQLAlchemy column."""

    def test_like(self):
        clause, params = where_of(Query(Author).where(Author.name.like("a%")))
        assert clause == "name LIKE $1"
        assert params == ("a%",)

    def test_ilike(self):
        # Postgres-only: ILIKE is a Postgres extension, sqlite has no
        # equivalent operator (see Expression.ilike's docstring).
        clause, params = where_of(Query(Author).where(Author.name.ilike("A%")))
        assert clause == "name ILIKE $1"
        assert params == ("A%",)

    def test_between(self):
        clause, params = where_of(Query(Book).where(Book.id.between(10, 20)))
        assert clause == "(id >= $1 AND id <= $2)"
        assert params == (10, 20)

    def test_between_end_to_end(self, run_query):
        rows = run_query(Query(Book.id).where(Book.id.between(11, 12)).order_by(Book.id))
        assert rows == [(11,), (12,)]


class TestInAndNotIn:
    def test_in_accepts_a_list(self):
        clause, params = Author.id.in_([1, 2, 3]).to_sql("?")
        assert clause == "id IN (?, ?, ?)"
        assert params == (1, 2, 3)

    def test_in_accepts_a_generator(self):
        # SQLAlchemy's InTest exercises `in_(iter(...))` explicitly (test_in_4)
        # because the historical implementation once required a concrete
        # sequence; sqlom's `list(self.values)` already handles any iterable.
        clause, params = Author.id.in_(x for x in (1, 2, 3)).to_sql("?")
        assert clause == "id IN (?, ?, ?)"
        assert params == (1, 2, 3)

    def test_in_accepts_a_set(self):
        # Small ints hash to themselves in CPython, so a set of them iterates
        # in a fixed, predictable order regardless of hash randomization.
        clause, params = Author.id.in_({1, 2, 3}).to_sql("?")
        assert clause == "id IN (?, ?, ?)"
        assert params == (1, 2, 3)

    def test_not_in_accepts_a_list(self):
        clause, params = Author.id.not_in([7]).to_sql("?")
        assert clause == "id NOT IN (?)"
        assert params == (7,)

    def test_empty_in_short_circuits_to_false(self):
        # `x IN ()` is invalid SQL on Postgres; an empty collection is a
        # perfectly ordinary thing for calling code to end up with, so this
        # renders the boolean constant it is logically equivalent to instead.
        assert Author.id.in_([]).to_sql("?") == ("FALSE", ())

    def test_empty_not_in_short_circuits_to_true(self):
        assert Author.id.not_in([]).to_sql("?") == ("TRUE", ())

    def test_in_with_a_correlated_subquery_column(self):
        clause, params = where_of(
            Query(Author).where(
                Author.id.not_in(Query(Book.author_id).where(Book.title == "x"))
            )
        )
        assert clause == "id NOT IN (SELECT author_id FROM t_books WHERE title = $1)"
        assert params == ("x",)

    def test_in_and_its_negation_both_render(self):
        # Mirrors SQLAlchemy's test_in_self_plus_negated: the same clause and
        # its `~` both appear, unaffected by each other. Unlike `ExistsClause`,
        # `InClause` has no `__invert__` override, so `~membership` goes
        # through the generic `Predicate.__invert__` and wraps in `NOT (...)`
        # rather than flipping to `NOT IN`.
        membership = Author.id.in_([5])
        clause, params = where_of(Query(Author).where(and_(membership, ~membership)))
        assert clause == "(id IN ($1) AND NOT (id IN ($2)))"
        assert params == (5, 5)

    def test_in_with_a_column_expression_value_is_rendered_as_a_column(self):
        # Matches SQLAlchemy's test_in_14 in test_operators.py: a bare
        # ColumnExpr inside the values list is a column reference, not a bind
        # parameter — `.in_([other_column])` renders `x IN (other_column)`.
        clause, params = Book.id.in_([Book.author_id]).to_sql("?")
        assert clause == "id IN (author_id)"
        assert params == ()

    def test_in_with_a_mix_of_columns_and_values(self):
        clause, params = Book.id.in_([Book.author_id, 5, Book.id]).to_sql("?")
        assert clause == "id IN (author_id, ?, id)"
        assert params == (5,)


class TestTupleComparisons:
    """`tuple_()` — SQLAlchemy's composite row-value comparisons. Portable:
    `(a, b) = (1, 2)` and `(a, b) IN ((1, 2), ...)` are standard SQL both
    sqlite and Postgres support identically, unlike `IS DISTINCT FROM`."""

    def test_tuple_equality_against_a_plain_python_tuple(self):
        clause, params = where_of(
            Query(Author).where(tuple_(Author.id, Author.name) == (1, "ada"))
        )
        assert clause == "(id, name) = ($1, $2)"
        assert params == (1, "ada")

    def test_tuple_inequality(self):
        clause, params = where_of(
            Query(Author).where(tuple_(Author.id, Author.name) != (1, "ada"))
        )
        assert clause == "(id, name) != ($1, $2)"
        assert params == (1, "ada")

    def test_tuple_equality_against_another_tuple_of_columns(self):
        # Mirrors test_compiler.py's test_tuple_clauselist_in, but for `==`
        # rather than `.in_()`.
        clause, params = where_of(
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .where(tuple_(Author.id, Author.name) == tuple_(Book.author_id, Book.title))
        )
        assert clause == "(t_authors.id, t_authors.name) = (t_books.author_id, t_books.title)"
        assert params == ()

    def test_tuple_in_a_list_of_plain_tuples(self):
        clause, params = where_of(
            Query(Author).where(
                tuple_(Author.id, Author.name).in_([(1, "ada"), (2, "brian")])
            )
        )
        assert clause == "(id, name) IN (($1, $2), ($3, $4))"
        assert params == (1, "ada", 2, "brian")

    def test_tuple_in_a_list_of_column_tuples(self):
        # test_compiler.py's test_tuple_clauselist_in: the IN list may itself
        # hold tuple_(...) of columns, not just plain value tuples.
        clause, params = where_of(
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .where(tuple_(Author.id, Author.name).in_(
                [tuple_(Book.author_id, Book.title)]
            ))
        )
        assert clause == (
            "(t_authors.id, t_authors.name) IN "
            "((t_books.author_id, t_books.title))"
        )
        assert params == ()

    def test_tuple_not_in(self):
        clause, params = where_of(
            Query(Author).where(tuple_(Author.id, Author.name).not_in([(1, "ada")]))
        )
        assert clause == "(id, name) NOT IN (($1, $2))"
        assert params == (1, "ada")

    def test_tuple_in_a_subquery(self):
        # test_compiler.py's test_select_in.
        clause, params = where_of(
            Query(Author).where(
                tuple_(Author.id, Author.name).in_(Query(Book.author_id, Book.title))
            )
        )
        assert clause == "(id, name) IN (SELECT author_id, title FROM t_books)"
        assert params == ()

    def test_tuple_comparison_rejects_a_mismatched_value(self):
        with pytest.raises(TypeError, match="tuple_"):
            tuple_(Author.id) == 5

    def test_tuple_needs_at_least_one_element(self):
        with pytest.raises(ValueError, match="at least one element"):
            tuple_()

    def test_tuple_equality_end_to_end(self, run_query):
        rows = run_query(Query(Author).where(tuple_(Author.id, Author.name) == (1, "ada")))
        assert len(rows) == 1 and rows[0].name == "ada"


# --------------------------------------------------------------------------
# Boolean composition: AND / OR / NOT, and the &/| precedence trap
# --------------------------------------------------------------------------


class TestBooleanComposition:
    def test_or_wrapped_inside_and(self):
        clause, params = where_of(
            Query(Author).where(
                or_(and_(Author.id == 1, Author.active == True), Author.id == 2)  # noqa: E712
            )
        )
        assert clause == "((id = $1 AND active = $2) OR id = $3)"
        assert params == (1, True, 2)

    def test_and_wrapped_inside_or(self):
        # The reverse nesting from test_or_wrapped_inside_and: SQLAlchemy's
        # compiler could drop these parens because AND binds tighter than OR
        # in SQL; sqlom always parenthesises a nested BooleanClause instead
        # of reasoning about relative precedence.
        clause, params = where_of(
            Query(Author).where(
                and_(Author.id == 1, or_(Author.active == True, Author.id == 2))  # noqa: E712
            )
        )
        assert clause == "(id = $1 AND (active = $2 OR id = $3))"
        assert params == (1, True, 2)

    def test_three_way_and_is_flattened_not_nested(self):
        clause, _ = where_of(
            Query(Author).where(and_(Author.id == 1, Author.id == 2, Author.id == 3))
        )
        assert clause == "(id = $1 AND id = $2 AND id = $3)"

    def test_not_of_and_wraps_the_whole_clause(self):
        clause, params = where_of(
            Query(Author).where(
                not_(and_(Author.active == True, or_(Author.id == 1, Author.id == 2)))  # noqa: E712
            )
        )
        assert clause == "NOT ((active = $1 AND (id = $2 OR id = $3)))"
        assert params == (True, 1, 2)

    def test_double_negation_nests_rather_than_cancelling(self):
        # sqlom's `~` always wraps in a fresh `Not`; it does not simplify
        # `NOT (NOT x)` down to `x` the way some compilers' De Morgan passes
        # might. Worth pinning down explicitly since it's the kind of thing
        # an optimization could silently break.
        clause, params = (~~(Author.id == 1)).to_sql("?")
        assert clause == "NOT (NOT (id = ?))"
        assert params == (1,)

    def test_mixed_and_or_operator_overloads_nest_correctly(self):
        clause, params = where_of(
            Query(Author).where(
                (Author.id == 1) | ((Author.active == True) & (Author.id == 2))  # noqa: E712
            )
        )
        assert clause == "(id = $1 OR (active = $2 AND id = $3))"
        assert params == (1, True, 2)

    def test_unparenthesised_comparison_with_bitwise_operator_is_a_python_trap(self):
        # `&`/`|` bind tighter than comparisons in Python, so an
        # un-parenthesised `A.x > 1 & A.y < 2` does not parse as the boolean
        # AND of two comparisons — Python evaluates `1 & A.y` first, as a
        # chained comparison operand, and that fails immediately because
        # plain `int` doesn't know how to `&` with a ColumnExpr and
        # ColumnExpr defines no reflected `__rand__`. This is exactly the
        # trap the docstring on `Predicate` warns about (see README §4);
        # parenthesising each side (as every other test in this file does)
        # is what avoids it.
        with pytest.raises(TypeError, match="unsupported operand type"):
            Author.id > 1 & Author.id < 2  # noqa: B015


# --------------------------------------------------------------------------
# Arithmetic, concat, and the escape hatch for un-wrapped operators
# --------------------------------------------------------------------------


class TestArithmetic:
    def test_arithmetic_expression_compared_to_a_column(self):
        # Exercises BinaryOp on the left of a Condition against a plain
        # ColumnExpr on the right — both sides parenthesise/render correctly
        # in combination, not just standalone.
        clause, params = where_of(Query(Book).where((Book.id + 5) == Book.author_id))
        assert clause == "(id + $1) = author_id"
        assert params == (5,)

    def test_concat_of_two_columns(self):
        # test_expressions.py already covers concat against a literal; this
        # is the column-to-column form, `title || name` — across a join,
        # since Author and Book are different tables (caught by the select-
        # list validation fixed alongside this port: an entity referencing an
        # unjoined source now raises, the same protection where()/join()/
        # order_by() already had).
        clause, params = select_of(
            Query(Book.title.concat(Author.name))
            .join(Author, Book.author_id == Author.id)
        )
        assert clause == "(t_books.title || t_authors.name)"
        assert params == ()

    def test_operate_supports_bitwise_operators(self):
        # sqlom has no dedicated bitwise operator methods (SQLAlchemy has
        # `.bitwise_and`/`.bitwise_or`/etc.); `.operate()` is the documented
        # escape hatch for exactly this.
        clause, params = select_of(Query(Book.id.operate("&", 1)))
        assert clause == "(id & $1)"
        assert params == (1,)
        clause, _ = select_of(Query(Book.id.operate("|", 2)))
        assert clause == "(id | $1)"

    def test_operate_chains_because_binaryop_is_itself_an_expression(self):
        # `.operate()` returns a `BinaryOp`, which is an `Expression`, so it
        # exposes `.operate()` too — chaining composes rather than needing a
        # separate combinator.
        clause, params = select_of(Query(Book.id.operate("&", 1).operate("+", 2)))
        assert clause == "((id & $1) + $2)"
        assert params == (1, 2)


# --------------------------------------------------------------------------
# func.* generic calls
# --------------------------------------------------------------------------


class TestGenericFunctions:
    def test_zero_argument_function(self):
        # A window/aggregate-free, argument-free call, e.g. `now()`. Needs a
        # column alongside it in the select list purely so the query has a
        # FROM to derive from — the function itself references no source.
        assert select_of(Query(Book.id, func.now()))[0] == "id, now()"

    def test_function_used_as_a_predicate_operand(self):
        clause, params = where_of(Query(Book).where(func.lower(Book.title) == "x"))
        assert clause == "lower(title) = $1"
        assert params == ("x",)

    def test_function_result_used_as_an_arithmetic_operand(self):
        clause, params = select_of(Query(func.coalesce(Book.author_id, 0) + 1))
        assert clause == "(coalesce(author_id, $1) + $2)"
        assert params == (0, 1)

    def test_sql_function_is_the_explicit_spelling_of_func_attribute_access(self):
        assert (sql_function("lower", Book.title).to_sql("?")
                == func.lower(Book.title).to_sql("?"))

    def test_func_has_no_dotted_package_namespace(self):
        # SQLAlchemy supports `func.foo_.bar_.baz()` as a dotted/quoted
        # package path (test_underscores_packages, test_quote_special_chars).
        # sqlom's `func` namespace returns a plain callable from attribute
        # access rather than a nested namespace object, so a second
        # attribute access on it fails — sqlom has no equivalent feature,
        # this just documents the boundary rather than a bug to fix.
        with pytest.raises(AttributeError):
            func.foo.bar

    @pytest.mark.parametrize("bad", ["1abc", "a b", "lower(x)", ""])
    def test_function_names_are_validated_as_identifiers(self, bad):
        with pytest.raises(ValueError, match="not a valid SQL function name"):
            sql_function(bad, Book.id)


# --------------------------------------------------------------------------
# Aggregate functions
# --------------------------------------------------------------------------


class TestAggregateFunctions:
    def test_count_star(self):
        assert select_of(Query(Book.id, count()))[0] == "id, count(*)"

    def test_count_of_a_column(self):
        assert select_of(Query(count(Book.id)))[0] == "count(id)"

    def test_count_distinct_of_a_column(self):
        assert select_of(Query(count(Book.author_id, distinct=True)))[0] == \
            "count(DISTINCT author_id)"

    @pytest.mark.parametrize("aggregate,expected", [
        (sum_, "sum(id)"),
        (avg, "avg(id)"),
        (min_, "min(id)"),
        (max_, "max(id)"),
    ])
    def test_typed_aggregate_shortcuts(self, aggregate, expected):
        assert select_of(Query(aggregate(Book.id)))[0] == expected

    def test_sum_distinct(self):
        assert select_of(Query(sum_(Book.id, distinct=True)))[0] == "sum(DISTINCT id)"

    def test_aggregate_in_a_having_clause(self):
        sql, params = (
            Query(Book.author_id, count(Book.id))
            .group_by(Book.author_id)
            .having(count(Book.id) > 1)
            .to_sql(placeholder="?")
        )
        assert sql == (
            "SELECT author_id, count(id) FROM t_books "
            "GROUP BY author_id HAVING count(id) > ?"
        )
        assert params == (1,)

    def test_aggregate_used_in_arithmetic(self):
        clause, params = select_of(Query(sum_(Book.id) + 1))
        assert clause == "(sum(id) + $1)"
        assert params == (1,)


class TestAggregateEndToEnd:
    """Real values out of sqlite, not just SQL text — the aggregates in
    test_expressions.py's TestCountForms only exercise `count()`."""

    def test_sum_avg_min_max(self, run_query):
        assert run_query(Query(sum_(Book.id))) == [(46,)]
        assert run_query(Query(avg(Book.id))) == [(11.5,)]
        assert run_query(Query(min_(Book.id))) == [(10,)]
        assert run_query(Query(max_(Book.id))) == [(13,)]

    def test_count_distinct(self, run_query):
        # 4 books, 3 distinct authors (author 1 wrote two).
        assert run_query(Query(count(Book.author_id, distinct=True))) == [(3,)]

    def test_group_by_with_having(self, run_query):
        rows = run_query(
            Query(Book.author_id, count(Book.id))
            .group_by(Book.author_id)
            .having(count(Book.id) > 1)
        )
        assert rows == [(1, 2)]


# --------------------------------------------------------------------------
# .label()
# --------------------------------------------------------------------------


class TestLabel:
    def test_label_on_a_column(self):
        clause, _ = select_of(Query(Book.title.label("book_title")))
        assert clause == "title AS book_title"

    def test_label_on_an_arithmetic_expression(self):
        clause, params = select_of(Query((Book.id + 1).label("next_id")))
        assert clause == "(id + $1) AS next_id"
        assert params == (1,)

    def test_label_on_an_aggregate(self):
        clause, _ = select_of(Query(count(Book.id).label("n")))
        assert clause == "count(id) AS n"

    def test_labelled_expression_is_usable_end_to_end(self, run_query):
        rows = run_query(Query(Book.id, count().over().label("total")).order_by(Book.id).limit(1))
        assert rows == [(10, 4)]
