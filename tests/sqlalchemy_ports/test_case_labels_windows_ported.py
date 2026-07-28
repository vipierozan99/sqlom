"""Ported from SQLAlchemy's test/sql/test_case_statement.py, test_labels.py,
test_lateral.py (window-function subset only; LATERAL joins are unsupported in
sqlom) — extending tests/test_expressions.py and tests/test_grouping.py.

Adapted to sqlom. Skipped:

* Everything in test_lateral.py — every test in that file exercises a LATERAL
  join (`.lateral()`, `select_from`, `join` variants over a correlated
  subquery), and sqlom has no LATERAL support at all (grepped sqlom/query.py
  and sqlom/expr.py for "lateral": no hits). There was no window-function
  pattern hiding in that file independent of LATERAL, so nothing was ported
  from it.
* Most of test_labels.py — it is almost entirely about SQLAlchemy's
  dialect-specific automatic label generation and truncation machinery
  (`LABEL_STYLE_*`, `max_identifier_length`, Oracle/DB2-style 30-character
  identifier limits, `IdentifierError`) plus `Table`/`MetaData`/DDL fixtures.
  sqlom has none of that: it never invents a label, requires an explicit
  `.label()` wherever SQL needs a name, and has no Table/MetaData/DDL layer.
  What *does* map — label collisions, relabelling, labelling a
  subquery-exposed column, and the "unlabelled aggregate/expression in a
  CTE or subquery is refused" rule from README §10/§12 — is ported below.
* test_case_statement.py's dict-based `case({cond: val, ...})` form,
  `text()`/`literal_column()` arguments, and the `.type` inference tests —
  sqlom has no raw-text expression type, and aggregates/case expressions
  are deliberately left `py_type=None` rather than inferring SQL types.
  Ported what maps: several WHEN branches, no ELSE, nested CASE, CASE used
  in WHERE/ORDER BY/GROUP BY/arithmetic, None as a THEN/ELSE value — and,
  since it has since been added (`case(..., value=col)` — the "simple
  case" form, `CASE col WHEN match THEN ... END`, with each match compared
  against `value` by equality rather than being its own predicate), that
  too: see `test_simple_case_form_with_value` and
  `test_simple_case_form_end_to_end`.
"""

import pytest

from sqlom import (
    Query,
    and_,
    case,
    count,
    dense_rank,
    first_value,
    func,
    lag,
    last_value,
    lead,
    ntile,
    or_,
    rank,
    row_number,
    sum_,
)
from tests.conftest import Book


def select_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" FROM ")[0].removeprefix("SELECT "), params


def where_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" WHERE ", 1)[1], params


# --------------------------------------------------------------------------
# CASE: more branch shapes, and CASE used outside a bare select list
# --------------------------------------------------------------------------


