"""`Column`-typed models backed by a real slotted dataclass.

Putting a `Column` descriptor at class scope right next to a same-named
dataclass field doesn't work: `@dataclass(slots=True)` reclaims that name for
a `__slots__` entry, and a class attribute of the same name collides with it
(`ValueError: 'id' in __slots__ conflicts with class variable`).

This sidesteps the collision instead of routing around it with a metaclass.
Give the dataclass storage the shadow name (`_rf_id`) and put the real
`Column` descriptor on a *subclass* of that dataclass, under the public name
(`id`). No collision -- they're different names on different classes in the
MRO -- so `Model.id` / `instance.id` resolve exactly the way a checker
already understands descriptor overloads: `Column.__get__` returns
`ColumnExpr[T]` at class scope (`obj is None`) and `T` at instance scope.

Two things make the constructor and the storage layer line up with that,
neither of which is optional:

1. `@dataclass_transform(field_specifiers=(Column,))` on `model()`, plus an
   explicit `id: Column[int] = Column(int)` annotation (not bare `id =
   Column(int)`) on every field. This is "descriptor-typed fields" -- the
   same mechanism SQLAlchemy's `Mapped[T]` / `mapped_column()` and msgspec's
   own stubs rely on. A checker sees the `Column[int]` annotation, infers the
   constructor parameter's type from `Column.__set__`'s value parameter
   (`int`), and still resolves class-level access through `Column.__get__`'s
   own overload (`ColumnExpr[int]`) rather than the annotation. `wrap()`
   below never sees any of this: `dataclass_transform` has zero runtime
   effect, it only stamps `__dataclass_transform__` on the function.
   Known gap: this only type-checks the `@model` bare-decorator form, not
   `@model(tablename=...)` -- pyright doesn't propagate the field-specifier
   synthesis through the intermediate `wrap` closure for that call shape.
   Nothing in this codebase calls it that way (every model passes
   `tablename` via `__tablename__` in the class body instead), so it's
   undiagnosed rather than worked around.

2. The rebuilt class must declare its own `__slots__ = ()`. Skipping this is
   an easy trap: a subclass that doesn't declare `__slots__` gets an
   (empty) per-instance `__dict__` by default even though the storage base
   is fully slotted. That silent `__dict__` is what orjson's native
   serializer keys off of -- finding it empty, it takes orjson's *faster*
   `__dict__`-reading path instead of the `__dataclass_fields__` fallback
   path (which does `getattr()` per field and would have reached `Column`
   correctly), and emits `{}` instead of raising. With `__slots__ = ()` in
   place, `obj.__dict__` doesn't exist at all, orjson takes the fallback
   path, and bare `orjson.dumps(instance)` -- no options, no custom hook --
   produces correct output. `DATACLASS_DUMP_OPTION` below is still worth
   using: it routes serialization through the compiled, model-specific
   `compile_json_default` hook instead of orjson's generic per-field
   `getattr` loop, which is measurably faster, but it is a speed choice now,
   not a correctness requirement.

`repr()` and `dataclasses.fields()` would otherwise surface the shadow names
(`_rf_id`), because those are what the storage dataclass actually declared --
fixed below by giving the model its own `__repr__` and its own
`__dataclass_fields__`, both keyed by the public name. The Field objects are
shallow copies of the storage's own (renaming the shared original would
corrupt the storage class), so `dataclasses.fields(Author)` and
`dataclasses.asdict(author)` both work against the public names -- the
latter via a copied Field's `.name` being `"id"`, so its internal
`getattr(obj, "id")` reaches the `Column` descriptor, not the raw slot.

`compile.py`'s codegen needs zero changes to target this storage: hydration
still builds instances via `object.__new__` + direct slot assignment on the
shadow name, exactly as it does today -- ordinary Python class, no
restriction like the one a `msgspec.Struct` base would impose.
"""

import copy
from dataclasses import dataclass, fields
from typing import Any, cast, dataclass_transform

