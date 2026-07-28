"""INSERT, UPDATE, DELETE and RETURNING — SQL generation and validation.

Execution against a real server is in test_dml_pg.py; these need no database.
"""

import pytest

from sqlom import (
    MAX_PARAMETERS,
    Alias,
    Delete,
    Insert,
    Query,
    Update,
    max_rows_per_statement,
)
from tests.conftest import Author, Book


class TestStatementBase:
    """_Statement is the shared base Insert/Update/Delete build on; each of
    them overrides _render(), so the base implementation itself needs its
    own direct test."""

    def test_render_is_not_implemented(self):
        from sqlom.dml import _Statement

        with pytest.raises(NotImplementedError):
            _Statement(Author)._render()


class TestInsert:
    def test_single_row_from_keywords(self):
        sql, params = Insert(Author).values(id=1, name="ada", active=True).to_sql(
            placeholder="$")
        assert sql == "INSERT INTO t_authors (id, name, active) VALUES ($1, $2, $3)"
        assert params == (1, "ada", True)

    def test_column_order_follows_the_dict_not_the_model(self):
        sql, params = Insert(Author).values(name="ada", id=1).to_sql(placeholder="$")
        assert sql.startswith("INSERT INTO t_authors (name, id)")
        assert params == ("ada", 1)

    def test_bulk_rows_render_one_multi_values_statement(self):
        # One round trip, and unlike executemany on asyncpg it supports RETURNING.
        sql, params = Insert(Author).values(
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        ).to_sql(placeholder="$")
        assert sql == (
            "INSERT INTO t_authors (id, name) VALUES ($1, $2), ($3, $4)"
        )
        assert params == (1, "a", 2, "b")

    def test_a_single_dict_is_one_row(self):
        assert Insert(Author).values({"id": 1}).row_count == 1

    def test_values_can_be_called_repeatedly_to_accumulate(self):
        statement = Insert(Author).values(id=1).values(id=2)
        assert statement.row_count == 2
        assert statement.to_sql(placeholder="$")[1] == (1, 2)

    def test_returning_a_column(self):
        sql, _ = Insert(Author).values(name="a").returning(Author.id).to_sql()
        assert sql.endswith("RETURNING id")

    def test_returning_the_whole_model(self):
        sql, _ = Insert(Author).values(name="a").returning(Author).to_sql()
        assert sql.endswith("RETURNING id, name, active")

    def test_returning_an_expression(self):
        sql, _ = (Insert(Author).values(name="a")
                  .returning(Author.id, Author.name.concat("!")).to_sql(placeholder="$"))
        assert sql.endswith("RETURNING id, (name || $2)")

    def test_writing_to_an_alias(self):
        alias = Alias(Author, "a")
        sql, _ = Insert(alias).values(id=1).to_sql()
        assert sql.startswith("INSERT INTO t_authors AS a (id)")


