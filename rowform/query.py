"""Compile a statement once with SQLAlchemy, then run it on the raw driver.

SQLAlchemy Core is the compiler and the schema; it is not on the row path. A
`CoreQuery` holds the compiled SQL string, the recipe for turning keyword
arguments into the driver's parameter shape, and (once the driver has described
its result) the generated hydrator.

    users = engine.prepare(sa.select(User).where(User.id > sa.bindparam("min")))
    rows = await engine.fetch_all(users, min=100)

Hoisting `prepare()` out of the request is the fast path, but it is an
optimisation rather than a requirement: passing a bare statement to `fetch_all`
looks it up in the engine's cache under SQLAlchemy's own structural cache key.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from .compile import compile_hydrator
from .planner import Plan, plan

R = TypeVar("R")


class CoreQuery(Generic[R]):
    """One statement, compiled for one dialect."""

    __slots__ = ("_compiled", "_expanding", "_hydrate", "_keys", "_plan", "_positional", "sql")

    def __init__(self, statement: Any, dialect: Any):
        compiled = self._compiled = statement.compile(dialect=dialect)
        # For an expanding statement this is a template with a POSTCOMPILE
        # placeholder in it, not something to execute; `bind()` returns the real
        # string. Kept because it is still the right cache identity and the right
        # thing to show in a repr.
        self.sql: str = compiled.string
        self._positional: bool = dialect.positional
        self._keys: tuple[str, ...] = tuple(compiled.positiontup or ())
        self._expanding: bool = bool(
            compiled.post_compile_params or compiled.literal_execute_params
        )
        self._plan: Plan | None = plan(statement) if _returns_rows(statement) else None
        self._hydrate: Any = None

    @property
    def returns_rows(self) -> bool:
        return self._plan is not None

    @property
    def entities(self) -> Plan | None:
        """What this statement's rows hydrate into; None for a write with no
        RETURNING."""
        return self._plan

    def bind(self, params: dict[str, Any] | None = None) -> tuple[str, Any]:
        """Keyword arguments -> `(sql, parameters)` in the driver's own shape.

        Three things happen here, and skipping any of them produces a statement
        that runs and is wrong:

        * **Bind processors.** Values are encoded by the same
          `type._cached_bind_processor(dialect)` the Core execution path uses, so
          a `Decimal`, `UUID`, `dict` or `datetime` reaches sqlite in the form its
          driver accepts — and, symmetrically, in the form `compile.py`'s result
          processors expect to decode on the way back.
        * **Post-compile expansion.** `User.id.in_([1, 2, 3])` compiles to a
          single `IN (__[POSTCOMPILE_id_1])` placeholder that is only turned into
          `IN (?, ?, ?)` once the values are known. That rewrites the SQL string,
          which is why this returns the statement rather than relying on
          `self.sql`.
        * **Parameter shape.** A tuple for the positional paramstyles (`qmark`,
          `numeric_dollar`, `format`), a dict for the named ones (`pyformat`,
          `named`), decided by the dialect rather than per driver.

        The order of a positional tuple always comes from `positiontup`, never
        from the parameters you think you passed: Core emits `LIMIT ? OFFSET ?`
        for a bare `.limit()`, supplying an OFFSET of 0 nobody asked for.
        """
        compiled = self._compiled

        if self._expanding:
            state = compiled.construct_expanded_state(params or None, escape_names=False)
            return state.statement, self._shape(
                state.parameters,
                {**compiled._bind_processors, **state.processors},
                tuple(state.positiontup or ()),
            )

        values = compiled.construct_params(params or None, escape_names=False)
        return self.sql, self._shape(values, compiled._bind_processors, self._keys)

    def _shape(self, values: dict[str, Any], processors: Any, keys: tuple[str, ...]) -> Any:
        if self._positional:
            return tuple(
                processors[key](values[key]) if key in processors else values[key]
                for key in keys
            )
        escaped = self._compiled.escaped_bind_names
        return {
            escaped.get(key, key) if escaped else key: (
                processors[key](value) if key in processors else value
            )
            for key, value in values.items()
        }

    def hydrator(self, dialect: Any, description: Any) -> Any:
        """The generated `rows -> list` function, built on first use.

        Deferred to here rather than to `__init__` because each column's
        `result_processor` needs the DBAPI type code the driver reports, and
        postgres `Numeric` raises without it (see `compile.result_processor`).
        The result is cached on this query, so the cost is once per statement
        rather than once per request.
        """
        hydrate = self._hydrate
        if hydrate is None:
            assert self._plan is not None
            hydrate = self._hydrate = compile_hydrator(
                self._plan, dialect, [column[1] for column in description]
            )
        return hydrate

    def __repr__(self) -> str:
        return f"<CoreQuery {self.sql!r} {self._plan!r}>"


def _returns_rows(statement: Any) -> bool:
    """A SELECT, or a write with RETURNING. Anything else hydrates to nothing."""
    return bool(getattr(statement, "is_select", False) or getattr(statement, "_returning", None))
