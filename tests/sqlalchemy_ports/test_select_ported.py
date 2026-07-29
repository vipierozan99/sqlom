"""Ported from SQLAlchemy's test/sql/test_select.py and test_selectable.py
(generative Select methods), adapted to rowform's model-based query builder.

rowform has no `Table`/`MetaData`/column-collection object graph: a source is
always a model class (or an `Alias`/`Subquery`/`CTE` of one), joins always
need an explicit ON predicate, and there is no ORM layer above Core. So this
file translates *intent*, not source: each test below is inspired by a
SQLAlchemy scenario but rebuilt against `Query`, `Author`/`Book`/`Tag`.

Skipped entirely (no rowform equivalent, or the concept does not exist here):
  * `Table`/`Column`/`ForeignKey`/`MetaData`/schema=/DDL/reflection — rowform
    has no Table object; a source is a model class.
  * `join_from()` and implicit-FK `join(child)` (inferring the ON clause
    from a declared `ForeignKey`) — rowform's `join()` always requires an
    explicit ON predicate; there is no FK metadata to infer one from.
  * `select(table.c)`, `.c[0:2]`, `.c["x", "y"]`, keyed `Column(key=...)`
    mapping — no `Table.c` column-collection object.
  * `select_from()` as a FROM declared independently of any selected column
    — the FROM is always derived from the first selected entity (or the
    first join source it needs); `with_only_columns()` and `filter_by()`
    are supported (see below) and don't change that.
  * Anonymous-label / column-correspondence machinery: `corresponding_column`,
    `proxy_set`, `_clone()`/`_copy_internals()`, `anon_1`-style auto-labels.
    rowform has no clause-element graph to clone or re-correspond; every
    non-trivial expression needs an explicit `.label()` to be addressable
    from outside, and unlabelled aggregates are refused by `cte()`/
    `subquery()` rather than given a guessed name.
  * `with_hint()`/`with_statement_hint()`, and other dialect-hint or
    ORM-mapper-specific tests — not applicable to a Core-only builder.
  * `correlate(None, ...)` / `correlate_except(...)` — rowform's `correlate()`
    only adds explicit outer sources; there is no "correlate everything
    except" or "clear correlation" spelling.
  * SQLAlchemy's generative-immutability contract (`test_methods_generative`,
    asserting `s1 is not s2` after each builder call) is the *opposite* of
    rowform's contract, which mutates in place and returns `self` — see
    `test_query_methods_mutate_in_place_and_return_self` below, which pins
    down the actual (inverted) behaviour instead of skipping it.

Both API gaps originally noticed while porting have since been fixed centrally
(not in this file) and are exercised below instead of skipped:
  * `CompoundSelect` now has `intersect_all`/`except_all` too, chaining a
    compound the same way `union`/`union_all`/`intersect`/`except_` always
    could — see `test_intersect_all_and_except_all_chain_on_a_compound`.
  * `Query.scalar_subquery()` now returns a real `ScalarSubquery` `Expression`
    rather than the bare `Query`, so — once `.label()`d, the same rule as any
    other unnamed expression — it can be placed directly in a SELECT list as
    a value: `Query(Author.name, sub.scalar_subquery().label("n"))`. See
    `test_scalar_subquery_as_a_selected_column`.
"""

from rowform import (
    Alias,
    CompoundSelect,
    Query,
    and_,
    avg,
    count,
    or_,
    select,
)
from tests.conftest import Author, Book, Tag

# --------------------------------------------------------------------------
# Basic construction
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_select_function_matches_query_constructor():
    # SQLAlchemy's select() and Select() are the same object; rowform's
    # select() is a thin wrapper around Query() for the same reason.
    assert select(Author).to_sql() == Query(Author).to_sql()


# rowform-original test (no SQLAlchemy equivalent)
def test_select_specific_columns_preserves_order():
    sql, _ = Query(Book.title, Book.id, Book.author_id).to_sql()
    assert sql == "SELECT title, id, author_id FROM t_books"


