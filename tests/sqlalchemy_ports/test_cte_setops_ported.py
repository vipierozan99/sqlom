"""Ported from SQLAlchemy's test/sql/test_cte.py, adding coverage beyond
tests/test_ctes.py and tests/test_set_operations.py. Adapted to rowform.

Skipped:

* Data-modifying CTEs (`WITH x AS (UPDATE/INSERT/DELETE ... RETURNING *) SELECT/
  INSERT/UPDATE/DELETE ...`) and `add_cte()` — rowform's DML statements have no
  CTE support at all, so nothing in SQLAlchemy's DML-CTE sections (roughly
  test_cte.py lines 1250-2118) has an equivalent to port.
* SQLAlchemy's `nesting=True` / `add_cte(nest_here=True)` feature, tested in
  `NestingCTETest` (test_cte.py lines 2119-3167). rowform always hoists every CTE
  it finds into one top-level WITH clause — there is no per-scope nesting, and
  that hoisting is already the subject of
  `test_ctes.py::TestRendering::test_cte_in_a_compound_select_is_hoisted_in_front`
  and `test_the_body_is_defined_once_however_often_it_is_referenced` — so the
  whole class has no rowform analogue.
* `alias()`-based tests (`test_union_cte_aliases`, `test_cloned_alias`,
  `test_all_aliases`, `test_multi_subq_alias`, `test_cte_refers_to_aliased_cte_
  twice`, `test_named_alias_*`) — rowform has no `Alias`-of-a-CTE construct; a CTE
  is referenced directly by the name it was built with.
* Identifier-quoting tests (`test_reserved_quote`, `test_multi_subq_quote`,
  `test_named_alias_quote`, `test_named_alias_disable_quote`) and
  `prefix_with()`/`suffix_with()` (`test_prefixes`, `test_suffixes`) — rowform
  does no identifier quoting and has no prefix/suffix hooks.
* Anonymous-label / duplicate-column-name resolution tests
  (`test_recursive_w_anon_labels`, `test_conflicting_names`,
  `test_wrecur_dupe_col_names*`, `test_cte_w_annotated`,
  `test_with_recursive_no_name_currently_buggy`) — these exercise
  SQLAlchemy's automatic `anon_1`-style labelling and same-name-CTE conflict
  detection, neither of which rowform has (every CTE/column in rowform is
  explicitly named, and building two CTEs under the same alias is just two
  independent Python objects with no cross-checking).
* `test_standalone_function` / `test_no_alias_construct` (the module-level
  `cte()` function and the "CTE is not directly constructible" guard) — rowform's
  `CTE` is public and directly constructible (`recursive_cte()` builds one that
  way internally); there is no standalone-function form to test.
* Positional-bind-parameter tests (`test_positional_binds*`) — dialect/paramstyle
  concerns already covered generically by the `$`-numbering tests in both
  existing files.
"""

import pytest

from rowform import (
    CompoundSelect,
    CTE,
    Query,
    count,
    exists,
    recursive_cte,
)

from tests.conftest import Author, Book


def sql_of(query, placeholder="$"):
    return query.to_sql(placeholder=placeholder)[0]


def book_counts(alias="counts"):
    return (Query(Book.author_id, count(Book.id).label("n"))
            .group_by(Book.author_id)
            .cte(alias))


