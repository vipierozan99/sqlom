"""OR, AND, NOT, IN and EXISTS — the predicate tree."""

import pytest

from sqlom import Query, and_, count, exists, not_, or_
from tests.conftest import Author, Book


def where_of(query, placeholder="$"):
    sql, params = query.to_sql(placeholder=placeholder)
    return sql.split(" WHERE ", 1)[1], params


class TestOrAnd:
    def test_or_of_two(self):
        clause, params = where_of(
            Query(Author).where(or_(Author.id == 1, Author.id == 2))
        )
        assert clause == "(id = $1 OR id = $2)"
        assert params == (1, 2)

    def test_and_of_two_is_explicit_and_parenthesised(self):
        clause, _ = where_of(Query(Author).where(and_(Author.id > 1, Author.id < 9)))
        assert clause == "(id > $1 AND id < $2)"

    def test_repeated_where_calls_and_without_extra_brackets(self):
        # This is the shape the benchmarks use; the SQL must not gain brackets.
        clause, _ = where_of(Query(Author).where(Author.id > 1).where(Author.id < 9))
        assert clause == "id > $1 AND id < $2"

    def test_multiple_arguments_to_one_where_call_and(self):
        clause, _ = where_of(Query(Author).where(Author.id > 1, Author.id < 9))
        assert clause == "id > $1 AND id < $2"

    def test_nested_or_inside_and(self):
        clause, params = where_of(
            Query(Author).where(
                and_(Author.active == True, or_(Author.id == 1, Author.id == 2))  # noqa: E712
            )
        )
        assert clause == "(active = $1 AND (id = $2 OR id = $3))"
        assert params == (True, 1, 2)

    def test_same_operator_nesting_is_flattened(self):
        clause, _ = where_of(
            Query(Author).where(or_(Author.id == 1, or_(Author.id == 2, Author.id == 3)))
        )
        assert clause == "(id = $1 OR id = $2 OR id = $3)"

    def test_single_argument_returns_the_predicate_unchanged(self):
        predicate = Author.id == 1
        assert or_(predicate) is predicate
        assert and_(predicate) is predicate

    def test_operator_forms(self):
        clause, _ = where_of(Query(Author).where((Author.id == 1) | (Author.id == 2)))
        assert clause == "(id = $1 OR id = $2)"
        clause, _ = where_of(Query(Author).where((Author.id > 1) & (Author.id < 9)))
        assert clause == "(id > $1 AND id < $2)"

    def test_empty_combination_is_refused(self):
        with pytest.raises(TypeError, match="at least one"):
            or_()

    def test_non_predicate_arguments_are_refused(self):
        with pytest.raises(TypeError, match="takes predicates"):
            or_(Author.id, 5)


class TestNot:
    def test_not_wraps_and_parenthesises(self):
        clause, params = where_of(Query(Author).where(not_(Author.id == 1)))
        assert clause == "NOT (id = $1)"
        assert params == (1,)

    def test_invert_operator(self):
        clause, _ = where_of(Query(Author).where(~(Author.id == 1)))
        assert clause == "NOT (id = $1)"

    def test_not_of_a_group(self):
        clause, _ = where_of(
            Query(Author).where(not_(or_(Author.id == 1, Author.id == 2)))
        )
        assert clause == "NOT ((id = $1 OR id = $2))"

    def test_not_refuses_a_non_predicate(self):
        with pytest.raises(TypeError, match="takes a predicate"):
            not_(Author.id)


class TestIn:
    def test_in_with_values(self):
        clause, params = where_of(Query(Author).where(Author.id.in_([1, 2, 3])))
        assert clause == "id IN ($1, $2, $3)"
        assert params == (1, 2, 3)

    def test_not_in(self):
        clause, params = where_of(Query(Author).where(Author.id.not_in([7])))
        assert clause == "id NOT IN ($1)"
        assert params == (7,)

    def test_empty_in_renders_false_rather_than_invalid_sql(self):
        # `x IN ()` is a syntax error in Postgres, and an empty collection is a
        # perfectly ordinary thing for calling code to arrive at.
        clause, params = where_of(Query(Author).where(Author.id.in_([])))
        assert clause == "FALSE"
        assert params == ()

    def test_empty_not_in_renders_true(self):
        clause, _ = where_of(Query(Author).where(Author.id.not_in([])))
        assert clause == "TRUE"

    def test_in_with_a_subquery(self):
        clause, params = where_of(
            Query(Author).where(
                Author.id.in_(Query(Book.author_id).where(Book.title == "x"))
            )
        )
        assert clause == "id IN (SELECT author_id FROM t_books WHERE title = $1)"
        assert params == ("x",)

    def test_like(self):
        clause, params = where_of(Query(Author).where(Author.name.like("a%")))
        assert clause == "name LIKE $1"
        assert params == ("a%",)

    def test_is_null_helpers_match_the_operator_form(self):
        assert (where_of(Query(Author).where(Author.name.is_null()))[0]
                == "name IS NULL")
        assert (where_of(Query(Author).where(Author.name.is_not_null()))[0]
                == "name IS NOT NULL")


