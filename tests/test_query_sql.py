"""SQL generation and query validation. No database needed."""

import pytest

from sqlom import Query
from tests.conftest import Author, AuthorDC, Book, Tag


class TestSingleModel:
    """The single-table shape every published benchmark measures. These assert the
    exact string, because a change here silently changes what was benchmarked."""

    def test_plain_select_is_unqualified(self):
        sql, params = Query(Author).to_sql()
        assert sql == "SELECT id, name, active FROM t_authors"
        assert params == ()

    def test_where_and_limit_numbered_for_asyncpg(self):
        sql, params = Query(Author).where(Author.id > 5).limit(3).to_sql(placeholder="$")
        assert sql == "SELECT id, name, active FROM t_authors WHERE id > $1 LIMIT $2"
        assert params == (5, 3)

    def test_positional_placeholders_repeat(self):
        sql, params = Query(Author).where(Author.id > 5).limit(3).to_sql(placeholder="%s")
        assert sql.endswith("WHERE id > %s LIMIT %s")
        assert params == (5, 3)

    def test_null_predicate_does_not_consume_a_placeholder_number(self):
        query = Query(Author).where(Author.name == None).where(Author.id > 7).limit(2)  # noqa: E711
        sql, params = query.to_sql(placeholder="$")
        assert sql.endswith("WHERE name IS NULL AND id > $1 LIMIT $2")
        assert params == (7, 2)

    def test_order_by_ascending_and_descending(self):
        assert Query(Author).order_by("id").to_sql()[0].endswith("ORDER BY id")
        assert Query(Author).order_by(Author.name, descending=True).to_sql()[0] \
            .endswith("ORDER BY name DESC")

    def test_params_are_an_immutable_tuple(self):
        _, params = Query(Author).where(Author.id > 1).to_sql()
        assert isinstance(params, tuple)
        with pytest.raises(AttributeError):
            params.append(2)

    def test_sql_is_cached_by_identity(self):
        query = Query(Author).where(Author.id > 1)
        assert query.to_sql() is query.to_sql()

    def test_mutation_invalidates_the_cache(self):
        query = Query(Author).where(Author.id > 1)
        first = query.to_sql()
        query.limit(5)
        assert query.to_sql() is not first
        assert "LIMIT" in query.to_sql()[0]

    def test_dataclass_models_work_too(self):
        sql, params = Query(AuthorDC).where(AuthorDC.id > 2).to_sql(placeholder="$")
        assert sql == "SELECT id, name, active FROM t_authors WHERE id > $1"
        assert params == (2,)


class TestValidation:
    def test_query_needs_at_least_one_entity(self):
        with pytest.raises(TypeError, match="at least one"):
            Query()

    def test_query_rejects_a_non_model(self):
        with pytest.raises(TypeError, match="takes models, aliases"):
            Query("t_authors")

    def test_where_rejects_a_non_condition(self):
        with pytest.raises(TypeError, match="takes a predicate"):
            Query(Author).where("id > 5")

    def test_where_rejects_a_predicate_from_an_unjoined_model(self):
        with pytest.raises(ValueError, match="not part of this query"):
            Query(Author).where(Book.title == "x")

    def test_where_rejects_an_unknown_column(self):
        from sqlom import ColumnExpr

        # Reaching a column that the model does not declare can only happen by
        # constructing the expression by hand, but the guard is what keeps a
        # renamed column from rendering as valid-looking SQL.
        ghost = ColumnExpr(Author, "nope", int)
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Query(Author).where(ghost > 1)

    def test_order_by_rejects_an_unknown_column(self):
        with pytest.raises(ValueError, match="has no column 'nope'"):
            Query(Author).order_by("nope")

    def test_order_by_rejects_an_unjoined_model(self):
        with pytest.raises(ValueError, match="not part of this query"):
            Query(Author).order_by(Book.title)

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3"])
    def test_limit_rejects_bad_values(self, bad):
        with pytest.raises((ValueError, TypeError)):
            Query(Author).limit(bad)

    def test_limit_zero_is_allowed(self):
        assert Query(Author).limit(0).to_sql()[1] == (0,)


