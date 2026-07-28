"""Expressions: columns, aliases, aggregates, and the predicate tree.

Everything here renders to a SQL fragment plus the parameters it binds. Two
protocol methods carry all of it:

    to_sql(nxt, resolve) -> (fragment, params)
    sources()            -> the table sources the fragment references

`nxt()` yields the next placeholder — a callable rather than a string because a
predicate tree binds an unknown number of parameters and numbering has to stay in
step across the whole render. `resolve(source, name)` renders a column reference,
qualified or bare depending on whether the query has more than one source.

**Typing.** `Expression` is generic in the Python type it produces, so
`ColumnExpr[int]` compares against ints and `Query(User, Post.title)` resolves to
`list[tuple[User, str]]`. The comparison operators deliberately narrow their
right-hand side to that type, which is what makes `User.id > "abc"` an error rather
than a runtime surprise. See tests/typing/.

**"Source" rather than "model"** is the load-bearing idea. A column belongs to a
model class, an `Alias` of one, or a `Subquery` — and once aliases exist, the model
class alone cannot identify which table a column came from, because a self-join has
the same model twice. Every place that used to compare model classes now compares
sources by identity.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    Iterable,
    Protocol,
    TypeVar,
    Union,
    overload,
)
from typing import cast as _type_narrow

from .dialects import current_dialect

if TYPE_CHECKING:
    from .query import CompoundSelect, Query

    # What a CTE or a derived table may wrap: a select, or a set operation over
    # selects. Spelled here rather than imported from query.py because that module
    # imports this one.
    Select = Union["Query[Any]", "CompoundSelect[Any]"]

T = TypeVar("T")
M = TypeVar("M")


class _TableSource(Protocol):
    """Runtime column map shared by ModelMeta classes and @model dataclasses."""

    __columns__: dict[str, Any]
    __tablename__: str


# `x = NULL` is never true in SQL — comparison against NULL yields unknown, so a
# row is neither matched nor excluded. Equality against None becomes IS / IS NOT,
# which also binds no parameter.
_NULL_FORMS = {"=": "IS NULL", "!=": "IS NOT NULL"}


def _bare(source, name):
    """Default column renderer: the bare name, no qualifier.

    A single-source query needs no qualifier, and adding one would change the SQL
    every existing benchmark emits. `Query` swaps in a qualifying resolver as soon
    as a second source makes bare names ambiguous.
    """
    return name


# --------------------------------------------------------------------------
# Sources: what can appear after FROM or JOIN
# --------------------------------------------------------------------------


def source_prefix(source):
    """What qualifies a column reference for this source."""
    alias = getattr(source, "alias", None)
    return alias if alias is not None else source.__tablename__


def source_name(source):
    """A human name for error messages."""
    alias = getattr(source, "alias", None)
    if alias is None:
        return source.__name__
    # The isinstance checks come first because both of these resolve unknown
    # attributes to output columns, so `hasattr(source, "model")` is True for a
    # derived table that happens to expose a column called `model`.
    if isinstance(source, CTE):
        return f"CTE {alias}"
    if isinstance(source, Subquery):
        return f"subquery {alias}"
    return f"{source.model.__name__} AS {alias}"


class Alias(Generic[M]):
    """An aliased reference to a model, which is what makes a self-join possible.

        mgr = Alias(Employee, "mgr")
        Query(Employee, mgr).join(mgr, Employee.manager_id == mgr.id)

    Columns are reached off the alias (`mgr.id`), and they carry the alias — not the
    model — as their source, so the two sides of a self-join stay distinguishable
    all the way through rendering.
    """

    __slots__ = ("model", "alias", "__columns__", "__tablename__")

    if TYPE_CHECKING:
        model: type[M]
        alias: str
        __columns__: dict[str, Any]
        __tablename__: str

    def __init__(self, model: type[M], alias: str) -> None:
        if not hasattr(model, "__columns__"):
            raise TypeError(f"Alias() takes a model, got {model!r}")
        if not alias or not isinstance(alias, str):
            raise TypeError("Alias() needs a non-empty string alias")
        self.model = model
        self.alias = alias
        table = _type_narrow(_TableSource, model)
        self.__columns__ = table.__columns__
        self.__tablename__ = table.__tablename__

    def __getattr__(self, name: str) -> ColumnExpr[Any]:
        # Statically this is ColumnExpr[Any]: an alias resolves columns from the
        # model's runtime column map, and a type checker has no way to know which
        # names exist or what they hold. Reaching a column off the model instead
        # (`Employee.id`) keeps the precise type; off an alias you trade that for
        # the self-join.
        columns = self.__columns__
        if name in columns:
            return ColumnExpr(self, name, columns[name].py_type)
        raise AttributeError(
            f"{self.model.__name__} AS {self.alias!r} has no column {name!r}"
        )

    def from_sql(self):
        return f"{self.__tablename__} AS {self.alias}", ()

    def __repr__(self):
        return f"<Alias {self.model.__name__} AS {self.alias}>"


def _named_output_columns(query: Any, kind: str, alias: str) -> Any:
    """The inner query's output columns, refusing anything SQL will not name.

    A derived table is referenced by column *name*, so every entity it selects
    must have one: a model contributes its columns, a `ColumnExpr` its own name, a
    `Labelled` its label. An unlabelled aggregate has no SQL name — Postgres calls
    `count(id)` "count" and sqlite calls it "count(id)" — so exposing a guessed
    name would render a reference to a column that does not exist. Requiring
    `.label()` is the only honest option.

    `query` may be a compound select (`UNION`/`INTERSECT`/`EXCEPT`) rather than a
    plain `Query` — a compound's output names come from its *first* operand only,
    same rule SQL itself uses, so the entity check walks down to that leaf.
    """
    base = query
    while not hasattr(base, "_entities"):
        base = base.operands[0]
    for entity_kind, entity in base._entities:
        if entity_kind == "model":
            continue
        if isinstance(entity, (ColumnExpr, Labelled)):
            continue
        raise ValueError(
            f"{kind} {alias!r} selects {entity!r}, which SQL gives no usable "
            f"column name. Add .label('name') so it can be referenced."
        )
    return query.output_columns()


class _SubqueryColumn:
    """Stands in for a `Column` on a subquery's output, so a `Subquery` can be
    used everywhere a model source can."""

    __slots__ = ("name", "py_type", "_storage_name")

    def __init__(self, name, py_type):
        self.name = name
        self.py_type = py_type
        self._storage_name = name


class Subquery:
    """A derived table: `(SELECT ...) AS alias`, usable in FROM and in joins.

    Built by `Query.subquery("name")`. Its columns are the inner query's output
    names, so `sub.total` works. It is a *source*, not a select entity — there is
    no model to hydrate into, so select its columns individually.
    """

    __slots__ = ("query", "alias", "__columns__", "__tablename__")

    def __init__(self, query: Select, alias: str) -> None:
        if not alias or not isinstance(alias, str):
            raise TypeError("subquery() needs a non-empty string alias")
        self.query = query
        self.alias = alias
        self.__tablename__ = None
        self.__columns__ = {
            name: _SubqueryColumn(name, py_type)
            for name, py_type in _named_output_columns(query, "subquery", alias)
        }

    def __getattr__(self, name: str) -> ColumnExpr[Any]:
        # ColumnExpr[Any]: a derived table's output names come from the inner
        # query's entity list at runtime, which a checker cannot enumerate.
        columns = self.__columns__
        if name in columns:
            return ColumnExpr(self, name, columns[name].py_type)
        raise AttributeError(
            f"subquery {self.alias!r} has no output column {name!r}; it exposes "
            f"{', '.join(columns) or '(nothing)'}"
        )

    def from_sql(self, nxt=None):
        sql, params = self.query._render(nxt)
        return f"({sql}) AS {self.alias}", params

    def __repr__(self):
        return f"<Subquery AS {self.alias}>"


class CTE:
    """A common table expression: `WITH name AS (SELECT ...)`.

    Built by `Query.cte("name")`. Used as a source exactly like a table — it
    renders as just its name in FROM and JOIN, with the body hoisted into the
    query's WITH clause. A query collects the CTEs it references automatically, so
    there is nothing to register.

    `recursive_cte()` builds the self-referencing form.
    """

    __slots__ = ("query", "alias", "recursive", "column_names", "__columns__",
                 "__tablename__", "_body")

    def __init__(self, query: Select, alias: str,
                 recursive: bool = False) -> None:
        if not alias or not isinstance(alias, str):
            raise TypeError("cte() needs a non-empty string alias")
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
            raise ValueError(
                f"{alias!r} is not a valid CTE name; expected an identifier"
            )
        self.query = query
        self.alias = alias
        self.recursive = recursive
        self.__tablename__ = None
        columns = _named_output_columns(query, "CTE", alias)
        # Annotated because `_named_output_columns` is Any-typed (it reaches into a
        # Query's entity list), and a list comprehension over Any is list[Any].
        self.column_names: list[str] = [name for name, _ in columns]
        self.__columns__ = {
            name: _SubqueryColumn(name, py_type) for name, py_type in columns
        }
        # For a recursive CTE the body is the base UNION ALL the recursive term,
        # set after construction so the recursive term can reference this object.
        self._body: Any = query

    def __getattr__(self, name: str) -> ColumnExpr[Any]:
        columns = self.__columns__
        if name in columns:
            return ColumnExpr(self, name, columns[name].py_type)
        raise AttributeError(
            f"CTE {self.alias!r} has no output column {name!r}; it exposes "
            f"{', '.join(columns) or '(nothing)'}"
        )

    def definition_sql(self, nxt: Any) -> tuple[str, tuple[Any, ...]]:
        """The `name AS (body)` entry for a WITH clause.

        Rendered with `with_clause=False`: only the statement's outermost render
        emits a WITH clause, and it owns every CTE in the graph. Letting the body
        emit its own would put a nested `WITH` inside the outer one and define the
        same CTE twice.
        """
        sql, params = self._body._render(nxt, with_clause=False)
        if self.recursive:
            # Naming the columns is what lets the recursive term refer to them
            # when the two arms label things differently.
            names = f"({', '.join(self.column_names)})"
            return f"{self.alias}{names} AS ({sql})", tuple(params)
        return f"{self.alias} AS ({sql})", tuple(params)

    def referenced_ctes(self) -> list[CTE]:
        """CTEs this one's body refers to, in dependency order.

        A recursive CTE's body refers to the CTE itself; that self-reference is
        not a dependency, so it is filtered out.
        """
        return [found for found in _collect_ctes(self._body) if found is not self]

    def __repr__(self) -> str:
        kind = "RECURSIVE CTE" if self.recursive else "CTE"
        return f"<{kind} {self.alias}>"


def recursive_cte(alias: str, base: Select,
                  step: Callable[[CTE], Select],
                  union_all: bool = True) -> CTE:
    """A self-referencing CTE.

        tree = recursive_cte(
            "tree",
            Query(Node.id, Node.parent_id).where(Node.parent_id == None),
            lambda cte: Query(Node.id, Node.parent_id)
                        .join(cte, Node.parent_id == cte.id),
        )
        Query(tree.id).order_by("id")

    `step` is a callable receiving the CTE, because the recursive term has to
    reference a CTE that does not exist until its own columns are known — which
    come from `base`. A lambda resolves that ordering without a two-phase API.

    `UNION ALL` by default, as recursive CTEs almost always want; pass
    `union_all=False` for `UNION`, which de-duplicates and terminates on cycles.
    """
    cte = CTE(base, alias, recursive=True)
    recursive_term = step(cte)
    if not hasattr(recursive_term, "_render"):
        raise TypeError(
            f"the recursive term must be a Query or compound, got "
            f"{type(recursive_term).__name__}"
        )
    cte._body = (base.union_all(recursive_term) if union_all
                 else base.union(recursive_term))
    return cte


_ATOMS = (str, bytes, bytearray, int, float, complex, bool, type(None))


def _child_values(node: Any) -> Any:
    """Every attribute value held by a node, `__slots__` and `__dict__` alike.

    Slots have to be read off the MRO because `__slots__` is per-class, and an
    unset slot raises `AttributeError` — which for `CTE` arrives via `__getattr__`
    as a column-lookup failure, so it is caught rather than tested for.

    A class may list attributes in `__value_fields__` to keep them out of the walk.
    That is for attributes holding *bound data* rather than nodes — a bulk INSERT's
    row list, say, which cannot contain a CTE by construction and which the walk
    would otherwise traverse element by element. Measured: it was 15.6 ms of the
    29 ms it takes to render a 16000-row insert.
    """
    skip = getattr(type(node), "__value_fields__", ())
    named: set[str] = set(skip)
    for klass in type(node).__mro__:
        for name in getattr(klass, "__slots__", ()):
            if name in named:
                continue
            named.add(name)
            try:
                yield getattr(node, name)
            except AttributeError:
                pass
    instance_dict = getattr(node, "__dict__", None)
    if instance_dict:
        yield from [
            value for name, value in instance_dict.items() if name not in skip
        ]


def walk_nodes(node: Any) -> Any:
    """Yield every node reachable from `node`, once each, depth-unbounded.

    Reflective for the same reason `_collect_ctes` is (see below), and guarded by
    identity so a self-referencing recursive CTE does not loop forever. `node`
    itself is not yielded — callers ask "what is *inside* this".
    """
    visited: set[int] = {id(node)}
    stack: list[Any] = list(_child_values(node))
    while stack:
        current = stack.pop()
        if isinstance(current, _ATOMS) or isinstance(current, type):
            continue
        key = id(current)
        if key in visited:
            continue
        visited.add(key)
        yield current
        if isinstance(current, (list, tuple, set, frozenset)):
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
        else:
            stack.extend(_child_values(current))


def _collect_ctes(node: Any) -> list[CTE]:
    """Every CTE a statement references, in dependency order, de-duplicated.

    Does not use `walk_nodes` despite the same traversal: a flat walk loses the
    ordering, and a CTE's dependencies have to be emitted before it.

    Order matters twice over: a CTE whose body uses another must come after it in
    the WITH clause, and the WITH clause is rendered first so its parameters must
    number first.

    This is a reflective walk over the node graph rather than a hand-written visit
    per node type. A CTE can be referenced from anywhere a name can appear — FROM,
    JOIN, an ON clause, a WHERE subquery, an EXISTS, a CASE arm, a window frame,
    inside another CTE — and enumerating those explicitly means every expression
    type added later silently drops its CTEs from the WITH clause, producing SQL
    that fails at the database with "relation does not exist". Walking attributes
    cannot miss one. It runs only when a statement's SQL is being built, which is
    once per query shape thanks to the render cache, so the cost is not on the hot
    path.

    The `visited` set is load-bearing, not just an optimization: a recursive CTE's
    body refers to the CTE itself, so an unguarded walk recurses forever.
    """
    found: list[CTE] = []
    visited: set[int] = set()

    def walk(current: Any) -> None:
        if isinstance(current, _ATOMS) or isinstance(current, type):
            return
        key = id(current)
        if key in visited:
            return
        visited.add(key)
        if isinstance(current, CTE):
            # Marked visited above, so the self-reference in a recursive body
            # stops here instead of recursing. Dependencies are walked first and
            # therefore land in `found` first.
            walk(current._body)
            if not any(existing is current for existing in found):
                found.append(current)
            return
        if isinstance(current, (list, tuple, set, frozenset)):
            for item in current:
                walk(item)
            return
        if isinstance(current, dict):
            for item in current.values():
                walk(item)
            return
        for value in _child_values(current):
            walk(value)

    for value in _child_values(node):
        walk(value)
    return found


def from_sql(source, nxt):
    """Render a source for a FROM or JOIN clause."""
    if isinstance(source, CTE):
        # A CTE is referenced by name; the body lives in the WITH clause.
        return source.alias, ()
    if isinstance(source, Subquery):
        return source.from_sql(nxt)
    if isinstance(source, Alias):
        return source.from_sql()
    return source.__tablename__, ()


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


class Expression(Generic[T]):
    """Anything that renders to a value-producing SQL fragment.

    Generic in the Python type the fragment produces: `ColumnExpr[int]`,
    `Aggregate[int]`. That parameter is what lets the comparison operators reject a
    wrong-typed right-hand side, and what lets `Query` work out its row type.
    """

    __slots__ = ()

    def to_sql(self, nxt, resolve=_bare):  # pragma: no cover - abstract
        raise NotImplementedError

    def sources(self):
        return ()

    def output_name(self) -> str:
        """The name this expression takes in a subquery's output. Overridden by
        anything with a better answer (a column its name, a function its own)."""
        return "expr"

    # Comparisons build predicates rather than booleans. The right-hand side is
    # narrowed to this expression's own type (or None, or another expression of
    # the same type), so `User.id > "abc"` is a type error.
    #
    # `__eq__`/`__ne__` returning something other than bool is an incompatible
    # override of `object`, and unavoidable for a query builder — SQLAlchemy has
    # the same ignore for the same reason. `__hash__` is redefined below so these
    # objects stay usable as dict keys.
    def __eq__(self, other: T | Expression[T] | None) -> Condition:  # type: ignore[override]
        return Condition(self, "=", other)

    def __ne__(self, other: T | Expression[T] | None) -> Condition:  # type: ignore[override]
        return Condition(self, "!=", other)

    def __gt__(self, other: T | Expression[T]) -> Condition:
        return Condition(self, ">", other)

    def __ge__(self, other: T | Expression[T]) -> Condition:
        return Condition(self, ">=", other)

    def __lt__(self, other: T | Expression[T]) -> Condition:
        return Condition(self, "<", other)

    def __le__(self, other: T | Expression[T]) -> Condition:
        return Condition(self, "<=", other)

    def in_(self, values: Iterable[T] | Query[Any]) -> InClause:
        """`IN (...)`. Takes a sequence of values or a `Query` subquery."""
        return InClause(self, values, negated=False)

    def not_in(self, values: Iterable[T] | Query[Any]) -> InClause:
        return InClause(self, values, negated=True)

    def like(self, pattern: str) -> Condition:
        return Condition(self, "LIKE", pattern)

    def ilike(self, pattern: str) -> Condition:
        """Case-insensitive `LIKE`. Postgres-only: `ILIKE` is a Postgres
        extension to standard SQL and sqlite has no equivalent operator."""
        return Condition(self, "ILIKE", pattern)

    def between(self, lower: Any, upper: Any) -> Predicate:
        """`col BETWEEN lower AND upper`, inclusive of both ends — spelled out
        as `and_(self >= lower, self <= upper)` since that is exactly what
        `BETWEEN` means and it costs nothing to render literally."""
        return and_(self >= lower, self <= upper)

    def is_null(self) -> Condition:
        return Condition(self, "=", None)

    def is_not_null(self) -> Condition:
        return Condition(self, "!=", None)

    def is_(self, other: None) -> Condition:
        """SQLAlchemy-style spelling of `is_null()`.

        Only `None` is accepted — SQL `IS` otherwise takes a literal
        (`TRUE`/`FALSE`), not a bound parameter, so a value comparison belongs to
        `==` instead, which already renders correctly either way.
        """
        if other is not None:
            raise TypeError("is_() only supports None; use == for a value comparison")
        return self.is_null()

    def is_not(self, other: None) -> Condition:
        """SQLAlchemy-style spelling of `is_not_null()`. Only `None` is accepted;
        see `is_()`."""
        if other is not None:
            raise TypeError(
                "is_not() only supports None; use != for a value comparison"
            )
        return self.is_not_null()

    def is_distinct_from(self, other: Any) -> "IsDistinctFrom":
        """`self IS DISTINCT FROM other` — null-safe inequality: unlike `!=`,
        two `NULL`s compare as *not* distinct (i.e. this is `False`), and
        `NULL` vs. a real value compares as distinct (`True`). Needs a
        dialect to render (`to_sql(dialect=SQLITE)`/`to_sql(dialect=
        POSTGRES)`) — sqlite and Postgres spell this completely differently.
        """
        return IsDistinctFrom(self, other)

    def is_not_distinct_from(self, other: Any) -> "IsDistinctFrom":
        """`self IS NOT DISTINCT FROM other` — null-safe equality, the
        negation of `is_distinct_from()`. Also needs a dialect."""
        return IsDistinctFrom(self, other, negated=True)

    def label(self, name: str) -> Labelled[T]:
        """Name this expression in the select list (`AS name`)."""
        return Labelled(self, name)

    def desc(self) -> "_OrderingExpr[T]":
        """Mark this expression as descending, for `order_by()`."""
        return _OrderingExpr(self, True)

    def asc(self) -> "_OrderingExpr[T]":
        """Mark this expression as ascending, for `order_by()`."""
        return _OrderingExpr(self, False)

    # --- arithmetic, as values rather than predicates ----------------------
    # These keep the operand's own type, so `Post.score * 2` is an
    # `Expression[int]` and still compares against ints. The right-hand side is
    # bound as a parameter, never interpolated.
    #
    # `+` renders SQL `+`. For text use `.concat()`, which renders `||`: Postgres
    # has no `+` for text, so a typed-through `+` on a string column would produce
    # SQL the server rejects.
    def __add__(self, other: T | Expression[T]) -> BinaryOp[T]:
        return BinaryOp(self, "+", other)

    def __radd__(self, other: T) -> BinaryOp[T]:
        return BinaryOp(other, "+", self)

    def __sub__(self, other: T | Expression[T]) -> BinaryOp[T]:
        return BinaryOp(self, "-", other)

    def __rsub__(self, other: T) -> BinaryOp[T]:
        return BinaryOp(other, "-", self)

    def __mul__(self, other: T | Expression[T]) -> BinaryOp[T]:
        return BinaryOp(self, "*", other)

    def __rmul__(self, other: T) -> BinaryOp[T]:
        return BinaryOp(other, "*", self)

    def __truediv__(self, other: T | Expression[T]) -> BinaryOp[Any]:
        return BinaryOp(self, "/", other)

    def __mod__(self, other: T | Expression[T]) -> BinaryOp[T]:
        return BinaryOp(self, "%", other)

    def __neg__(self) -> UnaryOp[T]:
        return UnaryOp("-", self)

    def concat(self, other: Any) -> BinaryOp[str]:
        """SQL `||`. The portable string concatenation on both backends."""
        return BinaryOp(self, "||", other)

    def operate(self, operator: str, other: Any) -> BinaryOp[Any]:
        """An operator this library does not wrap, e.g. `col.operate("#>>", path)`.

        Named `operate` rather than `op` because `Condition` and `BinaryOp` both
        carry an `op` *attribute*; a method of that name would be shadowed by the
        assignment in their `__init__` and silently stop existing on them.

        The operator is inserted verbatim, so it is restricted to the punctuation
        SQL operators are made of — everything else here binds values as
        parameters, and this is the one place a caller supplies a fragment.
        """
        import re

        if not re.fullmatch(r"[-+*/%<>=!~@#&|^?]{1,4}", operator):
            raise ValueError(
                f"{operator!r} is not an accepted SQL operator; expected 1-4 "
                f"operator characters"
            )
        return BinaryOp(self, operator, other)

    def cast(self, type_name: str, py_type: Any = None) -> "Cast[Any]":
        """`CAST(self AS type_name)` — SQLAlchemy's `col.cast(Type)`.

        `type_name` is a plain SQL type name (`"numeric"`, `"integer"`,
        `"numeric(12, 9)"`), not a type *object* — sqlom has no type system to
        instantiate one from (see README). Pass `py_type` to also declare the
        Python type of the result, the role SQLAlchemy's type object plays for
        hydration/typing purposes.
        """
        return Cast(self, type_name, py_type)

    def __hash__(self) -> int:
        return id(self)


class _OrderingExpr(Generic[T]):
    """Wraps an expression with an explicit direction, from `.desc()`/`.asc()`.

    Exists only to be unwrapped by `Query.order_by()` / `CompoundSelect.order_by()`
    — it carries no `to_sql()` of its own.
    """

    __slots__ = ("expression", "descending")

    def __init__(self, expression: Expression[T], descending: bool) -> None:
        self.expression = expression
        self.descending = descending

    def __repr__(self) -> str:
        return f"<{'DESC' if self.descending else 'ASC'} {self.expression!r}>"


class ColumnExpr(Expression[T]):
    """A column reference, produced by class-level attribute access.

    `source` is the model class, `Alias` or `Subquery` the column belongs to.
    `model` is kept as an alias for it because that was the original name and it
    reads naturally when there is no alias involved.
    """

    __slots__ = ("source", "name", "py_type")

    if TYPE_CHECKING:
        source: Any
        name: str
        py_type: type[T]

    def __init__(self, source: Any, name: str, py_type: type[T]) -> None:
        self.source = source
        self.name = name
        self.py_type = py_type

    @property
    def model(self):
        return self.source

    def to_sql(self, nxt, resolve=_bare):
        return resolve(self.source, self.name), ()

    def sources(self):
        return (self.source,)

    def output_name(self):
        return self.name

    def __hash__(self):
        return hash((id(self.source), self.name))

    def __repr__(self):
        return f"<ColumnExpr {source_prefix(self.source)}.{self.name}>"


class Labelled(Expression[T]):
    """`expr AS name` in a select list."""

    __slots__ = ("expr", "name")

    def __init__(self, expr: Expression[T], name: str) -> None:
        self.expr = expr
        self.name = name

    @property
    def py_type(self):
        return getattr(self.expr, "py_type", None)

    def to_sql(self, nxt, resolve=_bare):
        return self.expr.to_sql(nxt, resolve)

    def select_sql(self, nxt, resolve=_bare):
        sql, params = self.expr.to_sql(nxt, resolve)
        return f"{sql} AS {self.name}", params

    def sources(self):
        return self.expr.sources()

    def output_name(self):
        return self.name

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return f"<Labelled {self.expr!r} AS {self.name}>"


class ScalarSubquery(Expression[T]):
    """A `Query` used as a single value — what `Query.scalar_subquery()` returns.

    Renders as a parenthesised subquery wherever a value is expected: a
    comparison, an arithmetic operand, a function argument, an `UPDATE`
    assignment, or — once `.label()`d, same rule as any other unnamed
    expression — a `SELECT`-list entry. The one-row, one-column requirement is
    the database's to enforce, not sqlom's.
    """

    __slots__ = ("query",)

    def __init__(self, query: Any) -> None:
        self.query = query

    @property
    def py_type(self) -> None:
        return None  # the wrapped query's own entity type isn't tracked generically

    def to_sql(self, nxt, resolve=_bare):
        sql, params = self.query._render(nxt)
        return f"({sql})", tuple(params)

    def sources(self):
        # Self-contained: correlation is explicit via .correlate(), and this
        # subquery's own tables are not part of the outer query's FROM/joins.
        return ()

    def __repr__(self) -> str:
        return f"<ScalarSubquery {self.query!r}>"


class Cast(Expression[T]):
    """`CAST(expr AS type_name)` — SQLAlchemy's `cast(expr, Type)`/`col.cast(Type)`.

    `type_name` is inserted verbatim into the SQL, so — the same rule as a
    function name or a custom operator — it is validated as a plain type
    name (`numeric`, `integer`, `numeric(12, 9)`) rather than trusted.
    """

    __slots__ = ("expr", "type_name", "py_type")

    def __init__(self, expr: Any, type_name: str, py_type: Any = None) -> None:
        import re

        stripped = type_name.strip() if isinstance(type_name, str) else ""
        if not stripped or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_ ]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?", stripped
        ):
            raise ValueError(
                f"{type_name!r} is not a valid SQL type name for cast(); "
                f"expected something like 'numeric' or 'numeric(12, 9)'"
            )
        self.expr = expr
        self.type_name = stripped
        self.py_type = py_type

    def to_sql(self, nxt, resolve=_bare):
        sql, params = _operand_sql(self.expr, nxt, resolve)
        return f"CAST({sql} AS {self.type_name})", params

    def sources(self):
        return self.expr.sources() if isinstance(self.expr, Expression) else ()

    def output_name(self) -> str:
        return "cast"

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<Cast {self.expr!r} AS {self.type_name}>"


def cast(expr: Any, type_name: str, py_type: Any = None) -> Cast[Any]:
    """`CAST(expr AS type_name)`. See `Expression.cast()` — the free-function
    and method forms are equivalent, matching SQLAlchemy's `cast(col, Type)`
    and `col.cast(Type)`."""
    return Cast(expr, type_name, py_type)


class Literal(Expression[T]):
    """A Python value forced into value position — SQLAlchemy's `literal(value)`.

    Needed only where a bare Python value is not already accepted: as a whole
    `SELECT`-list entry, standalone. Everywhere else — arithmetic, function
    arguments, comparisons, `UPDATE` assignments — a bare value is already
    bound as a parameter (`_operand_sql`), so wrapping it there is optional,
    not required.
    """

    __slots__ = ("value", "py_type")

    def __init__(self, value: T, py_type: Any = None) -> None:
        self.value = value
        self.py_type = py_type if py_type is not None else type(value)

    def to_sql(self, nxt, resolve=_bare):
        placeholder = nxt() if callable(nxt) else nxt
        return placeholder, (self.value,)

    def sources(self):
        return ()

    def __repr__(self) -> str:
        return f"<Literal {self.value!r}>"


def literal(value: T, py_type: Any = None) -> Literal[T]:
    """Force a bare Python value into an `Expression`, usable standalone (a
    `SELECT`-list entry with nothing else wrapping it) — see `Literal`."""
    return Literal(value, py_type)


class _Keyword(Expression[Any]):
    """A literal SQL keyword expression: `TRUE` / `FALSE` / `NULL`, inserted
    verbatim rather than bound — both sqlite (3.23+) and Postgres understand
    all three as-is."""

    __slots__ = ("keyword", "py_type")

    def __init__(self, keyword: str, py_type: Any = None) -> None:
        self.keyword = keyword
        self.py_type = py_type

    def to_sql(self, nxt, resolve=_bare):
        return self.keyword, ()

    def sources(self):
        return ()

    def __repr__(self) -> str:
        return f"<{self.keyword}>"


def true() -> _Keyword:
    """Literal SQL `TRUE`, standalone. `col == True` already renders a bound
    `TRUE` for a comparison; this is for using the literal as a value on its
    own, e.g. `Query(Book.id, true())`."""
    return _Keyword("TRUE", bool)


def false() -> _Keyword:
    """Literal SQL `FALSE`, standalone. See `true()`."""
    return _Keyword("FALSE", bool)


def null() -> _Keyword:
    """Literal SQL `NULL`, standalone. `col.is_(None)`/`col == None` already
    handle the comparison case; this is for using `NULL` as a plain value,
    e.g. a `CASE` arm or a `SELECT`-list entry."""
    return _Keyword("NULL", None)


class Tuple(Expression[Any]):
    """A row value: `(a, b, c)` — SQLAlchemy's `tuple_(a, b, c)`.

    Comparable with `==`/`!=` against another `tuple_(...)` or a plain Python
    tuple of the same width (`tuple_(a, b) == (1, 2)` renders `(a, b) = (1,
    2)`), and usable with `.in_()`/`.not_in()` against a sequence of
    same-width tuples — each a `tuple_(...)` or a plain Python tuple, both
    accepted the same way a bare value is elsewhere in this library — or a
    subquery selecting that many columns.
    """

    __slots__ = ("elements",)

    def __init__(self, *elements: Any) -> None:
        if not elements:
            raise ValueError("tuple_() needs at least one element")
        self.elements = elements

    def to_sql(self, nxt, resolve=_bare):
        parts: list[str] = []
        params: tuple[Any, ...] = ()
        for element in self.elements:
            sql, element_params = _operand_sql(element, nxt, resolve)
            parts.append(sql)
            params += element_params
        return f"({', '.join(parts)})", params

    def sources(self):
        found: tuple[Any, ...] = ()
        for element in self.elements:
            if isinstance(element, Expression):
                found += element.sources()
        return found

    def _as_tuple(self, other: Any) -> "Expression[Any]":
        if isinstance(other, Expression):
            return other  # another Tuple, a ScalarSubquery, ...
        if isinstance(other, (tuple, list)):
            return Tuple(*other)
        raise TypeError(
            f"tuple_() compares against another tuple_(...) or a plain "
            f"Python tuple of the same width, got {other!r}"
        )

    def __eq__(self, other: Any) -> Condition:  # type: ignore[override]
        return Condition(self, "=", self._as_tuple(other))

    def __ne__(self, other: Any) -> Condition:  # type: ignore[override]
        return Condition(self, "!=", self._as_tuple(other))

    def in_(self, values: Any) -> InClause:
        """`(a, b) IN ((v1, v2), ...)`, or a subquery selecting the same
        number of columns."""
        if hasattr(values, "_render"):
            return InClause(self, values, negated=False)
        return InClause(
            self, [v if isinstance(v, Tuple) else Tuple(*v) for v in values],
            negated=False,
        )

    def not_in(self, values: Any) -> InClause:
        if hasattr(values, "_render"):
            return InClause(self, values, negated=True)
        return InClause(
            self, [v if isinstance(v, Tuple) else Tuple(*v) for v in values],
            negated=True,
        )

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<Tuple {self.elements!r}>"


def tuple_(*elements: Any) -> Tuple:
    """`(a, b, c)` as a comparable row value — SQLAlchemy's `tuple_(a, b, c)`.
    See `Tuple`."""
    return Tuple(*elements)


class Aggregate(Expression[T]):
    """`count(x)`, `sum(x)`, and friends. Usable in a select list and in HAVING."""

    __slots__ = ("func", "operand", "distinct")

    def __init__(self, func: str, operand: Any = None,
                 distinct: bool = False) -> None:
        self.func = func
        self.operand = operand
        self.distinct = distinct
        if distinct and (operand is None or self._counts_a_table):
            # `count(DISTINCT *)` is not valid SQL. Rendering it would produce a
            # syntax error from the server about a statement the caller did not
            # write, so it is refused here.
            raise ValueError(
                "distinct=True needs a column: count(*) and count(Model) have "
                "nothing to de-duplicate"
            )

    @property
    def _counts_a_table(self) -> bool:
        """True for `count(Model)`: renders `count(*)` but names the table.

        `count()` alone references nothing, so a query built only from it has no
        FROM clause to derive. `count(Model)` is the ordinary "how many rows" and
        supplies the table without making the caller name a column that has
        nothing to do with the question.
        """
        operand = self.operand
        return (operand is not None and not isinstance(operand, Expression)
                and hasattr(operand, "__columns__"))

    @property
    def py_type(self):
        # count() is always an integer; the rest depend on the column and on the
        # backend (Postgres avg() of an int is numeric, sum() is bigint), so they
        # are left untyped rather than guessed at — a wrong py_type would pick a
        # converter and corrupt the value.
        return int if self.func == "count" else None

    def to_sql(self, nxt, resolve=_bare):
        if self.operand is None or self._counts_a_table:
            inner, params = "*", ()
        else:
            inner, params = self.operand.to_sql(nxt, resolve)
        prefix = "DISTINCT " if self.distinct else ""
        return f"{self.func}({prefix}{inner})", params

    def sources(self):
        if self._counts_a_table:
            return (self.operand,)
        return () if self.operand is None else self.operand.sources()

    def output_name(self):
        if self.operand is None or self._counts_a_table:
            return f"{self.func}_all"
        return f"{self.func}_{self.operand.output_name()}"

    def over(self, **kwargs: Any) -> Over[T]:
        """`count() OVER (PARTITION BY ...)` — the aggregate as a window.

        A windowed aggregate is not an aggregate for grouping purposes: it produces
        a value per row rather than per group, so it needs no GROUP BY.
        """
        return Over(self, **kwargs)

    def __hash__(self):
        return id(self)

    def __repr__(self):
        inner = "*" if self.operand is None else repr(self.operand)
        return f"<Aggregate {self.func}({inner})>"


def count(column: Any = None, distinct: bool = False) -> Aggregate[int]:
    """`count(*)`, `count(col)`, or `count(Model)`.

    `count(Model)` also renders `count(*)`, but names the table — so
    `Query(count(Model))` has a FROM clause, where `Query(count())` on its own has
    nothing to select from and is refused.
    """
    return Aggregate("count", column, distinct)


def sum_(column: Expression[Any], distinct: bool = False) -> Aggregate[Any]:
    """`sum(col)`. Typed `Any` rather than the column's type: Postgres widens
    `sum(int)` to bigint and `sum(numeric)` to numeric, and psycopg/asyncpg may
    hand back `Decimal`. Claiming `int` here would be a lie the checker enforces."""
    return Aggregate("sum", column, distinct)


def avg(column: Expression[Any], distinct: bool = False) -> Aggregate[Any]:
    """`avg(col)`. `Any` for the same reason as `sum_`, more so — the average of
    an integer column is `numeric` in Postgres and arrives as `Decimal`."""
    return Aggregate("avg", column, distinct)


def min_(column: Expression[T]) -> Aggregate[T]:
    """`min(col)`. Keeps the column's type: a minimum is one of the values."""
    return Aggregate("min", column)