class TestCaseMoreVariants:
    def test_three_when_branches_with_else(self):
        clause, params = select_of(
            Query(case(
                (Book.id > 12, "d"),
                (Book.id > 11, "c"),
                (Book.id > 10, "b"),
                else_="a",
            ))
        )
        assert clause == (
            "CASE WHEN id > $1 THEN $2 WHEN id > $3 THEN $4 "
            "WHEN id > $5 THEN $6 ELSE $7 END"
        )
        assert params == (12, "d", 11, "c", 10, "b", "a")

    def test_case_without_else_end_to_end(self, run_query):
        # No ELSE means an unmatched row hydrates to None, not a database
        # error — this is the run-time counterpart of test_without_else in
        # tests/test_expressions.py, which only checks the rendered SQL.
        rows = run_query(
            Query(Book.id, case((Book.author_id == 1, "prolific")))
            .order_by(Book.id)
        )
        assert rows == [
            (10, "prolific"), (11, "prolific"), (12, None), (13, None),
        ]

    def test_case_value_used_in_where_clause(self):
        category = case((Book.author_id == 1, "prolific"), else_="other")
        clause, params = where_of(Query(Book.id).where(category == "prolific"))
        assert clause == (
            "CASE WHEN author_id = $1 THEN $2 ELSE $3 END = $4"
        )
        assert params == (1, "prolific", "other", "prolific")

    def test_case_value_used_in_where_clause_end_to_end(self, run_query):
        category = case((Book.author_id == 1, "prolific"), else_="other")
        rows = run_query(
            Query(Book.id).where(category == "prolific").order_by(Book.id)
        )
        assert rows == [(10,), (11,)]

    def test_case_value_used_in_order_by_end_to_end(self, run_query):
        # Sorting by a CASE puts rows in groups the schema itself has no
        # column for — here, "id under 12" before "id 12 and over" — with a
        # real column as the tiebreaker inside each group.
        grouping = case((Book.id < 12, 1), else_=0)
        rows = run_query(
            Query(Book.id).order_by(grouping, Book.id)
        )
        assert [book_id for book_id, in rows] == [12, 13, 10, 11]

    def test_case_used_in_arithmetic(self):
        clause, params = select_of(
            Query(case((Book.id > 10, 5), else_=0) + 1)
        )
        assert clause == "(CASE WHEN id > $1 THEN $2 ELSE $3 END + $4)"
        assert params == (10, 5, 0, 1)

    def test_case_condition_combining_and(self):
        clause, _ = select_of(
            Query(case(
                (and_(Book.id > 10, Book.author_id == 1), "match"),
                else_="no match",
            ))
        )
        assert clause == (
            "CASE WHEN (id > $1 AND author_id = $2) THEN $3 ELSE $4 END"
        )

    def test_case_condition_combining_or(self):
        clause, _ = select_of(
            Query(case(
                (or_(Book.author_id == 2, Book.author_id == 3), "minor"),
                else_="major",
            ))
        )
        assert clause == (
            "CASE WHEN (author_id = $1 OR author_id = $2) THEN $3 ELSE $4 END"
        )

    def test_nested_case_as_a_when_value(self):
        inner = case((Book.author_id == 1, "A"), else_="Z")
        outer = case((Book.id > 10, inner), else_="other")
        clause, params = select_of(Query(outer))
        assert clause == (
            "CASE WHEN id > $1 THEN CASE WHEN author_id = $2 THEN $3 "
            "ELSE $4 END ELSE $5 END"
        )
        assert params == (10, 1, "A", "Z", "other")

    def test_case_group_by_case_expression_end_to_end(self, run_query):
        tier = case((Book.id < 12, "early"), else_="late")
        rows = run_query(
            Query(tier.label("tier"), count())
            .group_by(tier)
            .order_by(tier)
        )
        assert rows == [("early", 2), ("late", 2)]

    def test_case_else_none_is_the_same_as_no_else(self):
        # Case.to_sql() only emits ELSE when self.else_ is not None, so
        # else_=None is indistinguishable from omitting else_ altogether —
        # there is no way to spell an explicit "ELSE NULL" through this API.
        with_none, params = select_of(
            Query(case((Book.id > 10, 1), else_=None))
        )
        without_else, _ = select_of(Query(case((Book.id > 10, 1))))
        assert with_none == without_else == "CASE WHEN id > $1 THEN $2 END"
        assert params == (10, 1)

    def test_simple_case_form_with_value(self):
        # Fixed: case() now accepts value=, the "simple CASE" form —
        # CASE value WHEN match THEN result ... END — matching SQLAlchemy's
        # case(..., value=col). Each pair's first element is compared
        # against `value` by equality rather than being its own predicate.
        clause, params = select_of(
            Query(case((1, "a"), (2, "b"), value=Book.author_id, else_="c"))
        )
        assert clause == "CASE author_id WHEN $1 THEN $2 WHEN $3 THEN $4 ELSE $5 END"
        assert params == (1, "a", 2, "b", "c")

    def test_simple_case_form_end_to_end(self, run_query):
        rows = run_query(
            Query(Book.id, case((1, "alpha"), (2, "beta"), value=Book.author_id,
                                 else_="other"))
            .order_by(Book.id)
        )
        assert rows == [
            (10, "alpha"), (11, "alpha"), (12, "beta"), (13, "other"),
        ]


# --------------------------------------------------------------------------
# Window functions: partition + order combinations not in test_expressions.py
# --------------------------------------------------------------------------