# rowform-original test (no SQLAlchemy equivalent)
def test_select_model_and_expression_together():
    sql, _ = Query(Book.author_id, count()).to_sql()
    assert sql == "SELECT author_id, count(*) FROM t_books"


# rowform-original test (no SQLAlchemy equivalent)
def test_filter_is_a_synonym_for_where():
    sql, params = Query(Author).filter(Author.id > 1).to_sql()
    assert sql == Query(Author).where(Author.id > 1).to_sql()[0]
    assert params == (1,)


# Ported from test/sql/test_select.py::SelectTest.test_filter_by_from_col (SQLAlchemy 2.0.51)
def test_filter_by_targets_the_primary_source_with_no_joins():
    sql, params = Query(Author).filter_by(name="ada", active=True).to_sql()
    assert sql == "SELECT id, name, active FROM t_authors WHERE name = ? AND active = ?"
    assert params == ("ada", True)


# Ported from test/sql/test_select.py::SelectTest.test_joins_w_filter_by (SQLAlchemy 2.0.51)
def test_filter_by_targets_the_most_recently_joined_source():
    # Mirrors SQLAlchemy's own filter_by() resolution rule: the last entity
    # joined in, not the primary one, once a join exists.
    sql, params = (
        Query(Author, Book)
        .join(Book, Book.author_id == Author.id)
        .filter_by(title="algorithms")
        .to_sql()
    )
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active, "
        "t_books.id, t_books.author_id, t_books.title FROM t_authors "
        "JOIN t_books ON t_books.author_id = t_authors.id "
        "WHERE t_books.title = ?"
    )
    assert params == ("algorithms",)


# Ported from test/sql/test_select.py::SelectTest.test_filter_by_no_property_from_table (SQLAlchemy 2.0.51)
def test_filter_by_rejects_an_unknown_column():
    import pytest

    with pytest.raises(ValueError, match="has no column"):
        Query(Author).filter_by(nope=1)


# rowform-original test (no SQLAlchemy equivalent)
def test_exists_method_matches_the_free_function():
    from rowform import exists

    inner = Query(Book).correlate(Author).where(Book.author_id == Author.id)
    method_form, method_params = Query(Author).where(inner.exists()).to_sql(placeholder="$")
    function_form, function_params = Query(Author).where(exists(inner)).to_sql(placeholder="$")
    assert method_form == function_form
    assert method_params == function_params == ()


# --------------------------------------------------------------------------
# JOIN construction (test_join_nofrom_* / test_joins_w_filter_by intent)
# --------------------------------------------------------------------------


# Ported from test/sql/test_select.py::SelectTest.test_join_nofrom_implicit_left_side_explicit_onclause (SQLAlchemy 2.0.51)
def test_join_explicit_onclause_two_tables():
    stmt = Query(Author, Book).join(Book, Book.author_id == Author.id)
    sql, params = stmt.to_sql()
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active, "
        "t_books.id, t_books.author_id, t_books.title "
        "FROM t_authors JOIN t_books ON t_books.author_id = t_authors.id"
    )
    assert params == ()


# Ported from test/sql/test_select.py::SelectTest.test_join_nofrom_implicit_left_side_explicit_onclause_3level (SQLAlchemy 2.0.51)
def test_join_three_level_chain_renders_in_order():
    # Mirrors test_join_nofrom_implicit_left_side_explicit_onclause_3level:
    # parent -> child -> grandchild, each JOIN naming the one before it.
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


# rowform-original test (no SQLAlchemy equivalent)
def test_join_isouter_keyword_matches_outer_join_method():
    # SQLAlchemy's join(..., isouter=True) vs .outerjoin(...); rowform spells
    # the same choice as a keyword on join() or a dedicated method.
    via_keyword = Query(Author, Book).join(
        Book, Book.author_id == Author.id, isouter=True
    )
    via_method = Query(Author, Book).outer_join(Book, Book.author_id == Author.id)
    assert via_keyword.to_sql() == via_method.to_sql()


