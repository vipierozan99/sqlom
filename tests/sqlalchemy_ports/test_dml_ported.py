"""Ported from SQLAlchemy's test/sql/test_insert.py, test_update.py,
test_delete.py, test_returning.py, test_values.py — extending
tests/test_dml.py, tests/test_upsert.py, tests/test_update_from.py.
Adapted to sqlom.

sqlom has no `Table`/`MetaData`/column-collection object, no `INSERT ...
SELECT` (and therefore no data-modifying CTE built on it), no server-side or
DDL-level column defaults, no `bindparam()`/custom-key-thing machinery, and no
per-dialect RETURNING quirks beyond what sqlite and Postgres both support —
`to_sql()` renders one dialect-agnostic string. So most of SQLAlchemy's DML
test surface does not translate: this file mines the remaining cases that do,
and each class below documents *which* SQLAlchemy test(s) it corresponds to.

Skipped entirely (no sqlom equivalent):
  * `INSERT ... SELECT`, and the data-modifying CTE built on it
    (`InsertImplicitReturningTest.test_insert_select*`,
    `EmptyTest`/`MultirowTest` sequence and server_default cases) — sqlom's
    `Insert` only takes `values()`, never a `Query` to select from.
  * Server-side/DDL-level defaults, `Sequence`, `onupdate=`, inline vs.
    non-inline default compilation, `return_defaults()` — sqlom has no
    schema/DDL layer, so there is nothing to prefetch or inline.
  * `bindparam()` and the "bind param named after a column" overlap rules
    (`test_binds_that_match_columns`, `test_bindparam_name_no_consume_error`)
    — sqlom binds plain Python values, not named `bindparam` placeholders.
  * `Values` the standalone derived-table/VALUES-as-FROM-clause construct
    (all of `test_values.py`) — an entirely different feature from
    `Insert.values()`; sqlom has no derived-table VALUES clause at all.
  * `.ordered_values()`, custom `__clause_element__` "key things",
    `.prefix_with()`, positional (non-dict) row values, dialect-specific
    paramstyle/positional-binding tests — no equivalents; sqlom's `values()`
    only takes column-name dicts or keywords, and `to_sql()` is one
    dialect-agnostic renderer, not a per-dialect compiler.
  * MySQL-specific and dialect-comparison tests, and anything keyed to a
    specific driver's paramstyle.

One behavioural quirk noticed while porting (not fixed — sqlom's generative
builders mutate and accumulate rather than merge, unlike SQLAlchemy's
immutable statements with dict-merge `.values()` semantics):
  * Calling `.set()` twice for the *same* column does not merge — it appends
    a second `col = ...` assignment, so `UPDATE t SET name = $1, name = $2`
    is rendered verbatim (Postgres rejects that at execution: "multiple
    assignments to same column"). See
    `TestUpdateAssignmentForms.test_repeated_set_calls_on_the_same_column_do_not_merge`.

One gap noticed while porting has since been fixed centrally (not in this
file): `Update.set(col=some_query.scalar_subquery())` — a scalar subquery as
an assignment *value*, mirroring SQLAlchemy's `test_correlated_update_two`
through `_five` — now renders `col = (SELECT ...)` correctly, because
`scalar_subquery()` returns a real `ScalarSubquery` `Expression` rather than
the bare `Query`. See `TestUpdateAssignmentForms::test_scalar_subquery_as_an_assignment_value`.
"""

import pytest

from sqlom import (
    Alias,
    DatabaseEngine,
    Delete,
    Insert,
    Query,
    Update,
    max_rows_per_statement,
)
from tests.conftest import Author, Book, Tag


def sql_of(statement, placeholder="$"):
    return statement.to_sql(placeholder=placeholder)[0]


