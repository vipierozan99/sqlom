"""Conditions: the three shapes a predicate can take, and what each binds."""

import pytest

from sqlom import ColumnExpr, Condition
from tests.conftest import Author, Book


def test_class_access_returns_an_expression_not_a_value():
    assert isinstance(Author.id, ColumnExpr)
    assert Author.id.model is Author
    assert Author.id.name == "id"
    assert Author.id.py_type is int


def test_comparison_builds_a_condition_not_a_bool():
    condition = Author.id > 100
    assert isinstance(condition, Condition)
    assert (condition.model, condition.column_name, condition.op, condition.value) == (
        Author, "id", ">", 100,
    )


@pytest.mark.parametrize("condition,expected", [
    (Author.id == 1, "id = ?"),
    (Author.id != 1, "id != ?"),
    (Author.id > 1, "id > ?"),
    (Author.id >= 1, "id >= ?"),
    (Author.id < 1, "id < ?"),
    (Author.id <= 1, "id <= ?"),
])
def test_operators_render(condition, expected):
    clause, params = condition.to_sql("?")
    assert clause == expected
    assert params == (1,)


def test_value_predicate_binds_exactly_one_parameter():
    clause, params = (Author.name == "ada").to_sql("$1")
    assert clause == "name = $1"
    assert params == ("ada",)


class TestNullPredicates:
    """`x = NULL` is never true in SQL, so equality against None must become IS."""

    def test_eq_none_renders_is_null_and_binds_nothing(self):
        clause, params = (Author.name == None).to_sql("$1")  # noqa: E711
        assert clause == "name IS NULL"
        assert params == ()

    def test_ne_none_renders_is_not_null(self):
        clause, params = (Author.name != None).to_sql("$1")  # noqa: E711
        assert clause == "name IS NOT NULL"
        assert params == ()

    def test_ordering_operators_against_none_are_left_alone(self):
        # `x > NULL` is unknown too, but there is no IS form for it; rendering it
        # verbatim is at least not a silent rewrite.
        clause, params = (Author.id > None).to_sql("$1")
        assert clause == "id > $1"
        assert params == (None,)


class TestColumnComparisons:
    """`Book.author_id == Author.id` is an ON clause, not a bound value."""

    def test_is_recognised_as_a_column_comparison(self):
        condition = Book.author_id == Author.id
        assert condition.is_column_comparison
        assert isinstance(condition.value, ColumnExpr)

    def test_binds_no_parameter(self):
        clause, params = (Book.author_id == Author.id).to_sql("$1")
        assert params == ()
        assert clause == "author_id = id"

    def test_qualified_rendering(self):
        def qualify(model, name):
            return f"{model.__tablename__}.{name}"

        clause, params = (Book.author_id == Author.id).to_sql("$1", qualify)
        assert clause == "t_books.author_id = t_authors.id"
        assert params == ()

    def test_models_reports_both_sides(self):
        assert (Book.author_id == Author.id).models() == (Book, Author)

    def test_models_reports_one_side_for_a_value_predicate(self):
        assert (Book.title == "x").models() == (Book,)


def test_repr_distinguishes_the_two_forms():
    # The repr identifies columns by the source's rendered prefix, since with an
    # alias in play the model name alone no longer says which table it is.
    assert repr(Author.id > 1) == "<Condition <ColumnExpr t_authors.id> > 1>"
    assert repr(Book.author_id == Author.id) == (
        "<Condition <ColumnExpr t_books.author_id> = <ColumnExpr t_authors.id>>"
    )


def test_column_expr_is_hashable_so_it_can_key_a_dict():
    assert hash(Author.id) == hash(Author.id)
    assert {Author.id: "x"}[Author.id] == "x"