class TestWindowMorePatterns:
    def test_rank_with_partition_and_order(self):
        clause, _ = select_of(
            Query(rank().over(partition_by=Book.author_id,
                              order_by=(Book.id, "DESC")))
        )
        assert clause == "rank() OVER (PARTITION BY author_id ORDER BY id DESC)"

    def test_dense_rank_with_partition_and_order(self):
        clause, _ = select_of(
            Query(dense_rank().over(partition_by=Book.author_id,
                                    order_by=(Book.id, "DESC")))
        )
        assert clause == (
            "dense_rank() OVER (PARTITION BY author_id ORDER BY id DESC)"
        )

    def test_ntile_with_partition_by(self):
        clause, params = select_of(
            Query(ntile(2).over(partition_by=Book.author_id, order_by=Book.id))
        )
        assert clause == "ntile($1) OVER (PARTITION BY author_id ORDER BY id)"
        assert params == (2,)

    def test_first_value_over_partition_and_order(self):
        clause, _ = select_of(
            Query(first_value(Book.id).over(partition_by=Book.author_id,
                                            order_by=Book.id))
        )
        assert clause == (
            "first_value(id) OVER (PARTITION BY author_id ORDER BY id)"
        )

    def test_last_value_over_partition_and_order_with_frame(self):
        # last_value() needs the frame widened to the whole partition, or it
        # only sees rows up to the current one — the same portability wart
        # SQLAlchemy's docs call out for this function.
        clause, _ = select_of(
            Query(last_value(Book.id).over(
                partition_by=Book.author_id, order_by=Book.id,
                frame="ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
            ))
        )
        assert clause == (
            "last_value(id) OVER (PARTITION BY author_id ORDER BY id "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
        )

    def test_lag_over_partition_and_order(self):
        clause, params = select_of(
            Query(lag(Book.id).over(partition_by=Book.author_id, order_by=Book.id))
        )
        assert clause == "lag(id, $1) OVER (PARTITION BY author_id ORDER BY id)"
        assert params == (1,)

    def test_lead_with_offset_over_partition_and_order(self):
        clause, params = select_of(
            Query(lead(Book.id, 2).over(partition_by=Book.author_id,
                                        order_by=Book.id))
        )
        assert clause == "lead(id, $1) OVER (PARTITION BY author_id ORDER BY id)"
        assert params == (2,)

    def test_lag_and_lead_accept_a_default_value(self):
        # Matches SQLAlchemy's func.lag(col, offset, default): the row-out-of-
        # range fallback is a third positional argument to the function call.
        clause, params = select_of(
            Query(lag(Book.id, 1, 0).over(order_by=Book.id))
        )
        assert clause == "lag(id, $1, $2) OVER (ORDER BY id)"
        assert params == (1, 0)

        clause, params = select_of(
            Query(lead(Book.id, 2, -1).over(order_by=Book.id))
        )
        assert clause == "lead(id, $1, $2) OVER (ORDER BY id)"
        assert params == (2, -1)

    def test_mixed_ascending_and_descending_order_columns(self):
        clause, _ = select_of(
            Query(rank().over(order_by=[Book.author_id, (Book.id, "DESC")]))
        )
        assert clause == "rank() OVER (ORDER BY author_id, id DESC)"

    def test_frame_combined_with_partition_and_order(self):
        clause, _ = select_of(
            Query(sum_(Book.id).over(
                partition_by=Book.author_id, order_by=Book.id,
                frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            ))
        )
        assert clause == (
            "sum(id) OVER (PARTITION BY author_id ORDER BY id "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        )

    def test_window_labelled_alongside_a_plain_column(self):
        clause, _ = select_of(
            Query(Book.id, row_number().over(order_by=Book.id).label("rn"))
        )
        assert clause == "id, row_number() OVER (ORDER BY id) AS rn"

    def test_rank_vs_dense_rank_with_ties_end_to_end(self, run_query):
        # rank() leaves gaps after a tie, dense_rank() does not — the
        # distinguishing behaviour between the two, exercised over an
        # order_by that deliberately ties two rows against two others.
        tier = case((Book.id < 12, "A"), else_="B")
        rows = run_query(
            Query(Book.id, rank().over(order_by=tier),
                  dense_rank().over(order_by=tier))
            .order_by(Book.id)
        )
        assert rows == [(10, 1, 1), (11, 1, 1), (12, 3, 2), (13, 3, 2)]

    def test_ntile_end_to_end(self, run_query):
        rows = run_query(
            Query(Book.id, ntile(2).over(order_by=Book.id)).order_by(Book.id)
        )
        assert rows == [(10, 1), (11, 1), (12, 2), (13, 2)]

    def test_lag_and_lead_end_to_end(self, run_query):
        rows = run_query(
            Query(Book.author_id, Book.id,
                  lag(Book.id).over(partition_by=Book.author_id, order_by=Book.id),
                  lead(Book.id).over(partition_by=Book.author_id, order_by=Book.id))
            .order_by(Book.author_id, Book.id)
        )
        assert rows == [
            (1, 10, None, 11),
            (1, 11, 10, None),
            (2, 12, None, None),
            (3, 13, None, None),
        ]

    def test_unlabelled_window_function_in_a_subquery_is_refused(self):
        with pytest.raises(ValueError, match="usable column name"):
            Query(Book.id, row_number().over(order_by=Book.id)).subquery("s")

    def test_unlabelled_window_function_in_a_cte_is_refused(self):
        with pytest.raises(ValueError, match="usable column name"):
            Query(Book.id, row_number().over(order_by=Book.id)).cte("c")

    def test_labelled_window_function_exposed_via_a_subquery_end_to_end(self, run_query):
        sub = (
            Query(Book.id, row_number().over(order_by=Book.id).label("rn"))
            .subquery("s")
        )
        rows = run_query(Query(sub.id, sub.rn).order_by(sub.id))
        assert rows == [(10, 1), (11, 2), (12, 3), (13, 4)]


