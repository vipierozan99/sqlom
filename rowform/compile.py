"""Per-statement code generation: driver rows -> model instances.

One function is generated per statement shape so each field store is a plain
`STORE_ATTR` against a fixed name, which CPython's specialising interpreter
quickens; `setattr`/descriptor calls defeat that. The source is kept on the
function as `__source__`.

Type conversion is SQLAlchemy's: each column's `result_processor` is asked of
the dialect-adapted type and inlined, so sqlite's string `DateTime` or
postgres's `Numeric` decode exactly as they would through `Row`.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import PlanError
from .planner import Plan

_LOG = logging.getLogger("rowform")


def result_processor(column: Any, dialect: Any, coltype: Any) -> Any:
    """SQLAlchemy's own value decoder for one selected column, or None.

    `_cached_result_processor` (private) because the processor must come from the
    dialect's implementation of the type — `sa.Numeric` is `_PsycopgNumeric` on
    psycopg. `coltype` is the DBAPI type code: postgres `Numeric` raises without
    one, which is why hydrators are built after the first execute.
    """
    return column.type._cached_result_processor(dialect, coltype)


def compile_hydrator(plan: Plan, dialect: Any, coltypes: list[Any]) -> Any:
    """Build a `rows -> list` function for one planned statement. `coltypes` are the
    DBAPI type codes from `cursor.description`, aligned with `plan.columns`; sqlite
    reports `None` for every column.
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
        # The trailing comma is load-bearing: `for f0, in rows` unpacks a 1-tuple.
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
            # Reached through an OUTER join: all-NULL means "no match" -> None. A row
            # whose columns are all genuinely NULL also becomes None.
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
    _LOG.debug("hydrator built:\n%s", source)
    return hydrate