class TestInsertValidation:
    def test_no_rows(self):
        with pytest.raises(ValueError, match="no rows"):
            Insert(Author).to_sql()

    def test_unknown_column(self):
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Insert(Author).values(nope=1)

    def test_empty_keywords(self):
        with pytest.raises(TypeError, match="at least one column"):
            Insert(Author).values()

    def test_empty_sequence(self):
        with pytest.raises(ValueError, match="empty sequence"):
            Insert(Author).values([])

    def test_rows_must_be_dicts(self):
        with pytest.raises(TypeError, match="sequence of dicts"):
            Insert(Author).values([(1, "a")])

    def test_rows_must_all_set_the_same_columns(self):
        # A missing key would shift values into the wrong columns.
        with pytest.raises(ValueError, match="same columns"):
            Insert(Author).values([{"id": 1, "name": "a"}, {"id": 2}])

    def test_accumulating_rows_with_different_columns_is_refused(self):
        with pytest.raises(ValueError, match="already called with columns"):
            Insert(Author).values(id=1).values(name="a")

    def test_keywords_and_a_sequence_together_are_refused(self):
        with pytest.raises(TypeError, match="either a sequence"):
            Insert(Author).values([{"id": 1}], name="a")

    def test_target_must_be_a_model(self):
        with pytest.raises(TypeError, match="model or Alias"):
            Insert("t_authors")

    def test_parameter_ceiling(self):
        # Postgres caps a statement at 65535 parameters and sqlite lower; exceeding
        # it is a server error, so the batch is refused where it is built.
        ceiling = max_rows_per_statement(Author, ["id", "name"])
        assert ceiling == MAX_PARAMETERS // 2
        rows = [{"id": n, "name": "x"} for n in range(ceiling + 1)]
        with pytest.raises(ValueError, match="exceeds the .* limit"):
            Insert(Author).values(rows)

    def test_exactly_at_the_ceiling_is_allowed(self):
        ceiling = max_rows_per_statement(Author, ["id"])
        statement = Insert(Author).values([{"id": n} for n in range(ceiling)])
        assert statement.row_count == ceiling

    def test_repr_reports_the_row_count(self):
        assert repr(Insert(Author).values([{"id": 1}, {"id": 2}])) == "<Insert Author x2>"

    def test_render_with_no_placeholder_generator_falls_back_to_question_marks(self):
        sql, params = Insert(Author).values(id=1)._render()
        assert sql == "INSERT INTO t_authors (id) VALUES (?)"
        assert params == [1]


class TestUpdate:
    def test_set_and_where(self):
        sql, params = (Update(Author).set(name="z").where(Author.id == 1)
                       .to_sql(placeholder="$"))
        assert sql == "UPDATE t_authors SET name = $1 WHERE id = $2"
        assert params == ("z", 1)

    def test_several_assignments(self):
        sql, params = Update(Author).set(name="z", active=False).to_sql(placeholder="$")
        assert sql == "UPDATE t_authors SET name = $1, active = $2"
        assert params == ("z", False)

    def test_assignment_from_an_expression_is_one_statement(self):
        # A read-modify-write without a round trip to read first.
        sql, params = (Update(Book).set(author_id=Book.author_id + 1)
                       .where(Book.id == 1).to_sql(placeholder="$"))
        assert sql == (
            "UPDATE t_books SET author_id = (author_id + $1) WHERE id = $2"
        )
        assert params == (1, 1)

    def test_returning(self):
        sql, _ = (Update(Author).set(name="z").where(Author.id == 1)
                  .returning(Author.id, Author.name).to_sql())
        assert sql.endswith("RETURNING id, name")

    def test_several_where_clauses_and(self):
        sql, _ = (Update(Author).set(name="z")
                  .where(Author.id > 1).where(Author.active == True)  # noqa: E712
                  .to_sql(placeholder="$"))
        assert sql.endswith("WHERE id > $2 AND active = $3")

    def test_no_assignments(self):
        with pytest.raises(ValueError, match="no assignments"):
            Update(Author).to_sql()

    def test_unknown_column_in_set(self):
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Update(Author).set(nope=1)

    def test_where_rejects_another_table(self):
        # Checked when the statement renders, not in where(): from_() may still be
        # coming, and requiring a particular builder order would be worse.
        with pytest.raises(ValueError, match="not the table being written to"):
            Update(Author).set(name="z").where(Book.id == 1).to_sql()

    def test_set_expression_rejects_another_table(self):
        with pytest.raises(ValueError, match="not the table being written to"):
            Update(Author).set(id=Book.id + 1).to_sql()

    def test_where_rejects_a_non_predicate(self):
        with pytest.raises(TypeError, match="takes a predicate"):
            Update(Author).set(name="z").where("id = 1")

    def test_repr(self):
        assert repr(Update(Author).set(name="z")) == "<Update Author>"

    def test_render_with_no_placeholder_generator_falls_back_to_question_marks(self):
        sql, params = Update(Author).set(name="z")._render()
        assert sql == "UPDATE t_authors SET name = ?"
        assert params == ["z"]


