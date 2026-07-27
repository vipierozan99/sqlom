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
    TypeVar,
    overload,
)

if TYPE_CHECKING:
    from .query import Query

T = TypeVar("T")
M = TypeVar("M")

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
    if alias is not None:
        return f"{getattr(source, 'model', source).__name__ if hasattr(source, 'model') else 'subquery'} AS {alias}"
    return source.__name__


def source_model(source):
    """The model class behind a source, or None for a subquery."""
    return getattr(source, "model", source) if hasattr(source, "alias") else source


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
        # `type[M]` carries no declaration of these; they come from ModelMeta or
        # the @model decorator at runtime, checked by the hasattr above.
        self.__columns__ = model.__columns__  # type: ignore[attr-defined]
        self.__tablename__ = model.__tablename__  # type: ignore[attr-defined]

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

    def __init__(self, query: Query[Any], alias: str) -> None:
        if not alias or not isinstance(alias, str):
            raise TypeError("subquery() needs a non-empty string alias")
        self.query = query
        self.alias = alias
        self.__tablename__ = None
        self.__columns__ = {
            name: _SubqueryColumn(name, py_type)
            for name, py_type in query.output_columns()
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


def from_sql(source, nxt):
    """Render a source for a FROM or JOIN clause."""
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

    def is_null(self) -> Condition:
        return Condition(self, "=", None)

    def is_not_null(self) -> Condition:
        return Condition(self, "!=", None)

    def label(self, name: str) -> Labelled[T]:
        """Name this expression in the select list (`AS name`)."""
        return Labelled(self, name)

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

    def __hash__(self) -> int:
        return id(self)


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

        if hasattr(self.right, "_render"):  # a Query used as a scalar subquery
            sql, right_params = self.right._render(advance)
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
        self.values = values
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
        placeholders = ", ".join(advance() for _ in values)
        return f"{left} {keyword} ({placeholders})", params + tuple(values)

    def sources(self):
        return self.left.sources()

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
    """`CASE WHEN cond THEN value ... ELSE other END`."""

    __slots__ = ("whens", "else_")

    def __init__(self, whens: Any, else_: Any = None) -> None:
        self.whens = list(whens)
        if not self.whens:
            raise ValueError("case() needs at least one (condition, value) pair")
        for pair in self.whens:
            if not (isinstance(pair, tuple) and len(pair) == 2):
                raise TypeError(
                    f"case() takes (condition, value) pairs, got {pair!r}"
                )
            if not isinstance(pair[0], Predicate):
                raise TypeError(
                    f"case() condition must be a predicate, got "
                    f"{type(pair[0]).__name__}"
                )
        self.else_ = else_

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        sql = "CASE"
        params: tuple[Any, ...] = ()
        for condition, value in self.whens:
            cond_sql, cond_params = condition.to_sql(advance, resolve)
            params += cond_params
            value_sql, value_params = _operand_sql(value, advance, resolve)
            params += value_params
            sql += f" WHEN {cond_sql} THEN {value_sql}"
        if self.else_ is not None:
            else_sql, else_params = _operand_sql(self.else_, advance, resolve)
            params += else_params
            sql += f" ELSE {else_sql}"
        return sql + " END", params

    def sources(self):
        found: tuple[Any, ...] = ()
        for condition, value in self.whens:
            found += condition.sources()
            if isinstance(value, Expression):
                found += value.sources()
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


def case(*whens: tuple[Predicate, Any], else_: Any = None) -> Case[Any]:
    """`case((User.active == True, "on"), else_="off")`.

    The pair type is spelled out so a bare predicate — `case(User.active == True)`
    — is a type error rather than a runtime one.
    """
    return Case(whens, else_)


# Window functions. Each is a FunctionCall, so `.over(...)` is available on it.
def row_number() -> FunctionCall[int]:
    return FunctionCall("row_number", py_type=int)


def rank() -> FunctionCall[int]:
    return FunctionCall("rank", py_type=int)


def dense_rank() -> FunctionCall[int]:
    return FunctionCall("dense_rank", py_type=int)


def lag(column: Expression[T], offset: int = 1) -> FunctionCall[T]:
    return FunctionCall("lag", column, offset)


def lead(column: Expression[T], offset: int = 1) -> FunctionCall[T]:
    return FunctionCall("lead", column, offset)


def first_value(column: Expression[T]) -> FunctionCall[T]:
    return FunctionCall("first_value", column)


def last_value(column: Expression[T]) -> FunctionCall[T]:
    return FunctionCall("last_value", column)


def ntile(buckets: int) -> FunctionCall[int]:
    return FunctionCall("ntile", buckets, py_type=int)
