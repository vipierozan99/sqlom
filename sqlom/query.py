from __future__ import annotations

from typing import Any, Generic, Iterable, Self, TypeVar, Union, overload

from .expr import (
    CTE,
    Aggregate,
    Alias,
    ColumnExpr,
    Expression,
    ExistsClause,
    Labelled,
    Predicate,
    ScalarSubquery,
    Subquery,
    _bare,
    _collect_ctes,
    _OrderingExpr,
    and_,
    from_sql,
    source_name,
    source_prefix,
)

# How each backend spells "aggregate these rows into one JSON array", plus
# how it renders a boolean. sqlite has no bool type (columns come back as
# 0/1), so booleans need an explicit cast to get real JSON true/false.
DIALECTS: dict[str, dict[str, Any]] = {
    "sqlite": {
        "placeholder": "?",
        "agg": "json_group_array",
        "object": "json_object",
        "bool": lambda col: f"json(CASE WHEN {col} THEN 'true' ELSE 'false' END)",
        "coalesce": False,
    },
    # psycopg3 uses positional %s rather than asyncpg's numbered $1. It also
    # registers a loader for json/jsonb, so a `json` result comes back as a
    # Python list/dict — which would defeat the entire point of building the
    # JSON in the database. `text_cast` keeps it as text on the wire so the
    # engine can hand back bytes. asyncpg has no such loader and needs no cast.
    "psycopg": {
        "placeholder": "%s",
        "agg": "json_agg",
        "object": "json_build_object",
        "bool": lambda col: col,
        "coalesce": True,
        "text_cast": True,
    },
    "postgres": {
        "placeholder": "$",
        "agg": "json_agg",
        "object": "json_build_object",
        "bool": lambda col: col,
        "coalesce": True,
    },
}

# How each join kind affects nullability. "right" is the table being joined;
# "left" is everything already in the query.
JOIN_KINDS = {
    "inner": ("JOIN", False, False),
    "left": ("LEFT OUTER JOIN", True, False),
    "right": ("RIGHT OUTER JOIN", False, True),
    "full": ("FULL OUTER JOIN", True, True),
}


R = TypeVar("R")
T = TypeVar("T")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")
M = TypeVar("M")

# What may be selected, and the Python type it contributes to the row. Writing it
# as one union rather than a family of overloads per model/column combination is
# what keeps Query.__init__ to seven overloads instead of 2**n of them: a checker
# solves T independently for each argument, so `Query(User, Post.title)` gives
# `tuple[User, str]` without anyone enumerating that pairing.
_Sel = Union[type[T], "Alias[T]", "Expression[T]"]

# The operand of a set operation: another select of the same row type.
_Selectable = Union["Query[R]", "CompoundSelect[R]"]


def json_bytes(payload: Any) -> bytes:
    """Normalise a DB-side JSON result to bytes, or say why it can't be.

    `fetch_json` promises response-ready bytes — that is its whole reason to
    exist. A driver that decodes json/jsonb into a Python list would quietly
    turn it into an object-building path with an extra parse, so this fails loudly
    instead of returning the wrong type. See the `text_cast` note in DIALECTS.
    """
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    if payload is None:
        return b"[]"
    raise TypeError(
        f"expected JSON text from the database, got {type(payload).__name__}. "
        f"The driver is decoding json/jsonb into Python objects — cast the "
        f"aggregate to text (see DIALECTS['psycopg']['text_cast'])"
    )


