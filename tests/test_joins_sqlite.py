"""Joins end to end against a real sqlite database.

These run the generated SQL *and* the generated hydrator, so they catch the class
of bug that unit-testing either half alone would miss: a select list and a
hydrator that disagree on column order or width.
"""

import pytest

from sqlom import Query
from tests.conftest import Author, Book, Tag


class TestInnerJoin:
    def test_pairs_are_matched_correctly(self, run_query):
        rows = run_query(
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .order_by(Book.id)
        )
        assert [(a.name, b.title) for a, b in rows] == [
            ("ada", "structures"),
            ("ada", "algorithms"),
            ("brian", "compilers"),
            ("carol", "typography"),
        ]

    def test_authors_without_books_are_excluded(self, run_query):
        rows = run_query(Query(Author, Book).join(Book, Book.author_id == Author.id))
        assert "dan" not in {a.name for a, _ in rows}

    def test_a_where_on_the_left_table(self, run_query):
        rows = run_query(
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .where(Author.active == True)  # noqa: E712
            .order_by(Book.id)
        )
        assert [b.title for _, b in rows] == ["structures", "algorithms", "compilers"]

    def test_a_where_on_the_right_table(self, run_query):
        rows = run_query(
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .where(Book.title == "compilers")
        )
        assert len(rows) == 1
        assert rows[0][0].name == "brian"

    def test_booleans_are_converted_on_the_joined_shape_too(self, run_query):
        rows = run_query(Query(Author, Book).join(Book, Book.author_id == Author.id))
        assert all(isinstance(a.active, bool) for a, _ in rows)

    def test_limit_applies_to_joined_rows(self, run_query):
        rows = run_query(
            Query(Author, Book)
            .join(Book, Book.author_id == Author.id)
            .order_by(Book.id)
            .limit(2)
        )
        assert len(rows) == 2
        assert [b.title for _, b in rows] == ["structures", "algorithms"]


class TestOuterJoin:
    def test_unmatched_right_side_is_none(self, run_query):
        rows = run_query(
            Query(Author, Book)
            .outer_join(Book, Book.author_id == Author.id)
            .order_by(Author.id)
        )
        by_author = {}
        for author, book in rows:
            by_author.setdefault(author.name, []).append(book)
        assert by_author["dan"] == [None]
        assert all(b is not None for b in by_author["ada"])

    def test_every_author_appears(self, run_query):
        rows = run_query(
            Query(Author, Book).outer_join(Book, Book.author_id == Author.id)
        )
        assert {a.name for a, _ in rows} == {"ada", "brian", "carol", "dan"}

    def test_left_side_is_never_none(self, run_query):
        rows = run_query(
            Query(Author, Book).outer_join(Book, Book.author_id == Author.id)
        )
        assert all(a is not None for a, _ in rows)


class TestFilteringJoin:
    """A join used only to filter: the joined model is not selected, so rows stay
    single-model and must hydrate to instances rather than 1-tuples."""

    def test_returns_plain_instances(self, run_query):
        rows = run_query(
            Query(Author)
            .join(Book, Book.author_id == Author.id)
            .where(Book.title == "typography")
        )
        assert len(rows) == 1
        assert isinstance(rows[0], Author)
        assert rows[0].name == "carol"

    def test_duplicates_appear_once_per_match(self, run_query):
        # ada has two books, so an inner join yields her twice. sqlom does not
        # de-duplicate; SQLAlchemy's ORM would via the identity map.
        rows = run_query(Query(Author).join(Book, Book.author_id == Author.id))
        assert [a.name for a in rows].count("ada") == 2


class TestColumnEntities:
    def test_model_plus_column(self, run_query):
        rows = run_query(
            Query(Author, Book.title)
            .join(Book, Book.author_id == Author.id)
            .order_by(Book.id)
        )
        assert [(a.name, title) for a, title in rows] == [
            ("ada", "structures"),
            ("ada", "algorithms"),
            ("brian", "compilers"),
            ("carol", "typography"),
        ]
        assert all(isinstance(t, str) for _, t in rows)

    def test_two_columns_only(self, run_query):
        rows = run_query(
            Query(Author.name, Book.title)
            .join(Book, Book.author_id == Author.id)
            .order_by(Book.id)
            .limit(1)
        )
        assert rows == [("ada", "structures")]


class TestThreeWayJoin:
    def test_chained_joins(self, run_query):
        rows = run_query(
            Query(Author, Book, Tag)
            .join(Book, Book.author_id == Author.id)
            .join(Tag, Tag.book_id == Book.id)
            .order_by(Tag.id)
        )
        assert [(a.name, b.title, t.label) for a, b, t in rows] == [
            ("ada", "structures", "classic"),
            ("brian", "compilers", "classic"),
        ]

    def test_mixed_inner_and_outer(self, run_query):
        rows = run_query(
            Query(Author, Book, Tag)
            .join(Book, Book.author_id == Author.id)
            .outer_join(Tag, Tag.book_id == Book.id)
            .order_by(Book.id)
        )
        assert [(b.title, t.label if t else None) for _, b, t in rows] == [
            ("structures", "classic"),
            ("algorithms", None),
            ("compilers", "classic"),
            ("typography", None),
        ]


class TestSingleModelUnchanged:
    """Joins must not have altered the plain path."""

    def test_plain_select(self, run_query):
        rows = run_query(Query(Author).order_by("id"))
        assert [a.name for a in rows] == ["ada", "brian", "carol", "dan"]
        assert all(isinstance(a, Author) for a in rows)

    def test_where_and_limit(self, run_query):
        rows = run_query(Query(Author).where(Author.active == True).order_by("id").limit(2))  # noqa: E712
        assert [a.name for a in rows] == ["ada", "brian"]

    def test_is_null_predicate_against_real_data(self, db, run_query):
        db.execute("INSERT INTO t_authors VALUES (99, NULL, 1)")
        try:
            rows = run_query(Query(Author).where(Author.name == None))  # noqa: E711
            assert [a.id for a in rows] == [99]
            rows = run_query(Query(Author).where(Author.name != None))  # noqa: E711
            assert 99 not in {a.id for a in rows}
        finally:
            db.execute("DELETE FROM t_authors WHERE id = 99")
            db.commit()
