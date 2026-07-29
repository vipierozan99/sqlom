"""Per-model code generation.

The generic `hydrate()`/`as_dict()` helpers in `column.py` walk the column
list and use `setattr`/`getattr` with string names on every row. Since a
model's column layout is fixed and known once, we can instead compile a
specialized function per model whose field accesses are ordinary
`STORE_ATTR`/`LOAD_ATTR` bytecode against a fixed slot. Measurably faster
than the reflective loop — see benchmarks/.

Both factories attach the generated source to the returned function as
`__source__` so the codegen stays inspectable rather than being magic.
"""

from collections.abc import Callable
from typing import Any

# Converters applied per column *type* during hydration. Drivers differ in
# what they hand back: sqlite3 returns 0/1 for booleans, whereas asyncpg
# returns real Python bools. Passing the right map keeps output identical
# across backends instead of silently leaking driver-native types into JSON.
Converters = dict[type, Callable[[Any], Any]]

SQLITE_CONVERTERS: Converters = {bool: bool}
ASYNCPG_CONVERTERS: Converters = {}
# psycopg3 decodes Postgres booleans to real Python bools, like asyncpg.
PSYCOPG_CONVERTERS: Converters = {}


def compile_hydrator(model_cls, converters=None):
    """Build a specialized `row -> instance` function for `model_cls`.

    `converters` maps a column's declared Python type to a callable applied
    to that column's raw driver value.
    """
    converters = converters or {}
    columns = list(model_cls.__columns__.values())

    namespace = {"_new": object.__new__, "_cls": model_cls}
    lines = ["def _hydrate(row):", "    obj = _new(_cls)"]

    for index, column in enumerate(columns):
        converter = converters.get(column.py_type)
        if converter is None:
            value_expr = f"row[{index}]"
        else:
            converter_name = f"_conv_{index}"
            namespace[converter_name] = converter
            value_expr = f"{converter_name}(row[{index}])"
        lines.append(f"    obj.{column._storage_name} = {value_expr}")

    lines.append("    return obj")
    source = "\n".join(lines)

    exec(source, namespace)
    fn = namespace["_hydrate"]
    fn.__source__ = source
    return fn


def compile_batch_hydrator(model_cls, converters=None):
    """Build a specialized `rows -> [instance]` function for `model_cls`.

    Faster than mapping `compile_hydrator` over the rows for two reasons:
    the row tuple is unpacked by the `for` statement itself (one UNPACK_SEQUENCE
    instead of a subscript per field), and `list.append` is bound once outside
    the loop.

    Field stores are written as plain attribute assignments on purpose. CPython
    3.11's specializing interpreter (PEP 659) quickens `obj.x = v` on a slotted
    class into `STORE_ATTR_SLOT`; routing through `setattr()` or the slot
    descriptor's `__set__` is an ordinary call that defeats that inline cache
    and measures several times slower.
    """
    converters = converters or {}
    columns = list(model_cls.__columns__.values())
    if not columns:
        raise ValueError(f"{model_cls.__name__} declares no columns")

    namespace = {"_new": object.__new__, "_cls": model_cls}
    field_vars = [f"f{i}" for i in range(len(columns))]

    lines = [
        "def _hydrate_all(rows):",
        "    out = []",
        "    append = out.append",
        # Trailing comma is load-bearing: `for f0 in rows` binds each row
        # *tuple* to f0 instead of unpacking it, so a single selected column
        # would nest. `for f0, in rows` unpacks, and the trailing comma is
        # harmless at every other arity.
        f"    for {', '.join(field_vars)}, in rows:",
        "        obj = _new(_cls)",
    ]

    for index, (column, var) in enumerate(zip(columns, field_vars)):
        converter = converters.get(column.py_type)
        if converter is None:
            value_expr = var
        else:
            converter_name = f"_conv_{index}"
            namespace[converter_name] = converter
            value_expr = f"{converter_name}({var})"
        lines.append(f"        obj.{column._storage_name} = {value_expr}")

    lines += ["        append(obj)", "    return out"]
    source = "\n".join(lines)

    exec(source, namespace)
    fn = namespace["_hydrate_all"]
    fn.__source__ = source
    return fn


