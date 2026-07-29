"""UNION, INTERSECT and EXCEPT."""

import pytest

from rowform import CompoundSelect, Query
from tests.conftest import Author, Book


class TestRendering:
    def test_union(self):
        sql, params = (Query(Author).where(Author.id > 2)
                       .union(Query(Author).where(Author.id < 2))
                       .to_sql(placeholder="$"))
        assert sql == (
            "SELECT id, name, active FROM t_authors WHERE id > $1 "
            "UNION "
            "SELECT id, name, active FROM t_authors WHERE id < $2"
        )
        assert params == (2, 2)

    @pytest.mark.parametrize("method,keyword", [
        ("union", "UNION"),
        ("union_all", "UNION ALL"),
        ("intersect", "INTERSECT"),
        ("intersect_all", "INTERSECT ALL"),
        ("except_", "EXCEPT"),
        ("except_all", "EXCEPT ALL"),
    ])
    def test_every_operator(self, method, keyword):
        compound = getattr(Query(Author), method)(Query(Author))
        assert f" {keyword} " in compound.to_sql()[0]

    def test_chaining_the_same_operator_flattens(self):
        compound = (Query(Author).union_all(Query(Author))
                    .union_all(Query(Author)))
        assert compound.to_sql()[0].count("UNION ALL") == 2
        assert len(compound.operands) == 3

    def test_chaining_a_different_operator_nests(self):
        # UNION and EXCEPT do not associate the way flattening would imply, so a
        # change of operator has to nest rather than extend.
        compound = Query(Author).union(Query(Author)).except_(Query(Author))
        assert len(compound.operands) == 2
        assert isinstance(compound.operands[0], CompoundSelect)

    def test_parameters_number_across_operands_in_order(self):
        import re

        sql, params = (Query(Author).where(Author.name == "a")
                       .union(Query(Author).where(Author.name == "b"))
                       .limit(5)
                       .to_sql(placeholder="$"))
        assert params == ("a", "b", 5)
        assert re.findall(r"\$\d+", sql) == ["$1", "$2", "$3"]

    def test_order_by_limit_offset_apply_to_the_whole_compound(self):
        sql, params = (Query(Author).union(Query(Author))
                       .order_by("name", descending=True)
                       .limit(10).offset(5)
                       .to_sql(placeholder="$"))
        assert sql.endswith("ORDER BY name DESC LIMIT $1 OFFSET $2")
        assert params == (10, 5)

    def test_order_by_accepts_a_column_of_the_first_operand(self):
        sql, _ = Query(Author).union(Query(Author)).order_by(Author.name).to_sql()
        # Unqualified: a compound has no single table to qualify against.
        assert sql.endswith("ORDER BY name")

    def test_sql_is_cached(self):
        compound = Query(Author).union(Query(Author))
        assert compound.to_sql() is compound.to_sql()

    def test_mutation_invalidates_the_cache(self):
        compound = Query(Author).union(Query(Author))
        first = compound.to_sql()
        compound.limit(3)
        assert compound.to_sql() is not first

    def test_chaining_intersect_on_a_compound(self):
        # test_every_operator above calls Query.intersect(); this is
        # CompoundSelect.intersect(), reached only by chaining off a compound.
        compound = Query(Author).intersect(Query(Author)).intersect(Query(Author))
        assert compound.to_sql()[0].count("INTERSECT") == 2
        assert len(compound.operands) == 3

    def test_repr(self):
        compound = Query(Author).union(Query(Author))
        assert repr(compound) == "<CompoundSelect UNION x2>"

    def test_render_with_no_placeholder_generator_falls_back_to_question_marks(self):
        sql, params = Query(Author).union(Query(Author)).limit(2)._render()
        assert sql == (
            "SELECT id, name, active FROM t_authors "
            "UNION SELECT id, name, active FROM t_authors LIMIT ?"
        )
        assert params == [2]


class TestValidation:
    def test_operands_must_select_the_same_number_of_columns(self):
        with pytest.raises(ValueError, match="same number of columns"):
            Query(Author.id).union(Query(Author.id, Author.name))

    def test_operand_must_be_a_query(self):
        with pytest.raises(TypeError, match="takes another Query"):
            Query(Author).union("SELECT 1")

    def test_needs_at_least_two_operands(self):
        with pytest.raises(ValueError, match="at least two operands"):
            CompoundSelect("UNION", [Query(Author)])

    def test_order_by_must_name_an_output_column(self):
        with pytest.raises(ValueError, match="not an output column"):
            Query(Author.id).union(Query(Author.id)).order_by("name")

    def test_order_by_rejects_something_with_no_name(self):
        with pytest.raises(TypeError, match="output column name"):
            Query(Author).union(Query(Author)).order_by(42)

    @pytest.mark.parametrize("bad", [-1, 1.5, True])
    def test_limit_validation_matches_query(self, bad):
        with pytest.raises((ValueError, TypeError)):
            Query(Author).union(Query(Author)).limit(bad)


class TestHydrationInterface:
    """A compound must present what the engines read off a Query, or fetch_all
    would need a special case and rows would not hydrate."""

    def test_row_shape_comes_from_the_first_operand(self):
        compound = Query(Author).union(Query(Author))
        assert compound.model is Author
        assert compound.is_multi_entity is False
        assert compound._hydration_key is Author

    def test_multi_entity_compound(self):
        compound = Query(Author.id, Author.name).union(Query(Author.id, Author.name))
        assert compound.is_multi_entity is True
        assert len(compound.hydration_spec()) == 2

    def test_output_columns(self):
        compound = Query(Author.id).union(Query(Author.id))
        assert compound.output_columns() == [("id", int)]


class TestEndToEnd:
    def test_union_deduplicates(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id < 3)
            .union(Query(Author.id).where(Author.id < 3))
            .order_by("id")
        )
        assert rows == [(1,), (2,)]

    def test_union_all_keeps_duplicates(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id == 1)
            .union_all(Query(Author.id).where(Author.id == 1))
        )
        assert rows == [(1,), (1,)]

    def test_intersect(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id < 3)
            .intersect(Query(Author.id).where(Author.id > 1))
        )
        assert rows == [(2,)]

    def test_except(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id < 3)
            .except_(Query(Author.id).where(Author.id == 1))
        )
        assert rows == [(2,)]

    def test_models_hydrate_from_a_compound(self, run_query):
        rows = run_query(
            Query(Author).where(Author.id == 1)
            .union(Query(Author).where(Author.id == 3))
            .order_by("id")
        )
        assert [a.name for a in rows] == ["ada", "carol"]
        assert all(isinstance(a, Author) for a in rows)
        # And the bool converter still applies through the compound.
        assert rows[0].active is True and rows[1].active is False

    def test_order_limit_offset_over_the_whole_result(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id <= 2)
            .union(Query(Author.id).where(Author.id >= 3))
            .order_by("id", descending=True)
            .limit(2)
        )
        assert rows == [(4,), (3,)]

    def test_compound_of_different_tables(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id == 1)
            .union(Query(Book.author_id).where(Book.id == 12))
            .order_by("id")
        )
        assert rows == [(1,), (2,)]

    def test_three_way_union(self, run_query):
        rows = run_query(
            Query(Author.id).where(Author.id == 1)
            .union(Query(Author.id).where(Author.id == 2))
            .union(Query(Author.id).where(Author.id == 3))
            .order_by("id")
        )
        assert rows == [(1,), (2,), (3,)]