class TestDependencyOrdering:
    """`_collect_ctes` walks the whole node graph and has to get the order
    right whenever the dependency graph is deeper than one level. The existing
    suite only goes two CTEs deep (`test_nested_ctes_are_emitted_in_dependency_
    order`); these push further: a three-level chain, a diamond where two
    siblings share one base, and a single CTE referenced from every kind of
    place a name can appear at once."""

    # rowform-original test (no SQLAlchemy equivalent)
    def test_three_level_chain_orders_dependencies(self):
        level_a = book_counts("level_a")
        level_b = Query(level_a.author_id, level_a.n).where(level_a.n > 0).cte("level_b")
        level_c = Query(level_b.author_id).where(level_b.n > 1).cte("level_c")
        rendered = sql_of(Query(level_c.author_id))
        assert rendered.count("WITH") == 1
        assert (rendered.index("level_a AS")
                < rendered.index("level_b AS")
                < rendered.index("level_c AS"))
        assert rendered == (
            "WITH level_a AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id), level_b AS (SELECT author_id, n FROM "
            "level_a WHERE n > $1), level_c AS (SELECT author_id FROM "
            "level_b WHERE n > $2) SELECT author_id FROM level_c"
        )

    # rowform-original test (no SQLAlchemy equivalent)
    def test_diamond_shared_base_defined_once_and_before_dependents(self):
        # Two sibling CTEs both build on `base`; the outer query joins the
        # siblings, never touching `base` directly. A walk that only followed
        # the outer query's immediate sources would miss `base` entirely, and
        # one that didn't de-duplicate would define it twice.
        base = book_counts("base")
        left = Query(base.author_id, base.n).where(base.n > 0).cte("left_cte")
        right = Query(base.author_id, base.n).where(base.n < 100).cte("right_cte")
        outer = Query(left.author_id, right.n).join(right, right.author_id == left.author_id)
        rendered = sql_of(outer)
        assert rendered.count("WITH") == 1
        assert rendered.count("base AS (") == 1
        assert (rendered.index("base AS")
                < rendered.index("left_cte AS")
                < rendered.index("right_cte AS"))
        assert rendered == (
            "WITH base AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id), left_cte AS (SELECT author_id, n FROM base "
            "WHERE n > $1), right_cte AS (SELECT author_id, n FROM base "
            "WHERE n < $2) SELECT left_cte.author_id, right_cte.n FROM "
            "left_cte JOIN right_cte ON right_cte.author_id = left_cte.author_id"
        )

    # rowform-original test (no SQLAlchemy equivalent)
    def test_cte_collected_once_from_five_reference_sites(self):
        # `counts` is referenced from: a JOIN, a second JOIN (via `other`,
        # whose own body references it), a WHERE IN subquery, and an EXISTS.
        # Every one of those is a different attribute path through the node
        # graph; the walk has to find all of them and still emit `counts`
        # only once.
        counts = book_counts()
        other = Query(counts.author_id).where(counts.n > 0).cte("other")
        query = (
            Query(Author, counts.n, other.author_id)
            .join(counts, counts.author_id == Author.id)
            .join(other, other.author_id == Author.id)
            .where(Author.id.in_(Query(counts.author_id).where(counts.n > 1)))
            .where(exists(
                Query(counts.author_id).correlate(Author)
                                       .where(counts.author_id == Author.id)
            ))
        )
        rendered = sql_of(query)
        assert rendered.count("WITH") == 1
        assert rendered.count("counts AS (") == 1
        assert rendered.index("counts AS") < rendered.index("other AS")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_recursive_cte_ordered_before_a_dependent_plain_cte(self):
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: Query(Book.id, Book.author_id).join(cte, Book.author_id == cte.id),
        )
        downstream = Query(tree.id).where(tree.id > 5).cte("downstream")
        rendered = sql_of(Query(downstream.id))
        assert rendered.startswith("WITH RECURSIVE ")
        assert rendered.index("tree(") < rendered.index("downstream AS")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_plain_cte_ordered_before_the_recursive_cte_that_joins_it(self):
        # The dependency runs the other way from the previous test: a plain
        # CTE that the recursive term itself joins to.
        active_authors = Query(Author.id).where(Author.active == True).cte(  # noqa: E712
            "active_authors"
        )
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: (Query(Book.id, Book.author_id)
                         .join(cte, Book.author_id == cte.id)
                         .join(active_authors, active_authors.id == Book.author_id)),
        )
        rendered = sql_of(Query(tree.id))
        assert rendered.startswith("WITH RECURSIVE ")
        assert "active_authors AS (" in rendered
        assert rendered.index("active_authors AS") < rendered.index("tree(")