# rowform-original test (no SQLAlchemy equivalent)
def test_join_full_keyword_matches_full_join_method():
    via_keyword = Query(Author, Book).join(
        Book, Book.author_id == Author.id, full=True
    )
    via_method = Query(Author, Book).full_join(Book, Book.author_id == Author.id)
    assert via_keyword.to_sql() == via_method.to_sql()


# rowform-original test (no SQLAlchemy equivalent)
def test_join_can_run_in_the_direction_opposite_the_foreign_key():
    # SQLAlchemy infers ON from the ForeignKey regardless of which table is
    # "left"; rowform takes an explicit predicate, so the same freedom just
    # falls out of writing Author second and joining to it.
    sql, _ = (
        Query(Book, Author)
        .join(Author, Author.id == Book.author_id)
        .to_sql()
    )
    assert sql.startswith(
        "SELECT t_books.id, t_books.author_id, t_books.title, "
        "t_authors.id, t_authors.name, t_authors.active "
        "FROM t_books JOIN t_authors ON t_authors.id = t_books.author_id"
    )


# Ported from test/sql/test_select.py::SelectTest.test_joins_w_filter_by (SQLAlchemy 2.0.51)
def test_repeated_join_and_where_calls_compose_across_three_tables():
    # Mirrors test_joins_w_filter_by: successive join()/where() pairs build
    # up one statement, each predicate AND-ed onto the last.
    sql, params = (
        Query(Author)
        .join(Book, Book.author_id == Author.id)
        .where(Book.title == "compilers")
        .join(Tag, Tag.book_id == Book.id)
        .where(Tag.label == "classic")
        .to_sql(placeholder="$")
    )
    assert sql.endswith(
        "WHERE t_books.title = $1 AND t_tags.label = $2"
    )
    assert params == ("compilers", "classic")


# rowform-original test (no SQLAlchemy equivalent)
def test_join_on_clause_link_can_be_anywhere_in_a_compound_predicate():
    # The linking comparison need not be the first term of an and_()'d ON
    # clause; the cross-join check walks the whole tree.
    stmt = Query(Author, Book).join(
        Book, and_(Book.title == "x", Book.author_id == Author.id)
    )
    assert "ON (t_books.title = ? AND t_books.author_id = t_authors.id)" in stmt.to_sql()[0]


# rowform-original test (no SQLAlchemy equivalent)
def test_join_on_clause_without_any_linking_condition_is_a_cross_join():
    import pytest

    with pytest.raises(ValueError, match="cross join"):
        Query(Author, Book).join(
            Book, and_(Book.title == "x", Book.id > 0)
        )


# Ported from test/sql/test_select.py::SelectTest.test_join_implicit_left_side_wo_cols_onelevel_union (SQLAlchemy 2.0.51)
def test_union_of_two_joined_selects():
    # Mirrors test_join_implicit_left_side_wo_cols_onelevel_union: a select
    # built from a join unions cleanly with a plain one.
    joined = (
        Query(Author.name)
        .join(Book, Book.author_id == Author.id)
        .where(Book.title == "compilers")
    )
    sql, _ = joined.union(Query(Author.name).where(Author.name == "dan")).to_sql()
    assert " UNION " in sql
    assert "JOIN t_books ON t_books.author_id = t_authors.id" in sql


# --------------------------------------------------------------------------
# Self-joins / aliases (JoinConditionTest / AliasTest intent)
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_self_join_two_levels_deep_with_two_aliases():
    # A chain of self-joins: Author -> mgr -> mgr2, each alias distinguished
    # from the model and from each other purely by identity.
    mgr = Alias(Author, "mgr")
    mgr2 = Alias(Author, "mgr2")
    sql, _ = (
        Query(Author, mgr, mgr2)
        .join(mgr, mgr.id == Author.id)
        .join(mgr2, mgr2.id == mgr.id)
        .to_sql()
    )
    assert (
        "FROM t_authors "
        "JOIN t_authors AS mgr ON mgr.id = t_authors.id "
        "JOIN t_authors AS mgr2 ON mgr2.id = mgr.id"
    ) in sql