def max_(column: Expression[T]) -> Aggregate[T]:
    """`max(col)`. Keeps the column's type, as `min_` does."""
    return Aggregate("max", column)


# --------------------------------------------------------------------------
# Predicates
# --------------------------------------------------------------------------


class Predicate(Expression[bool]):
    """Anything that renders to a boolean SQL fragment.

    `&`, `|` and `~` compose them. **Parenthesise the operands**: Python binds `&`
    and `|` tighter than the comparison operators, so `A.x > 1 | A.y < 2` parses as
    `A.x > (1 | A.y) < 2` and will not do what you meant. This is the same trap
    SQLAlchemy has, for the same reason.
    """

    __slots__ = ()

    def __and__(self, other: Predicate) -> Predicate:
        return and_(self, other)

    def __or__(self, other: Predicate) -> Predicate:
        return or_(self, other)

    def __invert__(self) -> Predicate:
        return Not(self)

    def __hash__(self) -> int:
        return id(self)


class Condition(Predicate):
    """A single comparison: `left OP right`.

    `right` may be a Python value (bound as a parameter), another expression (a
    column-to-column comparison, which is what an ON clause is made of), None
    (rendered as IS NULL / IS NOT NULL), or a scalar `Query`.
    """

    __slots__ = ("left", "op", "right")

    def __init__(self, left: Expression[Any], op: str, right: Any) -> None:
        self.left = left
        self.op = op
        self.right = right

    # --- back-compatible accessors for the column-vs-value case --------------

    @property
    def model(self):
        """The left side's source, or None if the left side is not a column."""
        return getattr(self.left, "source", None)

    @property
    def column_name(self):
        return getattr(self.left, "name", None)

    @property
    def value(self):
        return self.right

    @property
    def is_column_comparison(self):
        """True when the right-hand side is another column."""
        return isinstance(self.right, ColumnExpr)

    def to_sql(self, nxt, resolve=_bare):
        # `nxt` may be a plain placeholder string for the simple single-parameter
        # callers that predate the predicate tree.
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        left, params = self.left.to_sql(advance, resolve)

        if self.right is None and self.op in _NULL_FORMS:
            return f"{left} {_NULL_FORMS[self.op]}", params

        if isinstance(self.right, Expression):
            right, right_params = self.right.to_sql(advance, resolve)
            return f"{left} {self.op} {right}", params + right_params

        right = self.right
        if right is not None and hasattr(right, "_render"):  # scalar subquery
            sql, right_params = right._render(advance)
            return f"{left} {self.op} ({sql})", params + tuple(right_params)

        return f"{left} {self.op} {advance()}", params + (self.right,)

    def sources(self):
        sources = self.left.sources()
        if isinstance(self.right, Expression):
            sources += self.right.sources()
        return sources

    def models(self):
        """Every source this predicate references — one, or two for an ON clause."""
        return self.sources()

    def __repr__(self):
        if self.is_column_comparison:
            return f"<Condition {self.left!r} {self.op} {self.right!r}>"
        return f"<Condition {self.left!r} {self.op} {self.value!r}>"