from .column import STORAGE_PREFIX, Column
from .utils import compile_source

try:  # orjson is optional at import time; only needed to serialize.
    import orjson

    DATACLASS_DUMP_OPTION = orjson.OPT_PASSTHROUGH_DATACLASS
except ImportError:  # pragma: no cover
    DATACLASS_DUMP_OPTION = 2048


@dataclass_transform(field_specifiers=(Column,))
def model(cls=None, *, tablename=None):
    """Class decorator: real slotted-dataclass storage + class-scope `Column`s."""

    def wrap(cls):
        columns: dict[str, Column[Any]] = {
            key: value for key, value in cls.__dict__.items() if isinstance(value, Column)
        }
        if not columns:
            raise ValueError(f"{cls.__name__} declares no columns")

        # 1. A slotted dataclass holding only the shadow-named fields. Built from
        #    a fresh class rather than a namespace dict, so `dataclass()` can find
        #    `__annotations__` the normal way.
        storage_annotations = {column._storage_name: column.py_type for column in columns.values()}
        storage_cls = type(
            f"{STORAGE_PREFIX}{cls.__name__}Storage", (), {"__annotations__": storage_annotations}
        )
        storage = dataclass(slots=True)(storage_cls)

        # 2. Rebuild the model to inherit from that dataclass. The Column
        #    instances already live in cls.__dict__ under their public names --
        #    nothing about them needs to change, since "id" here and "_rf_id" on
        #    `storage` never collide.
        namespace = dict(cls.__dict__)
        namespace.pop("__dict__", None)
        namespace.pop("__weakref__", None)
        # See module docstring point 2: without this, the rebuilt class gets its
        # own (empty) __dict__ by default, which breaks orjson's native path.
        namespace["__slots__"] = ()
        rebuilt = type(cls)(cls.__name__, (storage, *cls.__bases__), namespace)

        rebuilt.__tablename__ = (
            tablename or getattr(cls, "__tablename__", None) or cls.__name__.lower()
        )
        rebuilt.__columns__ = columns
        # json_default() (the generic hook for heterogeneous payloads) dispatches
        # on this per model_cls; compiled once here rather than lazily on first
        # use, since there's no metaclass __getattr__ to hook that the way
        # ModelMeta used to.
        from .compile import compile_json_default

        rebuilt.__json_default__ = compile_json_default(rebuilt)

        # 3. Re-key __dataclass_fields__ and __repr__ to the public names. Copy
        #    each Field rather than mutate it in place -- it's shared with
        #    `storage`, whose own fields() must keep reporting the shadow names.
        storage_fields = {field.name: field for field in fields(cast(Any, storage))}
        renamed_fields = {}
        for name, column in columns.items():
            field = copy.copy(storage_fields[cast(str, column._storage_name)])
            field.name = name
            renamed_fields[name] = field
        rebuilt.__dataclass_fields__ = renamed_fields

        if "__repr__" not in cls.__dict__:

            def __repr__(self, columns=columns):
                parts = ", ".join(f"{name}={getattr(self, name)!r}" for name in columns)
                return f"{self.__class__.__qualname__}({parts})"

            rebuilt.__repr__ = __repr__

        # 4. Public-name __init__. `storage`'s own generated __init__ takes the
        #    shadow names (_rf_id, ...) as parameters, so User(id=1) would fail
        #    without this. Doesn't touch hydration's speed: hydrate()/
        #    compile_hydrator() build instances via object.__new__ + slot
        #    assignment and never call __init__ at all. This is purely for
        #    hand-constructing instances.
        if "__init__" not in cls.__dict__:
            params = ", ".join(columns)
            body = "\n".join(f"    self.{name} = {name}" for name in columns)
            source = f"def __init__(self, {params}):\n{body}"
            rebuilt.__init__ = compile_source(source, "__init__")

        return rebuilt

    return wrap(cls) if cls is not None else wrap