# rowform-original test (no SQLAlchemy equivalent)
def test_join_target_must_already_be_a_known_source_or_the_new_one():
    import pytest

    # Neither Tag nor an unjoined Author participates in this ON clause, so
    # it can only be a cross join.
    with pytest.raises(ValueError, match="cross join"):
        (
            Query(Author)
            .join(Book, Book.author_id == Author.id)
            .join(Tag, Book.author_id == Author.id)
        )


# --------------------------------------------------------------------------
# Generative vs. mutating method contract
# --------------------------------------------------------------------------


# Ported from test/sql/test_select.py::SelectTest.test_methods_generative (SQLAlchemy 2.0.51)
def test_query_methods_mutate_in_place_and_return_self():
    # SQLAlchemy's test_methods_generative asserts `s1 is not s2` after every
    # builder call, because Select is immutable and each method clones. This
    # is deliberately the opposite here: Query mutates in place, and every
    # chainable method returns `self` so it can still be composed inline.
    query = Query(Author)
    where_result = query.where(Author.id > 1)
    order_result = where_result.order_by(Author.name)
    limit_result = order_result.limit(5)
    assert where_result is query
    assert order_result is query
    assert limit_result is query


# --------------------------------------------------------------------------
# UNION-family chaining (test_select_multiple_compound_elements intent)
# --------------------------------------------------------------------------


# Ported from test/sql/test_select.py::SelectTest.test_select_multiple_compound_elements (SQLAlchemy 2.0.51)
def test_union_chain_of_three_operands_renders_two_keywords():
    stmt = Query(Author.id).union(Query(Author.id)).union(Query(Author.id))
    sql, _ = stmt.to_sql()
    assert sql.count(" UNION ") == 2
    assert len(stmt.operands) == 3


# Ported from test/sql/test_select.py::SelectTest.test_select_multiple_compound_elements (SQLAlchemy 2.0.51)
def test_mixing_operators_nests_rather_than_flattening():
    stmt = Query(Author.id).union(Query(Author.id)).intersect(Query(Author.id))
    sql, _ = stmt.to_sql()
    # The UNION is fully rendered before INTERSECT appears, i.e. it is
    # nested as (a UNION b) INTERSECT c rather than a 3-way flat list.
    assert sql.index(" UNION ") < sql.index(" INTERSECT ")
    assert len(stmt.operands) == 2
    assert isinstance(stmt.operands[0], CompoundSelect)


# rowform-original test (no SQLAlchemy equivalent)
def test_compound_select_needs_a_query_not_a_string():
    import pytest

    with pytest.raises(TypeError, match="takes another Query"):
        Query(Author).union("SELECT 1")


# rowform-original test (no SQLAlchemy equivalent)
def test_intersect_all_and_except_all_chain_on_a_compound():
    # Fixed: CompoundSelect now has intersect_all()/except_all(), so a
    # *second* INTERSECT ALL/EXCEPT ALL chains onto an existing compound the
    # same way union/union_all/intersect/except_ already did.
    stmt = (Query(Author.id).intersect_all(Query(Author.id))
            .intersect_all(Query(Author.id)))
    sql, _ = stmt.to_sql()
    assert sql.count(" INTERSECT ALL ") == 2

    stmt2 = Query(Author.id).except_all(Query(Author.id)).except_all(Query(Author.id))
    sql2, _ = stmt2.to_sql()
    assert sql2.count(" EXCEPT ALL ") == 2


# --------------------------------------------------------------------------
# Correlated subqueries (correlate() requirement)
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_correlated_scalar_subquery_in_a_comparison():
    # A comparison needs an Expression on its left, so the correlated
    # subquery goes on the right: "books above the average id for that
    # author" — `other` distinguishes the inner reference to t_books from
    # the outer one it is correlated against.
    other = Alias(Book, "other")
    average_for_author = (
        Query(avg(other.id))
        .correlate(Book)
        .where(other.author_id == Book.author_id)
    )
    sql, params = (
        Query(Book.title)
        .where(Book.id > average_for_author.scalar_subquery())
        .to_sql(placeholder="$")
    )
    assert sql == (
        "SELECT title FROM t_books WHERE id > "
        "(SELECT avg(other.id) FROM t_books AS other "
        "WHERE other.author_id = t_books.author_id)"
    )
    assert params == ()