class IsDistinctFrom(Predicate):
    """`left IS DISTINCT FROM right` / `left IS NOT DISTINCT FROM right` —
    null-safe (in)equality: two `NULL`s are *not* distinct, `NULL` versus a
    real value *is* distinct — the case plain `=`/`!=` get wrong, since SQL's
    three-valued logic makes `NULL = NULL` and `NULL != NULL` both `NULL`
    (neither true nor false), not `TRUE`.

    sqlite and Postgres spell this completely differently — sqlite has no
    `DISTINCT FROM` keyword at all, using its own null-safe `IS`/`IS NOT`
    instead — so, unlike everything else in this library (which stays
    permissive and dialect-less by default), this raises rather than
    guessing if rendered with no dialect in effect. See `sqlom/dialects.py`.
    """

    __slots__ = ("left", "right", "negated")

    def __init__(self, left: Expression[Any], right: Any,
                 negated: bool = False) -> None:
        self.left = left
        self.right = right
        self.negated = negated

    def to_sql(self, nxt, resolve=_bare):
        dialect = current_dialect()
        if dialect is None:
            method = "is_not_distinct_from" if self.negated else "is_distinct_from"
            raise ValueError(
                f"{method}() needs a dialect: call to_sql(dialect=SQLITE) or "
                f"to_sql(dialect=POSTGRES) — sqlite and Postgres spell "
                f"null-safe comparison completely differently, so there is "
                f"no dialect-less default to fall back on"
            )
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        left, params = self.left.to_sql(advance, resolve)

        if isinstance(self.right, Expression):
            right, right_params = self.right.to_sql(advance, resolve)
            return (dialect.is_distinct_from_sql(left, right, self.negated),
                    params + right_params)

        right = self.right
        if right is not None and hasattr(right, "_render"):  # a bare scalar subquery
            sql, right_params = right._render(advance)
            return (dialect.is_distinct_from_sql(left, f"({sql})", self.negated),
                    params + tuple(right_params))

        return (dialect.is_distinct_from_sql(left, str(advance()), self.negated),
                params + (self.right,))

    def sources(self):
        sources = self.left.sources()
        if isinstance(self.right, Expression):
            sources += self.right.sources()
        return sources

    def __repr__(self):
        keyword = "IS NOT DISTINCT FROM" if self.negated else "IS DISTINCT FROM"
        return f"<{keyword} {self.left!r} {self.right!r}>"