class TestCteAsFromSource:
    """`.cte("name")` returns something usable as a source the same way a
    model is: passed to `Query()` directly, not column-by-column, it selects
    every output column of the CTE. Both existing files only ever select
    individual `cte.column` expressions."""

    # rowform-original test (no SQLAlchemy equivalent)
    def test_plain_cte_selected_as_a_whole_entity(self):
        counts = book_counts()
        query = Query(counts)
        assert query.output_columns() == [("author_id", int), ("n", int)]
        assert sql_of(query) == (
            "WITH counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id) SELECT author_id, n FROM counts"
        )

    # rowform-original test (no SQLAlchemy equivalent)
    def test_cte_as_whole_entity_then_joined_to_a_model(self):
        counts = book_counts()
        query = Query(counts).join(Author, Author.id == counts.author_id)
        assert sql_of(query) == (
            "WITH counts AS (SELECT author_id, count(id) AS n FROM t_books "
            "GROUP BY author_id) SELECT counts.author_id, counts.n FROM "
            "counts JOIN t_authors ON t_authors.id = counts.author_id"
        )

    # rowform-original test (no SQLAlchemy equivalent)
    def test_recursive_cte_selected_as_a_whole_entity(self):
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: Query(Book.id, Book.author_id).join(cte, Book.author_id == cte.id),
        )
        query = Query(tree)
        assert query.output_columns() == [("id", int), ("author_id", int)]
        assert sql_of(query) == (
            "WITH RECURSIVE tree(id, author_id) AS (SELECT id, author_id "
            "FROM t_books WHERE author_id = $1 UNION ALL SELECT t_books.id, "
            "t_books.author_id FROM t_books JOIN tree ON t_books.author_id "
            "= tree.id) SELECT id, author_id FROM tree"
        )


class TestRecursiveEdgeCases:
    # rowform-original test (no SQLAlchemy equivalent)
    def test_recursive_term_may_relabel_columns_the_base_names_win(self):
        # The recursive term can select the "same" columns under different
        # labels than the base did; the CTE is still addressed, and its
        # WITH-clause column list is still spelled, using the base's names.
        tree = recursive_cte(
            "tree",
            Query(Book.id.label("bid"), Book.author_id).where(Book.author_id == 1),
            lambda cte: (Query(Book.id.label("totally_different_name"), Book.author_id)
                         .join(cte, Book.author_id == cte.author_id)),
        )
        assert tree.column_names == ["bid", "author_id"]
        rendered = sql_of(Query(tree.bid))
        assert rendered == (
            "WITH RECURSIVE tree(bid, author_id) AS (SELECT id AS bid, "
            "author_id FROM t_books WHERE author_id = $1 UNION ALL SELECT "
            "t_books.id AS totally_different_name, t_books.author_id FROM "
            "t_books JOIN tree ON t_books.author_id = tree.author_id) "
            "SELECT bid FROM tree"
        )

    # rowform-original test (no SQLAlchemy equivalent)
    def test_recursive_step_may_union_two_recursive_terms(self):
        # `step` need not return a single Query — anything with `_render`
        # passes the type check, including a compound built from two
        # differently-filtered recursive terms.
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: (
                Query(Book.id, Book.author_id).join(cte, Book.author_id == cte.id)
                .where(Book.id < 100)
                .union(
                    Query(Book.id, Book.author_id).join(cte, Book.author_id == cte.id)
                    .where(Book.id >= 100)
                )
            ),
        )
        rendered = sql_of(Query(tree.id))
        assert rendered.count("UNION") == 2
        assert rendered.startswith("WITH RECURSIVE tree(id, author_id) AS (")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_recursive_cte_referenced_from_a_where_subquery(self, run_query, db):
        # test_ctes.py's `test_a_cte_referenced_only_inside_a_subquery_is_
        # still_defined` covers a *plain* CTE this way; recursion adds its own
        # self-reference to the walk, so it is worth checking the guard still
        # lets the outer reference through.
        tree = recursive_cte(
            "tree",
            Query(Book.id, Book.author_id).where(Book.author_id == 1),
            lambda cte: Query(Book.id, Book.author_id).join(cte, Book.author_id == cte.id),
        )
        query = (Query(Author.name)
                 .where(Author.id.in_(Query(tree.author_id)))
                 .order_by(Author.name))
        sql, params = query.to_sql(placeholder="?")
        assert sql.startswith("WITH RECURSIVE tree")
        assert sql.count("WITH") == 1
        assert db.execute(sql, params).fetchall() == [("ada",)]