class TestExists:
    def test_correlated_exists(self):
        clause, params = where_of(
            Query(Author).where(
                exists(Query(Book.id).correlate(Author)
                       .where(Book.author_id == Author.id))
            )
        )
        assert clause == (
            "EXISTS (SELECT t_books.id FROM t_books "
            "WHERE t_books.author_id = t_authors.id)"
        )
        assert params == ()

    def test_not_exists(self):
        clause, _ = where_of(
            Query(Author).where(
                ~exists(Query(Book.id).correlate(Author)
                        .where(Book.author_id == Author.id))
            )
        )
        assert clause.startswith("NOT EXISTS (")

    def test_correlation_must_be_declared(self):
        # Without correlate(), referencing the outer table is indistinguishable
        # from a typo, so it is refused rather than guessed at.
        with pytest.raises(ValueError, match="not part of this query"):
            Query(Book.id).where(Book.author_id == Author.id)

    def test_exists_refuses_a_non_query(self):
        with pytest.raises(TypeError, match="takes a Query"):
            exists(Author.id == 1)


class TestScalarSubquery:
    def test_comparison_against_a_subquery(self):
        clause, _ = where_of(
            Query(Book).where(Book.id > Query(count(Book.id)).scalar_subquery())
        )
        assert clause == "id > (SELECT count(id) FROM t_books)"

    def test_a_query_works_without_the_explicit_call(self):
        # scalar_subquery() returns self; it exists for readability, so the bare
        # query must behave identically or the API would be lying.
        clause, _ = where_of(Query(Book).where(Book.id > Query(count(Book.id))))
        assert clause == "id > (SELECT count(id) FROM t_books)"


class TestEndToEnd:
    def test_or_against_real_rows(self, run_query):
        rows = run_query(
            Query(Author).where(or_(Author.name == "ada", Author.name == "dan"))
            .order_by("id")
        )
        assert [a.name for a in rows] == ["ada", "dan"]

    def test_not_against_real_rows(self, run_query):
        rows = run_query(Query(Author).where(~(Author.active == True)))  # noqa: E712
        assert [a.name for a in rows] == ["carol"]

    def test_in_against_real_rows(self, run_query):
        rows = run_query(Query(Author).where(Author.id.in_([1, 3])).order_by("id"))
        assert [a.name for a in rows] == ["ada", "carol"]

    def test_empty_in_returns_nothing(self, run_query):
        assert run_query(Query(Author).where(Author.id.in_([]))) == []

    def test_in_subquery_against_real_rows(self, run_query):
        rows = run_query(
            Query(Author)
            .where(Author.id.in_(Query(Book.author_id).where(Book.title == "compilers")))
        )
        assert [a.name for a in rows] == ["brian"]

    def test_correlated_exists_against_real_rows(self, run_query):
        rows = run_query(
            Query(Author)
            .where(exists(Query(Book.id).correlate(Author)
                          .where(Book.author_id == Author.id)))
            .order_by("id")
        )
        assert [a.name for a in rows] == ["ada", "brian", "carol"]

    def test_not_exists_finds_the_authorless_row(self, run_query):
        rows = run_query(
            Query(Author)
            .where(~exists(Query(Book.id).correlate(Author)
                           .where(Book.author_id == Author.id)))
        )
        assert [a.name for a in rows] == ["dan"]

    def test_complex_mixed_predicate(self, run_query):
        rows = run_query(
            Query(Author)
            .where(or_(and_(Author.active == True, Author.id.in_([1, 2])),  # noqa: E712
                       Author.name == "carol"))
            .order_by("id")
        )
        assert [a.name for a in rows] == ["ada", "brian", "carol"]