class BooleanClause(Predicate):
    """`AND` or `OR` over two or more predicates, always parenthesised.

    Parentheses are unconditional rather than precedence-aware: `AND` binds tighter
    than `OR` in SQL, so an un-parenthesised nested clause silently changes meaning,
    and a redundant pair of brackets costs nothing.
    """

    __slots__ = ("op", "parts")

    def __init__(self, op: str, parts: Iterable[Predicate]) -> None:
        self.op = op
        self.parts = list(parts)

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        fragments, params = [], ()
        for part in self.parts:
            fragment, part_params = part.to_sql(advance, resolve)
            fragments.append(fragment)
            params += part_params
        joined = f" {self.op} ".join(fragments)
        return f"({joined})", params

    def sources(self):
        sources = ()
        for part in self.parts:
            sources += part.sources()
        return sources

    def __repr__(self):
        return f"<{self.op} {self.parts!r}>"


class Not(Predicate):
    __slots__ = ("part",)

    def __init__(self, part: Predicate) -> None:
        self.part = part

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        fragment, params = self.part.to_sql(advance, resolve)
        return f"NOT ({fragment})", params

    def sources(self):
        return self.part.sources()

    def __repr__(self):
        return f"<NOT {self.part!r}>"


class InClause(Predicate):
    """`x IN (...)` over a sequence of values or a subquery."""

    __slots__ = ("left", "values", "negated")

    def __init__(self, left: Expression[Any], values: Any,
                 negated: bool = False) -> None:
        self.left = left
        # Materialise eagerly (unless it's a subquery) so a one-shot iterator
        # (a generator, say) isn't exhausted by `sources()` before `to_sql()`
        # gets to it — both now read the same concrete list. Annotated `Any`
        # (rather than left to infer `list[Any]`) since it may also hold a
        # subquery, checked via `hasattr(..., "_render")` below.
        self.values: Any = values if hasattr(values, "_render") else list(values)
        self.negated = negated

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        left, params = self.left.to_sql(advance, resolve)
        keyword = "NOT IN" if self.negated else "IN"

        if hasattr(self.values, "_render"):
            sql, sub_params = self.values._render(advance)
            return f"{left} {keyword} ({sql})", params + tuple(sub_params)

        values = list(self.values)
        if not values:
            # `x IN ()` is a syntax error in Postgres, and an empty IN is a
            # perfectly reasonable thing for calling code to end up with, so
            # render the constant it is equivalent to.
            return ("FALSE" if not self.negated else "TRUE"), params
        fragments: list[str] = []
        bound: list[Any] = []
        for value in values:
            if isinstance(value, Expression):
                # A column (or any expression) belongs in the IN list as a SQL
                # reference, not bound as a parameter — `col.in_([other_col])`
                # renders `col IN (other_col)`.
                fragment, value_params = value.to_sql(advance, resolve)
                fragments.append(fragment)
                bound.extend(value_params)
            else:
                fragments.append(str(advance()))
                bound.append(value)
        return f"{left} {keyword} ({', '.join(fragments)})", params + tuple(bound)

    def sources(self):
        sources = self.left.sources()
        if not hasattr(self.values, "_render"):
            for value in self.values:
                if isinstance(value, Expression):
                    sources += value.sources()
        return sources

    def __repr__(self):
        return f"<{'NOT IN' if self.negated else 'IN'} {self.left!r}>"


