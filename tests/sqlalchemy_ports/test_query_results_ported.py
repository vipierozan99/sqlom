"""Ported from SQLAlchemy's test/orm/test_query.py (SQLAlchemy 2.0.51) —
specifically the parts that execute a query against a real database and
assert the *correct objects come back*, not the ORM machinery around them.

rowform has no ORM: no `Session`, no identity map, no unit of work, no
`relationship()`/lazy or eager loading, no mapper configuration. It compiles
a per-model `row -> object` function once and hydrates rows positionally
(README "Architecture Under the Hood") — closer to a dataclass row-mapper
than an ORM. So this file deliberately narrows `test_query.py` down to one
question: given a `Query`, does the right set of model instances (or tuples,
for a multi-entity/column select) come back? Everything about *how* that
answer is reached — session identity, relationship traversal, mapper
configuration — is out of scope by design, not an oversight.

`test_query.py`'s fixtures (`User`/`Address`/`Order`, from `_fixtures.py`)
are related via `relationship()` and joined in these tests via
`User.addresses` (relationship-based join syntax). rowform has no such syntax
— every join here is translated to rowform's explicit `.join(Book, Book.
author_id == Author.id)` form, using the existing `Author`/`Book`/`Tag`
fixtures (tests/conftest.py) — a one-to-many chain, same shape as User/
Address, just smaller (4 authors, 4 books, 2 tags).

Skipped entirely, and why:
  * `GetTest` — every test in it is `Session.get(Model, pk)`, an identity-map
    primary-key lookup (including composite-PK variants). rowform has no
    session and no `.get()` shortcut at all; every query is built explicitly
    via `Query(Model).where(...)`.
  * `RowTupleTest`, most of `RowLabelingTest` — custom column keys via
    imperative mapper configuration (`properties={"uname": ...}`), legacy
    `Row`/`LegacyRow` distinctions, and mapper `column_descriptions`
    introspection. rowform's models are plain `ModelMeta`/`@model` classes with
    no separate "mapped attribute name" vs. "column name" concept to test.
  * `OperatorTest`, `ExpressionTest`, `ComparatorTest` — pure SQL-generation
    tests already covered by tests/sqlalchemy_ports/test_operators_functions_ported.py
    and test_compiler_ported.py; nothing here is specifically about execution
    results.
  * `ExistsTest` — all SQL-compilation assertions (`self.assert_compile`),
    no execution/result-correctness content to port.
  * `test_select_with_bindparam_offset_limit*` (FilterTest) — `bindparam()`
    cannot back `Query.limit()`/`.offset()` in rowform (see
    tests/sqlalchemy_ports/test_bindparam_ported.py's `TestOutOfScope`);
    both reject anything that isn't a plain `int` immediately.
  * `CountTest.test_count_char`/`test_loader_options_ignored` — asserts
    exact nested-subquery SQL text for `.count()`, an ORM `Query`-specific
    method rowform doesn't have (rowform counts via `count()`/`count(Model)`
    directly), and loader-options are an eager-loading concept.
  * `SetOpsWDeferredTest` — deferred-column loading, no rowform equivalent.
"""

from rowform import Query, count, literal, literal_column
from tests.conftest import Author, Book


# --------------------------------------------------------------------------
# Single entity vs. multiple entities/columns — test_query.py's
# OnlyReturnTuplesTest tests an ORM Query's `.only_return_tuples()`/`.tuples()`
# toggle; rowform has no such toggle because it isn't needed — the shape is
# already exactly determined by what was selected (Query.is_multi_entity),
# and this is what that distinction looks like end to end.
# --------------------------------------------------------------------------


class TestSingleEntityVsTuples:
    # Ported from test/orm/test_query.py::OnlyReturnTuplesTest.test_single_entity_false (SQLAlchemy 2.0.51)
    def test_a_single_whole_model_hydrates_to_real_instances(self, run_query):
        rows = run_query(Query(Author).order_by(Author.id))
        assert all(isinstance(row, Author) for row in rows)
        assert [row.name for row in rows] == ["ada", "brian", "carol", "dan"]

    # Ported from test/orm/test_query.py::OnlyReturnTuplesTest.test_multiple_entity_false (SQLAlchemy 2.0.51)
    def test_two_columns_hydrate_to_tuples(self, run_query):
        rows = run_query(Query(Author.id, Author.name).order_by(Author.id))
        assert all(isinstance(row, tuple) for row in rows)
        assert rows == [(1, "ada"), (2, "brian"), (3, "carol"), (4, "dan")]

    # Ported from test/orm/test_query.py::OnlyReturnTuplesTest.test_multiple_entity_true (SQLAlchemy 2.0.51)
    def test_a_column_plus_a_whole_model_hydrates_to_tuples(self, run_query):
        rows = run_query(
            Query(Author.id, Author).where(Author.id == 1)
        )
        assert len(rows) == 1
        row_id, author = rows[0]
        assert isinstance(row_id, int) and row_id == 1
        assert isinstance(author, Author)
        assert (author.id, author.name, author.active) == (1, "ada", True)