def compile_join_hydrator(entities, converters=None, wrap=True):
    """Build a `rows -> [tuple]` function for a multi-entity (joined) select.

    `entities` is a sequence of specs describing what each slot of the output
    tuple holds, in select order:

        ("model", model_cls, nullable)  build an instance from that model's columns
        ("column", py_type)             take a single scalar

    A joined row arrives as one flat tuple — `(u.id, u.name, ..., p.id, p.title)`
    — so this generates the same shape as `compile_batch_hydrator`: unpack the
    whole row in the `for` statement, then straight-line slot stores. No slicing,
    no per-entity function call, no zip.

    `wrap` controls the output shape. True (the default) appends a tuple per row.
    False appends the single entity directly, which is what a one-model query needs
    even when a RIGHT or FULL join can make it None — `Query(Author).right_join(...)`
    yields `[author, None, author]`, not a list of 1-tuples. It only applies to a
    single-entity spec.

    `nullable` marks an entity reached by an OUTER join, where the row can carry
    all-NULL columns for a missing match. Those become `None` rather than an
    object with every field set to None, which is what SQLAlchemy does and what
    calling code expects from a left join. The test is "every selected column of
    this entity is NULL", since rowform models declare no primary key to test
    instead — so an entity whose columns are *all* genuinely NULL in the data
    hydrates as None. Give such a query at least one NOT NULL column, or select
    it without the outer join.
    """
    converters = converters or {}
    namespace = {"_new": object.__new__}

    # One field variable per selected column, across all entities.
    total = 0
    plans = []
    for slot, spec in enumerate(entities):
        if spec[0] == "model":
            _, model_cls, nullable = spec
            columns = list(model_cls.__columns__.values())
            if not columns:
                raise ValueError(f"{model_cls.__name__} declares no columns")
            namespace[f"_cls{slot}"] = model_cls
            plans.append((slot, columns, nullable, total))
            total += len(columns)
        elif spec[0] == "column":
            plans.append((slot, None, False, total))
            total += 1
        else:  # pragma: no cover - internal
            raise ValueError(f"unknown entity spec {spec[0]!r}")

    if not plans:
        raise ValueError("a query must select at least one entity")

    field_vars = [f"f{i}" for i in range(total)]
    lines = [
        "def _hydrate_join(rows):",
        "    out = []",
        "    append = out.append",
        # Trailing comma is load-bearing: `for f0 in rows` binds each row
        # *tuple* to f0 instead of unpacking it, so a single selected column
        # would nest. `for f0, in rows` unpacks, and the trailing comma is
        # harmless at every other arity.
        f"    for {', '.join(field_vars)}, in rows:",
    ]

    object_vars = []
    for slot, columns, nullable, offset in plans:
        if columns is None:
            object_vars.append(field_vars[offset])
            continue

        obj = f"o{slot}"
        object_vars.append(obj)
        mine = field_vars[offset:offset + len(columns)]
        indent = "        "
        if nullable:
            test = " is None and ".join(mine) + " is None"
            lines.append(f"{indent}if {test}:")
            lines.append(f"{indent}    {obj} = None")
            lines.append(f"{indent}else:")
            indent += "    "
        lines.append(f"{indent}{obj} = _new(_cls{slot})")
        for column, var in zip(columns, mine):
            converter = converters.get(column.py_type)
            if converter is None:
                value_expr = var
            else:
                converter_name = f"_conv_{slot}_{var}"
                namespace[converter_name] = converter
                value_expr = f"{converter_name}({var})"
            lines.append(f"{indent}{obj}.{column._storage_name} = {value_expr}")

    if wrap or len(object_vars) != 1:
        lines.append(f"        append(({', '.join(object_vars)},))")
    else:
        lines.append(f"        append({object_vars[0]})")
    lines.append("    return out")
    source = "\n".join(lines)

    exec(source, namespace)
    fn = namespace["_hydrate_join"]
    fn.__source__ = source
    return fn


def compile_json_default(model_cls):
    """Build a specialized `orjson(default=...)` hook for `model_cls`.

    rowform models aren't stdlib dataclasses, so orjson can't introspect them
    and calls back into Python once per object. Making that callback a
    straight-line dict literal is meaningfully cheaper than a comprehension
    over the column map.
    """
    items = ", ".join(
        f"{name!r}: obj.{column._storage_name}"
        for name, column in model_cls.__columns__.items()
    )
    source = f"def _default(obj):\n    return {{{items}}}"

    namespace = {}
    exec(source, namespace)
    fn = namespace["_default"]
    fn.__source__ = source
    return fn


def json_default(obj):
    """Generic orjson hook for payloads mixing several model types.

    Slower than a model-specific `compile_json_default` (one dict lookup
    plus an attribute load per object), but it handles heterogeneous lists.
    """
    try:
        builder = type(obj).__json_default__
    except AttributeError:
        raise TypeError(f"Cannot serialize {type(obj)!r}") from None
    return builder(obj)