class ExistsClause(Predicate):
    """`EXISTS (subquery)`."""

    __slots__ = ("query", "negated")

    def __init__(self, query: Query[Any], negated: bool = False) -> None:
        self.query = query
        self.negated = negated

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        sql, params = self.query._render(advance)
        keyword = "NOT EXISTS" if self.negated else "EXISTS"
        return f"{keyword} ({sql})", tuple(params)

    def sources(self):
        return ()

    def __invert__(self) -> Predicate:
        return ExistsClause(self.query, not self.negated)

    def __repr__(self):
        return f"<{'NOT EXISTS' if self.negated else 'EXISTS'}>"


def and_(*predicates: Predicate) -> Predicate:
    """Combine predicates with AND. One argument returns it unchanged."""
    return _combine("AND", predicates)


def or_(*predicates: Predicate) -> Predicate:
    """Combine predicates with OR. One argument returns it unchanged."""
    return _combine("OR", predicates)


def _combine(op: str, predicates: Iterable[Predicate]) -> Predicate:
    flat = []
    for predicate in predicates:
        if not isinstance(predicate, Predicate):
            raise TypeError(
                f"{op.lower()}_() takes predicates built from column comparisons, "
                f"got {type(predicate).__name__}"
            )
        # Flatten same-operator nesting so `or_(a, or_(b, c))` renders as one
        # three-way OR rather than nested brackets.
        if isinstance(predicate, BooleanClause) and predicate.op == op:
            flat.extend(predicate.parts)
        else:
            flat.append(predicate)
    if not flat:
        raise TypeError(f"{op.lower()}_() needs at least one predicate")
    if len(flat) == 1:
        return flat[0]
    return BooleanClause(op, flat)


