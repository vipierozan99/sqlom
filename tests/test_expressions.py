"""Arithmetic, SQL functions, CASE and window functions."""

import pytest

from sqlom import (
    Query,
    case,
    count,
    dense_rank,
    first_value,
    func,
    lag,
    last_value,
    lead,
    ntile,
    rank,
    row_number,
    sql_function,
    sum_,
)
from tests.conftest import Author, Book


def select_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" FROM ")[0].removeprefix("SELECT "), params


def where_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" WHERE ", 1)[1], params


class TestArithmetic:
    def test_column_times_literal_binds_the_literal(self):
        # The right-hand side is a parameter, never interpolated — the same rule
        # as everywhere else in the builder.
        clause, params = select_of(Query(Book.id * 2))
        assert clause == "(id * $1)"
        assert params == (2,)

    @pytest.mark.parametrize("expression,expected", [
        (Book.id + 1, "(id + $1)"),
        (Book.id - 1, "(id - $1)"),
        (Book.id * 2, "(id * $1)"),
        (Book.id / 2, "(id / $1)"),
        (Book.id % 2, "(id % $1)"),
    ])
    def test_operators(self, expression, expected):
        assert select_of(Query(expression))[0] == expected

    def test_reflected_operators(self):
        assert select_of(Query(10 - Book.id))[0] == "($1 - id)"
        assert select_of(Query(1 + Book.id))[0] == "($1 + id)"
        assert select_of(Query(2 * Book.id))[0] == "($1 * id)"

    def test_negation(self):
        assert select_of(Query(-Book.id))[0] == "(-id)"

    def test_column_to_column(self):
        clause, params = select_of(Query(Book.id + Book.author_id))
        assert clause == "(id + author_id)"
        assert params == ()

    def test_always_parenthesised(self):
        # SQL precedence differs from Python's in places, and a redundant bracket
        # costs nothing while a missing one changes meaning.
        assert select_of(Query((Book.id + 1) * 2))[0] == "((id + $1) * $2)"

    def test_usable_in_a_predicate(self):
        clause, params = where_of(Query(Book).where(Book.id * 2 > 10))
        assert clause == "(id * $1) > $2"
        assert params == (2, 10)

    def test_concat_renders_the_portable_operator(self):
        # Postgres has no `+` for text, so string joining is `||` and not `+`.
        clause, params = select_of(Query(Book.title.concat("!")))
        assert clause == "(title || $1)"
        assert params == ("!",)

    def test_custom_operator(self):
        clause, _ = where_of(Query(Book).where(Book.id.operate("%", 2) == 0))
        assert clause == "(id % $1) = $2"

    @pytest.mark.parametrize("bad", ["; DROP TABLE x", "SELECT", "", "======"])
    def test_custom_operator_rejects_non_operators(self, bad):
        # This is the one place a caller supplies a fragment rather than a value.
        with pytest.raises(ValueError, match="not an accepted SQL operator"):
            Book.id.operate(bad, 1)

    def test_operate_is_not_shadowed_by_the_op_attribute(self):
        """`Condition` and `BinaryOp` both carry an `op` attribute, so a method of
        that name would be shadowed on them by their own __init__."""
        assert (Book.id == 1).op == "="
        assert (Book.id + 1).op == "+"
        assert callable(Book.id.operate)


class TestFunctions:
    def test_func_namespace(self):
        assert select_of(Query(func.lower(Book.title)))[0] == "lower(title)"

    def test_multiple_arguments_mix_columns_and_values(self):
        clause, params = select_of(Query(func.coalesce(Book.author_id, 0)))
        assert clause == "coalesce(author_id, $1)"
        assert params == (0,)

    def test_nested_calls(self):
        assert select_of(Query(func.upper(func.lower(Book.title))))[0] == \
            "upper(lower(title))"

    def test_sql_function_can_declare_a_type(self):
        expression = sql_function("lower", Book.title, py_type=str)
        assert expression.py_type is str

    @pytest.mark.parametrize("bad", ["bad; drop", "1abc", "", "lower(x)", "a b"])
    def test_function_names_are_validated(self, bad):
        with pytest.raises(ValueError, match="not a valid SQL function name"):
            sql_function(bad, Book.id)

    def test_dunder_attributes_are_not_functions(self):
        with pytest.raises(AttributeError):
            func._private


