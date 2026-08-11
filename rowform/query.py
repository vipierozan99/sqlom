"""Compile a statement once with SQLAlchemy, then run it on the raw driver.

SQLAlchemy Core is the compiler and the schema; it is not on the row path. A
`CoreQuery` holds the compiled SQL string, the recipe for turning keyword
arguments into the driver's parameter shape, and (once the driver has described
its result) the generated hydrator.

    users = db.prepare(sa.select(User).where(User.id > sa.bindparam("min")))
    rows = await db.fetch_all(users, min=100)

Hoisting `prepare()` out of the request is the fast path, but it is an
optimisation rather than a requirement: passing a bare statement to `fetch_all`
looks it up in the engine's cache under SQLAlchemy's own structural cache key.
"""

from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

from sqlalchemy import Select
from sqlalchemy.engine.result import SimpleResultMetaData

from .compile import compile_hydrator
from .errors import PlanError
from .planner import Plan, plan

_LOG = logging.getLogger("rowform")

R = TypeVar("R")


class CoreQuery(Generic[R]):
    """One statement, compiled for one dialect."""

    __slots__ = (
        "_compiled",
        "_expanding",
        "_hydrate",
        "_keys",
        "_metadata",
        "_plan",
        "_positional",
        "dialect",
        "is_select",
        "sql",
    )

    def __init__(self, statement: Any, dialect: Any):
        #: The dialect this statement was compiled for. Kept so an engine can
        #: refuse a `CoreQuery` prepared for another driver — its SQL carries the
        #: wrong paramstyle and would die with a cryptic driver error (`_query_for`).
        self.dialect = dialect
        # Compiling *with* the cache key is what later lets `bind()` accept
        # another statement's literals: it records which bind parameters were
        # abstracted away by the key, so they can be substituted per call.
        # Without it SQLAlchemy refuses `extracted_parameters` outright.
        cache_key = statement._generate_cache_key()
        compiled = self._compiled = (
            statement.compile(dialect=dialect, cache_key=cache_key)
            if cache_key is not None
            else statement.compile(dialect=dialect)
        )
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
        self._metadata: Any = None
        #: A SELECT, as opposed to a write with RETURNING. Recorded because
        #: postgres will only `DECLARE` a cursor for the former, which decides
        #: whether the psycopg driver can stream this statement at all.
        self.is_select: bool = bool(getattr(statement, "is_select", False))
        # Once per statement, not per execute: the compile is the cached thing, so
        # this is also the log line that shows whether caching is working.
        _LOG.debug("compiled: %s", self.sql)

    @property
    def returns_rows(self) -> bool:
        return self._plan is not None

    @property
    def result_metadata(self) -> Any:
        """Column labels for `conn.execute()`'s `Result`, built once.

        SQLAlchemy caches `CursorResultMetaData` on the compiled object for the
        same reason — it caches `CursorResultMetaData` on the compiled object:
        it is a function of the statement, not of the call, and constructing one
        per execute measured at ~1.4 us where reusing one is free. Verified safe to share across results,
        including with a `.mappings()` in between.
        """
        metadata = self._metadata
        if metadata is None:
            from .result import keys_for

            if self._plan is None:
                raise PlanError("this statement returns no rows; it has no result metadata")
            metadata = self._metadata = SimpleResultMetaData(keys_for(self._plan))
        return metadata

    @property
    def entities(self) -> Plan | None:
        """What this statement's rows hydrate into; None for a write with no
        RETURNING."""
        return self._plan

    def bind(
        self, params: dict[str, Any] | None = None, extracted: Any = None
    ) -> tuple[str, Any]:
        """Keyword arguments -> `(sql, parameters)` in the driver's own shape.

        `extracted` carries the *calling* statement's literal values, and is not
        optional when this query came out of a cache. Two statements that differ
        only in their literals share one structural cache key — that is the point
        of compiling once — so the compiled object holds whichever literals the
        first one happened to have. `insert(...).values(id=51)` executed against a
        query compiled from `values(id=50)` would silently insert 50 again.
        SQLAlchemy's own answer is `CacheKey.bindparams`, which is what the engine
        passes here.

        Three more things happen, and skipping any of them produces a statement
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
        values = compiled.construct_params(
            params or None, extracted_parameters=extracted, escape_names=False
        )

        if self._expanding:
            state = compiled._process_parameters_for_postcompile(values)
            return state.statement, self._shape(
                state.parameters,
                {**compiled._bind_processors, **state.processors},
                tuple(state.positiontup or ()),
            )

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


def _one_row(statement: Any) -> Any:
    """`statement`, narrowed to a single row where that is safe to do.

    `fetch_one` read the whole result and threw away everything after the first
    row, so "get me this user" transferred and hydrated the entire table. Adding
    the LIMIT is the fix, but only for a `Select` that sets none of its own:

    * a caller's `.limit()` may be a bind parameter, and replacing it would leave
      their value with nothing to bind to;
    * a `CoreQuery` is already compiled, so there is no statement left to narrow —
      hoist it with `.limit(1)` already applied if you want that.

    An OFFSET without a LIMIT is narrowed too: the first row of *that* statement
    is still what the caller asked for.

    `_limit_clause` is SQLAlchemy-private, like the rest of the compiler surface
    this library reads; there is no public way to ask a Select whether it is
    limited.
    """
    if isinstance(statement, Select) and statement._limit_clause is None:
        return statement.limit(1)
    return statement
