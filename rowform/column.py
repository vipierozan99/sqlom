"""Descriptor-based column primitive.

**How the dual return type is expressed to a type checker.** `Column` is a
descriptor whose `__get__` is overloaded on whether it is reached from the class or
an instance:

    User.id    ->  ColumnExpr[int]   (obj is None)
    user.id    ->  int               (obj is an instance)

That overload is the whole trick, and it's what makes `Model.id` statically typed
as `ColumnExpr[int]` while `instance.id` is `int` -- see `model.py` for how a real
slotted dataclass ends up holding the actual storage while `Column` sits on a
subclass under the public name.
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

# The prefix a public column name is stored under, e.g. "id" -> "_rf_id". A named
# constant rather than an inline literal because model.py's synthesized storage
# dataclass has to use this exact same convention for its own field names to line
# up with what Column.__get__/__set__ read and write.
STORAGE_PREFIX = "_rf_"


class Column(Generic[T]):
    """A declared column. `Column(int)` is a `Column[int]`."""

    if TYPE_CHECKING:
        py_type: type[T]
        # Both are None until __set_name__ runs, which is why they are not
        # declared as plain `str`.
        name: str | None
        _storage_name: str | None

    def __init__(self, py_type: type[T]) -> None:
        self.py_type = py_type
        self.name = None
        self._storage_name = None

    def __set_name__(self, owner: type[Any], name: str) -> None:
        self.name = name
        self._storage_name = f"{STORAGE_PREFIX}{name}"

    @overload
    def __get__(self, obj: None, owner: type[Any]) -> ColumnExpr[T]: ...

    @overload
    def __get__(self, obj: object, owner: type[Any] | None = None) -> T: ...

    def __get__(self, obj, owner=None):
        if obj is None:
            assert self.name is not None  # __set_name__ runs before class-level access
            return ColumnExpr(owner, self.name, self.py_type)
        assert self._storage_name is not None  # ditto for instance access
        return getattr(obj, self._storage_name)

    def __set__(self, obj: object, value: T) -> None:
        # _storage_name is set by __set_name__, which the interpreter runs at class
        # creation; a Column reachable as a descriptor has always been through it.
        assert self._storage_name is not None
        setattr(obj, self._storage_name, value)


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
    for column, value in zip(columns.values(), row):
        setattr(obj, column._storage_name, value)
    return obj


def as_dict(obj: Any) -> dict[str, Any]:
    """orjson `default=` hook for heterogeneous payloads mixing several model
    types. A model-specific hook (see `compile_json_default`) is faster; this
    is the generic fallback `json_default` uses."""
    cls = type(obj)
    if hasattr(cls, "__columns__"):
        return {
            name: getattr(obj, column._storage_name) for name, column in cls.__columns__.items()
        }
    raise TypeError(f"Cannot serialize {cls!r}")
