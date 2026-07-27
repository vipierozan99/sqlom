"""`UPDATE ... FROM` and `DELETE ... USING`.

There is no ON clause in either: the join condition goes in `where()`, which is how
SQL spells it and which makes a forgotten condition a cross product rather than a
syntax error. Both builders refuse to render without one.

The qualification rule is the other thing worth testing. A second table makes bare
column names ambiguous, so every *reference* gets qualified — but the SET target must
stay unqualified, because Postgres rejects `SET t.col = ...` outright.

Portability: `UPDATE ... FROM` works on Postgres and on sqlite 3.33+, and is executed
against sqlite below. `DELETE ... USING` is Postgres-only; sqlite has no such form, so
those tests check the rendering here and the execution lives in `test_dml_pg.py`.
"""

import sqlite3

import pytest

from sqlom import Alias, Column, Delete, ModelMeta, Query, Update, count

from tests.conftest import Author, Book, Tag


def sql_of(statement, placeholder="$"):
    return statement.to_sql(placeholder=placeholder)[0]


class TestUpdateFromRendering:
    def test_basic(self):
        statement = (Update(Book).set(title=Author.name).from_(Author)
                     .where(Author.id == Book.author_id))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "UPDATE t_books SET title = t_authors.name FROM t_authors "
            "WHERE t_authors.id = t_books.author_id"
        )
        assert params == ()

    def test_set_target_stays_unqualified(self):
        # `SET t_books.title = ...` is rejected by Postgres, so the left-hand side
        # is emitted directly while the value is resolved and qualified.
        assert "SET title = t_authors.name" in sql_of(
            Update(Book).set(title=Author.name).from_(Author)
                        .where(Author.id == Book.author_id)
        )

    def test_extra_conditions_are_qualified_too(self):
        sql, params = (Update(Book).set(title=Author.name).from_(Author)
                       .where(Author.id == Book.author_id, Author.active == True)
                       .to_sql(placeholder="$"))
        assert sql.endswith(
            "WHERE t_authors.id = t_books.author_id AND t_authors.active = $1"
        )
        assert params == (True,)

    def test_returning_is_qualified_and_may_name_the_other_table(self):
        # Postgres allows RETURNING to reach the FROM tables; the qualification
        # follows from the statement having two sources.
        assert sql_of(
            Update(Book).set(title=Author.name).from_(Author)
                        .where(Author.id == Book.author_id)
                        .returning(Book.id, Author.name)
        ).endswith("RETURNING t_books.id, t_authors.name")

    def test_returning_the_whole_target_model(self):
        assert sql_of(
            Update(Book).set(title=Author.name).from_(Author)
                        .where(Author.id == Book.author_id)
                        .returning(Book)
        ).endswith("RETURNING t_books.id, t_books.author_id, t_books.title")

    def test_several_from_tables(self):
        statement = (Update(Book).set(title=Tag.label)
                     .from_(Author, Tag)
                     .where(Author.id == Book.author_id, Tag.book_id == Book.id))
        assert " FROM t_authors, t_tags WHERE " in sql_of(statement)

    def test_from_can_be_called_before_set(self):
        # Builder order must not matter; column references are validated when the
        # statement renders, not in the method that received them.
        forwards = sql_of(Update(Book).set(title=Author.name).from_(Author)
                          .where(Author.id == Book.author_id))
        backwards = sql_of(Update(Book).from_(Author).set(title=Author.name)
                           .where(Author.id == Book.author_id))
        assert forwards == backwards

    def test_where_can_be_called_before_from(self):
        assert sql_of(
            Update(Book).where(Author.id == Book.author_id)
                        .set(title=Author.name).from_(Author)
        ) == sql_of(
            Update(Book).set(title=Author.name).from_(Author)
                        .where(Author.id == Book.author_id)
        )

    def test_an_alias_makes_a_self_update_possible(self):
        other = Alias(Author, "other")
        statement = (Update(Author).set(name=other.name).from_(other)
                     .where(other.id == Author.id + 1))
        assert sql_of(statement) == (
            "UPDATE t_authors SET name = other.name FROM t_authors AS other "
            "WHERE other.id = (t_authors.id + $1)"
        )

    def test_a_subquery_condition_still_works(self):
        statement = (Update(Book).set(title=Author.name).from_(Author)
                     .where(Author.id == Book.author_id,
                            Book.id.in_(Query(Tag.book_id))))
        assert "IN (SELECT book_id FROM t_tags)" in sql_of(statement)

    def test_without_from_nothing_changes(self):
        # One source, bare names — every previously rendered UPDATE is unchanged.
        sql, params = (Update(Book).set(title="x").where(Book.id == 1)
                       .to_sql(placeholder="$"))
        assert sql == "UPDATE t_books SET title = $1 WHERE id = $2"
        assert params == ("x", 1)