# --------------------------------------------------------------------------
# Filtering, ordering, limit/offset — test_query.py::FilterTest
# --------------------------------------------------------------------------


class TestFilterResults:
    # Ported from test/orm/test_query.py::FilterTest.test_basic (SQLAlchemy 2.0.51)
    def test_basic_returns_every_row_as_the_right_instance(self, run_query):
        authors = run_query(Query(Author).order_by(Author.id))
        assert [a.id for a in authors] == [1, 2, 3, 4]
        assert [a.name for a in authors] == ["ada", "brian", "carol", "dan"]

    # Ported from test/orm/test_query.py::FilterTest.test_limit_offset (SQLAlchemy 2.0.51)
    def test_limit_offset_returns_exactly_the_right_slice(self, run_query):
        authors = run_query(Query(Author).order_by(Author.id).limit(2).offset(1))
        assert [a.name for a in authors] == ["brian", "carol"]

    # Ported from test/orm/test_query.py::FilterTest.test_limit_offset (SQLAlchemy 2.0.51)
    def test_limit_offset_past_the_end_returns_nothing(self, run_query):
        authors = run_query(Query(Author).order_by(Author.id).limit(2).offset(10))
        assert authors == []

    # Ported from test/orm/test_query.py::FilterTest.test_one_filter (SQLAlchemy 2.0.51)
    def test_a_where_clause_returns_exactly_the_matching_instances(self, run_query):
        authors = run_query(Query(Author).where(Author.id > 2).order_by(Author.id))
        assert [a.name for a in authors] == ["carol", "dan"]

    # rowform-original test (no SQLAlchemy equivalent) — the boolean-column
    # counterpart of the id-comparison filter above.
    def test_a_boolean_column_filter_returns_exactly_the_matching_instances(
        self, run_query
    ):
        authors = run_query(Query(Author).where(Author.active == True).order_by(Author.id))  # noqa: E712
        assert [a.name for a in authors] == ["ada", "brian", "dan"]


# --------------------------------------------------------------------------
# Counting rows — test_query.py::CountTest. rowform has no ORM Query.count();
# the equivalent is executing count()/count(Model) and reading the scalar
# back, which is what these check.
# --------------------------------------------------------------------------


class TestCountResults:
    # Ported from test/orm/test_query.py::CountTest.test_basic (SQLAlchemy 2.0.51)
    def test_count_of_a_whole_table(self, run_query):
        assert run_query(Query(count(Author)))[0][0] == 4

    # Ported from test/orm/test_query.py::CountTest.test_basic (SQLAlchemy 2.0.51)
    def test_count_with_a_filter(self, run_query):
        rows = run_query(Query(count(Author)).where(Author.active == True))  # noqa: E712
        assert rows[0][0] == 3

    # Ported from test/orm/test_query.py::CountTest.test_multiple_entity (SQLAlchemy 2.0.51)
    def test_count_of_an_unconditional_cross_join_is_the_full_product(
        self, run_query
    ):
        # select_from() is rowform's equivalent of SQLAlchemy's
        # `.join(Address, true())` unconditional-join escape hatch (see
        # test_select_ported.py's select_from() section).
        rows = run_query(Query(count(Author)).select_from(Book))
        assert rows[0][0] == 4 * 4  # 4 authors x 4 books, a real cartesian product

    # Ported from test/orm/test_query.py::CountTest.test_multiple_entity (SQLAlchemy 2.0.51)
    def test_count_of_a_real_join_only_counts_matching_pairs(self, run_query):
        rows = run_query(
            Query(count(Author)).join(Book, Book.author_id == Author.id)
        )
        # ada has 2 books, brian 1, carol 1, dan 0 -> 4 matching pairs, not 16
        assert rows[0][0] == 4

    # Ported from test/orm/test_query.py::CountTest.test_cols (SQLAlchemy 2.0.51)
    def test_count_distinct_vs_plain_count_of_a_column(self, run_query):
        # 4 books, but only 3 distinct authors among them.
        assert run_query(Query(count(Book.author_id)))[0][0] == 4
        assert run_query(Query(count(Book.author_id, distinct=True)))[0][0] == 3


