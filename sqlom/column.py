"""Descriptor-based column and expression primitives.

This is the real (if minimal) implementation of the pattern described in
the README: a metaclass owns both `__slots__` generation and the
descriptor protocol, so `Column` can behave as a live instance accessor
*and* return a query expression at class-level access.
"""


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


class Column:
    def __init__(self, py_type: type):
        self.py_type = py_type
        self.name = None
        self._storage_name = None

    def __set_name__(self, owner, name):
        self.name = name
        self._storage_name = f"_{name}"

    def __get__(self, obj, owner=None):
        if obj is None:
            return ColumnExpr(owner, self.name, self.py_type)
        return getattr(obj, self._storage_name)

    def __set__(self, obj, value):
        setattr(obj, self._storage_name, value)


class ModelMeta(type):
    """Owns slot generation instead of delegating to @dataclass, since a
    Column descriptor and a same-named slot can't coexist (see README)."""

    def __new__(mcs, name, bases, namespace):
        columns = {
            key: value for key, value in namespace.items() if isinstance(value, Column)
        }
        namespace["__slots__"] = tuple(f"_{col_name}" for col_name in columns)
        namespace["__columns__"] = columns
        return super().__new__(mcs, name, bases, namespace)

    def __getattr__(cls, name):
        # Compile the per-model orjson hook on first use, then cache it as a
        # real class attribute so later lookups never reach __getattr__.
        if name == "__json_default__":
            from .compile import compile_json_default

            fn = compile_json_default(cls)
            setattr(cls, name, fn)
            return fn
        raise AttributeError(name)


def hydrate(model_cls, row):
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


def as_dict(obj):
    """orjson `default=` hook: sqlom models aren't stdlib dataclasses, so
    orjson can't introspect them natively."""
    cls = type(obj)
    if hasattr(cls, "__columns__"):
        return {
            name: getattr(obj, column._storage_name)
            for name, column in cls.__columns__.items()
        }
    raise TypeError(f"Cannot serialize {cls!r}")