class TestJoins:
    def test_inner_join_qualifies_every_column(self):
        sql, params = (Query(Author, Book)
                       .join(Book, Book.author_id == Author.id)
                       .to_sql())
        assert sql == (
            "SELECT t_authors.id, t_authors.name, t_authors.active, "
            "t_books.id, t_books.author_id, t_books.title "
            "FROM t_authors JOIN t_books ON t_books.author_id = t_authors.id"
        )
        assert params == ()

    def test_outer_join_keyword(self):
        sql, _ = Query(Author, Book).outer_join(Book, Book.author_id == Author.id).to_sql()
        assert "LEFT OUTER JOIN t_books ON t_books.author_id = t_authors.id" in sql

    def test_on_clause_binds_no_parameters(self):
        query = (Query(Author, Book)
                 .join(Book, Book.author_id == Author.id)
                 .where(Author.id > 1)
                 .limit(5))
        sql, params = query.to_sql(placeholder="$")
        # The join contributes no placeholder, so WHERE still starts at $1.
        assert "WHERE t_authors.id > $1 LIMIT $2" in sql
        assert params == (1, 5)

    def test_where_may_reference_a_joined_model(self):
        sql, params = (Query(Author)
                       .join(Book, Book.author_id == Author.id)
                       .where(Book.title == "compilers")
                       .to_sql(placeholder="$"))
        assert sql.endswith("WHERE t_books.title = $1")
        assert params == ("compilers",)

    def test_order_by_may_reference_a_joined_model(self):
        sql, _ = (Query(Author, Book)
                  .join(Book, Book.author_id == Author.id)
                  .order_by(Book.title)
                  .to_sql())
        assert sql.endswith("ORDER BY t_books.title")

    def test_selecting_a_single_column_alongside_a_model(self):
        sql, _ = (Query(Author, Book.title)
                  .join(Book, Book.author_id == Author.id)
                  .to_sql())
        assert sql == (
            "SELECT t_authors.id, t_authors.name, t_authors.active, t_books.title "
            "FROM t_authors JOIN t_books ON t_books.author_id = t_authors.id"
        )

    def test_three_way_join(self):
        sql, _ = (Query(Author, Book, Tag)
                  .join(Book, Book.author_id == Author.id)
                  .join(Tag, Tag.book_id == Book.id)
                  .to_sql())
        assert "JOIN t_books ON t_books.author_id = t_authors.id" in sql
        assert "JOIN t_tags ON t_tags.book_id = t_books.id" in sql

    def test_joins_render_in_the_order_added(self):
        sql, _ = (Query(Author)
                  .join(Book, Book.author_id == Author.id)
                  .join(Tag, Tag.book_id == Book.id)
                  .to_sql())
        assert sql.index("t_books") < sql.index("t_tags")


class TestJoinValidation:
    def test_on_must_link_the_two_tables(self):
        with pytest.raises(ValueError, match="cross join"):
            Query(Author, Book).join(Book, Book.title == "x")

    def test_on_must_be_a_predicate(self):
        with pytest.raises(TypeError, match="ON predicate"):
            Query(Author, Book).join(Book, "books.author_id = authors.id")

    def test_join_target_must_be_a_source(self):
        with pytest.raises(TypeError, match="takes a model, Alias or Subquery"):
            Query(Author).join("t_books", Book.author_id == Author.id)

    def test_on_must_mention_the_joined_table(self):
        with pytest.raises(ValueError, match="cross join"):
            (Query(Author)
             .join(Book, Book.author_id == Author.id)
             .join(Tag, Book.author_id == Author.id))

    def test_on_cannot_reference_an_unknown_model(self):
        with pytest.raises(ValueError, match="not part of this query"):
            Query(Author).join(Tag, Tag.book_id == Book.id)

    def test_unaliased_self_join_is_refused_and_points_at_the_fix(self):
        with pytest.raises(ValueError, match="alias one side"):
            Query(Author).join(Author, Author.id == Author.id)

    def test_selecting_an_unjoined_model_is_caught_at_render_time(self):
        # Book is selected but never joined: the resulting SQL would be a cross
        # join, so `where` on it is the first thing that must complain.
        query = Query(Author, Book)
        with pytest.raises(ValueError, match="not part of this query"):
            query.where(Book.title == "x")


class TestJsonSql:
    def test_single_model_json(self):
        sql, params = Query(Author).where(Author.id > 1).to_json_sql(dialect="postgres")
        assert sql.startswith("SELECT coalesce(json_agg(json_build_object(")
        assert params == (1,)

    def test_psycopg_dialect_casts_to_text(self):
        sql, _ = Query(Author).to_json_sql(dialect="psycopg")
        assert sql.startswith("SELECT (coalesce(") and ")::text" in sql

    def test_sqlite_dialect_casts_booleans(self):
        sql, _ = Query(Author).to_json_sql(dialect="sqlite")
        assert "json_group_array" in sql
        assert "CASE WHEN active THEN 'true' ELSE 'false' END" in sql

    def test_joins_are_refused_rather_than_shaped_wrongly(self):
        query = Query(Author, Book).join(Book, Book.author_id == Author.id)
        with pytest.raises(NotImplementedError, match="single-model query"):
            query.to_json_sql()

    def test_multi_entity_without_a_join_is_also_refused(self):
        with pytest.raises(NotImplementedError):
            Query(Author, Book).to_json_sql()


class TestHydrationKey:
    """The engines cache compiled hydrators on this key, so its shape matters for
    both correctness (two different queries must not share a hydrator) and speed
    (a plain select must still key on the class itself)."""

    def test_single_model_keys_on_the_class_itself(self):
        assert Query(Author).limit(1)._hydration_key is Author

    def test_multi_entity_keys_on_a_tuple(self):
        key = Query(Author, Book).join(Book, Book.author_id == Author.id)._hydration_key
        assert isinstance(key, tuple) and len(key) == 2

    def test_inner_and_outer_join_do_not_share_a_key(self):
        inner = Query(Author, Book).join(Book, Book.author_id == Author.id)
        outer = Query(Author, Book).outer_join(Book, Book.author_id == Author.id)
        assert inner._hydration_key != outer._hydration_key

    def test_selecting_a_column_differs_from_selecting_the_model(self):
        a = Query(Author, Book).join(Book, Book.author_id == Author.id)
        b = Query(Author, Book.title).join(Book, Book.author_id == Author.id)
        assert a._hydration_key != b._hydration_key

    def test_a_filtering_join_reuses_the_plain_single_model_hydrator(self):
        # A join changes the SQL, not the row shape: Book is joined for filtering
        # but never selected, so rows are still Author-shaped and must hydrate to
        # Author instances, not 1-tuples.
        query = Query(Author).join(Book, Book.author_id == Author.id)
        assert query.is_multi_entity is False
        assert query._hydration_key is Author