class TestDelete:
    def test_with_where(self):
        sql, params = Delete(Author).where(Author.id == 1).to_sql(placeholder="$")
        assert sql == "DELETE FROM t_authors WHERE id = $1"
        assert params == (1,)

    def test_unconditional_delete_must_be_explicit(self):
        # A forgotten where() that empties a table is not a mistake worth making
        # easy to write.
        with pytest.raises(ValueError, match="would empty the table"):
            Delete(Author).to_sql()

    def test_all_rows_opts_in(self):
        assert Delete(Author).all_rows().to_sql()[0] == "DELETE FROM t_authors"

    def test_returning(self):
        sql, _ = Delete(Author).where(Author.id == 1).returning(Author).to_sql()
        assert sql.endswith("RETURNING id, name, active")

    def test_where_rejects_another_table(self):
        with pytest.raises(ValueError, match="not the table being written to"):
            Delete(Author).where(Book.id == 1).to_sql()

    def test_where_rejects_a_non_predicate(self):
        with pytest.raises(TypeError, match="takes a predicate"):
            Delete(Author).where("id = 1")

    def test_repr(self):
        assert repr(Delete(Author).all_rows()) == "<Delete Author>"

    def test_render_with_no_placeholder_generator_falls_back_to_question_marks(self):
        # all_rows() alone never calls nxt() (no WHERE, no RETURNING): use a
        # condition instead, so the fallback placeholder is actually reached.
        sql, params = Delete(Author).where(Author.id == 1)._render()
        assert sql == "DELETE FROM t_authors WHERE id = ?"
        assert params == [1]


class TestReturningValidation:
    def test_returning_another_model_is_refused(self):
        with pytest.raises(ValueError, match="can only reference"):
            Insert(Author).values(id=1).returning(Book)

    def test_returning_another_table_column_is_refused(self):
        with pytest.raises(ValueError, match="not the table being written to"):
            Insert(Author).values(id=1).returning(Book.id).to_sql()

    def test_returning_a_non_entity_is_refused(self):
        with pytest.raises(TypeError, match="takes the model or its columns"):
            Insert(Author).values(id=1).returning("id")

    def test_returning_an_unknown_column_of_the_right_table_is_refused(self):
        from sqlom import ColumnExpr

        # Passes the "is this table being written to" check (it is Author),
        # then fails the deeper "does that table actually have this column"
        # check — only reachable by hand-building the reference, as in the
        # other has-no-column tests.
        ghost = ColumnExpr(Author, "nope", int)
        with pytest.raises(ValueError, match="has no column 'nope'"):
            (Update(Author).set(name="z").where(Author.id == 1)
             .returning(ghost).to_sql())

    def test_returns_rows_reflects_whether_returning_was_called(self):
        assert Insert(Author).values(id=1).returns_rows is False
        assert Insert(Author).values(id=1).returning(Author.id).returns_rows is True


class TestHydrationInterface:
    """A statement with RETURNING must present what the engines read off a Query."""

    def test_returning_a_model_hydrates_as_instances(self):
        statement = Insert(Author).values(id=1).returning(Author)
        assert statement.is_multi_entity is False
        assert statement._hydration_key is Author

    def test_returning_a_model_hydration_spec_and_output_columns(self):
        # test_returning_columns_hydrates_as_tuples and test_output_columns
        # below only exercise the "column" kind; a whole-model RETURNING
        # takes a different branch through both methods.
        statement = Insert(Author).values(id=1).returning(Author)
        assert statement.hydration_spec() == [("model", Author, False)]
        assert statement.output_columns() == [
            ("id", int), ("name", str), ("active", bool),
        ]

    def test_returning_columns_hydrates_as_tuples(self):
        statement = Insert(Author).values(id=1).returning(Author.id, Author.name)
        assert statement.is_multi_entity is True
        assert statement.hydration_spec() == [("column", int), ("column", str)]

    def test_output_columns(self):
        statement = Insert(Author).values(id=1).returning(Author.id)
        assert statement.output_columns() == [("id", int)]

    def test_sql_is_cached_and_invalidated(self):
        statement = Insert(Author).values(id=1)
        first = statement.to_sql()
        assert statement.to_sql() is first
        statement.returning(Author.id)
        assert statement.to_sql() is not first