class TestReturningAccumulates:
    """`returning()` may be called more than once; each call adds columns
    rather than replacing them — mirrors
    `ReturnCombinationTests.test_return_combinations`, which chains
    `.returning(t.c.x)` then `.returning(t.c.y)` on INSERT/UPDATE/DELETE and
    expects both to appear."""

    # Ported from test/sql/test_returning.py::ReturnCombinationTests.test_return_combinations (SQLAlchemy 2.0.51)
    def test_insert_returning_called_twice_accumulates_columns(self):
        statement = Insert(Author).values(id=1).returning(Author.id)
        statement.returning(Author.name)
        sql, params = statement.to_sql(placeholder="$")
        assert sql == "INSERT INTO t_authors (id) VALUES ($1) RETURNING id, name"
        assert params == (1,)

    # Ported from test/sql/test_returning.py::ReturnCombinationTests.test_return_combinations (SQLAlchemy 2.0.51)
    def test_update_returning_called_twice_accumulates_columns(self):
        statement = (Update(Author).set(name="z").where(Author.id == 1)
                     .returning(Author.id))
        statement.returning(Author.name)
        assert sql_of(statement).endswith("RETURNING id, name")

    # Ported from test/sql/test_returning.py::ReturnCombinationTests.test_return_combinations (SQLAlchemy 2.0.51)
    def test_delete_returning_called_twice_accumulates_columns(self):
        statement = Delete(Author).where(Author.id == 1).returning(Author.id)
        statement.returning(Author.name)
        assert sql_of(statement).endswith("RETURNING id, name")

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_on_conflict_do_nothing_returning_called_twice_accumulates_columns(self):
        statement = (Insert(Author).values(id=1)
                     .on_conflict_do_nothing(Author.id)
                     .returning(Author.id))
        statement.returning(Author.name)
        assert sql_of(statement).endswith(
            "ON CONFLICT (id) DO NOTHING RETURNING id, name"
        )

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_mixing_a_column_and_the_whole_model_across_two_returning_calls(self):
        # Not forbidden: the "only one whole-model call" rule only fires
        # within output_columns()/hydration_spec() bookkeeping, not at
        # returning() time. The column named by both calls is simply
        # repeated in the RETURNING list — valid SQL, if redundant.
        statement = Insert(Author).values(id=1).returning(Author.id)
        statement.returning(Author)
        sql, _ = statement.to_sql(placeholder="$")
        assert sql.endswith("RETURNING id, id, name, active")
        assert statement.is_multi_entity is True


class TestReturningExpressions:
    """More RETURNING permutations: expressions (not just bare columns), an
    aliased target, and an expression reaching the extra `FROM`/`USING`
    source — extends `test_dml.py`'s single-expression case and
    `test_update_from.py`'s qualified-but-plain-column RETURNING case.
    Mirrors the arithmetic-expression shapes in
    `ReturnCombinationTests.test_dml_returning_c_labels_four`."""

    # Ported from test/sql/test_returning.py::ReturnCombinationTests.test_dml_returning_c_labels_four (SQLAlchemy 2.0.51)
    def test_delete_returning_several_arithmetic_expressions(self):
        statement = (Delete(Author).where(Author.id == 1)
                     .returning(Author.id, Author.id * -1, Author.id + 10))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_authors WHERE id = $1 "
            "RETURNING id, (id * $2), (id + $3)"
        )
        assert params == (1, -1, 10)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_delete_returning_expression_over_the_using_source(self):
        statement = (Delete(Book).using(Author)
                     .where(Author.id == Book.author_id)
                     .returning(Book.id, Author.name.concat("!")))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_books USING t_authors "
            "WHERE t_authors.id = t_books.author_id "
            "RETURNING t_books.id, (t_authors.name || $1)"
        )
        assert params == ("!",)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_insert_returning_an_alias_column_and_an_expression(self):
        alias = Alias(Author, "a")
        statement = (Insert(alias).values(id=1, name="x")
                     .returning(alias.id, alias.name.concat("!")))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "INSERT INTO t_authors AS a (id, name) VALUES ($1, $2) "
            "RETURNING id, (name || $3)"
        )
        assert params == (1, "x", "!")

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_update_returning_expression_over_the_from_source(self):
        statement = (Update(Book).set(title=Author.name).from_(Author)
                     .where(Author.id == Book.author_id)
                     .returning(Book.id, Author.name.concat("!")))
        assert sql_of(statement).endswith(
            "RETURNING t_books.id, (t_authors.name || $1)"
        )


