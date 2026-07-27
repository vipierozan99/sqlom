"""GROUP BY, aggregates, HAVING, DISTINCT, OFFSET and derived tables."""

import pytest

from sqlom import Alias, Query, avg, count, max_, min_, sum_
from tests.conftest import Author, Book, Tag


class TestAggregateRendering:
    def test_count_star(self):
        assert count().to_sql(lambda: "?")[0] == "count(*)"

    def test_count_column(self):
        assert count(Book.id).to_sql(lambda: "?")[0] == "count(id)"

    def test_count_distinct(self):
        assert count(Book.author_id, distinct=True).to_sql(lambda: "?")[0] == \
            "count(DISTINCT author_id)"

    @pytest.mark.parametrize("factory,expected", [
        (sum_, "sum(id)"), (avg, "avg(id)"), (min_, "min(id)"), (max_, "max(id)"),
    ])
    def test_the_other_aggregates(self, factory, expected):
        assert factory(Book.id).to_sql(lambda: "?")[0] == expected

    def test_count_is_typed_as_int_and_the_rest_are_untyped(self):
        # An untyped aggregate picks no converter. Guessing (avg of an int is
        # numeric in Postgres, bigint for sum) would corrupt the value.
        assert count().py_type is int
        assert avg(Book.id).py_type is None
        assert sum_(Book.id).py_type is None

    def test_label_renders_as_alias_in_the_select_list(self):
        sql, _ = Query(Book.author_id, count().label("n")).group_by(Book.author_id).to_sql()
        assert sql.startswith("SELECT author_id, count(*) AS n FROM t_books")

    def test_label_is_the_subquery_output_name(self):
        query = Query(Book.author_id, count().label("n")).group_by(Book.author_id)
        assert [name for name, _ in query.output_columns()] == ["author_id", "n"]

    def test_unlabelled_aggregate_gets_a_derived_output_name(self):
        query = Query(count(Book.id)).group_by(Book.author_id)
        assert [name for name, _ in query.output_columns()] == ["count_id"]


class TestGroupBy:
    def test_group_by_a_column_expression(self):
        sql, _ = Query(Book.author_id, count()).group_by(Book.author_id).to_sql()
        assert sql.endswith("GROUP BY author_id")

    def test_group_by_a_bare_name(self):
        sql, _ = Query(Book.author_id, count()).group_by("author_id").to_sql()
        assert sql.endswith("GROUP BY author_id")

    def test_group_by_several_columns(self):
        sql, _ = (Query(Book.author_id, Book.title, count())
                  .group_by(Book.author_id, Book.title).to_sql())
        assert sql.endswith("GROUP BY author_id, title")

    def test_group_by_rejects_an_unknown_column(self):
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Query(Book).group_by("nope")

    def test_group_by_rejects_an_unjoined_source(self):
        with pytest.raises(ValueError, match="not part of this query"):
            Query(Book).group_by(Author.id)

    def test_clause_order_is_group_having_order_limit(self):
        sql, _ = (Query(Book.author_id, count())
                  .where(Book.id > 0)
                  .group_by(Book.author_id)
                  .having(count() > 1)
                  .order_by(Book.author_id)
                  .limit(5).offset(1)
                  .to_sql(placeholder="$"))
        for earlier, later in (("WHERE", "GROUP BY"), ("GROUP BY", "HAVING"),
                               ("HAVING", "ORDER BY"), ("ORDER BY", "LIMIT"),
                               ("LIMIT", "OFFSET")):
            assert sql.index(earlier) < sql.index(later), sql


class TestHaving:
    def test_having_on_an_aggregate(self):
        sql, params = (Query(Book.author_id, count())
                       .group_by(Book.author_id)
                       .having(count() > 1)
                       .to_sql(placeholder="$"))
        assert sql.endswith("HAVING count(*) > $1")
        assert params == (1,)

    def test_several_having_clauses_and(self):
        sql, _ = (Query(Book.author_id, count())
                  .group_by(Book.author_id)
                  .having(count() > 1)
                  .having(max_(Book.id) < 100)
                  .to_sql(placeholder="$"))
        assert sql.endswith("HAVING count(*) > $1 AND max(id) < $2")

    def test_having_numbering_follows_where(self):
        sql, params = (Query(Book.author_id, count())
                       .where(Book.id > 5)
                       .group_by(Book.author_id)
                       .having(count() > 1)
                       .to_sql(placeholder="$"))
        assert "WHERE id > $1" in sql and "HAVING count(*) > $2" in sql
        assert params == (5, 1)

    def test_having_refuses_a_non_predicate(self):
        with pytest.raises(TypeError, match="takes a predicate"):
            Query(Book).having(count())

    def test_having_on_a_labelled_aggregate(self):
        # .label() wraps the aggregate in a Labelled node; the column-reference
        # walk that validates having() has to see through that wrapper too.
        sql, params = (Query(Book.author_id, count().label("n"))
                       .group_by(Book.author_id)
                       .having(count().label("n") > 1)
                       .to_sql(placeholder="$"))
        assert sql.endswith("HAVING count(*) > $1")
        assert params == (1,)


class TestDistinctAndOffset:
    def test_distinct(self):
        sql, _ = Query(Book.author_id).distinct().to_sql()
        assert sql.startswith("SELECT DISTINCT author_id")

    def test_distinct_can_be_turned_off(self):
        assert not Query(Book.author_id).distinct().distinct(False).to_sql()[0] \
            .startswith("SELECT DISTINCT")

    def test_offset(self):
        sql, params = Query(Book).limit(5).offset(10).to_sql(placeholder="$")
        assert sql.endswith("LIMIT $1 OFFSET $2")
        assert params == (5, 10)

    @pytest.mark.parametrize("bad", [-1, True, 1.5])
    def test_offset_validation_matches_limit(self, bad):
        with pytest.raises((ValueError, TypeError)):
            Query(Book).offset(bad)