class TestCase:
    def test_single_when_with_else(self):
        clause, params = select_of(
            Query(case((Book.id > 10, "hi"), else_="lo"))
        )
        assert clause == "CASE WHEN id > $1 THEN $2 ELSE $3 END"
        assert params == (10, "hi", "lo")

    def test_several_whens(self):
        clause, _ = select_of(
            Query(case((Book.id > 10, "a"), (Book.id > 5, "b"), else_="c"))
        )
        assert clause == "CASE WHEN id > $1 THEN $2 WHEN id > $3 THEN $4 ELSE $5 END"

    def test_without_else(self):
        clause, _ = select_of(Query(case((Book.id > 10, "hi"))))
        assert clause == "CASE WHEN id > $1 THEN $2 END"

    def test_values_may_be_expressions(self):
        clause, _ = select_of(Query(case((Book.id > 1, Book.author_id), else_=Book.id)))
        assert clause == "CASE WHEN id > $1 THEN author_id ELSE id END"

    def test_requires_at_least_one_pair(self):
        with pytest.raises(ValueError, match="at least one"):
            case()

    def test_condition_must_be_a_predicate(self):
        with pytest.raises(TypeError, match="must be a predicate"):
            case((Book.id, "x"))

    def test_pairs_must_be_pairs(self):
        with pytest.raises(TypeError, match=r"\(condition, value\) pairs"):
            case((Book.id > 1, "a", "b"))

    def test_labelled(self):
        clause, _ = select_of(Query(case((Book.id > 1, "a")).label("tier")))
        assert clause.endswith("END AS tier")


class TestWindows:
    def test_row_number_with_partition_and_order(self):
        clause, _ = select_of(
            Query(row_number().over(partition_by=Book.author_id,
                                    order_by=(Book.id, "DESC")))
        )
        assert clause == "row_number() OVER (PARTITION BY author_id ORDER BY id DESC)"

    def test_bare_over(self):
        # A window function references no column, so the query needs one to
        # establish its FROM table — hence Book.id here.
        clause, _ = select_of(Query(Book.id, row_number().over()))
        assert clause == "id, row_number() OVER ()"

    def test_several_order_columns(self):
        clause, _ = select_of(Query(rank().over(order_by=[Book.author_id, Book.id])))
        assert clause == "rank() OVER (ORDER BY author_id, id)"

    def test_a_directed_pair_is_one_entry_not_two(self):
        """`(col, "DESC")` and `(col_a, col_b)` are both tuples; only the second
        element being a direction string tells them apart."""
        one = select_of(Query(rank().over(order_by=(Book.id, "DESC"))))[0]
        two = select_of(Query(rank().over(order_by=(Book.id, Book.author_id))))[0]
        assert one == "rank() OVER (ORDER BY id DESC)"
        assert two == "rank() OVER (ORDER BY id, author_id)"

    def test_aggregate_over(self):
        clause, _ = select_of(Query(sum_(Book.id).over(partition_by=Book.author_id)))
        assert clause == "sum(id) OVER (PARTITION BY author_id)"

    def test_count_over(self):
        assert select_of(Query(Book.id, count().over()))[0] == "id, count(*) OVER ()"

    def test_frame_clause(self):
        clause, _ = select_of(
            Query(sum_(Book.id).over(order_by=Book.id,
                                     frame="ROWS BETWEEN 1 PRECEDING AND CURRENT ROW"))
        )
        assert clause.endswith("ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)")

    @pytest.mark.parametrize("bad", ["ROWS; DROP TABLE x", "ROWS BETWEEN (1)", "a-b"])
    def test_frame_is_validated_because_it_is_inserted_verbatim(self, bad):
        with pytest.raises(ValueError, match="may only contain"):
            sum_(Book.id).over(frame=bad)

    def test_order_direction_is_validated_at_construction(self):
        # Deferring to render time means a typo surfaces on first execution.
        with pytest.raises(ValueError, match="must be 'ASC' or 'DESC'"):
            sum_(Book.id).over(order_by=(Book.id, "SIDEWAYS"))

    @pytest.mark.parametrize("factory,expected", [
        (row_number, "row_number()"),
        (rank, "rank()"),
        (dense_rank, "dense_rank()"),
    ])
    def test_ranking_functions(self, factory, expected):
        clause, _ = select_of(Query(Book.id, factory().over()))
        assert clause == f"id, {expected} OVER ()"

    def test_lag_and_lead_take_an_offset(self):
        clause, params = select_of(Query(lag(Book.id), lead(Book.id, 2)))
        assert clause == "lag(id, $1), lead(id, $2)"
        assert params == (1, 2)

    def test_value_functions(self):
        clause, _ = select_of(Query(first_value(Book.id), last_value(Book.id)))
        assert clause == "first_value(id), last_value(id)"

    def test_a_query_of_only_table_independent_expressions_is_refused(self):
        # There is nothing to put after FROM, and guessing a table would be worse
        # than saying so.
        with pytest.raises(TypeError, match="no table to select from"):
            Query(row_number().over())

    def test_ntile(self):
        clause, params = select_of(Query(ntile(4).over(order_by=Book.id)))
        assert clause == "ntile($1) OVER (ORDER BY id)"
        assert params == (4,)

    def test_window_over_a_joined_column_qualifies(self):
        clause, _ = select_of(
            Query(Author.id, row_number().over(partition_by=Author.id,
                                               order_by=Book.id))
            .join(Book, Book.author_id == Author.id)
        )
        assert "PARTITION BY t_authors.id ORDER BY t_books.id" in clause