class TestInsertValuesEdgeCases:
    """More multi-row VALUES rendering and parameter-ordering cases, plus the
    row ceiling with the model's default width — extends `MultirowTest`
    (`test_multi_multi`, `test_named`) and `EmptyTest`
    (`test_insert_with_empty_collection_values`)."""

    # Ported from test/sql/test_insert.py::EmptyTest.test_insert_with_empty_collection_values (SQLAlchemy 2.0.51)
    def test_empty_dict_row_renders_an_empty_column_list(self):
        # `values()` with no keywords raises (test_empty_keywords in
        # test_dml.py); `values({})` — an explicit empty *dict* — does not,
        # and renders the same empty-column-list shape SQLAlchemy uses for
        # `table.insert().values({})` with `supports_default_values=False`.
        sql, params = Insert(Author).values({}).to_sql(placeholder="$")
        assert sql == "INSERT INTO t_authors () VALUES ()"
        assert params == ()

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_default_row_ceiling_uses_the_full_model_width(self):
        # test_parameter_ceiling in test_dml.py always passes an explicit
        # columns list; calling max_rows_per_statement(model) with none
        # falls back to every column the model declares.
        assert max_rows_per_statement(Book) == 32766 // len(Book.__columns__)

    # Ported from test/sql/test_insert.py::MultirowTest.test_mix_single_and_multi_single_first (SQLAlchemy 2.0.51)
    def test_single_row_then_bulk_rows_accumulate_into_one_statement(self):
        # SQLAlchemy's MultirowTest forbids mixing single- and multi-row
        # .values() calls (test_mix_single_and_multi_single_first raises
        # InvalidRequestError). sqlom has no such rule: values() just keeps
        # extending self._rows as long as the column set matches, single or
        # bulk, in either order.
        statement = Insert(Author).values(id=1, name="d1").values(
            [{"id": 2, "name": "d2"}, {"id": 3, "name": "d3"}]
        )
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "INSERT INTO t_authors (id, name) VALUES "
            "($1, $2), ($3, $4), ($5, $6)"
        )
        assert params == (1, "d1", 2, "d2", 3, "d3")
        assert statement.row_count == 3

    # Ported from test/sql/test_insert.py::MultirowTest.test_mix_single_and_multi_multi_first (SQLAlchemy 2.0.51)
    def test_bulk_rows_then_single_row_accumulate_into_one_statement(self):
        # The reverse order (test_mix_single_and_multi_multi_first in
        # SQLAlchemy) is likewise just accumulation here.
        statement = Insert(Author).values(
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        ).values(id=3, name="c")
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "INSERT INTO t_authors (id, name) VALUES "
            "($1, $2), ($3, $4), ($5, $6)"
        )
        assert params == (1, "a", 2, "b", 3, "c")


class TestUpdateAssignmentForms:
    """`col = col op val`-style assignments mixed with plain literals in the
    same SET clause, and the effect of calling `.set()`/`.values()` more than
    once — mirrors `UpdateTest.test_update_6`/`test_update_7` (an expression
    assignment alongside a literal one in the same statement, params in
    declaration order) and `test_update_10` (accumulating `.values()` calls)."""

    # Ported from test/sql/test_update.py::UpdateTest.test_update_6 (SQLAlchemy 2.0.51)
    def test_expression_and_literal_assignments_interleave_params_in_order(self):
        statement = (Update(Book).set(title=Book.title.concat("!"), author_id=5)
                     .where(Book.id == 1))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "UPDATE t_books SET title = (title || $1), author_id = $2 "
            "WHERE id = $3"
        )
        assert params == ("!", 5, 1)

    # Ported from test/sql/test_update.py::UpdateTest.test_update_7 (SQLAlchemy 2.0.51)
    def test_literal_then_expression_assignment_also_interleaves_params(self):
        # Same shape, opposite declaration order, via two separate .set()
        # calls rather than one — params still follow assignment order.
        statement = (Update(Book).set(title="x").set(author_id=Book.author_id + 1)
                     .where(Book.id == 1))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "UPDATE t_books SET title = $1, author_id = (author_id + $2) "
            "WHERE id = $3"
        )
        assert params == ("x", 1, 1)

    # Ported from test/sql/test_update.py::UpdateTest.test_update_10 (SQLAlchemy 2.0.51)
    def test_repeated_set_calls_on_the_same_column_do_not_merge(self):
        # Documents a real behavioural difference from SQLAlchemy: there,
        # re-`.values()`-ing the same column overwrites it (test_update_10);
        # here, set() only ever appends, so the same column can appear
        # twice in one SET clause. Postgres rejects this at execution time
        # ("multiple assignments to same column name") — sqlom does not
        # catch it at build time. See this module's docstring.
        statement = Update(Author).set(name="a").set(name="b")
        sql, params = statement.to_sql(placeholder="$")
        assert sql == "UPDATE t_authors SET name = $1, name = $2"
        assert params == ("a", "b")

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_values_alias_and_set_can_be_mixed_in_the_same_chain(self):
        # Update.values is literally `set` (see dml.py), so a chain that
        # calls both names still accumulates into one assignment list.
        statement = (Update(Book).values(title="x").set(author_id=5)
                     .where(Book.id == 1))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == "UPDATE t_books SET title = $1, author_id = $2 WHERE id = $3"
        assert params == ("x", 5, 1)

    # Ported from test/sql/test_update.py::UpdateTest.test_correlated_update_two (SQLAlchemy 2.0.51)
    def test_scalar_subquery_as_an_assignment_value(self):
        # Fixed: scalar_subquery() now returns a real ScalarSubquery
        # Expression, so it renders as `col = (SELECT ...)` — mirroring
        # SQLAlchemy's test_correlated_update_two through _five — instead
        # of silently becoming a bound parameter.
        latest_title = (Query(Book.title).correlate(Author)
                         .where(Book.author_id == Author.id)
                         .order_by(Book.id.desc()).limit(1).scalar_subquery())
        statement = Update(Author).set(name=latest_title).where(Author.id == 1)
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "UPDATE t_authors SET name = (SELECT t_books.title FROM t_books "
            "WHERE t_books.author_id = t_authors.id "
            "ORDER BY t_books.id DESC LIMIT $1) WHERE id = $2"
        )
        assert params == (1, 1)


