"""`Column`-typed models: a metaclass-backed, real stdlib `@dataclass(slots=True)`.

    @model
    class User:
        id: Column[int] = Column(int)

    User.id  -> ColumnExpr[int]   (class access, via ColumnMeta)
    user.id  -> 1                  (instance access, plain dataclass slot)

See docs/FINDINGS.md ("The `@model` metaclass") for the design rationale --
why a metaclass, the dataclass default-probe sequencing trap, and the
`@model(tablename=...)` typing gap.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, TypeVar, dataclass_transform, overload

from .column import Column, ColumnExpr

T = TypeVar("T")

try:  # orjson is optional at import time; only needed to serialize.
    import orjson

    DATACLASS_DUMP_OPTION = orjson.OPT_PASSTHROUGH_DATACLASS
except ImportError:  # pragma: no cover
    DATACLASS_DUMP_OPTION = 2048


class ColumnMeta(type):
    """Metaclass mapping class-level column access to `ColumnExpr`.

    Subclasses the PUBLIC `type` (dataclasses impose no metaclass of their
    own), so there is no private-API dependency. Interception is gated on
    `__column_exprs__` existing, which is set only AFTER `dataclass()` runs
    (see module docstring's sequencing trap).
    """

    def __getattribute__(cls, name: str) -> Any:
        try:
            exprs = type.__getattribute__(cls, "__column_exprs__")
        except AttributeError:
            exprs = None  # not yet enabled (during @dataclass) -> delegate all
        if exprs is not None and name in exprs:
            return exprs[name]
        return type.__getattribute__(cls, name)


@overload
def model(cls: type[T], /) -> type[T]: ...
@overload
def model(*, tablename: str | None = ...) -> Callable[[type[T]], type[T]]: ...
@dataclass_transform(field_specifiers=(Column,))
def model(cls=None, *, tablename=None):
    """Class decorator: slotted dataclass storage + class-scope `Column`s via metaclass."""

    def wrap(cls):
        columns: dict[str, Column[Any]] = {
            name: value for name, value in vars(cls).items() if isinstance(value, Column)
        }
        if not columns:
            raise ValueError(f"{cls.__name__} declares no columns")

        # Carry over everything the user wrote (custom __repr__/__init__/other
        # methods, __tablename__, ...) except the Columns themselves -- those
        # must be absent so @dataclass's default probe sees MISSING for each
        # field name instead of a Column instance (or, once enabled, a
        # ColumnExpr) -- and except __dict__/__weakref__, which slots=True
        # replaces.
        namespace: dict[str, Any] = {
            key: value
            for key, value in vars(cls).items()
            if key not in columns and key not in ("__dict__", "__weakref__")
        }
        namespace["__annotations__"] = {name: col.py_type for name, col in columns.items()}

        # Typed as Any: a direct `ColumnMeta(...)` call ties the static type to
        # the metaclass itself, which neither matches the `type[T]` the
        # overloads above promise callers nor has the extra attributes
        # attached below -- same as the untyped `type(cls)(...)` call this
        # replaced.
        base: Any = ColumnMeta(cls.__name__, cls.__bases__, namespace)

        # dataclass(slots=True) rebuilds via base.__class__(...) -> preserves
        # ColumnMeta and creates the real, public-named slot descriptors. With
        # interception off, its getattr(cls, field_name) default probe sees
        # MISSING, so every field ends up required (matching the source,
        # which never assigns a real default -- `Column(int)` is a marker,
        # not a default value).
        built = dataclasses.dataclass(slots=True)(base)

        # Enable interception + attach metadata (class attrs are allowed
        # despite __slots__; only *instance* attrs are restricted).
        built.__column_exprs__ = {
            name: ColumnExpr(built, name, col.py_type) for name, col in columns.items()
        }
        built.__columns__ = columns
        built.__tablename__ = (
            tablename or getattr(cls, "__tablename__", None) or cls.__name__.lower()
        )
        # json_default() (the generic hook for heterogeneous payloads) dispatches
        # on this per model_cls; compiled once here rather than lazily on first
        # use, since there's no metaclass __getattr__ to hook that the way
        # ModelMeta used to.
        from .compile import compile_json_default

        built.__json_default__ = compile_json_default(built)

        return built

    return wrap(cls) if cls is not None else wrap
