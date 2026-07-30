"""Decide what a statement's rows mean, from the statement rather than the model.

Planning hydration from a model's declaration order is a silent-corruption
hazard: `select(User.name, User.id)` yields a different column order, and because
the generated hydrator unpacks positionally (`compile.py`) the fields would be
mis-assigned with nothing to catch it.

So the plan comes from `stmt.selected_columns`: a contiguous run of selected
columns that *is* some model's full column list becomes a model entity, and
anything else is a scalar. That mirrors SQLAlchemy, which is what makes it fall
out without an error path — `select(User.name, User.id)` simply degrades to a
`(str, int)` tuple, exactly what SQLAlchemy returns for that query.

Columns are compared **by identity**. `Column.__eq__` builds a SQL expression; it
does not compare.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.sql.expression import Join

from .errors import PlanError
from .model import model_for

# One slot of the output tuple, in select order:
#   ("model", model_cls, [(attr_name, ColumnElement), ...], nullable)
#   ("column", ColumnElement)
Entity = tuple[Any, ...]


class Plan:
    """What one statement's rows hydrate into."""

    __slots__ = ("columns", "entities", "wrap")

    def __init__(self, entities: list[Entity], columns: list[Any]):
        self.entities = entities
        self.columns = columns
        # One selected entity yields that entity; two or more yield a tuple.
        #
        # This is the one place the result shape deliberately departs from
        # SQLAlchemy, which returns `Row` objects throughout and makes you call
        # `.scalars()` to unwrap. Departing for models but not for scalars was
        # the obvious middle, and it is worse on both counts: it needs two rules
        # instead of one, and it is not expressible in the type system —
        # `select(User)` and `select(User.name)` are `Select[Tuple[User]]` and
        # `Select[Tuple[str]]`, distinguishable only by arity, so `fetch_all`
        # can be typed exactly if and only if arity alone decides the shape.
        self.wrap = len(entities) != 1

    def __repr__(self) -> str:
        parts = [
            e[1].__name__ + ("|None" if e[3] else "") if e[0] == "model" else str(e[1])
            for e in self.entities
        ]
        return f"<Plan {', '.join(parts)}{'' if self.wrap else ' (unwrapped)'}>"


def plan(stmt: Any) -> Plan:
    """Build the entity plan for a `Select`, or for a write with RETURNING."""
    columns = list(
        stmt.selected_columns if getattr(stmt, "is_select", False) else stmt.exported_columns
    )
    nullable_froms = _nullable_froms(stmt)

    entities: list[Entity] = []
    index = 0
    while index < len(columns):
        matched = _match_model(columns, index, nullable_froms)
        if matched is None:
            entities.append(("column", columns[index]))
            index += 1
        else:
            entity, width = matched
            entities.append(entity)
            index += width

    if not entities:
        raise PlanError("a statement must select at least one column")
    return Plan(entities, columns)


def _match_model(
    columns: list[Any], index: int, nullable_froms: set[int]
) -> tuple[Entity, int] | None:
    """Is the run starting at `index` exactly some model's full column list?"""
    from_clause = getattr(columns[index], "table", None)
    if from_clause is None:
        return None
    model = model_for(from_clause)
    if model is None:
        return None

    declared = type.__getattribute__(model, "__columns__")
    width = len(declared)
    if index + width > len(columns):
        return None

    # An alias has its own Column objects proxying the table's, so resolve each
    # declared column through the from clause actually being selected. This is
    # what lets a self-join hydrate both sides as models (R8) instead of
    # degrading to scalars.
    pairs = []
    for attr, column in declared.items():
        try:
            resolved = from_clause.columns[column.key]
        except KeyError:
            return None
        pairs.append((attr, resolved))

    if any(columns[index + offset] is not col for offset, (_, col) in enumerate(pairs)):
        return None

    return ("model", model, pairs, id(from_clause) in nullable_froms), width


def _nullable_froms(stmt: Any) -> set[int]:
    """`id()` of every from clause reachable only through an OUTER join.

    "Nullable" for a model entity means the row can carry all-NULL columns for a
    missing match, which `compile.py` turns into `None` rather than an object
    with every field set to None.
    """
    marked: set[int] = set()
    # A write with RETURNING has one table and no joins, so there is nothing to
    # walk — and no `get_final_froms` to walk it with.
    if not hasattr(stmt, "get_final_froms"):
        return marked

    def leaves(clause: Any, into: set[int]) -> None:
        if isinstance(clause, Join):
            leaves(clause.left, into)
            leaves(clause.right, into)
        else:
            into.add(id(clause))

    def walk(clause: Any) -> None:
        if not isinstance(clause, Join):
            return
        walk(clause.left)
        walk(clause.right)
        if clause.isouter or clause.full:
            leaves(clause.right, marked)
        if clause.full:
            leaves(clause.left, marked)

    for from_clause in stmt.get_final_froms():
        walk(from_clause)
    return marked