class TestSubqueryConditions:
    """A scalar subquery used as a comparison value in WHERE, correlated and
    not — mirrors `DeleteTest.test_non_correlated_select` /
    `test_correlated_select` and `UpdateTest.test_correlated_update_four` /
    `_five`. `scalar_subquery()` on a `Query` just returns the query itself
    (see `query.py`), so any comparison operator accepts it directly."""

    # Ported from test/sql/test_delete.py::DeleteTest.test_non_correlated_select (SQLAlchemy 2.0.51)
    def test_delete_where_a_non_correlated_scalar_subquery(self):
        subquery = Query(Book.title).where(Book.id == 7).scalar_subquery()
        statement = Delete(Author).where(Author.name == subquery)
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_authors WHERE name = "
            "(SELECT title FROM t_books WHERE id = $1)"
        )
        assert params == (7,)

    # Ported from test/sql/test_delete.py::DeleteTest.test_correlated_select (SQLAlchemy 2.0.51)
    def test_delete_where_a_correlated_scalar_subquery(self):
        subquery = (Query(Book.title).correlate(Author)
                    .where(Book.author_id == Author.id).scalar_subquery())
        statement = Delete(Author).where(Author.name == subquery)
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_authors WHERE name = "
            "(SELECT t_books.title FROM t_books "
            "WHERE t_books.author_id = t_authors.id)"
        )
        assert params == ()

    # Ported from test/sql/test_update.py::UpdateTest.test_correlated_update_five (SQLAlchemy 2.0.51)
    def test_update_where_a_correlated_scalar_subquery(self):
        subquery = (Query(Book.title).correlate(Author)
                    .where(Book.author_id == Author.id).scalar_subquery())
        statement = Update(Author).set(active=False).where(Author.name == subquery)
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "UPDATE t_authors SET active = $1 WHERE name = "
            "(SELECT t_books.title FROM t_books "
            "WHERE t_books.author_id = t_authors.id)"
        )
        assert params == (False,)


