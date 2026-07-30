"""Per-statement code generation: raw driver rows -> model instances.

A generic hydrator would walk the plan and use `setattr` with a string name on
every field of every row. Since a statement's result layout is fixed and known
once, we compile a specialised function for it instead, whose field accesses are
ordinary `STORE_ATTR` bytecode against a fixed name.

Three things make the generated code fast, and all three are deliberate:

* The row tuple is unpacked by the `for` statement itself — one
  `UNPACK_SEQUENCE` per row instead of a subscript per field.
* `list.append` is bound once outside the loop.
* Field stores are written as plain attribute assignments. CPython 3.11's
  specialising interpreter (PEP 659) quickens `obj.x = v` into a cached
  `STORE_ATTR`; routing through `setattr()` or a descriptor's `__set__` is an
  ordinary call that defeats that inline cache and measures several times slower.

The generated source is attached to the returned function as `__source__`, so the
codegen stays inspectable rather than being magic.

**Type conversion comes from SQLAlchemy, not from a table here.** Each selected
column's `result_processor` is asked of the *dialect-adapted* type, so a
`DateTime` on sqlite (stored as a string) or a `Numeric` on postgres decodes
exactly as it would through `Row`. Where the driver already returns the right
Python object the processor is `None` and the field compiles to a bare store —
which is most columns on asyncpg, and why bypassing `Row` costs nothing there.
An earlier design hand-maintained `SQLITE_CONVERTERS = {bool: bool}` instead;
measured against a widened shape, that covered 1 of 8 columns that need
conversion (docs/PLAN_CORE_COMPILER.md §7 R1).
"""

from __future__ import annotations

from typing import Any

from .errors import PlanError
from .planner import Plan


def result_processor(column: Any, dialect: Any, coltype: Any) -> Any:
    """SQLAlchemy's own value decoder for one selected column, or None.

    `_cached_result_processor` rather than the public `type.result_processor`
    because the processor has to come from the *dialect's* implementation of the
    type — `sa.Numeric` becomes `_PsycopgNumeric` on psycopg, and only that
    subclass knows how to read the driver's output. This is the same call
    `DefaultExecutionContext.get_result_processor` makes, and the same memo
    dict, so it is the contract Row itself runs on.

    `coltype` is the DBAPI type code from `cursor.description`. It is not
    optional decoration: postgres `Numeric.result_processor` *raises* for an
    unknown code, which is why hydrators are planned after the first execute
    rather than at compile time.
    """
    return column.type._cached_result_processor(dialect, coltype)


def compile_hydrator(plan: Plan, dialect: Any, coltypes: list[Any]) -> Any:
    """Build a `rows -> list` function for one planned statement.

    `coltypes` are the DBAPI type codes from `cursor.description`, positionally
    aligned with `plan.columns`. Drivers that report no type code (sqlite) pass
    `None` for every column, which is exactly what SQLAlchemy passes there too.
    """
    if len(coltypes) != len(plan.columns):
        raise PlanError(
            f"the statement plans {len(plan.columns)} columns but the driver "
            f"described {len(coltypes)}; refusing to hydrate rather than "
            f"mis-assign fields"
        )

    processors = [
        result_processor(column, dialect, coltype)
        for column, coltype in zip(plan.columns, coltypes)
    ]

    namespace: dict[str, Any] = {"_new": object.__new__}
    field_vars = [f"f{i}" for i in range(len(plan.columns))]

    def read(index: int) -> str:
        """The expression yielding column `index`'s Python value."""
        processor = processors[index]
        if processor is None:
            return field_vars[index]
        name = f"_p{index}"
        namespace[name] = processor
        return f"{name}({field_vars[index]})"

    lines = [
        "def _hydrate(rows):",
        "    out = []",
        "    append = out.append",
        # The trailing comma is load-bearing: `for f0 in rows` binds each row
        # *tuple* to f0 instead of unpacking it, so a single selected column
        # would nest. `for f0, in rows` unpacks, and the comma is harmless at
        # every other arity.
        f"    for {', '.join(field_vars)}, in rows:",
    ]

    slots: list[str] = []
    offset = 0
    for position, entity in enumerate(plan.entities):
        if entity[0] == "column":
            slots.append(read(offset))
            offset += 1
            continue

        _, model_cls, pairs, nullable = entity
        target = f"o{position}"
        slots.append(target)
        namespace[f"_c{position}"] = model_cls
        mine = field_vars[offset : offset + len(pairs)]

        indent = "        "
        if nullable:
            # Reached through an OUTER join, so an all-NULL run means "no match"
            # and hydrates as None rather than an object with every field None.
            # The test is "every selected column of this entity is NULL", so an
            # entity whose columns are all genuinely NULL in the data also
            # becomes None; give such a query at least one NOT NULL column.
            lines.append(f"{indent}if {' is None and '.join(mine)} is None:")
            lines.append(f"{indent}    {target} = None")
            lines.append(f"{indent}else:")
            indent += "    "

        lines.append(f"{indent}{target} = _new(_c{position})")
        for attr, _ in pairs:
            lines.append(f"{indent}{target}.{attr} = {read(offset)}")
            offset += 1

    if plan.wrap:
        lines.append(f"        append(({', '.join(slots)},))")
    else:
        lines.append(f"        append({slots[0]})")
    lines.append("    return out")

    source = "\n".join(lines)
    exec(source, namespace)  # noqa: S102 -- our own generated source, not external input
    hydrate = namespace["_hydrate"]
    hydrate.__source__ = source
    return hydrate