def not_(predicate: Predicate) -> Predicate:
    if not isinstance(predicate, Predicate):
        raise TypeError(
            f"not_() takes a predicate, got {type(predicate).__name__}"
        )
    return ~predicate


def exists(query: Query[Any]) -> Predicate:
    """`EXISTS (query)`. Negate with `~exists(query)`."""
    if not hasattr(query, "_render"):
        raise TypeError(f"exists() takes a Query, got {type(query).__name__}")
    return ExistsClause(query)


# --------------------------------------------------------------------------
# Composite value expressions: arithmetic, functions, CASE, windows
# --------------------------------------------------------------------------


def _operand_sql(value: Any, nxt: Any, resolve: Any) -> tuple[str, tuple[Any, ...]]:
    """Render either side of a binary operation.

    An `Expression` renders itself; anything else is a literal and gets bound as a
    parameter rather than interpolated, which is the whole reason this helper
    exists rather than an f-string at each call site.
    """
    if isinstance(value, Expression):
        return value.to_sql(nxt, resolve)
    if hasattr(value, "_render"):  # a Query used as a scalar subquery
        sql, params = value._render(nxt)
        return f"({sql})", tuple(params)
    return nxt(), (value,)


class BinaryOp(Expression[T]):
    """`left OP right` as a *value*: `a + b`, `a || b`, `a * 2`.

    Always parenthesised. SQL operator precedence differs from Python's in places
    (notably `||` versus comparison), and a redundant bracket costs nothing while a
    missing one silently changes meaning.
    """

    __slots__ = ("left", "op", "right")

    def __init__(self, left: Any, op: str, right: Any) -> None:
        self.left = left
        self.op = op
        self.right = right

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        left, left_params = _operand_sql(self.left, advance, resolve)
        right, right_params = _operand_sql(self.right, advance, resolve)
        return f"({left} {self.op} {right})", left_params + right_params

    def sources(self):
        found: tuple[Any, ...] = ()
        for side in (self.left, self.right):
            if isinstance(side, Expression):
                found += side.sources()
        return found

    def output_name(self) -> str:
        return "expr"

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<BinaryOp {self.left!r} {self.op} {self.right!r}>"