class TestEndToEnd:
    def test_arithmetic(self, run_query):
        rows = run_query(Query(Book.id, Book.id * 10).order_by(Book.id).limit(2))
        assert rows == [(10, 100), (11, 110)]

    def test_concat(self, run_query):
        rows = run_query(
            Query(Book.title.concat("!")).where(Book.id == 12)
        )
        assert rows == [("compilers!",)]

    def test_function(self, run_query):
        rows = run_query(Query(func.upper(Book.title)).where(Book.id == 12))
        assert rows == [("COMPILERS",)]

    def test_case(self, run_query):
        rows = run_query(
            Query(Book.id, case((Book.author_id == 1, "ada"), else_="other"))
            .order_by(Book.id)
        )
        assert rows == [(10, "ada"), (11, "ada"), (12, "other"), (13, "other")]

    def test_row_number_per_partition(self, run_query):
        rows = run_query(
            Query(Book.author_id, Book.id,
                  row_number().over(partition_by=Book.author_id, order_by=Book.id))
            .order_by(Book.author_id, Book.id)
        )
        assert rows == [(1, 10, 1), (1, 11, 2), (2, 12, 1), (3, 13, 1)]

    def test_running_total_with_a_frame(self, run_query):
        rows = run_query(
            Query(Book.id, sum_(Book.id).over(order_by=Book.id,
                                              frame="ROWS UNBOUNDED PRECEDING"))
            .order_by(Book.id)
        )
        assert [total for _, total in rows] == [10, 21, 33, 46]

    def test_windowed_aggregate_needs_no_group_by(self, run_query):
        rows = run_query(
            Query(Book.id, count().over()).order_by(Book.id).limit(2)
        )
        assert rows == [(10, 4), (11, 4)]

    def test_arithmetic_in_a_predicate(self, run_query):
        rows = run_query(Query(Book.id).where(Book.id % 2 == 0).order_by(Book.id))
        assert rows == [(10,), (12,)]


class TestCountForms:
    def test_count_star(self, ):
        assert select_of(Query(Book.id, count()))[0] == "id, count(*)"

    def test_count_of_a_model_renders_star_and_supplies_the_table(self):
        # `Query(count())` alone has no FROM to derive; `count(Model)` is the
        # ordinary "how many rows" and names the table without making the caller
        # pick an irrelevant column.
        sql, _ = Query(count(Book)).to_sql()
        assert sql == "SELECT count(*) FROM t_books"

    def test_count_of_a_model_with_a_predicate(self):
        sql, params = Query(count(Book)).where(Book.author_id == 1).to_sql(placeholder="$")
        assert sql == "SELECT count(*) FROM t_books WHERE author_id = $1"
        assert params == (1,)

    def test_count_distinct_needs_a_column(self):
        # count(DISTINCT *) is a syntax error; refusing here beats a server error
        # about a statement the caller did not write.
        with pytest.raises(ValueError, match="needs a column"):
            count(distinct=True)
        with pytest.raises(ValueError, match="needs a column"):
            count(Book, distinct=True)

    def test_count_of_a_model_end_to_end(self, run_query):
        assert run_query(Query(count(Book))) == [(4,)]
        assert run_query(Query(count(Book)).where(Book.author_id == 1)) == [(2,)]