class TestDeleteUsingVariations:
    """More `USING`/target-alias combinations than
    `test_update_from.py::TestDeleteUsingRendering` covers: an alias mixed
    with a plain table in the same `using()`, an aliased delete *target*
    with `using()`, RETURNING with several using tables, and a `USING`
    condition combined with an uncorrelated IN-subquery — mirrors
    SQLAlchemy's `DeleteFromRoundTripTest.test_exec_alias_plus_table` and
    `test_exec_two_table_plus_alias` (aliasing one side of a multi-table
    delete) and `test_update_from.py::test_a_subquery_condition_still_works`
    (the UPDATE analogue of the last case)."""

    # Ported from test/sql/test_delete.py::DeleteFromRoundTripTest.test_exec_two_table_plus_alias (SQLAlchemy 2.0.51)
    def test_using_mixes_an_alias_and_a_plain_table(self):
        author_alias = Alias(Author, "a")
        statement = (Delete(Book).using(author_alias, Tag)
                     .where(author_alias.id == Book.author_id,
                            Tag.book_id == Book.id))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_books USING t_authors AS a, t_tags "
            "WHERE a.id = t_books.author_id AND t_tags.book_id = t_books.id"
        )
        assert params == ()

    # Ported from test/sql/test_delete.py::DeleteFromRoundTripTest.test_exec_alias_plus_table (SQLAlchemy 2.0.51)
    def test_alias_as_the_delete_target_with_using(self):
        book_alias = Alias(Book, "b")
        statement = (Delete(book_alias).using(Author)
                     .where(Author.id == book_alias.author_id,
                            Author.active == True))  # noqa: E712
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_books AS b USING t_authors "
            "WHERE t_authors.id = b.author_id AND t_authors.active = $1"
        )
        assert params == (True,)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_returning_with_several_using_tables(self):
        statement = (Delete(Book).using(Author, Tag)
                     .where(Author.id == Book.author_id, Tag.book_id == Book.id)
                     .returning(Book.id))
        sql, _ = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_books USING t_authors, t_tags "
            "WHERE t_authors.id = t_books.author_id "
            "AND t_tags.book_id = t_books.id RETURNING t_books.id"
        )

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_using_condition_combined_with_an_uncorrelated_subquery(self):
        statement = (Delete(Book).using(Author).where(
            Author.id == Book.author_id,
            Book.id.in_(Query(Tag.book_id)),
        ))
        sql, params = statement.to_sql(placeholder="$")
        assert sql == (
            "DELETE FROM t_books USING t_authors "
            "WHERE t_authors.id = t_books.author_id "
            "AND t_books.id IN (SELECT book_id FROM t_tags)"
        )
        assert params == ()


class TestEngineReturningMismatch:
    """README §12: `execute()` on a statement with RETURNING, or
    `fetch_all()` on one without, is a `ValueError` rather than a silently
    empty/incomplete result. Both checks run before the engine ever touches
    its connection pool — `DatabaseEngine.__init__` does not connect, and
    `connect()` is never called here — so this is exercised without any
    live database: the *matching* combinations fall through to a distinct
    RuntimeError ("not connected"), proving the RETURNING check specifically
    is what stops the mismatched ones."""

    @pytest.fixture
    def engine(self):
        # Never connected: self.pool stays None, so this needs no server.
        return DatabaseEngine("postgresql://fake:fake@localhost/fake")

    # sqlom-original test (no SQLAlchemy equivalent)
    async def test_execute_rejects_a_statement_that_has_returning(self, engine):
        statement = Insert(Author).values(id=1).returning(Author.id)
        with pytest.raises(ValueError, match="use fetch_all"):
            await engine.execute(statement)

    # sqlom-original test (no SQLAlchemy equivalent)
    async def test_fetch_all_rejects_a_statement_that_has_no_returning(self, engine):
        statement = Insert(Author).values(id=1)
        with pytest.raises(ValueError, match="has no returning"):
            await engine.fetch_all(statement)

    # sqlom-original test (no SQLAlchemy equivalent)
    async def test_execute_accepts_a_statement_without_returning_and_reaches_the_pool(
        self, engine
    ):
        statement = Insert(Author).values(id=1)
        # Past the RETURNING check, the next thing execute() does is ask
        # for the pool — which does not exist because connect() was never
        # called. A RuntimeError here (not a ValueError) shows the mismatch
        # check let this combination through.
        with pytest.raises(RuntimeError, match="not connected"):
            await engine.execute(statement)

    # sqlom-original test (no SQLAlchemy equivalent)
    async def test_fetch_all_accepts_a_statement_with_returning_and_reaches_the_pool(
        self, engine
    ):
        statement = Insert(Author).values(id=1).returning(Author.id)
        with pytest.raises(RuntimeError, match="not connected"):
            await engine.fetch_all(statement)