class TestCompoundAssociativity:
    """UNION/EXCEPT chained through the same operator flatten into one
    `CompoundSelect` (already covered), but EXCEPT is not associative: `(A
    EXCEPT B) EXCEPT C` and `A EXCEPT (B EXCEPT C)` are different queries.
    rowform's render never parenthesises an operand, so the two can only be told
    apart by which one is nested where."""

    # rowform-original test (no SQLAlchemy equivalent)
    def test_chained_except_matches_left_associative_sql_default(self, run_query):
        # Fluent chaining always nests the running total as the *first*
        # operand (`_combine` builds `[self, other]`), so the unparenthesised
        # SQL text reads left to right exactly as it was built — which is
        # also plain SQL's own default grouping for EXCEPT, so no parens are
        # needed for this shape to be correct.
        rows = run_query(
            Query(Author.id).where(Author.id < 3)
            .except_(Query(Author.id).where(Author.id < 2))
            .except_(Query(Author.id).where(Author.id == 2))
        )
        # ({1,2} EXCEPT {1}) EXCEPT {2} = {2} EXCEPT {2} = {}
        assert rows == []

    # rowform-original test (no SQLAlchemy equivalent)
    def test_manually_nested_compound_renders_without_parens(self):
        # NOTE: gap - CompoundSelect._render never wraps an operand in
        # parentheses, including when that operand is itself a CompoundSelect
        # occupying a non-first position. Built directly (bypassing the
        # fluent API, which never produces this shape), a right-nested
        # EXCEPT and a left-nested EXCEPT render byte-identical SQL text, even
        # though EXCEPT is not associative and the two mean different things.
        # There is no fluent-API test for the "wrong" grouping actually
        # changing results because most backends (sqlite included) do not
        # accept a parenthesised operand in this position at all, so the gap
        # is currently more "silently ambiguous" than "silently wrong" — but
        # it is real and worth a maintainer's attention if `CompoundSelect`
        # is ever constructed directly with a non-first nested compound.
        a = Query(Author.id).where(Author.id < 3)
        b = Query(Author.id).where(Author.id < 2)
        c = Query(Author.id).where(Author.id == 2)
        left_nested = a.except_(b).except_(c)
        right_nested = CompoundSelect("EXCEPT", [a, CompoundSelect("EXCEPT", [b, c])])
        assert sql_of(left_nested) == sql_of(right_nested)

    # rowform-original test (no SQLAlchemy equivalent)
    def test_three_way_intersect_is_order_independent(self, run_query):
        # Unlike EXCEPT, INTERSECT is associative and commutative, so however
        # rowform groups a chain of them the result is the same.
        rows = run_query(
            Query(Author.id).where(Author.id.in_([1, 2, 3]))
            .intersect(Query(Author.id).where(Author.id.in_([2, 3, 4])))
            .intersect(Query(Author.id).where(Author.id.in_([2, 3])))
        )
        assert sorted(rows) == [(2,), (3,)]