# rowform-original test (no SQLAlchemy equivalent)
def test_scalar_subquery_as_a_selected_column():
    # Fixed: scalar_subquery() now returns a real Expression, so — labelled,
    # the same rule as any other unnamed expression selected on its own —
    # it can be placed directly in a SELECT list, not just inside where()/
    # having()/a comparison.
    book_count = Query(count(Book.id)).correlate(Author).where(
        Book.author_id == Author.id
    ).scalar_subquery()
    sql, params = (
        Query(Author.name, book_count.label("n_books"))
        .to_sql(placeholder="$")
    )
    assert sql == (
        "SELECT name, (SELECT count(t_books.id) FROM t_books "
        "WHERE t_books.author_id = t_authors.id) AS n_books FROM t_authors"
    )
    assert params == ()


# rowform-original test (no SQLAlchemy equivalent)
def test_unlabelled_scalar_subquery_in_a_select_list_still_needs_a_label_to_be_wrapped():
    # Selecting an unlabelled scalar subquery directly still works (it
    # renders as "expr", same default as any other unnamed expression) —
    # but wrapping *that* query as a further CTE/subquery still requires a
    # label, exactly like an unlabelled aggregate does.
    import pytest

    book_count = Query(count(Book.id)).scalar_subquery()
    outer = Query(Author.name, book_count)
    sql, _ = outer.to_sql()
    assert "SELECT count(id) FROM t_books" in sql

    with pytest.raises(ValueError, match="usable column name"):
        outer.cte("author_counts")


# rowform-original test (no SQLAlchemy equivalent)
def test_correlation_must_be_declared_or_the_reference_is_rejected():
    import pytest

    # Without correlate(Author), referencing Author from inside the
    # subquery is indistinguishable from a typo — rowform refuses it outright
    # rather than silently emitting a cross join.
    with pytest.raises(ValueError, match="not part of this query"):
        Query(Book.id).where(Book.author_id == Author.id).scalar_subquery()


# --------------------------------------------------------------------------
# CTEs used as a source (test_selectable.py's `stmt.cte()` usage)
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_select_from_a_cte_with_a_where_clause():
    active_authors = Query(Author).where(Author.active == True).cte("active_authors")
    sql, params = Query(active_authors.name).where(active_authors.id > 1).to_sql(placeholder="$")
    assert sql == (
        "WITH active_authors AS (SELECT id, name, active FROM t_authors "
        "WHERE active = $1) "
        "SELECT name FROM active_authors WHERE id > $2"
    )
    assert params == (True, 1)


# rowform-original test (no SQLAlchemy equivalent)
def test_cte_joined_to_a_plain_table():
    book_counts = (
        Query(Book.author_id, count().label("n"))
        .group_by(Book.author_id)
        .cte("book_counts")
    )
    sql, _ = (
        Query(Author.name, book_counts.n)
        .join(book_counts, book_counts.author_id == Author.id)
        .to_sql()
    )
    assert "JOIN book_counts ON book_counts.author_id = t_authors.id" in sql


# --------------------------------------------------------------------------
# ORDER BY / GROUP BY / HAVING / DISTINCT / LIMIT / OFFSET chaining
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_distinct_order_by_and_limit_chained():
    sql, params = (
        Query(Book.author_id)
        .distinct()
        .order_by(Book.author_id)
        .limit(2)
        .to_sql(placeholder="$")
    )
    assert sql == (
        "SELECT DISTINCT author_id FROM t_books "
        "ORDER BY author_id LIMIT $1"
    )
    assert params == (2,)


# rowform-original test (no SQLAlchemy equivalent)
def test_order_by_several_columns_with_per_column_direction():
    sql, _ = (
        Query(Book)
        .order_by(Book.author_id.asc(), Book.title.desc())
        .to_sql()
    )
    assert sql.endswith("ORDER BY author_id, title DESC")