class UnaryOp(Expression[T]):
    """`-x`, and anywhere else a prefix operator is needed."""

    __slots__ = ("op", "operand")

    def __init__(self, op: str, operand: Expression[T]) -> None:
        self.op = op
        self.operand = operand

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        inner, params = self.operand.to_sql(advance, resolve)
        return f"({self.op}{inner})", params

    def sources(self):
        return self.operand.sources()

    def output_name(self) -> str:
        return "expr"

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<UnaryOp {self.op}{self.operand!r}>"


_IDENTIFIER = None  # set below, after `re` is imported lazily


class FunctionCall(Expression[T]):
    """A call to a SQL function: `lower(x)`, `coalesce(a, b)`.

    The name is validated against an identifier pattern. Everything else in this
    library binds values as parameters and never interpolates them, and a function
    name is the one place a caller supplies a *fragment* — so it is checked rather
    than trusted.
    """

    __slots__ = ("name", "args", "py_type")

    def __init__(self, name: str, *args: Any, py_type: Any = None) -> None:
        import re

        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(
                f"{name!r} is not a valid SQL function name; expected an identifier "
                f"like 'lower' or 'coalesce'"
            )
        self.name = name
        self.args = args
        self.py_type = py_type

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        parts, params = [], ()
        for arg in self.args:
            sql, arg_params = _operand_sql(arg, advance, resolve)
            parts.append(sql)
            params += arg_params
        return f"{self.name}({', '.join(parts)})", params

    def sources(self):
        found: tuple[Any, ...] = ()
        for arg in self.args:
            if isinstance(arg, Expression):
                found += arg.sources()
        return found

    def output_name(self) -> str:
        return self.name

    def over(self, **kwargs: Any) -> Over[T]:
        """Turn this into a window function. See `Over`."""
        return Over(self, **kwargs)

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<FunctionCall {self.name}({len(self.args)} args)>"