class Query(Generic[R]):
    """A SELECT over one or more sources.

        Query(User)                                        -> [User]
        Query(User, Post).join(Post, Post.user_id == User.id)
                                                           -> [(User, Post)]
        Query(User, Post.title).join(Post, ...)             -> [(User, str)]
        Query(Post.user_id, count()).group_by(Post.user_id) -> [(int, int)]

    A multi-entity query yields tuples in select order, like SQLAlchemy's
    `select(User, Post)`. An entity that an outer join can leave unmatched arrives
    as `None`.
    """

    source: Any

    # Row type per arity. A lone model yields instances, so `Query(User)` is
    # `Query[User]`; a lone expression yields one-tuples, matching runtime and
    # SQLAlchemy's `select(col)`. Past five entities the row degrades to
    # `tuple[Any, ...]` rather than growing the list without end.
    @overload
    def __init__(self: Query[M], entity: type[M], /) -> None: ...

    @overload
    def __init__(self: Query[M], entity: Alias[M], /) -> None: ...

    @overload
    def __init__(self: Query[tuple[T]], entity: Expression[T], /) -> None: ...

    @overload
    def __init__(self: Query[tuple[T, T2]],
                 e1: _Sel[T], e2: _Sel[T2], /) -> None: ...

    @overload
    def __init__(self: Query[tuple[T, T2, T3]],
                 e1: _Sel[T], e2: _Sel[T2], e3: _Sel[T3], /) -> None: ...

    @overload
    def __init__(self: Query[tuple[T, T2, T3, T4]],
                 e1: _Sel[T], e2: _Sel[T2], e3: _Sel[T3], e4: _Sel[T4],
                 /) -> None: ...

    @overload
    def __init__(self: Query[tuple[Any, ...]],
                 *entities: _Sel[Any]) -> None: ...

    def __init__(self, *entities):
        if not entities:
            raise TypeError("Query() needs at least one model, column or aggregate")

        self._entities = [self._classify_entity(entity, "Query()") for entity in entities]

        # The primary source: the FROM table, and the default target of a bare
        # column name in order_by/group_by. Taken from the first entity that has
        # one — `count(*)` references no table, so `Query(count(), Book.id)` has
        # to look past it.
        source = None
        for kind, entity in self._entities:
            candidates = (entity,) if kind == "model" else entity.sources()
            if candidates:
                source = candidates[0]
                break
        if source is None:
            raise TypeError(
                "no table to select from — every selected entity is independent "
                "of any column (e.g. Query(count())). Select at least one column, "
                "or count a column: Query(count(Model.id))."
            )
        self.source = source

        self._conditions = []       # AND-ed predicates for WHERE
        self._having = []           # AND-ed predicates for HAVING
        self._joins = []            # (source, on, kind)
        self._group_by = []         # expressions
        self._order_by = []         # (expression, descending)
        self._limit = None
        self._offset = None
        self._distinct = False
        self._correlated = []       # extra sources allowed in predicates
        self._ctes = []             # CTEs forced in via with_(); see that method
        self._for_update = None     # (strength, kwargs) from with_for_update()
        # Compiled SQL is cached per (kind, dialect/placeholder). A hot endpoint
        # builds the same query shape on every request, and regenerating the
        # string each time is pure overhead — it measured as ~4% of throughput.
        self._sql_cache = {}
        self._hydration_key = None
        self._recompute_key()

    @staticmethod
    def _classify_entity(entity: Any, what: str) -> tuple[str, Any]:
        """Turn a raw `Query()`/`add_columns()`/`with_only_columns()` argument
        into an internal `("model" | "expr", entity)` pair."""
        if isinstance(entity, Expression):
            return ("expr", entity)
        if isinstance(entity, Subquery):
            raise TypeError(
                f"a subquery cannot be selected as a whole ({entity!r}); there "
                f"is no model to hydrate into. Select its columns: "
                f"Query({entity.alias}.some_column)"
            )
        if hasattr(entity, "__columns__"):
            return ("model", entity)
        raise TypeError(
            f"{what} takes models, aliases, columns or aggregates "
            f"(e.g. Query(User) or Query(User, Post.title)), got {entity!r}"
        )

    # ------------------------------------------------------------------ model

    @property
    def model(self):
        """The primary model class. For an aliased primary source this is the
        model behind the alias, which is what hydration needs."""
        return getattr(self.source, "model", self.source)

    def _sources(self):
        """Every source a predicate may reference."""
        return ([self.source]
                + [source for source, _, _ in self._joins]
                + list(self._correlated))

    def _nullable_sources(self):
        """Which sources an outer join can leave with no matching row.

        Only ever grows. An INNER join after an outer one arguably un-nullifies a
        source, but reasoning about that correctly is subtle, and the two error
        directions are not symmetric: over-marking costs a few redundant NULL
        checks per row, while under-marking builds an object whose every field is
        None and hands it back as real data. So this stays conservative.
        """
        nullable = set()
        for source, _, kind in self._joins:
            _, right_null, left_null = JOIN_KINDS[kind]
            if right_null:
                nullable.add(source)
            if left_null:
                # A RIGHT/FULL join can leave everything to its left unmatched.
                nullable.add(self.source)
                nullable.update(
                    previous for previous, _, _ in self._joins if previous is not source
                )
        return nullable

    def _recompute_key(self):
        """The cache key an engine uses to look up this query's hydrator.

        Deliberately just the model class whenever exactly one model is selected
        and nothing can null it, so the engine's dict lookup stays as cheap as it
        was before joins existed. A join changes the SQL, not the row shape — so a
        join used only for filtering reuses the plain single-model hydrator.
        """
        nullable = self._nullable_sources()
        if not self.is_multi_entity and self.source not in nullable:
            self._hydration_key = self.model
            return
        parts = []
        for kind, entity in self._entities:
            if kind == "model":
                parts.append(("model", getattr(entity, "model", entity),
                              entity in nullable))
            else:
                parts.append(("expr", id(entity), getattr(entity, "py_type", None)))
        self._hydration_key = tuple(parts)

    def hydration_spec(self) -> list[tuple[Any, ...]]:
        """The entity specs `compile_join_hydrator` needs, in select order."""
        nullable = self._nullable_sources()
        specs: list[tuple[Any, ...]] = []
        for kind, entity in self._entities:
            if kind == "model":
                specs.append(("model", getattr(entity, "model", entity),
                              entity in nullable))
            else:
                specs.append(("column", getattr(entity, "py_type", None)))
        return specs

    def output_columns(self) -> list[tuple[str, Any]]:
        """`(name, py_type)` per selected column — what a subquery exposes."""
        columns: list[tuple[str, Any]] = []
        for kind, entity in self._entities:
            if kind == "model":
                columns.extend(
                    (name, column.py_type) for name, column in entity.__columns__.items()
                )
            else:
                columns.append((entity.output_name(), getattr(entity, "py_type", None)))
        return columns

    @property
    def is_multi_entity(self):
        """True when rows hydrate to tuples rather than single instances."""
        return not (len(self._entities) == 1 and self._entities[0][0] == "model")

    def _invalidate(self):
        self._sql_cache.clear()
        self._recompute_key()

    # ------------------------------------------------------------------ joins

    def _add_join(self, source, on, kind):
        if isinstance(source, Expression) or not hasattr(source, "__columns__"):
            raise TypeError(
                f"join() takes a model, Alias or Subquery, got {source!r}"
            )
        if any(source is existing for existing in self._sources()):
            raise ValueError(
                f"{source_name(source)} is already in this query. To join a table "
                f"to itself, alias one side: Alias({self.model.__name__}, "
                f"'other')"
            )
        prefix = source_prefix(source)
        for existing in self._sources():
            if source_prefix(existing) == prefix:
                raise ValueError(
                    f"two sources would both render as {prefix!r}; alias one of "
                    f"them so their columns can be told apart"
                )
        if not isinstance(on, Predicate):
            raise TypeError(
                f"join() needs an ON predicate comparing columns "
                f"(e.g. Post.user_id == User.id), got {type(on).__name__}"
            )
        known = self._sources() + [source]
        for referenced in on.sources():
            if not any(referenced is candidate for candidate in known):
                raise ValueError(
                    f"ON clause references {source_name(referenced)}, which is not "
                    f"part of this query"
                )
        if not _links_sources(on, source, self._sources()):
            raise ValueError(
                f"ON clause does not compare a column of {source_name(source)} to "
                f"a column of a table already in the query, so this would be a "
                f"cross join. Got {on!r}."
            )
        self._joins.append((source, on, kind))
        self._invalidate()
        return self

    def join(self, source: Any, on: Predicate,
             isouter: bool = False, full: bool = False) -> Self:
        """INNER JOIN, or LEFT/FULL OUTER JOIN via `isouter=`/`full=`
        (SQLAlchemy's `Select.join()` keywords)."""
        kind = "full" if full else ("left" if isouter else "inner")
        return self._add_join(source, on, kind)

    def outer_join(self, source: Any, on: Predicate) -> Self:
        """LEFT OUTER JOIN. A selected right-hand entity hydrates as `None` when
        there is no match."""
        return self._add_join(source, on, "left")

    # SQLAlchemy spells LEFT OUTER JOIN without the underscore.
    outerjoin = outer_join
    left_join = outer_join

    def right_join(self, source: Any, on: Predicate) -> Self:
        """RIGHT OUTER JOIN. Note this makes the *left* side nullable, including
        the primary entity — so `Query(User, Post).right_join(Post, ...)` can yield
        `(None, post)`."""
        return self._add_join(source, on, "right")

    def full_join(self, source: Any, on: Predicate) -> Self:
        """FULL OUTER JOIN. Either side can be `None`."""
        return self._add_join(source, on, "full")

    def correlate(self, *sources: Any) -> Self:
        """Declare outer sources this query may reference as a subquery.

        A correlated subquery mentions a table from the query that contains it,
        which the inner query has no other way to know about:

            Query(User).where(exists(
                Query(Post).correlate(User).where(Post.user_id == User.id)
            ))

        Explicit rather than inferred: guessing which references are correlations
        and which are mistakes is exactly how a typo becomes a cross join.
        """
        for source in sources:
            if not hasattr(source, "__columns__"):
                raise TypeError(f"correlate() takes models or aliases, got {source!r}")
            self._correlated.append(source)
        self._invalidate()
        return self

    # ------------------------------------------------------- set operations

    def _compound(self, operator: str, other: _Selectable[R]) -> CompoundSelect[R]:
        if not hasattr(other, "output_columns"):
            raise TypeError(
                f"{operator} takes another Query or compound, got "
                f"{type(other).__name__}"
            )
        return CompoundSelect(operator, [self, other])

    def union(self, other: _Selectable[R]) -> CompoundSelect[R]:
        """`UNION` — de-duplicates, as SQL does."""
        return self._compound("UNION", other)

    def union_all(self, other: _Selectable[R]) -> CompoundSelect[R]:
        """`UNION ALL` — keeps duplicates, and is the cheaper of the two."""
        return self._compound("UNION ALL", other)

    def intersect(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._compound("INTERSECT", other)

    def intersect_all(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._compound("INTERSECT ALL", other)

    def except_(self, other: _Selectable[R]) -> CompoundSelect[R]:
        """`EXCEPT`. Named with a trailing underscore because `except` is a keyword."""
        return self._compound("EXCEPT", other)

    def except_all(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._compound("EXCEPT ALL", other)

    # ---------------------------------------------------------------- CTEs

    def cte(self, alias: str) -> CTE:
        """Wrap this query as a common table expression.

        The CTE is used as a source like a table; whichever query references it
        hoists the definition into its own WITH clause automatically, so there is
        nothing to register.
        """
        return CTE(self, alias)

    def with_(self, *ctes: CTE) -> Self:
        """Force a CTE into this query's WITH clause.

        Rarely needed: references are found wherever they appear, including inside
        a subquery, an EXISTS or another CTE. This exists for the case where the
        CTE is not referenced by anything sqlom can see — raw SQL via
        `sql_function`, say — and would otherwise be left undefined.
        """
        for entry in ctes:
            if not isinstance(entry, CTE):
                raise TypeError(f"with_() takes CTEs, got {type(entry).__name__}")
            self._ctes.append(entry)
        self._invalidate()
        return self

    def add_cte(self, *ctes: CTE, nest_here: bool = False) -> Self:
        """SQLAlchemy's name for `with_()` (`HasCTE.add_cte`). `nest_here` is
        not supported — sqlom always hoists every CTE to the outermost
        statement's WITH clause, so passing `True` raises rather than
        silently placing it somewhere else."""
        if nest_here:
            raise NotImplementedError(
                "add_cte(nest_here=True) is not supported; sqlom always "
                "hoists every CTE to the outermost statement's WITH clause"
            )
        return self.with_(*ctes)

    def subquery(self, alias: str) -> Subquery:
        """Wrap this query as a derived table usable in FROM and joins."""
        return Subquery(self, alias)

    def scalar_subquery(self) -> ScalarSubquery[Any]:
        """Use this query as a single value: in a comparison, an arithmetic or
        function operand, an `UPDATE` assignment, or (labelled) a SELECT-list
        entry.

        Returns a real `Expression`, so it composes wherever one is expected —
        rather than relying on the duck-typed `hasattr(x, "_render")` fallback
        those call sites also still accept for a bare `Query`. It exists so
        calling code reads as intended and so the one-row-one-column
        requirement has somewhere to be documented: it is the database that
        enforces it, not sqlom.
        """
        return ScalarSubquery(self)

    # ------------------------------------------------------------- predicates

    def _check(self, predicate, what):
        sources = self._sources()
        for referenced in predicate.sources():
            if not any(referenced is candidate for candidate in sources):
                raise ValueError(
                    f"{what} references {source_name(referenced)}, which is not "
                    f"part of this query (selecting from "
                    f"{', '.join(source_name(s) for s in sources)}). Add a join, "
                    f"or correlate() it if this is a subquery."
                )
        for column in _columns_in(predicate):
            if column.name not in column.source.__columns__:
                raise ValueError(
                    f"{source_name(column.source)} has no column {column.name!r}"
                )

    def where(self, *predicates: Predicate) -> Self:
        """AND-ed predicates. Use `or_()` / `and_()` / `~` for anything else.

        Several arguments are equivalent to several `where()` calls; both AND.
        """
        for predicate in predicates:
            if not isinstance(predicate, Predicate):
                raise TypeError(
                    f"where() takes a predicate built from a column comparison "
                    f"(e.g. {self.model.__name__}.id > 100), got "
                    f"{type(predicate).__name__}"
                )
            self._check(predicate, "condition")
            self._conditions.append(predicate)
        self._invalidate()
        return self

    def filter(self, *predicates: Predicate) -> Self:
        """SQLAlchemy's synonym for `where()` — the ORM `Query.filter()` name,
        also present on Core's `Select` since 1.4."""
        return self.where(*predicates)

    def filter_by(self, **kwargs: Any) -> Self:
        """Equality WHERE clauses by column name, against whichever source
        was joined in most recently (or the primary source, with no joins) —
        SQLAlchemy's convenience form: `filter_by(name="ada")` is
        `where(Model.name == "ada")` for that source.
        """
        target = self._joins[-1][0] if self._joins else self.source
        columns = getattr(target, "__columns__", None)
        if columns is None:
            raise TypeError(
                f"filter_by() needs a source with columns, got {target!r}"
            )
        conditions = []
        for name, value in kwargs.items():
            if name not in columns:
                raise ValueError(f"{source_name(target)} has no column {name!r}")
            conditions.append(ColumnExpr(target, name, columns[name].py_type) == value)
        return self.where(*conditions)

    def exists(self) -> ExistsClause:
        """SQLAlchemy's `Select.exists()` — a method form of the free
        `exists(query)` function, usable as a predicate: `Query(...).where(
        other.exists())`."""
        return ExistsClause(self)

    def having(self, *predicates: Predicate) -> Self:
        """Predicates applied after grouping, where aggregates are allowed."""
        for predicate in predicates:
            if not isinstance(predicate, Predicate):
                raise TypeError(
                    f"having() takes a predicate, got {type(predicate).__name__}"
                )
            self._check(predicate, "having clause")
            self._having.append(predicate)
        self._invalidate()
        return self

    # ----------------------------------------------------------- shaping

    def _as_expression(self, column, what):
        """Accept a ColumnExpr, an Aggregate, or a bare column-name string."""
        if isinstance(column, Expression):
            self._check_expression(column, what)
            return column
        if isinstance(column, str):
            if column not in self.source.__columns__:
                raise ValueError(
                    f"{source_name(self.source)} has no column {column!r}"
                )
            return ColumnExpr(self.source, column,
                              self.source.__columns__[column].py_type)
        raise TypeError(f"{what} takes a column or aggregate, got {column!r}")

    def _check_expression(self, expression, what):
        sources = self._sources()
        for referenced in expression.sources():
            if not any(referenced is candidate for candidate in sources):
                raise ValueError(
                    f"{what} references {source_name(referenced)}, which is not "
                    f"part of this query"
                )
        for column in _columns_in(expression):
            if column.name not in column.source.__columns__:
                raise ValueError(
                    f"{source_name(column.source)} has no column {column.name!r}"
                )

    def _check_entities(self):
        """Every selected entity's source must already be in the join graph.

        `where()`/`order_by()`/`group_by()`/`join()` all refuse a reference to
        an unknown source immediately — but the *select list itself* never
        got the same check, so `Query(Author, Book)` with no `.join()` (or
        `Query(Author.name, Book.title)`) silently rendered a FROM clause
        missing a table whose columns it was about to reference. Checked here,
        at render time, rather than in `__init__` — a join usually arrives in
        a later chained call, after the entities that need it.
        """
        sources = self._sources()
        for kind, entity in self._entities:
            if kind == "model":
                if not any(entity is candidate for candidate in sources):
                    raise ValueError(
                        f"{source_name(entity)} is selected but not part of "
                        f"this query's FROM/JOIN (selecting from "
                        f"{', '.join(source_name(s) for s in sources)}). "
                        f"Add a join linking it in."
                    )
            else:
                self._check_expression(entity, "select list")

    def group_by(self, *columns: Expression[Any] | str) -> Self:
        """GROUP BY. Takes columns or bare column-name strings.

        No attempt is made to check that every non-aggregated selected column is
        grouped — that is the database's job, and it produces a clear error.
        """
        for column in columns:
            self._group_by.append(self._as_expression(column, "group_by"))
        self._invalidate()
        return self

    def order_by(self, *columns: Expression[Any] | str | _OrderingExpr[Any],
                 descending: bool = False) -> Self:
        """Order the result set. Accepts columns, aggregates, or bare names.

        A column may also carry its own direction via `.desc()`/`.asc()`
        (SQLAlchemy's spelling), e.g. `order_by(Post.created_at.desc())` — that
        per-column direction wins over the `descending=` keyword.

        Worth being explicit about why this exists: `LIMIT` without `ORDER BY`
        returns an arbitrary subset. Postgres is free to pick a different plan
        for a differently-spelled but equivalent query and hand back different
        rows, so any query that limits without ordering is only reproducible by
        luck. Anything paginating, or comparing two implementations byte for
        byte, needs a total order.
        """
        for column in columns:
            expression: Expression[Any] | str
            if isinstance(column, _OrderingExpr):
                expression, direction = column.expression, column.descending
            else:
                expression, direction = column, descending
            self._order_by.append(
                (self._as_expression(expression, "order_by"), direction)
            )
        self._invalidate()
        return self

    def distinct(self, on: bool = True) -> Self:
        self._distinct = bool(on)
        self._invalidate()
        return self

    def add_columns(self, *entities: Any) -> Self:
        """Append more entities to the select list — SQLAlchemy's
        `Select.add_columns()`. Each new entity's source(s) must already be
        part of this query's FROM/JOIN, checked at render time by
        `_check_entities()` like every other selected entity — so a `join()`
        for the new entity's table may come before or after this call.
        """
        for entity in entities:
            self._entities.append(self._classify_entity(entity, "add_columns()"))
        self._invalidate()
        return self

    def with_only_columns(self, *entities: Any) -> Self:
        """Replace the select list entirely — SQLAlchemy's
        `Select.with_only_columns()`. The primary FROM table and any joins
        are untouched; only which columns are *returned* changes, so a
        column from a table no longer selected still renders correctly as
        long as that table is still joined in.
        """
        if not entities:
            raise TypeError("with_only_columns() needs at least one entity")
        self._entities = [
            self._classify_entity(entity, "with_only_columns()") for entity in entities
        ]
        self._invalidate()
        return self

    def limit(self, n: int) -> Self:
        # sqlite reads a negative LIMIT as "no limit" while Postgres raises, so
        # the same query would either silently return everything or blow up
        # depending on the backend. Neither is a useful thing to ship.
        self._limit = _non_negative_int(n, "limit")
        self._invalidate()
        return self

    def offset(self, n: int) -> Self:
        self._offset = _non_negative_int(n, "offset")
        self._invalidate()
        return self

    def with_for_update(self, *, nowait: bool = False, read: bool = False,
                        of: Any = None, skip_locked: bool = False,
                        key_share: bool = False) -> Self:
        """Row locking: `SELECT ... FOR UPDATE` — SQLAlchemy's
        `GenerativeSelect.with_for_update()`.

        Postgres-only; sqlite has no locking clause at all, so this is one of
        the few sqlom-generated statements that only ever makes sense against
        Postgres (same status as `DELETE ... USING`, see README §11).
        `of=` takes a model/alias or a sequence of them, restricting the lock
        to specific tables in a join (`FOR UPDATE OF t1, t2`, Postgres-only,
        same as SQLAlchemy). `key_share=True` selects the weaker
        `FOR NO KEY UPDATE`/`FOR KEY SHARE` forms (with `read=`) instead of
        `FOR UPDATE`/`FOR SHARE`. `nowait=`/`skip_locked=` are mutually
        exclusive at the database level; that isn't enforced here — the
        server rejects both together.
        """
        if read and key_share:
            strength = "FOR KEY SHARE"
        elif key_share:
            strength = "FOR NO KEY UPDATE"
        elif read:
            strength = "FOR SHARE"
        else:
            strength = "FOR UPDATE"
        self._for_update = (strength, of, nowait, skip_locked)
        self._invalidate()
        return self

    # ------------------------------------------------------------- rendering

    def _resolver(self):
        """How to render a column reference.

        With a single source, bare names — which keeps the SQL of every existing
        single-table query byte-identical. With more than one, qualify by table
        name or alias, because `id` would otherwise be ambiguous.
        """
        if not self._joins and not self._correlated:
            return _bare
        return lambda source, name: f"{source_prefix(source)}.{name}"

    def _select_list(self, nxt, resolve):
        parts, params = [], ()
        for kind, entity in self._entities:
            if kind == "model":
                parts.extend(resolve(entity, name) for name in entity.__columns__)
            else:
                render = getattr(entity, "select_sql", entity.to_sql)
                sql, entity_params = render(nxt, resolve)
                parts.append(sql)
                params += entity_params
        return parts, params

    def _render(self, nxt=None, resolve=None, with_clause=False):
        """Build the whole statement. `nxt` is the shared placeholder generator, so
        a subquery's parameters number in sequence with the outer query's.

        `with_clause` defaults to False because only the outermost render of a
        statement emits `WITH`, and it emits every CTE in the graph. A nested
        render — a derived table, a scalar subquery, an EXISTS, a compound operand,
        a CTE body — must not emit one, or the same CTE gets defined twice.
        """
        self._check_entities()
        params = []
        if nxt is None:
            def _nxt():
                return "?"
            nxt = _nxt
        if resolve is None:
            resolve = self._resolver()

        def advance():
            placeholder = nxt()
            return placeholder

        # WITH comes first in the statement, so its parameters must number first.
        with_sql = _with_clause(self, advance, params) if with_clause else ""

        select_parts, select_params = self._select_list(advance, resolve)
        params.extend(select_params)
        prefix = "SELECT DISTINCT " if self._distinct else "SELECT "
        sql = with_sql + prefix + ", ".join(select_parts)

        from_sql_text, from_params = from_sql(self.source, advance)
        params.extend(from_params)
        sql += f" FROM {from_sql_text}"

        for source, on, kind in self._joins:
            keyword, _, _ = JOIN_KINDS[kind]
            joined_text, joined_params = from_sql(source, advance)
            params.extend(joined_params)
            clause, clause_params = on.to_sql(advance, resolve)
            params.extend(clause_params)
            sql += f" {keyword} {joined_text} ON {clause}"

        if self._conditions:
            # Each condition is rendered separately and joined with AND rather
            # than wrapped in one BooleanClause. A compound predicate already
            # brings its own brackets, and this keeps `WHERE a AND b` free of the
            # redundant outer pair — which matters only because it keeps the SQL
            # of every previously benchmarked query byte-identical.
            sql += " WHERE " + _and_join(self._conditions, advance, resolve, params)

        if self._group_by:
            terms = []
            for expression in self._group_by:
                text, group_params = expression.to_sql(advance, resolve)
                params.extend(group_params)
                terms.append(text)
            sql += " GROUP BY " + ", ".join(terms)

        if self._having:
            sql += " HAVING " + _and_join(self._having, advance, resolve, params)

        if self._order_by:
            terms = []
            for expression, descending in self._order_by:
                text, order_params = expression.to_sql(advance, resolve)
                params.extend(order_params)
                terms.append(f"{text} DESC" if descending else text)
            sql += " ORDER BY " + ", ".join(terms)

        if self._limit is not None:
            sql += f" LIMIT {advance()}"
            params.append(self._limit)
        if self._offset is not None:
            sql += f" OFFSET {advance()}"
            params.append(self._offset)

        if self._for_update is not None:
            strength, of, nowait, skip_locked = self._for_update
            sql += f" {strength}"
            if of is not None:
                of_sources = of if isinstance(of, (list, tuple)) else [of]
                sql += " OF " + ", ".join(source_prefix(s) for s in of_sources)
            if nowait:
                sql += " NOWAIT"
            elif skip_locked:
                sql += " SKIP LOCKED"

        return sql, params

    def to_sql(self, placeholder: str = "?") -> tuple[str, tuple[Any, ...]]:
        """Return `(sql, params)`. `params` is a **tuple**.

        Deliberately immutable: the result is cached and the same object is
        handed to every caller, so a mutable list would let one caller alter the
        bindings every later execution of this query uses, while the SQL string
        stayed the same. Copying per call would be safe too, but this is on the
        hot path — a tuple is safe *and* free. Every driver here accepts one
        (sqlite3 and psycopg take a sequence, asyncpg is splatted).
        """
        cached = self._sql_cache.get(("select", placeholder))
        if cached is not None:
            return cached
        sql, params = self._render(_placeholders(placeholder), with_clause=True)
        result = (sql, tuple(params))
        self._sql_cache[("select", placeholder)] = result
        return result

    def to_json_sql(self, dialect: str = "postgres") -> tuple[str, tuple[Any, ...]]:
        """Build SQL that returns the whole result set as a single JSON array.

        This pushes row shaping and JSON encoding into the database, so Python
        never materializes per-row objects or dicts — the driver hands back one
        string that can go straight into the HTTP response body.
        """
        cached = self._sql_cache.get(("json", dialect))
        if cached is not None:
            return cached
        # DB-side JSON builds one flat object per row from one table's columns.
        # Shaping a join into nested JSON is a different feature (and a different
        # decision about what the shape should be), so refuse rather than emit
        # something plausible and wrong.
        if self._joins or self.is_multi_entity or self._group_by:
            raise NotImplementedError(
                "to_json_sql() supports a single-model query with no joins or "
                "grouping; this query selects "
                f"{len(self._entities)} entities with {len(self._joins)} join(s). "
                "Use fetch_all() and serialize in Python."
            )
        spec = DIALECTS[dialect]
        columns = self.source.__columns__

        object_args = ", ".join(
            f"'{name}', {spec['bool'](name) if column.py_type is bool else name}"
            for name, column in columns.items()
        )

        # The WITH clause stays *inside* the derived table rather than being
        # hoisted in front of the outer SELECT. Both dialects allow that, and it
        # keeps the parameters in textual order, which is what positional
        # placeholders require.
        inner, params = self._render(
            _placeholders(spec["placeholder"]), with_clause=True
        )

        agg = f"{spec['agg']}({spec['object']}({object_args}))"
        if spec["coalesce"]:
            agg = f"coalesce({agg}, '[]'::json)"
        if spec.get("text_cast"):
            agg = f"({agg})::text"

        result = (f"SELECT {agg} FROM ({inner}) AS t", tuple(params))
        self._sql_cache[("json", dialect)] = result
        return result

    def __repr__(self):
        entities = ", ".join(
            source_name(e) if k == "model" else repr(e) for k, e in self._entities
        )
        return f"<Query {entities}{' +joins' if self._joins else ''}>"


def _with_clause(node, advance, params):
    """Render the `WITH ...` prefix for a statement, appending its parameters.

    Returns `""` when nothing references a CTE, so a query that uses none is
    byte-identical to what it rendered before CTEs existed.
    """
    ctes = _collect_ctes(node)
    if not ctes:
        return ""
    entries = []
    for cte in ctes:
        definition, cte_params = cte.definition_sql(advance)
        params.extend(cte_params)
        entries.append(definition)
    # RECURSIVE is a property of the WITH clause, not of one entry, so a single
    # recursive CTE marks the whole clause. Non-recursive entries are unaffected.
    recursive = "RECURSIVE " if any(cte.recursive for cte in ctes) else ""
    return f"WITH {recursive}" + ", ".join(entries) + " "


def _and_join(predicates, advance, resolve, params):
    fragments = []
    for predicate in predicates:
        fragment, predicate_params = predicate.to_sql(advance, resolve)
        params.extend(predicate_params)
        fragments.append(fragment)
    return " AND ".join(fragments)


def _links_sources(predicate, joined, existing):
    """Does this ON predicate compare a column of `joined` to a column of one of
    `existing`?

    Without such a link the join is a cross product with a filter — legal SQL,
    almost never what was meant, and catastrophic on a large table. Compound ON
    clauses are allowed (`and_(Post.user_id == User.id, Post.published == True)`),
    so this looks for the link anywhere in the tree rather than requiring the
    whole clause to be one comparison.
    """
    stack = [predicate]
    while stack:
        node = stack.pop()
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        if isinstance(left, Expression) and isinstance(right, Expression):
            # Either side may be an expression rather than a bare column —
            # `Node.id == tree.parent_id + 1` links the two tables just as much as
            # `Node.id == tree.parent_id` does — so this compares the *sets* of
            # sources each side reaches.
            pair = {id(column.source) for column in _columns_in(left)}
            pair |= {id(column.source) for column in _columns_in(right)}
            if id(joined) in pair and any(id(s) in pair for s in existing):
                return True
        for child in (left, right, getattr(node, "part", None)):
            if isinstance(child, Expression):
                stack.append(child)
        stack.extend(getattr(node, "parts", ()))
    return False


def _placeholders(placeholder):
    """Return a `nxt()` that yields placeholders in the backend's style.

    "$" means asyncpg-style numbering ($1, $2, ...). "?" (sqlite) and "%s"
    (psycopg) are positional and repeat unchanged. Numbering here rather than
    post-processing with a regex avoids a substitution pass over the SQL.
    """
    if placeholder == "$":
        counter = [0]

        def nxt():
            counter[0] += 1
            return f"${counter[0]}"

        return nxt
    return lambda: placeholder


def _non_negative_int(n, what):
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"{what}() takes an int, got {type(n).__name__}")
    if n < 0:
        raise ValueError(f"{what}() must be >= 0, got {n}")
    return n


def _columns_in(expression):
    """Every ColumnExpr reachable from an expression, for validation."""
    found = []
    stack = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, ColumnExpr):
            found.append(node)
        elif isinstance(node, Labelled):
            stack.append(node.expr)
        elif isinstance(node, Aggregate):
            if node.operand is not None:
                stack.append(node.operand)
        else:
            for attribute in ("left", "right", "part"):
                child = getattr(node, attribute, None)
                if isinstance(child, Expression):
                    stack.append(child)
            for child in getattr(node, "parts", ()):
                stack.append(child)
    return found


SET_OPERATORS = {
    "union": "UNION",
    "union_all": "UNION ALL",
    "intersect": "INTERSECT",
    "intersect_all": "INTERSECT ALL",
    "except_": "EXCEPT",
    "except_all": "EXCEPT ALL",
}


class CompoundSelect(Generic[R]):
    """Two or more SELECTs joined by UNION / INTERSECT / EXCEPT.

    Presents the same surface the engines use — `to_sql`, `_hydration_key`,
    `hydration_spec`, `is_multi_entity`, `model` — so `fetch_all` needs no special
    case and rows hydrate exactly as they do for a single select.

    The row shape comes from the *first* operand. SQL requires every operand to
    agree on column count and compatible types, and this checks the count, since a
    mismatch there is a confusing server error rather than an obvious one. Type
    compatibility is left to the database.

    `ORDER BY`, `LIMIT` and `OFFSET` apply to the whole compound and are rendered
    after the last operand. They reference output column *names*, not
    table-qualified ones — a compound has no single table to qualify against — so
    ordering takes a column of the first operand or a bare string.
    """

    def __init__(self, operator: str, operands: Any) -> None:
        self.operator = operator
        self.operands = list(operands)
        if len(self.operands) < 2:
            raise ValueError("a compound select needs at least two operands")
        widths = {len(operand.output_columns()) for operand in self.operands}
        if len(widths) != 1:
            raise ValueError(
                f"every operand of a {operator} must select the same number of "
                f"columns; got widths {sorted(widths)}"
            )
        self._order_by: list[tuple[Any, bool]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._ctes: list[CTE] = []
        self._sql_cache: dict[Any, Any] = {}

    # --- the interface the engines rely on ---------------------------------

    @property
    def model(self) -> Any:
        return self.operands[0].model

    @property
    def is_multi_entity(self) -> bool:
        return self.operands[0].is_multi_entity

    @property
    def _hydration_key(self) -> Any:
        return self.operands[0]._hydration_key

    def hydration_spec(self) -> list[tuple[Any, ...]]:
        return self.operands[0].hydration_spec()

    def output_columns(self) -> list[tuple[str, Any]]:
        return self.operands[0].output_columns()

    # --- building ----------------------------------------------------------

    def _combine(self, operator: str, other: _Selectable[R]) -> CompoundSelect[R]:
        # Chaining the same operator extends this compound; a different one nests,
        # because UNION and EXCEPT do not associate the way that would imply.
        if operator == self.operator:
            return CompoundSelect(operator, [*self.operands, other])
        return CompoundSelect(operator, [self, other])

    def union(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._combine("UNION", other)

    def union_all(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._combine("UNION ALL", other)

    def intersect(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._combine("INTERSECT", other)

    def except_(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._combine("EXCEPT", other)

    def intersect_all(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._combine("INTERSECT ALL", other)

    def except_all(self, other: _Selectable[R]) -> CompoundSelect[R]:
        return self._combine("EXCEPT ALL", other)

    def cte(self, alias: str) -> CTE:
        """Wrap this compound select as a common table expression. See
        `Query.cte()` — the same rule applies: output column names come from the
        first operand."""
        return CTE(self, alias)

    def subquery(self, alias: str) -> Subquery:
        """Wrap this compound select as a derived table, usable in FROM and
        joins. See `Query.subquery()`."""
        return Subquery(self, alias)

    def with_(self, *ctes: CTE) -> Self:
        """Force a CTE into this compound's WITH clause. See `Query.with_()`."""
        for entry in ctes:
            if not isinstance(entry, CTE):
                raise TypeError(f"with_() takes CTEs, got {type(entry).__name__}")
            self._ctes.append(entry)
        self._sql_cache.clear()
        return self

    def add_cte(self, *ctes: CTE, nest_here: bool = False) -> Self:
        """SQLAlchemy's name for `with_()`. See `Query.add_cte()`."""
        if nest_here:
            raise NotImplementedError(
                "add_cte(nest_here=True) is not supported; sqlom always "
                "hoists every CTE to the outermost statement's WITH clause"
            )
        return self.with_(*ctes)

    def order_by(self, *columns: Any, descending: bool = False) -> Self:
        for column in columns:
            if isinstance(column, _OrderingExpr):
                expression, direction = column.expression, column.descending
            else:
                expression, direction = column, descending
            name = (expression if isinstance(expression, str)
                     else getattr(expression, "name", None))
            if name is None:
                raise TypeError(
                    f"a compound select orders by output column name; got {expression!r}"
                )
            known = {output for output, _ in self.output_columns()}
            if name not in known:
                raise ValueError(
                    f"{name!r} is not an output column of this compound "
                    f"({', '.join(sorted(known))})"
                )
            self._order_by.append((name, direction))
        self._sql_cache.clear()
        return self

    def limit(self, n: int) -> Self:
        self._limit = _non_negative_int(n, "limit")
        self._sql_cache.clear()
        return self

    def offset(self, n: int) -> Self:
        self._offset = _non_negative_int(n, "offset")
        self._sql_cache.clear()
        return self

    # --- rendering ---------------------------------------------------------

    def _render(self, nxt=None, resolve=None, with_clause=False):
        if nxt is None:
            def _nxt():
                return "?"
            nxt = _nxt
        parts, params = [], []
        # One WITH clause in front of the whole compound, covering every operand.
        # An operand cannot carry its own: `SELECT ... UNION WITH x AS (...)` is
        # not valid SQL, and even in the first position it would scope a CTE the
        # later operands also reference.
        with_sql = _with_clause(self, nxt, params) if with_clause else ""
        for operand in self.operands:
            sql, operand_params = operand._render(nxt)
            params.extend(operand_params)
            parts.append(sql)
        sql = with_sql + f" {self.operator} ".join(parts)
        if self._order_by:
            terms = ", ".join(
                f"{name} DESC" if descending else name
                for name, descending in self._order_by
            )
            sql += f" ORDER BY {terms}"
        if self._limit is not None:
            sql += f" LIMIT {nxt()}"
            params.append(self._limit)
        if self._offset is not None:
            sql += f" OFFSET {nxt()}"
            params.append(self._offset)
        return sql, params

    def to_sql(self, placeholder: str = "?") -> tuple[str, tuple[Any, ...]]:
        cached = self._sql_cache.get(placeholder)
        if cached is not None:
            return cached
        sql, params = self._render(_placeholders(placeholder), with_clause=True)
        result = (sql, tuple(params))
        self._sql_cache[placeholder] = result
        return result

    def __repr__(self) -> str:
        return f"<CompoundSelect {self.operator} x{len(self.operands)}>"


def select(*entities: Any) -> Query[Any]:
    """SQLAlchemy-style constructor for `Query` — `select(User, Post.title)` is
    exactly `Query(User, Post.title)`. The class stays the public name too, since
    subquery/CTE/type annotations read better as `Query[...]`."""
    return Query(*entities)
