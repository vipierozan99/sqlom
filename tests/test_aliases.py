"""Table aliases and self-joins."""

import pytest

from sqlom import Alias, ColumnExpr, Query
from tests.conftest import Author, Book


class TestAliasBasics:
    def test_columns_carry_the_alias_as_their_source(self):
        a = Alias(Author, "a")
        assert isinstance(a.id, ColumnExpr)
        assert a.id.source is a
        assert a.id.source is not Author
        assert a.id.py_type is int

    def test_unknown_column_raises_rather_than_rendering(self):
        a = Alias(Author, "a")
        with pytest.raises(AttributeError, match="has no column 'nope'"):
            a.nope

    def test_alias_needs_a_model_and_a_name(self):
        with pytest.raises(TypeError, match="takes a model"):
            Alias("t_authors", "a")
        with pytest.raises(TypeError, match="non-empty string"):
            Alias(Author, "")

    def test_from_clause_renders_as_table_as_alias(self):
        a = Alias(Author, "a")
        sql, _ = Query(a).where(a.id > 1).to_sql()
        assert sql == "SELECT id, name, active FROM t_authors AS a WHERE id > ?"

    def test_single_aliased_source_stays_unqualified(self):
        # One source is unambiguous, so bare names are still correct — and this
        # keeps the rule "qualify only when you must" uniform.
        a = Alias(Author, "a")
        assert " AS a WHERE id" in Query(a).where(a.id > 1).to_sql()[0]

    def test_primary_model_is_the_aliased_model_for_hydration(self):
        a = Alias(Author, "a")
        assert Query(a).model is Author
        assert Query(a)._hydration_key is Author


class TestSelfJoin:
    def test_renders_with_both_sides_distinguished(self):
        mgr = Alias(Author, "mgr")
        sql, _ = (Query(Author, mgr)
                  .join(mgr, Author.id == mgr.id)
                  .to_sql())
        assert sql == (
            "SELECT t_authors.id, t_authors.name, t_authors.active, "
            "mgr.id, mgr.name, mgr.active "
            "FROM t_authors JOIN t_authors AS mgr ON t_authors.id = mgr.id"
        )

    def test_predicates_target_the_right_side(self):
        mgr = Alias(Author, "mgr")
        sql, params = (Query(Author, mgr)
                       .join(mgr, Author.id == mgr.id)
                       .where(mgr.name == "ada")
                       .where(Author.name == "bo")
                       .to_sql(placeholder="$"))
        assert "WHERE mgr.name = $1 AND t_authors.name = $2" in sql
        assert params == ("ada", "bo")

    def test_order_by_targets_the_right_side(self):
        mgr = Alias(Author, "mgr")
        sql, _ = (Query(Author, mgr)
                  .join(mgr, Author.id == mgr.id)
                  .order_by(mgr.name)
                  .to_sql())
        assert sql.endswith("ORDER BY mgr.name")

    def test_two_aliases_of_the_same_model(self):
        one, two = Alias(Author, "one"), Alias(Author, "two")
        sql, _ = (Query(one, two)
                  .join(two, one.id == two.id)
                  .to_sql())
        assert "FROM t_authors AS one JOIN t_authors AS two ON one.id = two.id" in sql

    def test_unaliased_self_join_is_refused(self):
        with pytest.raises(ValueError, match="alias one side"):
            Query(Author).join(Author, Author.id == Author.id)

    def test_two_sources_rendering_the_same_prefix_are_refused(self):
        # An alias whose name collides with a real table name would make every
        # qualified reference ambiguous.
        clash = Alias(Book, "t_authors")
        with pytest.raises(ValueError, match="would both render as"):
            Query(Author).join(clash, clash.author_id == Author.id)

    def test_the_same_alias_object_cannot_be_joined_twice(self):
        mgr = Alias(Author, "mgr")
        query = Query(Author, mgr).join(mgr, Author.id == mgr.id)
        with pytest.raises(ValueError, match="already in this query"):
            query.join(mgr, Author.id == mgr.id)


class TestSelfJoinEndToEnd:
    """t_books has two rows for author 1, so a self-join on author_id pairs them."""

    def test_pairs_rows_of_the_same_table(self, run_query):
        other = Alias(Book, "other")
        rows = run_query(
            Query(Book, other)
            .join(other, Book.author_id == other.author_id)
            .where(Book.id < other.id)
            .order_by(Book.id)
        )
        assert [(a.title, b.title) for a, b in rows] == [
            ("structures", "algorithms")
        ]

    def test_alias_only_query(self, run_query):
        a = Alias(Author, "a")
        rows = run_query(Query(a).where(a.active == True).order_by(a.id))  # noqa: E712
        assert [x.name for x in rows] == ["ada", "brian", "dan"]
        assert all(isinstance(x, Author) for x in rows)

    def test_alias_joined_to_a_different_model(self, run_query):
        a = Alias(Author, "a")
        rows = run_query(
            Query(a, Book)
            .join(Book, Book.author_id == a.id)
            .where(a.name == "brian")
        )
        assert len(rows) == 1
        assert rows[0][0].name == "brian" and rows[0][1].title == "compilers"
