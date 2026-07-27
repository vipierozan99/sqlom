"""RIGHT and FULL outer joins, and the nullability they imply.

The interesting part is not the keyword, it is which side can be `None`. A LEFT
join nulls the table being joined; a RIGHT join nulls everything already in the
query, *including the primary entity*; a FULL join nulls both. Getting that
backwards produces objects whose every field is None, handed back as real rows.
"""

import pytest

from sqlom import Query
from tests.conftest import Author, Book


class TestKeywords:
    @pytest.mark.parametrize("method,keyword", [
        ("join", "JOIN"),
        ("outer_join", "LEFT OUTER JOIN"),
        ("left_join", "LEFT OUTER JOIN"),
        ("right_join", "RIGHT OUTER JOIN"),
        ("full_join", "FULL OUTER JOIN"),
    ])
    def test_rendering(self, method, keyword):
        query = getattr(Query(Author, Book), method)(Book, Book.author_id == Author.id)
        assert f"{keyword} t_books ON" in query.to_sql()[0]

    def test_left_join_is_an_alias_for_outer_join(self):
        a = Query(Author, Book).outer_join(Book, Book.author_id == Author.id)
        b = Query(Author, Book).left_join(Book, Book.author_id == Author.id)
        assert a.to_sql() == b.to_sql()


class TestNullability:
    def spec(self, query):
        return query.hydration_spec()

    def test_inner_join_nulls_neither_side(self):
        spec = self.spec(Query(Author, Book).join(Book, Book.author_id == Author.id))
        assert [nullable for _, _, nullable in spec] == [False, False]

    def test_left_join_nulls_the_joined_side(self):
        spec = self.spec(
            Query(Author, Book).outer_join(Book, Book.author_id == Author.id)
        )
        assert [nullable for _, _, nullable in spec] == [False, True]

    def test_right_join_nulls_the_primary_side(self):
        spec = self.spec(
            Query(Author, Book).right_join(Book, Book.author_id == Author.id)
        )
        assert [nullable for _, _, nullable in spec] == [True, False]

    def test_full_join_nulls_both(self):
        spec = self.spec(
            Query(Author, Book).full_join(Book, Book.author_id == Author.id)
        )
        assert [nullable for _, _, nullable in spec] == [True, True]

    def test_a_right_join_rekeys_even_a_single_entity_query(self):
        # Query(Author).right_join(...) can yield a NULL Author, so it must NOT
        # reuse the plain single-model hydrator, which would build an object of
        # Nones and return it as a row.
        query = Query(Author).right_join(Book, Book.author_id == Author.id)
        assert query._hydration_key is not Author

    def test_a_left_join_does_not_rekey_a_single_entity_query(self):
        query = Query(Author).outer_join(Book, Book.author_id == Author.id)
        assert query._hydration_key is Author


class TestEndToEnd:
    """dan has no books; every book has an author, so a RIGHT join from authors
    to books produces no NULL authors — one is added to make it observable."""

    def test_left_join_nulls_the_book(self, run_query):
        rows = run_query(
            Query(Author, Book)
            .outer_join(Book, Book.author_id == Author.id)
            .where(Author.name == "dan")
        )
        assert len(rows) == 1 and rows[0][1] is None

    def test_right_join_nulls_the_author(self, db, run_query):
        db.execute("INSERT INTO t_books VALUES (99, 999, 'orphan')")
        try:
            rows = run_query(
                Query(Author, Book)
                .right_join(Book, Book.author_id == Author.id)
                .where(Book.title == "orphan")
            )
            assert len(rows) == 1
            author, book = rows[0]
            assert author is None
            assert book.title == "orphan"
        finally:
            db.execute("DELETE FROM t_books WHERE id = 99")
            db.commit()

    def test_full_join_nulls_either_side(self, db, run_query):
        db.execute("INSERT INTO t_books VALUES (99, 999, 'orphan')")
        try:
            rows = run_query(
                Query(Author, Book)
                .full_join(Book, Book.author_id == Author.id)
                .order_by(Author.id)
            )
            missing_book = [(a, b) for a, b in rows if b is None]
            missing_author = [(a, b) for a, b in rows if a is None]
            assert [a.name for a, _ in missing_book] == ["dan"]
            assert [b.title for _, b in missing_author] == ["orphan"]
            # And the matched rows still hydrate both sides.
            matched = [(a, b) for a, b in rows if a is not None and b is not None]
            assert len(matched) == 4
        finally:
            db.execute("DELETE FROM t_books WHERE id = 99")
            db.commit()

    def test_right_join_on_a_single_entity_query_yields_none(self, db, run_query):
        db.execute("INSERT INTO t_books VALUES (99, 999, 'orphan')")
        try:
            rows = run_query(
                Query(Author)
                .right_join(Book, Book.author_id == Author.id)
                .where(Book.title == "orphan")
            )
            # A one-model query yields instances, not tuples — even when a RIGHT
            # join makes them nullable. So an unmatched row is a bare None, not
            # an Author whose every field is None.
            assert rows == [None]
        finally:
            db.execute("DELETE FROM t_books WHERE id = 99")
            db.commit()