# rowform-original test (no SQLAlchemy equivalent)
def test_order_by_a_labelled_aggregate_renders_the_expression_not_the_label():
    # order_by() takes the expression object directly here, not a bare
    # string, so it renders the aggregate itself rather than "ORDER BY n".
    labelled = count().label("n")
    sql, _ = (
        Query(Book.author_id, labelled)
        .group_by(Book.author_id)
        .order_by(labelled, descending=True)
        .to_sql()
    )
    assert sql.endswith("ORDER BY count(*) DESC")


# rowform-original test (no SQLAlchemy equivalent)
def test_group_by_two_columns_after_a_join_with_having():
    sql, params = (
        Query(Book.author_id, Tag.label, count())
        .join(Tag, Tag.book_id == Book.id)
        .group_by(Book.author_id, Tag.label)
        .having(and_(count() > 0, count() < 100))
        .to_sql(placeholder="$")
    )
    assert "GROUP BY t_books.author_id, t_tags.label" in sql
    assert "HAVING (count(*) > $1 AND count(*) < $2)" in sql
    assert params == (0, 100)


# rowform-original test (no SQLAlchemy equivalent)
def test_group_by_a_bare_string_only_resolves_against_the_primary_source():
    import pytest

    # "title" belongs to Book, which is joined but not primary here; the
    # bare-string shorthand only ever looks at the FROM table, so this must
    # fail exactly like an unknown column would.
    with pytest.raises(ValueError, match="has no column 'title'"):
        Query(Author).join(Book, Book.author_id == Author.id).group_by("title")


# rowform-original test (no SQLAlchemy equivalent)
def test_offset_without_limit_is_still_valid_sql():
    sql, params = Query(Author).order_by(Author.id).offset(2).to_sql(placeholder="$")
    assert sql.endswith("ORDER BY id OFFSET $1")
    assert params == (2,)


# --------------------------------------------------------------------------
# WHERE composition with and_/or_ across joins
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_where_combines_or_across_two_joined_tables():
    sql, params = (
        Query(Author)
        .join(Book, Book.author_id == Author.id)
        .where(or_(Author.name == "ada", Book.title == "compilers"))
        .to_sql(placeholder="$")
    )
    assert sql.endswith(
        "WHERE (t_authors.name = $1 OR t_books.title = $2)"
    )
    assert params == ("ada", "compilers")


# rowform-original test (no SQLAlchemy equivalent)
def test_where_combines_and_of_or_across_a_three_way_join():
    sql, _ = (
        Query(Author)
        .join(Book, Book.author_id == Author.id)
        .join(Tag, Tag.book_id == Book.id)
        .where(
            and_(
                Author.active == True,
                or_(Book.title == "compilers", Tag.label == "classic"),
            )
        )
        .to_sql(placeholder="$")
    )
    assert sql.endswith(
        "WHERE (t_authors.active = $1 "
        "AND (t_books.title = $2 OR t_tags.label = $3))"
    )


# --------------------------------------------------------------------------
# Labels and derived tables
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_label_a_joined_column_alongside_the_primary_model():
    sql, _ = (
        Query(Author, Book.title.label("book_title"))
        .join(Book, Book.author_id == Author.id)
        .to_sql()
    )
    assert sql.startswith(
        "SELECT t_authors.id, t_authors.name, t_authors.active, "
        "t_books.title AS book_title "
    )


# rowform-original test (no SQLAlchemy equivalent)
def test_subquery_output_columns_are_named_after_their_labels():
    sub = (
        Query(Book.author_id, count().label("total"))
        .group_by(Book.author_id)
        .subquery("counts")
    )
    assert [name for name, _ in sub.query.output_columns()] == ["author_id", "total"]
    assert sub.total.source is sub