class TestDerivedTables:
    def test_subquery_exposes_its_output_columns(self):
        sub = Query(Book.author_id, count().label("n")).group_by(Book.author_id).subquery("s")
        assert set(sub.__columns__) == {"author_id", "n"}
        assert sub.n.source is sub

    def test_unknown_output_column_raises(self):
        sub = Query(Book.author_id).subquery("s")
        with pytest.raises(AttributeError, match="has no output column"):
            sub.nope

    def test_renders_as_a_parenthesised_derived_table(self):
        sub = Query(Book.author_id, count().label("n")).group_by(Book.author_id).subquery("s")
        sql, _ = Query(Author, sub.n).join(sub, sub.author_id == Author.id).to_sql()
        assert (
            "FROM t_authors JOIN (SELECT author_id, count(*) AS n FROM t_books "
            "GROUP BY author_id) AS s ON s.author_id = t_authors.id"
        ) in sql

    def test_a_subquery_cannot_be_selected_whole(self):
        sub = Query(Book.author_id).subquery("s")
        with pytest.raises(TypeError, match="cannot be selected as a whole"):
            Query(sub)

    def test_subquery_needs_a_non_empty_alias(self):
        with pytest.raises(TypeError, match="non-empty string alias"):
            Query(Book.author_id).subquery("")

    def test_referencing_an_unjoined_subquery_names_it_in_the_error(self):
        # source_name() has a dedicated branch for a Subquery (as for CTE) so
        # the error reads "subquery s" rather than a raw repr.
        sub = Query(Book.author_id, count().label("n")).group_by(Book.author_id).subquery("s")
        with pytest.raises(ValueError, match="references subquery s, which is not part"):
            Query(Author).where(sub.n > 1)

    def test_subquery_parameters_number_before_the_outer_ones(self):
        sub = (Query(Book.author_id, count().label("n"))
               .where(Book.id > 5)
               .group_by(Book.author_id)
               .subquery("s"))
        sql, params = (Query(Author, sub.n)
                       .join(sub, sub.author_id == Author.id)
                       .where(Author.name == "ada")
                       .to_sql(placeholder="$"))
        assert params == (5, "ada")
        assert sql.index("$1") < sql.index("$2")

    def test_subquery_as_the_primary_source(self):
        sub = Query(Book.author_id, count().label("n")).group_by(Book.author_id).subquery("s")
        sql, _ = Query(sub.author_id, sub.n).to_sql()
        assert sql.startswith("SELECT author_id, n FROM (SELECT author_id")


class TestEndToEnd:
    def test_count_per_group(self, run_query):
        rows = run_query(
            Query(Book.author_id, count())
            .group_by(Book.author_id)
            .order_by(Book.author_id)
        )
        assert rows == [(1, 2), (2, 1), (3, 1)]

    def test_having_filters_groups(self, run_query):
        rows = run_query(
            Query(Book.author_id, count())
            .group_by(Book.author_id)
            .having(count() > 1)
        )
        assert rows == [(1, 2)]

    def test_aggregates_over_real_data(self, run_query):
        rows = run_query(Query(count(), min_(Book.id), max_(Book.id), sum_(Book.id)))
        assert rows == [(4, 10, 13, 46)]

    def test_count_distinct(self, run_query):
        rows = run_query(Query(count(Book.author_id, distinct=True)))
        assert rows == [(3,)]

    def test_group_by_with_a_join(self, run_query):
        rows = run_query(
            Query(Author.name, count(Book.id))
            .join(Book, Book.author_id == Author.id)
            .group_by(Author.name)
            .order_by(Author.name)
        )
        assert rows == [("ada", 2), ("brian", 1), ("carol", 1)]

    def test_order_by_an_aggregate(self, run_query):
        rows = run_query(
            Query(Book.author_id, count())
            .group_by(Book.author_id)
            .order_by(count(), descending=True)
            .limit(1)
        )
        assert rows == [(1, 2)]

    def test_distinct_over_real_data(self, run_query):
        rows = run_query(Query(Book.author_id).distinct().order_by(Book.author_id))
        assert rows == [(1,), (2,), (3,)]

    def test_offset_over_real_data(self, run_query):
        rows = run_query(Query(Author.id).order_by(Author.id).limit(2).offset(1))
        assert rows == [(2,), (3,)]

    def test_derived_table_end_to_end(self, run_query):
        sub = (Query(Book.author_id, count().label("n"))
               .group_by(Book.author_id)
               .subquery("s"))
        rows = run_query(
            Query(Author, sub.n)
            .join(sub, sub.author_id == Author.id)
            .where(sub.n > 1)
        )
        assert len(rows) == 1
        author, n = rows[0]
        assert author.name == "ada" and n == 2

    def test_derived_table_joined_to_an_alias(self, run_query):
        a = Alias(Author, "a")
        sub = (Query(Tag.book_id, count().label("n"))
               .group_by(Tag.book_id)
               .subquery("s"))
        rows = run_query(
            Query(a.name, Book.title, sub.n)
            .join(Book, Book.author_id == a.id)
            .join(sub, sub.book_id == Book.id)
            .order_by(Book.id)
        )
        assert rows == [("ada", "structures", 1), ("brian", "compilers", 1)]
