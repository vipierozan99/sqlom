"""Descriptor-based column primitive.

**How the dual return type is expressed to a type checker.** `Column` is a
descriptor whose `__get__` is overloaded on whether it is reached from the class or
an instance:

    User.id    ->  ColumnExpr[int]   (obj is None)
    user.id    ->  int               (obj is an instance)

That overload is the whole trick, and it's what makes `Model.id` statically typed
as `ColumnExpr[int]` while `instance.id` is `int` -- see `model.py` for how a
metaclass reads `Column.py_type` at build time and discards the `Column`
instance itself, so the built class's `id` is a real, public-named slot with no
descriptor in the way at runtime.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar, overload

# ColumnExpr, Condition and the predicate tree live in expr.py, which grew out of
# this module once aliases, OR-groups and aggregates arrived. They are re-exported
# here because that is where they were first defined and callers import them from
# `rowform` anyway.
from .expr import (  # noqa: F401
    Aggregate,
    Alias,
    BooleanClause,
    ColumnExpr,
    Condition,
    ExistsClause,
    Expression,
    InClause,
    Labelled,
    Not,
    Predicate,
    Subquery,
    _bare,
    _TableSource,
    and_,
    avg,
    count,
    exists,
    max_,
    min_,
    not_,
    or_,
    source_name,
    source_prefix,
    sum_,
)

T = TypeVar("T")
M = TypeVar("M", bound=_TableSource)


class Column(Generic[T]):
    """A declared column. `Column(int)` is a `Column[int]`."""

    if TYPE_CHECKING:
        py_type: type[T]
        name: str | None  # None until __set_name__ runs

    def __init__(self, py_type: type[T]) -> None:
        self.py_type = py_type
        self.name = None

    def __set_name__(self, owner: type[Any], name: str) -> None:
        self.name = name

    @overload
    def __get__(self, obj: None, owner: type[Any]) -> ColumnExpr[T]: ...

    @overload
    def __get__(self, obj: object, owner: type[Any] | None = None) -> T: ...

    def __get__(self, obj, owner=None):
        # Dead at runtime for @model-built classes -- the metaclass never
        # installs this descriptor on the built class (see model.py). Kept so
        # the overloads above have a body for the type checker to attach to.
        assert self.name is not None  # __set_name__ runs before class-level access
        if obj is None:
            return ColumnExpr(owner, self.name, self.py_type)
        return getattr(obj, self.name)

    def __set__(self, obj: object, value: T) -> None:
        assert self.name is not None
        setattr(obj, self.name, value)


def hydrate(model_cls: type[M], row: Sequence[Any]) -> M:
    """Build an instance from a positional row (tuple/Record) without going
    through __init__ or per-field keyword dispatch.

    The width check is not ceremony: `zip` stops at the shorter side, so a row
    with too few values would produce an object with unset slots that only fails
    later at read time, and a row with too many would silently drop columns. The
    compiled hydrators get this for free from tuple unpacking; this reflective
    path has to ask.
    """
    columns = model_cls.__columns__
    if len(row) != len(columns):
        raise ValueError(
            f"{model_cls.__name__} has {len(columns)} columns "
            f"({', '.join(columns)}) but the row has {len(row)} values"
        )
    obj = object.__new__(model_cls)
    for name, value in zip(columns, row):
        setattr(obj, name, value)
    return obj


def as_dict(obj: Any) -> dict[str, Any]:
    """orjson `default=` hook for heterogeneous payloads mixing several model
    types. A model-specific hook (see `compile_json_default`) is faster; this
    is the generic fallback `json_default` uses."""
    cls = type(obj)
    if hasattr(cls, "__columns__"):
        return {name: getattr(obj, name) for name in cls.__columns__}
    raise TypeError(f"Cannot serialize {cls!r}")