# --------------------------------------------------------------------------
# DISTINCT — test_query.py::DistinctTest
# --------------------------------------------------------------------------


class TestDistinctResults:
    # Ported from test/orm/test_query.py::DistinctTest.test_basic (SQLAlchemy 2.0.51)
    def test_distinct_over_a_whole_model_with_no_duplicates_changes_nothing(
        self, run_query
    ):
        authors = run_query(Query(Author).distinct().order_by(Author.id))
        assert [a.name for a in authors] == ["ada", "brian", "carol", "dan"]

    # Ported from test/orm/test_query.py::DistinctTest.test_basic_standalone (SQLAlchemy 2.0.51)
    def test_distinct_on_a_single_column_after_a_join_removes_duplicates(
        self, run_query
    ):
        # Every author with at least one book, but ada would appear twice
        # (2 books) without DISTINCT.
        rows = run_query(
            Query(Author.id)
            .join(Book, Book.author_id == Author.id)
            .distinct()
            .order_by(Author.id)
        )
        assert rows == [(1,), (2,), (3,)]


# --------------------------------------------------------------------------
# GROUP BY + JOIN + HAVING — test_query.py::AggregateTest.test_having
# --------------------------------------------------------------------------


class TestGroupByHavingResults:
    # Ported from test/orm/test_query.py::AggregateTest.test_having (SQLAlchemy 2.0.51)
    def test_having_more_than_one_book_returns_only_the_matching_author(
        self, run_query
    ):
        rows = run_query(
            Query(Author.name)
            .join(Book, Book.author_id == Author.id)
            .group_by(Author.id)
            .having(count(Book.id) > 1)
            .order_by(Author.id)
        )
        assert rows == [("ada",)]  # only ada has more than one book

    # Ported from test/orm/test_query.py::AggregateTest.test_having (SQLAlchemy 2.0.51)
    def test_having_fewer_than_two_books_returns_the_rest(self, run_query):
        rows = run_query(
            Query(Author.name)
            .join(Book, Book.author_id == Author.id)
            .group_by(Author.id)
            .having(count(Book.id) < 2)
            .order_by(Author.id)
        )
        # dan has no books at all, so an inner join already excludes him —
        # only brian and carol (one book each) show up here.
        assert rows == [("brian",), ("carol",)]


# --------------------------------------------------------------------------
# Set operations — test_query.py::SetOpsTest
# --------------------------------------------------------------------------


class TestSetOpsResults:
    # Ported from test/orm/test_query.py::SetOpsTest.test_union (SQLAlchemy 2.0.51)
    def test_union_of_two_disjoint_filters_returns_the_combined_rows(
        self, run_query
    ):
        ada = Query(Author.name).where(Author.name == "ada")
        brian = Query(Author.name).where(Author.name == "brian")
        rows = run_query(ada.union(brian).order_by("name"))
        assert rows == [("ada",), ("brian",)]

    # Ported from test/orm/test_query.py::SetOpsTest.test_union (SQLAlchemy 2.0.51)
    def test_chained_union_of_three_filters_returns_all_three(self, run_query):
        ada = Query(Author.name).where(Author.name == "ada")
        brian = Query(Author.name).where(Author.name == "brian")
        carol = Query(Author.name).where(Author.name == "carol")
        rows = run_query(ada.union(brian).union(carol).order_by("name"))
        assert rows == [("ada",), ("brian",), ("carol",)]

    # Ported from test/orm/test_query.py::SetOpsTest.test_statement_labels (SQLAlchemy 2.0.51)
    def test_union_of_two_joined_queries_returns_the_combined_pairs(
        self, run_query
    ):
        structures = (
            Query(Author.name, Book.title)
            .join(Book, Book.author_id == Author.id)
            .where(Book.title == "structures")
        )
        compilers = (
            Query(Author.name, Book.title)
            .join(Book, Book.author_id == Author.id)
            .where(Book.title == "compilers")
        )
        rows = run_query(structures.union(compilers).order_by("name"))
        assert rows == [("ada", "structures"), ("brian", "compilers")]

    # Ported from test/orm/test_query.py::SetOpsTest.test_union_literal_expressions_results (SQLAlchemy 2.0.51)
    def test_union_with_a_literal_and_a_literal_column_mixed_in(self, run_query):
        q1 = Query(Author.name, literal("x").label("tag")).where(Author.name == "ada")
        q2 = Query(Author.name, literal_column("'y'").label("tag")).where(
            Author.name == "ada"
        )
        rows = run_query(q1.union(q2).order_by("name", "tag"))
        assert rows == [("ada", "x"), ("ada", "y")]