class TestOrderByOnCompound:
    """A compound orders by output column *name*; the existing suite covers
    a bare model column and a rejection of a name absent from every operand.
    These push into label-derived names and multi-column ordering."""

    # rowform-original test (no SQLAlchemy equivalent)
    def test_order_by_a_label_introduced_by_the_first_operand(self):
        compound = Query(Author.id.label("author_id")).union(Query(Book.author_id))
        sql, _ = compound.order_by("author_id").to_sql()
        assert sql.endswith("ORDER BY author_id")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_order_by_accumulates_across_multiple_calls(self):
        first = book_counts()
        second = book_counts("more_counts")
        compound = (Query(first.author_id, first.n)
                    .union(Query(second.author_id, second.n)))
        compound.order_by("author_id").order_by("n", descending=True)
        sql, _ = compound.to_sql()
        assert sql.endswith("ORDER BY author_id, n DESC")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_order_by_an_aggregate_labels_name(self):
        n_label = count(Book.id).label("n")
        compound = (Query(Book.author_id, n_label).group_by(Book.author_id)
                    .union(Query(Book.author_id, count(Book.id).label("n"))
                           .group_by(Book.author_id)))
        sql, _ = compound.order_by(n_label).to_sql()
        assert sql.endswith("ORDER BY n")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_order_by_rejects_a_name_only_present_in_the_second_operand(self):
        # `output_columns()` for a compound comes from the *first* operand
        # only, so a name the second operand happens to expose under a
        # different label still is not orderable.
        compound = (Query(Author.id, Author.name.label("x"))
                    .union(Query(Author.id, Author.active.label("y"))))
        assert compound.output_columns() == [("id", int), ("x", str)]
        with pytest.raises(ValueError, match="not an output column"):
            compound.order_by("y")


class TestOperandValidation:
    # rowform-original test (no SQLAlchemy equivalent)
    @pytest.mark.parametrize("method", [
        "union", "union_all", "intersect", "intersect_all", "except_", "except_all",
    ])
    def test_column_count_mismatch_detected_for_every_operator(self, method):
        # test_set_operations.py only checks this for the plain `union()`
        # entry point; the same construction path (`CompoundSelect.__init__`)
        # backs every operator, so this parametrizes across all six.
        with pytest.raises(ValueError, match="same number of columns"):
            getattr(Query(Author.id), method)(Query(Author.id, Author.name))

    # rowform-original test (no SQLAlchemy equivalent)
    def test_column_count_mismatch_surfaces_even_when_only_the_third_operand_differs(self):
        # The first two operands agree; only the third breaks the width. The
        # check runs over every operand's width, not just adjacent pairs, so
        # this still raises rather than silently unioning the first two and
        # choking on the third at the database instead.
        with pytest.raises(ValueError, match="same number of columns"):
            Query(Author).union(Query(Author)).union(Query(Author.id))

    # rowform-original test (no SQLAlchemy equivalent)
    def test_a_cte_backed_query_must_still_match_column_count(self):
        counts = book_counts()
        with pytest.raises(ValueError, match="same number of columns"):
            Query(counts.author_id).union(Query(counts.author_id, counts.n))


class TestForcedInclusion:
    """`.with_()` forces a CTE rowform's own walk cannot see. These check it
    plays well with CTEs the walk *can* see, rather than only the
    entirely-invisible case the existing test covers."""

    # rowform-original test (no SQLAlchemy equivalent)
    def test_with_does_not_duplicate_a_naturally_referenced_cte(self):
        counts = book_counts()
        query = (Query(Author, counts.n)
                 .join(counts, counts.author_id == Author.id)
                 .with_(counts))
        rendered = sql_of(query)
        assert rendered.count("counts AS (") == 1

    # rowform-original test (no SQLAlchemy equivalent)
    def test_with_accepts_several_ctes_in_one_call(self):
        counts = book_counts()
        other = Query(Author.id).cte("other_ids")
        rendered = sql_of(Query(Author).with_(counts, other))
        assert "counts AS (" in rendered
        assert "other_ids AS (" in rendered
        assert rendered.index("counts AS") < rendered.index("other_ids AS")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_add_cte_is_an_alias_for_with_(self):
        # SQLAlchemy's actual name for this (HasCTE.add_cte), added alongside
        # with_() rather than instead of it.
        counts = book_counts()
        rendered = sql_of(Query(Author).add_cte(counts))
        assert "counts AS (" in rendered

    # rowform-original test (no SQLAlchemy equivalent)
    def test_add_cte_nest_here_is_rejected_rather_than_silently_ignored(self):
        counts = book_counts()
        with pytest.raises(NotImplementedError, match="nest_here"):
            Query(Author).add_cte(counts, nest_here=True)