# --------------------------------------------------------------------------
# Labels: collisions, relabelling, subquery-exposed columns, the
# unlabelled-expression-in-a-derived-table rule (README §10/§12)
# --------------------------------------------------------------------------


class TestLabelEdgeCases:
    def test_label_collision_renders_both_labels_verbatim(self):
        # Unlike SQLAlchemy, which auto-disambiguates colliding labels
        # (LABEL_STYLE_DISAMBIGUATE_ONLY), sqlom does not invent names at
        # all — it renders exactly what was asked for, collision included.
        sql, _ = Query(Book.id.label("n"), Book.author_id.label("n")).to_sql()
        assert sql == "SELECT id AS n, author_id AS n FROM t_books"

    def test_relabelling_a_labelled_expression_replaces_the_name(self):
        # Labelled inherits .label() from Expression, so this is legal; the
        # inner label is discarded rather than stacked into two AS clauses.
        twice_labelled = sum_(Book.id).label("first").label("second")
        clause, _ = select_of(Query(twice_labelled))
        assert clause == "sum(id) AS second"

    def test_labelling_a_subquery_exposed_column(self):
        sub = Query(Book.id, Book.title.label("book_title")).subquery("s")
        clause, _ = select_of(Query(sub.book_title.label("renamed")))
        assert clause == "book_title AS renamed"

    def test_labelling_a_subquery_exposed_column_keeps_its_type(self):
        sub = Query(Book.id, Book.title.label("book_title")).subquery("s")
        query = Query(sub.book_title.label("renamed"))
        assert query.output_columns() == [("renamed", str)]

    def test_unlabelled_case_in_a_subquery_is_refused(self):
        with pytest.raises(ValueError, match="usable column name"):
            Query(Book.id, case((Book.id > 10, "hi"), else_="lo")).subquery("s")

    def test_unlabelled_arithmetic_in_a_cte_is_refused(self):
        with pytest.raises(ValueError, match="usable column name"):
            Query(Book.id, Book.id * 2).cte("c")

    def test_labelled_binary_op_renders_as_alias(self):
        clause, params = select_of(Query((Book.id * 2).label("doubled")))
        assert clause == "(id * $1) AS doubled"
        assert params == (2,)

    def test_labelled_function_call_renders_as_alias(self):
        clause, _ = select_of(Query(func.upper(Book.title).label("upper_title")))
        assert clause == "upper(title) AS upper_title"

    def test_output_name_prefers_the_label_over_the_expression(self):
        assert (Book.id * 2).label("doubled").output_name() == "doubled"
        assert func.upper(Book.title).label("up").output_name() == "up"
        assert count().label("n").output_name() == "n"