class TestDeleteUsingRendering:
    def test_basic(self):
        statement = (Delete(Book).using(Author)
                     .where(Author.id == Book.author_id, Author.active == False))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_books USING t_authors "
            "WHERE t_authors.id = t_books.author_id AND t_authors.active = $1"
        )
        assert params == (False,)

    def test_several_using_tables(self):
        statement = (Delete(Book).using(Author, Tag)
                     .where(Author.id == Book.author_id, Tag.book_id == Book.id))
        assert " USING t_authors, t_tags WHERE " in sql_of(statement)

    def test_returning(self):
        assert sql_of(
            Delete(Book).using(Author).where(Author.id == Book.author_id)
                        .returning(Book.id)
        ).endswith("RETURNING t_books.id")

    def test_aliased(self):
        alias = Alias(Author, "a")
        assert " USING t_authors AS a WHERE a.id = " in sql_of(
            Delete(Book).using(alias).where(alias.id == Book.author_id)
        )

    def test_without_using_nothing_changes(self):
        assert sql_of(Delete(Book).where(Book.id == 1)) == (
            "DELETE FROM t_books WHERE id = $1"
        )


class TestValidation:
    def test_update_from_without_where_is_refused(self):
        with pytest.raises(ValueError, match="from_.. but no where"):
            Update(Book).set(title="x").from_(Author).to_sql()

    def test_delete_using_without_where_is_refused(self):
        with pytest.raises(ValueError, match="using.. but no where"):
            Delete(Book).using(Author).to_sql()

    def test_all_rows_does_not_license_a_using_cross_product(self):
        # `USING other` with no condition deletes each target row once per row of
        # `other`, which is not what "delete everything" asked for.
        with pytest.raises(ValueError, match="using.. but no where"):
            Delete(Book).using(Author).all_rows().to_sql()

    def test_from_needs_a_table(self):
        with pytest.raises(TypeError, match="at least one table"):
            Update(Book).set(title="x").from_()

    def test_using_needs_a_table(self):
        with pytest.raises(TypeError, match="at least one table"):
            Delete(Book).using()

    def test_from_rejects_a_column(self):
        with pytest.raises(TypeError, match="takes a model or Alias"):
            Update(Book).set(title="x").from_(Author.id)

    def test_the_same_table_twice_is_refused(self):
        with pytest.raises(ValueError, match="already part of this statement"):
            Update(Book).set(title="x").from_(Book)

    def test_two_sources_with_the_same_qualifier_are_refused(self):
        # An unaliased self-reference would make every column ambiguous, and the
        # generated SQL would silently mean the wrong table.
        class SameTable(metaclass=ModelMeta):
            __tablename__ = "t_books"

            id = Column(int)
            author_id = Column(int)
            title = Column(str)

        with pytest.raises(ValueError, match="both qualify as 't_books'"):
            Update(Book).set(title="x").from_(SameTable)

    def test_a_table_that_is_in_neither_clause_is_refused(self):
        with pytest.raises(ValueError, match="not the table being written to"):
            (Update(Book).set(title=Author.name).from_(Author)
             .where(Author.id == Book.author_id, Tag.label == "x").to_sql())

    def test_the_refusal_lists_the_extra_sources(self):
        with pytest.raises(ValueError, match="or any of Author"):
            (Update(Book).set(title="x").from_(Author)
             .where(Tag.id == 1).to_sql())

    def test_set_target_must_belong_to_the_target_table(self):
        # `from_()` widens what may be *read*, never what may be written.
        with pytest.raises(ValueError, match="has no column 'label'"):
            Update(Book).set(label="x").from_(Tag)