# --------------------------------------------------------------------------
# End to end (a handful of the above, run against real sqlite rows)
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_three_way_join_end_to_end(run_query):
    rows = run_query(
        Query(Author.name, Book.title, Tag.label)
        .join(Book, Book.author_id == Author.id)
        .join(Tag, Tag.book_id == Book.id)
        .order_by(Book.id)
    )
    assert rows == [("ada", "structures", "classic"), ("brian", "compilers", "classic")]


# rowform-original test (no SQLAlchemy equivalent)
def test_correlated_scalar_subquery_end_to_end(run_query):
    # ada's books are 10 ("structures") and 11 ("algorithms"); the average
    # id for her is 10.5, so only the higher-id book clears the bar. Every
    # other author has exactly one book, tying the average and excluding it.
    other = Alias(Book, "other")
    average_for_author = (
        Query(avg(other.id))
        .correlate(Book)
        .where(other.author_id == Book.author_id)
    )
    rows = run_query(
        Query(Book.title)
        .where(Book.id > average_for_author.scalar_subquery())
    )
    assert rows == [("algorithms",)]


# rowform-original test (no SQLAlchemy equivalent)
def test_cte_end_to_end(run_query):
    active_authors = Query(Author).where(Author.active == True).cte("active_authors")
    rows = run_query(
        Query(active_authors.name).order_by(active_authors.id)
    )
    assert rows == [("ada",), ("brian",), ("dan",)]


# --------------------------------------------------------------------------
# add_columns() / with_only_columns() — SQLAlchemy's Select methods for
# amending the select list after construction (test_select.py uses
# with_only_columns() as a utility inside its correlate()/join tests rather
# than testing it in isolation; this section covers it directly).
# --------------------------------------------------------------------------


# rowform-original test (no SQLAlchemy equivalent)
def test_add_columns_appends_to_the_select_list():
    sql, _ = Query(Author.id).add_columns(Author.name).to_sql()
    assert sql == "SELECT id, name FROM t_authors"


# rowform-original test (no SQLAlchemy equivalent)
def test_add_columns_can_reference_a_source_joined_afterward():
    # Builder order doesn't matter, same as where()/order_by()/group_by():
    # validation happens at render time, not at the add_columns() call.
    stmt = Query(Author).add_columns(Book.title)
    stmt.join(Book, Book.author_id == Author.id)
    sql, _ = stmt.to_sql()
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active, t_books.title "
        "FROM t_authors JOIN t_books ON t_books.author_id = t_authors.id"
    )


# rowform-original test (no SQLAlchemy equivalent)
def test_add_columns_of_an_unjoined_source_is_rejected_at_render():
    import pytest

    stmt = Query(Author).add_columns(Book.title)
    with pytest.raises(ValueError, match="not part of this query"):
        stmt.to_sql()


# rowform-original test (no SQLAlchemy equivalent)
def test_with_only_columns_replaces_the_select_list():
    stmt = Query(Author).where(Author.active == True).with_only_columns(
        Author.name
    )
    sql, params = stmt.to_sql()
    assert sql == "SELECT name FROM t_authors WHERE active = ?"
    assert params == (True,)


# rowform-original test (no SQLAlchemy equivalent)
def test_with_only_columns_keeps_froms_and_joins_intact():
    # The FROM/JOIN graph is untouched — only which columns come back
    # changes, so a column from a table no longer selected still renders
    # correctly as long as it is still joined in.
    stmt = (Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .with_only_columns(Book.title))
    sql, _ = stmt.to_sql()
    assert sql == (
        "SELECT t_books.title FROM t_authors "
        "JOIN t_books ON t_books.author_id = t_authors.id"
    )


# rowform-original test (no SQLAlchemy equivalent)
def test_with_only_columns_updates_is_multi_entity():
    stmt = Query(Author, Book).join(Book, Book.author_id == Author.id)
    assert stmt.is_multi_entity
    stmt.with_only_columns(Author)
    assert not stmt.is_multi_entity


# rowform-original test (no SQLAlchemy equivalent)
def test_with_only_columns_needs_at_least_one_entity():
    import pytest

    with pytest.raises(TypeError, match="needs at least one entity"):
        Query(Author).with_only_columns()


