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

# Converters applied per column *type* during hydration. Drivers differ in
# what they hand back: sqlite3 returns 0/1 for booleans, whereas asyncpg
# returns real Python bools. Passing the right map keeps output identical
# across backends instead of silently leaking driver-native types into JSON.
SQLITE_CONVERTERS = {bool: bool}
ASYNCPG_CONVERTERS = {}


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
        f"    for {', '.join(field_vars)} in rows:",
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


def compile_json_default(model_cls):
    """Build a specialized `orjson(default=...)` hook for `model_cls`.

    sqlom models aren't stdlib dataclasses, so orjson can't introspect them
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
