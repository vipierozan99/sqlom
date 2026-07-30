"""What a statement's rows mean — planned from the statement, never the model.

This is the correctness-critical half of the library (docs/PLAN_CORE_COMPILER.md
§5c, R7). The generated hydrator unpacks rows positionally, so a plan that
disagrees with the SELECT list by even one column mis-assigns fields silently.
The matrix below is §8 P3's, asserted at the plan level; `test_engines.py` asserts
the same statements end to end against real rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from conftest import Author, Book, Tag

import rowform
from rowform import plan


def kinds(p):
    return [e[0] for e in p.entities]


def models(p):
    return [e[1] if e[0] == "model" else e[1].type.__class__.__name__ for e in p.entities]


class TestWholeModels:
    def test_select_model(self):
        p = plan(sa.select(Author))
        assert kinds(p) == ["model"]
        assert models(p) == [Author]
        assert p.wrap is False, "a lone entity is not wrapped in a 1-tuple"

    def test_two_models_over_a_join(self):
        p = plan(sa.select(Author, Book).join(Book))
        assert kinds(p) == ["model", "model"]
        assert models(p) == [Author, Book]
        assert p.wrap is True

    def test_three_models(self):
        p = plan(sa.select(Author, Book, Tag).select_from(Author).join(Book).join(Tag))
        assert models(p) == [Author, Book, Tag]

    def test_model_plus_scalar(self):
        p = plan(sa.select(Author, Book.title).join(Book))
        assert kinds(p) == ["model", "column"]
        assert p.entities[0][1] is Author

    def test_scalar_before_model(self):
        p = plan(sa.select(Book.title, Author).join(Book))
        assert kinds(p) == ["column", "model"]
        assert p.entities[1][1] is Author


class TestScalars:
    def test_two_columns_of_one_model(self):
        p = plan(sa.select(Author.id, Author.name))
        assert kinds(p) == ["column", "column"]
        assert p.wrap is True

    def test_reversed_columns_do_not_become_a_model(self):
        """The trap this whole module exists for: a positional hydrator built
        from the declaration would assign name->id and id->name."""
        p = plan(sa.select(Author.name, Author.id, Author.active))
        assert kinds(p) == ["column"] * 3

    def test_a_full_column_list_in_declaration_order_does_become_a_model(self):
        p = plan(sa.select(Author.id, Author.name, Author.active))
        assert kinds(p) == ["model"]

    def test_a_partial_column_list_stays_scalar(self):
        p = plan(sa.select(Author.id, Author.name))
        assert kinds(p) == ["column", "column"]

    def test_aggregate(self):
        p = plan(sa.select(sa.func.count()).select_from(Author))
        assert kinds(p) == ["column"]
        assert p.wrap is False, "one entity is one entity, scalar or model"

    def test_labelled_expression(self):
        p = plan(sa.select(Author.name, sa.func.length(Author.name).label("n")))
        assert kinds(p) == ["column", "column"]

    def test_a_foreign_table_is_all_scalars(self):
        other = sa.table("elsewhere", sa.column("a"), sa.column("b"))
        assert kinds(plan(sa.select(other))) == ["column", "column"]


class TestNullability:
    def test_inner_join_is_not_nullable(self):
        p = plan(sa.select(Author, Book).join(Book))
        assert [e[3] for e in p.entities] == [False, False]

    def test_outer_join_marks_the_right_side(self):
        p = plan(sa.select(Author, Book).outerjoin(Book))
        assert [e[3] for e in p.entities] == [False, True]

    def test_full_join_marks_both_sides(self):
        joined = Author.__table__.join(Book.__table__, full=True)
        p = plan(sa.select(Author, Book).select_from(joined))
        assert [e[3] for e in p.entities] == [True, True]

    def test_outer_join_nullability_survives_a_further_inner_join(self):
        p = plan(
            sa.select(Author, Book, Tag).select_from(Author).outerjoin(Book).join(Tag)
        )
        assert [e[3] for e in p.entities] == [False, True, False]


class TestAliases:
    def test_a_self_join_hydrates_both_sides_as_models(self):
        """R8, closed rather than accepted: declared columns are resolved through
        the FromClause actually selected, so an alias's distinct Column objects
        still match."""
        other = sa.alias(Author.__table__, "a2")
        p = plan(sa.select(Author, other).join(other, Author.id == other.c.id))
        assert kinds(p) == ["model", "model"]
        assert models(p) == [Author, Author]

    def test_the_alias_entity_reads_the_alias_columns(self):
        other = sa.alias(Author.__table__, "a2")
        p = plan(sa.select(other))
        _, _, pairs, _ = p.entities[0]
        assert [c.table for _, c in pairs] == [other] * 3

    def test_rowform_alias_plans_the_same_way(self):
        other = rowform.alias(Author, "a2")
        p = plan(sa.select(Author, other).join(other, Author.id == other.id))
        assert models(p) == [Author, Author]

    def test_rowform_alias_is_nullable_under_an_outer_join(self):
        other = rowform.alias(Author, "a2")
        p = plan(sa.select(Author, other).outerjoin(other, Author.id < other.id))
        assert [e[3] for e in p.entities] == [False, True]


class TestSubqueriesAndCtes:
    """Undeclared, a subquery is scalars — a CTE's `.element` is a `Select`, so
    there is nothing for `model_for` to walk to. `alias(of=...)` supplies it."""

    def test_an_undeclared_subquery_is_scalars(self):
        assert kinds(plan(sa.select(sa.select(Author).subquery()))) == ["column"] * 3

    def test_a_declared_subquery_is_the_model(self):
        sub = sa.select(Author).subquery()
        declared = rowform.alias(Author, of=sub)
        p = plan(sa.select(declared))
        assert models(p) == [Author]
        assert p.wrap is False

    def test_a_declared_cte_is_the_model(self):
        declared = rowform.alias(Author, of=sa.select(Author).cte("recent"))
        assert models(plan(sa.select(declared))) == [Author]

    def test_a_declared_cte_joins_against_the_table(self):
        declared = rowform.alias(Author, of=sa.select(Author).cte("recent"))
        p = plan(sa.select(Author, declared).join(declared, Author.id == declared.id))
        assert models(p) == [Author, Author]

    def test_a_column_of_a_declared_subquery_is_still_a_scalar(self):
        sub = sa.select(Author).subquery()
        rowform.alias(Author, of=sub)
        assert kinds(plan(sa.select(sub.c.name))) == ["column"]


class TestReturning:
    def test_insert_returning_a_whole_model(self):
        p = plan(sa.insert(Author.__table__).returning(Author.__table__))
        assert kinds(p) == ["model"]

    def test_update_returning_one_column(self):
        p = plan(sa.update(Author.__table__).returning(Author.id))
        assert kinds(p) == ["column"]


def test_column_equality_is_sql_not_comparison():
    """`Column.__eq__` builds a SQL expression. Truth-testing one falls back to
    object identity, so `==` can never mean "corresponds to" — an alias's
    proxied column is the same column and still compares false. That is why
    planner.py matches with `is` and resolves alias columns through the
    FromClause instead."""
    assert isinstance(Author.id == Book.id, sa.sql.elements.BinaryExpression)

    aliased = sa.alias(Author.__table__, "a2")
    assert aliased.c.id is not Author.id
    assert bool(aliased.c.id == Author.id) is False
    assert aliased.c.id.key == Author.id.key  # what correspondence actually is


def test_plan_repr_names_its_entities():
    p = plan(sa.select(Author, Book).outerjoin(Book))
    assert "Author" in repr(p)
    assert "Book|None" in repr(p)