class _FunctionNamespace:
    """`func.lower(x)` — any SQL function by attribute access.

    Returns `FunctionCall[Any]`: a checker cannot know what `lower` returns, and
    claiming otherwise would be a guess it then enforces. Use
    `sql_function("lower", x, py_type=str)` when the type is worth stating.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Callable[..., FunctionCall[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, py_type: Any = None) -> FunctionCall[Any]:
            return FunctionCall(name, *args, py_type=py_type)

        return call


func = _FunctionNamespace()


def sql_function(name: str, *args: Any, py_type: Any = None) -> FunctionCall[Any]:
    """`func.name(...)` with an explicit result type."""
    return FunctionCall(name, *args, py_type=py_type)


class Case(Expression[T]):
    """`CASE WHEN cond THEN value ... ELSE other END` (the "searched" form), or
    — when `value` is given — `CASE value WHEN match THEN result ... ELSE
    other END` (the "simple" form, SQLAlchemy's `case(..., value=col)`), where
    each `match` is compared against `value` by equality rather than being a
    predicate of its own.
    """

    __slots__ = ("whens", "else_", "value")

    def __init__(self, whens: Any, else_: Any = None, value: Any = None) -> None:
        self.whens = list(whens)
        if not self.whens:
            raise ValueError("case() needs at least one (condition, value) pair")
        self.value = value
        for pair in self.whens:
            if not (isinstance(pair, tuple) and len(pair) == 2):
                raise TypeError(
                    f"case() takes (condition, value) pairs, got {pair!r}"
                )
            if value is None and not isinstance(pair[0], Predicate):
                raise TypeError(
                    f"case() condition must be a predicate, got "
                    f"{type(pair[0]).__name__} (pass value=... for the simple "
                    f"CASE form, where each pair's first element is compared "
                    f"against it instead of being a predicate)"
                )
        self.else_ = else_

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        params: tuple[Any, ...] = ()
        if self.value is not None:
            value_sql, value_params = _operand_sql(self.value, advance, resolve)
            params += value_params
            sql = f"CASE {value_sql}"
        else:
            sql = "CASE"
        for left, result in self.whens:
            if self.value is not None:
                left_sql, left_params = _operand_sql(left, advance, resolve)
            else:
                left_sql, left_params = left.to_sql(advance, resolve)
            params += left_params
            value_sql, value_params = _operand_sql(result, advance, resolve)
            params += value_params
            sql += f" WHEN {left_sql} THEN {value_sql}"
        if self.else_ is not None:
            else_sql, else_params = _operand_sql(self.else_, advance, resolve)
            params += else_params
            sql += f" ELSE {else_sql}"
        return sql + " END", params

    def sources(self):
        found: tuple[Any, ...] = ()
        if isinstance(self.value, Expression):
            found += self.value.sources()
        for left, result in self.whens:
            if isinstance(left, Expression):
                found += left.sources()
            elif self.value is None:
                found += left.sources()  # left is a Predicate in the searched form
            if isinstance(result, Expression):
                found += result.sources()
        if isinstance(self.else_, Expression):
            found += self.else_.sources()
        return found

    def output_name(self) -> str:
        return "case"

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<Case {len(self.whens)} when(s)>"


class Over(Expression[T]):
    """A window: `f(...) OVER (PARTITION BY ... ORDER BY ... frame)`.

    `partition_by` and `order_by` take a single column or a sequence. `order_by`
    entries may be a bare column (ascending) or `(column, "DESC")`. `frame` is
    passed through verbatim after validation, e.g. `"ROWS BETWEEN 1 PRECEDING AND
    CURRENT ROW"`, since the grammar is large and mostly not worth modelling.
    """

    __slots__ = ("function", "partition_by", "order_by", "frame")

    def __init__(self, function: Expression[T], partition_by: Any = (),
                 order_by: Any = (), frame: str | None = None) -> None:
        self.function = function
        self.partition_by = _as_sequence(partition_by)
        self.order_by = _as_order_sequence(order_by)
        if frame is not None:
            import re

            if not re.fullmatch(r"[A-Za-z0-9_ ]+", frame):
                raise ValueError(
                    f"frame clause {frame!r} may only contain letters, digits, "
                    f"underscores and spaces; it is inserted verbatim"
                )
        self.frame = frame

    @property
    def py_type(self) -> Any:
        return getattr(self.function, "py_type", None)

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        inner, params = self.function.to_sql(advance, resolve)
        clauses = []
        if self.partition_by:
            parts = []
            for expression in self.partition_by:
                sql, extra = expression.to_sql(advance, resolve)
                params += extra
                parts.append(sql)
            clauses.append("PARTITION BY " + ", ".join(parts))
        if self.order_by:
            parts = []
            for entry in self.order_by:
                expression, direction = _order_entry(entry)
                sql, extra = expression.to_sql(advance, resolve)
                params += extra
                parts.append(f"{sql} {direction}" if direction else sql)
            clauses.append("ORDER BY " + ", ".join(parts))
        if self.frame:
            clauses.append(self.frame)
        return f"{inner} OVER ({' '.join(clauses)})", params

    def sources(self):
        found = self.function.sources()
        for expression in self.partition_by:
            found += expression.sources()
        for entry in self.order_by:
            found += _order_entry(entry)[0].sources()
        return found

    def output_name(self) -> str:
        return self.function.output_name()

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<Over {self.function!r}>"


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _as_order_sequence(value: Any) -> tuple[Any, ...]:
    """Normalise a window ORDER BY.

    `(col, "DESC")` is a single directed entry, while `(col_a, col_b)` is two
    entries — the two are only told apart by the second element being a direction
    string, so that check lives here rather than being guessed at render time.
    """
    if value is None:
        return ()
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[1], str)):
        entries: tuple[Any, ...] = (value,)
    else:
        entries = _as_sequence(value)
    # Validate eagerly. Deferring to render time means a typo surfaces on the
    # first query execution rather than where it was written.
    for entry in entries:
        _order_entry(entry)
    return entries


def _order_entry(entry: Any) -> tuple[Any, str]:
    """Accept `col` or `(col, "DESC")` in a window's ORDER BY."""
    if isinstance(entry, tuple):
        expression, direction = entry
        direction = str(direction).upper()
        if direction not in ("ASC", "DESC"):
            raise ValueError(
                f"window order direction must be 'ASC' or 'DESC', got {direction!r}"
            )
        return expression, direction
    return entry, ""


def case(*whens: tuple[Any, Any], value: Any = None, else_: Any = None) -> Case[Any]:
    """Searched form: `case((User.active == True, "on"), else_="off")` ->
    `CASE WHEN active = $1 THEN 'on' ELSE 'off' END`. The pair type is spelled
    out so a bare predicate — `case(User.active == True)` — is a type error
    rather than a runtime one.

    Simple form, with `value=`: `case((1, "a"), (2, "b"), value=Post.status)`
    -> `CASE status WHEN 1 THEN 'a' WHEN 2 THEN 'b' END` — each pair's first
    element is compared against `value` by equality rather than being a
    predicate of its own, matching SQLAlchemy's `case(..., value=col)`.
    """
    return Case(whens, else_, value)


# Window functions. Each is a FunctionCall, so `.over(...)` is available on it.
def row_number() -> FunctionCall[int]:
    return FunctionCall("row_number", py_type=int)


def rank() -> FunctionCall[int]:
    return FunctionCall("rank", py_type=int)


def dense_rank() -> FunctionCall[int]:
    return FunctionCall("dense_rank", py_type=int)


_NO_DEFAULT = object()


def lag(column: Expression[T], offset: int = 1, default: Any = _NO_DEFAULT) -> FunctionCall[T]:
    """`lag(column, offset)`, or `lag(column, offset, default)` when the window
    has no such row (SQL's own default there is `NULL`)."""
    if default is _NO_DEFAULT:
        return FunctionCall("lag", column, offset)
    return FunctionCall("lag", column, offset, default)


def lead(column: Expression[T], offset: int = 1, default: Any = _NO_DEFAULT) -> FunctionCall[T]:
    """`lead(column, offset)`, or `lead(column, offset, default)` when the window
    has no such row (SQL's own default there is `NULL`)."""
    if default is _NO_DEFAULT:
        return FunctionCall("lead", column, offset)
    return FunctionCall("lead", column, offset, default)


def first_value(column: Expression[T]) -> FunctionCall[T]:
    return FunctionCall("first_value", column)


def last_value(column: Expression[T]) -> FunctionCall[T]:
    return FunctionCall("last_value", column)


def ntile(buckets: int) -> FunctionCall[int]:
    return FunctionCall("ntile", buckets, py_type=int)


class Excluded(Expression[T]):
    """The row that failed to insert, inside `ON CONFLICT DO UPDATE`.

    Renders `excluded.<column>`. Both Postgres and sqlite expose the proposed row
    under that name, so an upsert can write the incoming value over the stored one:

        Insert(User).values(email="a@b.c", hits=1).on_conflict_do_update(
            User.email, set_={"hits": User.hits + excluded(User.hits)}
        )

    Built by `excluded()`, and it deliberately reports **no** source: `excluded` is
    not a table in the statement, so a validator that treated it as one would demand
    a join for it.
    """

    __slots__ = ("column",)

    if TYPE_CHECKING:
        column: ColumnExpr[T]

    def __init__(self, column: ColumnExpr[T]) -> None:
        if not isinstance(column, ColumnExpr):
            raise TypeError(
                f"excluded() takes a model column, got {type(column).__name__}"
            )
        self.column = column

    def to_sql(self, nxt, resolve=_bare):
        return f"excluded.{self.column.name}", ()

    def output_name(self) -> str:
        return self.column.name

    @property
    def py_type(self) -> Any:
        return self.column.py_type

    def __repr__(self) -> str:
        return f"<excluded.{self.column.name}>"


def excluded(column: ColumnExpr[T]) -> Excluded[T]:
    """`excluded.<column>` — the value the conflicting INSERT tried to write.

    SQLAlchemy spells this `stmt.excluded.email`, which needs the statement in a
    variable first. A free function reads the same and composes inline.
    """
    return Excluded(column)