# rowform-original test (no SQLAlchemy equivalent)
def test_add_columns_and_with_only_columns_end_to_end(run_query):
    rows = run_query(
        Query(Author.id)
        .add_columns(Book.title)
        .join(Book, Book.author_id == Author.id)
        .where(Author.name == "ada")
        .order_by(Book.id)
    )
    assert rows == [(1, "structures"), (1, "algorithms")]

    narrowed = (Query(Author, Book)
                .join(Book, Book.author_id == Author.id)
                .with_only_columns(Book.title)
                .where(Author.name == "ada")
                .order_by(Book.id))
    assert run_query(narrowed) == [("structures",), ("algorithms",)]


# --------------------------------------------------------------------------
# select_from() — an explicit FROM source needing no ON clause, the one
# sanctioned exception to "join() always requires a real linking condition"
# (README §12). Ported from test/sql/test_compiler.py::CompileTest, scoped
# down: rowform's select_from() only ever *adds* a source (see its docstring
# for the SQLAlchemy behaviours this deliberately doesn't replicate: naming
# the *sole* FROM table with nothing else selected, and re-ordering/
# re-asserting a source already implied elsewhere).
# --------------------------------------------------------------------------


# Ported from test/sql/test_compiler.py::SelectTest.test_select_from_ordering (SQLAlchemy 2.0.51)
def test_select_from_adds_a_genuinely_unrelated_table_as_a_cross_join():
    # Once a second source is in play — select_from() included, same as a
    # join — every column renders table-qualified, since `id` would
    # otherwise be ambiguous (README §3).
    sql, params = Query(Author, Book).select_from(Book).to_sql()
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active, "
        "t_books.id, t_books.author_id, t_books.title "
        "FROM t_authors, t_books"
    )
    assert params == ()


# Ported from test/sql/test_compiler.py::SelectTest.test_select_from_ordering (SQLAlchemy 2.0.51)
def test_select_from_accepts_several_sources_at_once():
    sql, _ = Query(Author).select_from(Book, Tag).to_sql()
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active "
        "FROM t_authors, t_books, t_tags"
    )


# rowform-original test (no SQLAlchemy equivalent)
def test_select_from_end_to_end_cross_join(run_query):
    rows = run_query(
        Query(Author.id, Book.id).select_from(Book).order_by(Author.id, Book.id)
    )
    # 4 authors x 4 books = 16 rows, a genuine cartesian product.
    assert len(rows) == 16


# rowform-original test (no SQLAlchemy equivalent)
def test_select_from_rejects_a_source_already_in_the_query():
    import pytest

    with pytest.raises(ValueError, match="already in this query"):
        Query(Author, Book).select_from(Author)


# rowform-original test (no SQLAlchemy equivalent)
def test_select_from_still_requires_a_columns_source_or_model_alias_subquery():
    import pytest

    with pytest.raises(TypeError, match="select_from\\(\\) takes a model"):
        Query(Author).select_from(Book.id)


# rowform-original test (no SQLAlchemy equivalent)
def test_joins_cross_join_guard_is_unaffected_by_select_from_existing():
    import pytest

    # select_from()'s existence must not weaken join()'s own guard: join()
    # still always refuses an ON clause that links nothing.
    with pytest.raises(ValueError, match="cross join"):
        Query(Author).join(Book, Author.active == True)


# rowform-original test (no SQLAlchemy equivalent)
def test_select_from_combined_with_a_real_join():
    # select_from() adds an unrelated table; join() still needs a real ON
    # clause for anything joined normally alongside it.
    sql, _ = (
        Query(Author, Book, Tag)
        .select_from(Tag)
        .join(Book, Book.author_id == Author.id)
        .to_sql()
    )
    assert sql == (
        "SELECT t_authors.id, t_authors.name, t_authors.active, "
        "t_books.id, t_books.author_id, t_books.title, "
        "t_tags.id, t_tags.book_id, t_tags.label "
        "FROM t_authors, t_tags "
        "JOIN t_books ON t_books.author_id = t_authors.id"
    )
