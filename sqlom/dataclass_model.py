"""Models that are *real* stdlib dataclasses and still support `User.id > 100`.

The README originally claimed these two things can't be combined. The narrow
version of that claim is true: a class attribute and a `__slots__` entry of
the same name collide (`ValueError: 'id' in __slots__ conflicts with class
variable`), so you can't put a `Column` descriptor at class scope next to a
dataclass field of the same name.

But the collision is only a problem because both live on *the class*. Attribute
lookup on a class consults `type(cls).__mro__` first, and a **data descriptor
found on the metaclass wins over the class's own entry**. So putting the
query-expression descriptors on a per-model metaclass gives:

    User.id       -> ColumnExpr  (metaclass data descriptor wins)
    instance.id   -> the value   (plain slot; metaclass isn't consulted)

`@dataclass(slots=True)` rebuilds the class via `cls.__class__(...)`, which
preserves a custom metaclass, so the two compose cleanly.

Serialization caveat, and it's a big one: orjson recognizes dataclasses
natively, so it will *ignore* a `default=` hook for them. Its native path
reads `__dict__` directly, and for a slotted dataclass that attribute doesn't
exist — orjson provokes and clears an `AttributeError` per instance, then
walks `__dataclass_fields__` with two `getattr`s per field. Measured here that
fallback costs ~416 ns/object versus ~182 ns for a compiled dict literal. So
pass `orjson.OPT_PASSTHROUGH_DATACLASS` to route slotted models back to
`compile_json_default`. `DATACLASS_DUMP_OPTION` below is that flag.
"""

import typing
from dataclasses import dataclass, fields

from .column import ColumnExpr

try:  # orjson is optional at import time; only needed to serialize.
    import orjson

    DATACLASS_DUMP_OPTION = orjson.OPT_PASSTHROUGH_DATACLASS
except ImportError:  # pragma: no cover
    DATACLASS_DUMP_OPTION = 2048


class _FieldColumn:
    """Adapter so a dataclass field looks like a `Column` to the compilers.

    For a dataclass the value lives under the field's own name, so there's no
    shadow storage name to indirect through.
    """

    __slots__ = ("name", "py_type", "_storage_name")

    def __init__(self, name, py_type):
        self.name = name
        self.py_type = py_type
        self._storage_name = name


class ColumnDescriptor:
    """Data descriptor installed on the *metaclass*.

    Having `__set__` is what makes it a data descriptor, which is what gives it
    priority over the slot descriptor of the same name on the class itself.
    """

    __slots__ = ("name", "py_type")

    def __init__(self, name, py_type):
        self.name = name
        self.py_type = py_type

    def __get__(self, cls, metacls=None):
        if cls is None:
            return self
        return ColumnExpr(cls, self.name, self.py_type)

    def __set__(self, cls, value):
        raise AttributeError(
            f"{self.name!r} is a column; assigning it on the class would "
            f"replace the query expression"
        )


def model(cls=None, *, tablename=None, slots=True):
    """Class decorator: stdlib dataclass + class-level query expressions."""

    def wrap(cls):
        hints = typing.get_type_hints(cls)
        annotations = getattr(cls, "__annotations__", {})
        if not annotations:
            raise ValueError(f"{cls.__name__} declares no annotated columns")

        # 1. Rebuild the class under a fresh metaclass unique to this model, so
        #    installing descriptors on it can't leak into sibling models.
        meta = type(f"{cls.__name__}Meta", (type(cls),), {})
        namespace = dict(cls.__dict__)
        namespace.pop("__dict__", None)
        namespace.pop("__weakref__", None)
        rebuilt = meta(cls.__name__, cls.__bases__, namespace)

        # 2. dataclass() recreates via cls.__class__(...), keeping `meta`.
        dc = dataclass(slots=slots)(rebuilt)

        # 3. Expose the metadata the query builder and compilers expect.
        dc.__tablename__ = tablename or getattr(cls, "__tablename__", None) or cls.__name__.lower()
        dc.__columns__ = {
            f.name: _FieldColumn(f.name, hints.get(f.name, f.type)) for f in fields(dc)
        }

        # 4. Install the query-expression descriptors on the metaclass.
        for name, column in dc.__columns__.items():
            setattr(type(dc), name, ColumnDescriptor(name, column.py_type))

        return dc

    return wrap(cls) if cls is not None else wrap