class TestKnownGaps:
    """Not part of the port itself — these record behaviour a maintainer
    should look at, verified against the actual library rather than assumed.
    Skipped rather than asserted, per the porting brief, since asserting the
    crash would just be pinning a bug in place."""

    # rowform-original test (no SQLAlchemy equivalent)
    def test_a_compound_select_can_be_wrapped_as_a_cte(self):
        # Fixed: CompoundSelect now has .cte()/.subquery()/.with_(), and
        # _named_output_columns() walks down to the first operand for the
        # naming check (a compound's output names come from its first operand,
        # same rule SQL itself uses).
        union_query = (Query(Author.id).where(Author.id < 3)
                       .union(Query(Author.id).where(Author.id > 2)))
        wrapped = union_query.cte("both_ids")
        assert sql_of(Query(wrapped.id)).startswith("WITH both_ids AS (")

    # rowform-original test (no SQLAlchemy equivalent)
    def test_a_compound_select_can_be_wrapped_as_a_subquery(self):
        union_query = (Query(Author.id).where(Author.id < 3)
                       .union(Query(Author.id).where(Author.id > 2)))
        wrapped = union_query.subquery("both_ids")
        sql, params = Query(wrapped.id).to_sql()
        assert "both_ids" in sql
        assert wrapped.id.py_type is int

    # rowform-original test (no SQLAlchemy equivalent)
    def test_intersect_all_and_except_all_chain_further(self):
        a = Query(Author.id).where(Author.id < 4)
        b = Query(Author.id).where(Author.id > 1)
        c = Query(Author.id).where(Author.active == True)
        combined = a.intersect_all(b).intersect_all(c)
        sql, _ = combined.to_sql()
        assert sql.count("INTERSECT ALL") == 2

        combined2 = a.except_all(b).except_all(c)
        sql2, _ = combined2.to_sql()
        assert sql2.count("EXCEPT ALL") == 2

    # rowform-original test (no SQLAlchemy equivalent)
    def test_with_forces_a_cte_into_a_compound_selects_with_clause(self):
        floating = Query(Author.id).where(Author.active == True).cte("floating")
        combined = (Query(Author.id).where(Author.id < 3)
                    .union(Query(Author.id).where(Author.id > 2))
                    .with_(floating))
        sql, _ = combined.to_sql()
        assert sql.startswith("WITH floating AS (")


class TestAgainstSqlite:
    # rowform-original test (no SQLAlchemy equivalent)
    def test_diamond_dependency_executes_correctly(self, db):
        base = book_counts("base")
        left = Query(base.author_id, base.n).where(base.n > 0).cte("left_cte")
        right = Query(base.author_id, base.n).where(base.n < 100).cte("right_cte")
        query = (Query(left.author_id, right.n)
                 .join(right, right.author_id == left.author_id)
                 .order_by(left.author_id))
        sql, params = query.to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [(1, 2), (2, 1), (3, 1)]

    # rowform-original test (no SQLAlchemy equivalent)
    def test_five_reference_site_cte_executes_correctly(self, db):
        counts = book_counts()
        other = Query(counts.author_id).where(counts.n > 0).cte("other")
        query = (
            Query(Author.name, counts.n, other.author_id)
            .join(counts, counts.author_id == Author.id)
            .join(other, other.author_id == Author.id)
            .where(Author.id.in_(Query(counts.author_id).where(counts.n > 1)))
            .where(exists(
                Query(counts.author_id).correlate(Author)
                                       .where(counts.author_id == Author.id)
            ))
        )
        sql, params = query.to_sql(placeholder="?")
        assert db.execute(sql, params).fetchall() == [("ada", 2, 1)]