class TestAgainstSqlite:
    """`UPDATE ... FROM` landed in sqlite 3.33 (2020), so this runs for real."""

    @pytest.fixture
    def writable(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "update_from.sqlite3")
        conn.execute("CREATE TABLE t_authors (id INTEGER PRIMARY KEY, name TEXT, "
                     "active INTEGER)")
        conn.execute("CREATE TABLE t_books (id INTEGER PRIMARY KEY, author_id "
                     "INTEGER, title TEXT)")
        conn.executemany("INSERT INTO t_authors VALUES (?, ?, ?)",
                         [(1, "ada", 1), (2, "brian", 0)])
        conn.executemany("INSERT INTO t_books VALUES (?, ?, ?)",
                         [(10, 1, "old"), (11, 2, "old"), (12, 1, "old")])
        conn.commit()
        yield conn
        conn.close()

    def run(self, conn, statement):
        sql, params = statement.to_sql(placeholder="?")
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    def books(self, conn):
        return conn.execute(
            "SELECT id, title FROM t_books ORDER BY id"
        ).fetchall()

    def test_copies_across_the_join(self, writable):
        changed = self.run(writable, Update(Book).set(title=Author.name)
                           .from_(Author).where(Author.id == Book.author_id))
        assert changed == 3
        assert self.books(writable) == [(10, "ada"), (11, "brian"), (12, "ada")]

    def test_the_condition_restricts_which_rows_change(self, writable):
        self.run(writable, Update(Book).set(title=Author.name).from_(Author)
                 .where(Author.id == Book.author_id, Author.active == True))
        # Only brian's book is untouched — his row is inactive.
        assert self.books(writable) == [(10, "ada"), (11, "old"), (12, "ada")]

    def test_expression_values_read_both_tables(self, writable):
        self.run(writable, Update(Book).set(title=Author.name.concat("!"))
                 .from_(Author).where(Author.id == Book.author_id))
        assert self.books(writable) == [(10, "ada!"), (11, "brian!"), (12, "ada!")]

    def test_self_update_through_an_alias(self, writable):
        other = Alias(Author, "other")
        self.run(writable, Update(Author).set(name=other.name).from_(other)
                 .where(other.id == Author.id + 1))
        # Each author takes the next one's name; author 2 has no successor, so
        # the condition matches no row for it and it keeps its own.
        assert writable.execute(
            "SELECT id, name FROM t_authors ORDER BY id"
        ).fetchall() == [(1, "brian"), (2, "brian")]

    def test_a_cte_can_drive_the_update(self, writable):
        prolific = (Query(Book.author_id, count(Book.id).label("n"))
                    .group_by(Book.author_id).cte("prolific"))
        self.run(writable, Update(Book).set(title="many").where(
            Book.author_id.in_(Query(prolific.author_id).where(prolific.n > 1))
        ))
        assert self.books(writable) == [(10, "many"), (11, "old"), (12, "many")]

    def test_delete_via_a_subquery_is_the_sqlite_route(self, writable):
        # sqlite has no DELETE ... USING. The documented alternative is a
        # subquery, and it is worth proving that it actually is equivalent.
        self.run(writable, Delete(Book).where(
            Book.author_id.in_(Query(Author.id).where(Author.active == False))
        ))
        assert self.books(writable) == [(10, "old"), (12, "old")]
