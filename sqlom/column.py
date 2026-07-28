"""Descriptor-based column and expression primitives.

**How the dual return type is expressed to a type checker.** `Column` is a
descriptor whose `__get__` is overloaded on whether it is reached from the class or
an instance:

    User.id    ->  ColumnExpr[int]   (obj is None)
    user.id    ->  int               (obj is an instance)

That is the whole trick, and it is why the `ModelMeta` style is statically typed
while the `@model` dataclass style is not — there the descriptor lives on the
*metaclass*, and no checker models a metaclass data descriptor shadowing a class
attribute. See tests/typing/.


This is the real (if minimal) implementation of the pattern described in
the README: a metaclass owns both `__slots__` generation and the
descriptor protocol, so `Column` can behave as a live instance accessor
*and* return a query expression at class-level access.
"""


from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Generic, Sequence, TypeVar, overload

# ColumnExpr, Condition and the predicate tree live in expr.py, which grew out of
# this module once aliases, OR-groups and aggregates arrived. They are re-exported
# here because that is where they were first defined and callers import them from
# `sqlom` anyway.
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
    _TableSource,
    _bare,
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
        self._storage_name = f"_{name}"

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


class ModelMeta(type):
    """Owns slot generation instead of delegating to @dataclass, since a
    Column descriptor and a same-named slot can't coexist (see README)."""

    # Declared for type checkers so model classes expose these without a
    # catch-all. The `__getattr__` below is hidden from them deliberately: a
    # metaclass `__getattr__` is a fallback for *any* attribute, so a checker that
    # saw it would type `User.typo` as whatever it returns instead of reporting the
    # typo. Declaring the one name it actually serves keeps both properties.
    if TYPE_CHECKING:
        __columns__: dict[str, Column[Any]]
        __tablename__: str
        __json_default__: Callable[[Any], dict[str, Any]]

    def __new__(mcs, name, bases, namespace):
        columns = {
            key: value for key, value in namespace.items() if isinstance(value, Column)
        }
        namespace["__slots__"] = tuple(f"_{col_name}" for col_name in columns)
        namespace["__columns__"] = columns
        return super().__new__(mcs, name, bases, namespace)

    if not TYPE_CHECKING:
      def __getattr__(cls, name):
        # Compile the per-model orjson hook on first use, then cache it as a
        # real class attribute so later lookups never reach __getattr__.
        if name == "__json_default__":
            from .compile import compile_json_default

            fn = compile_json_default(cls)
            setattr(cls, name, fn)
            return fn
        raise AttributeError(name)


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
    """orjson `default=` hook: sqlom models aren't stdlib dataclasses, so
    orjson can't introspect them natively."""
    cls = type(obj)
    if hasattr(cls, "__columns__"):
        return {
            name: getattr(obj, column._storage_name)
            for name, column in cls.__columns__.items()
        }
    raise TypeError(f"Cannot serialize {cls!r}")
