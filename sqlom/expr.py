"""Expressions: columns, aliases, aggregates, and the predicate tree.

Everything here renders to a SQL fragment plus the parameters it binds. Two
protocol methods carry all of it:

    to_sql(nxt, resolve) -> (fragment, params)
    sources()            -> the table sources the fragment references

`nxt()` yields the next placeholder — a callable rather than a string because a
predicate tree binds an unknown number of parameters and numbering has to stay in
step across the whole render. `resolve(source, name)` renders a column reference,
qualified or bare depending on whether the query has more than one source.

**"Source" rather than "model"** is the load-bearing idea. A column belongs to a
model class, an `Alias` of one, or a `Subquery` — and once aliases exist, the model
class alone cannot identify which table a column came from, because a self-join has
the same model twice. Every place that used to compare model classes now compares
sources by identity.
"""

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


class Alias:
    """An aliased reference to a model, which is what makes a self-join possible.

        mgr = Alias(Employee, "mgr")
        Query(Employee, mgr).join(mgr, Employee.manager_id == mgr.id)

    Columns are reached off the alias (`mgr.id`), and they carry the alias — not the
    model — as their source, so the two sides of a self-join stay distinguishable
    all the way through rendering.
    """

    __slots__ = ("model", "alias", "__columns__", "__tablename__")

    def __init__(self, model, alias):
        if not hasattr(model, "__columns__"):
            raise TypeError(f"Alias() takes a model, got {model!r}")
        if not alias or not isinstance(alias, str):
            raise TypeError("Alias() needs a non-empty string alias")
        self.model = model
        self.alias = alias
        self.__columns__ = model.__columns__
        self.__tablename__ = model.__tablename__

    def __getattr__(self, name):
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

    def __init__(self, query, alias):
        if not alias or not isinstance(alias, str):
            raise TypeError("subquery() needs a non-empty string alias")
        self.query = query
        self.alias = alias
        self.__tablename__ = None
        self.__columns__ = {
            name: _SubqueryColumn(name, py_type)
            for name, py_type in query.output_columns()
        }

    def __getattr__(self, name):
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


class Expression:
    """Anything that renders to a value-producing SQL fragment."""

    __slots__ = ()

    def to_sql(self, nxt, resolve=_bare):  # pragma: no cover - abstract
        raise NotImplementedError

    def sources(self):
        return ()

    # Comparisons build predicates rather than booleans.
    def __eq__(self, other):
        return Condition(self, "=", other)

    def __ne__(self, other):
        return Condition(self, "!=", other)

    def __gt__(self, other):
        return Condition(self, ">", other)

    def __ge__(self, other):
        return Condition(self, ">=", other)

    def __lt__(self, other):
        return Condition(self, "<", other)

    def __le__(self, other):
        return Condition(self, "<=", other)

    def in_(self, values):
        """`IN (...)`. Takes a sequence of values or a `Query` subquery."""
        return InClause(self, values, negated=False)

    def not_in(self, values):
        return InClause(self, values, negated=True)

    def like(self, pattern):
        return Condition(self, "LIKE", pattern)

    def is_null(self):
        return Condition(self, "=", None)

    def is_not_null(self):
        return Condition(self, "!=", None)

    def label(self, name):
        """Name this expression in the select list (`AS name`)."""
        return Labelled(self, name)

    def __hash__(self):
        return id(self)


class ColumnExpr(Expression):
    """A column reference, produced by class-level attribute access.

    `source` is the model class, `Alias` or `Subquery` the column belongs to.
    `model` is kept as an alias for it because that was the original name and it
    reads naturally when there is no alias involved.
    """

    __slots__ = ("source", "name", "py_type")

    def __init__(self, source, name, py_type):
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


class Labelled(Expression):
    """`expr AS name` in a select list."""

    __slots__ = ("expr", "name")

    def __init__(self, expr, name):
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


class Aggregate(Expression):
    """`count(x)`, `sum(x)`, and friends. Usable in a select list and in HAVING."""

    __slots__ = ("func", "operand", "distinct")

    def __init__(self, func, operand=None, distinct=False):
        self.func = func
        self.operand = operand
        self.distinct = distinct

    @property
    def py_type(self):
        # count() is always an integer; the rest depend on the column and on the
        # backend (Postgres avg() of an int is numeric, sum() is bigint), so they
        # are left untyped rather than guessed at — a wrong py_type would pick a
        # converter and corrupt the value.
        return int if self.func == "count" else None

    def to_sql(self, nxt, resolve=_bare):
        if self.operand is None:
            inner, params = "*", ()
        else:
            inner, params = self.operand.to_sql(nxt, resolve)
        prefix = "DISTINCT " if self.distinct else ""
        return f"{self.func}({prefix}{inner})", params

    def sources(self):
        return () if self.operand is None else self.operand.sources()

    def output_name(self):
        if self.operand is None:
            return f"{self.func}_all"
        return f"{self.func}_{self.operand.output_name()}"

    def __hash__(self):
        return id(self)

    def __repr__(self):
        inner = "*" if self.operand is None else repr(self.operand)
        return f"<Aggregate {self.func}({inner})>"


def count(column=None, distinct=False):
    """`count(*)` with no argument, `count(col)` with one."""
    return Aggregate("count", column, distinct)


def sum_(column, distinct=False):
    return Aggregate("sum", column, distinct)


def avg(column, distinct=False):
    return Aggregate("avg", column, distinct)


def min_(column):
    return Aggregate("min", column)


def max_(column):
    return Aggregate("max", column)


# --------------------------------------------------------------------------
# Predicates
# --------------------------------------------------------------------------


class Predicate(Expression):
    """Anything that renders to a boolean SQL fragment.

    `&`, `|` and `~` compose them. **Parenthesise the operands**: Python binds `&`
    and `|` tighter than the comparison operators, so `A.x > 1 | A.y < 2` parses as
    `A.x > (1 | A.y) < 2` and will not do what you meant. This is the same trap
    SQLAlchemy has, for the same reason.
    """

    __slots__ = ()

    def __and__(self, other):
        return and_(self, other)

    def __or__(self, other):
        return or_(self, other)

    def __invert__(self):
        return Not(self)

    def __hash__(self):
        return id(self)


class Condition(Predicate):
    """A single comparison: `left OP right`.

    `right` may be a Python value (bound as a parameter), another expression (a
    column-to-column comparison, which is what an ON clause is made of), None
    (rendered as IS NULL / IS NOT NULL), or a scalar `Query`.
    """

    __slots__ = ("left", "op", "right")

    def __init__(self, left, op, right):
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

    def __init__(self, op, parts):
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

    def __init__(self, part):
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

    def __init__(self, left, values, negated=False):
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

    def __init__(self, query, negated=False):
        self.query = query
        self.negated = negated

    def to_sql(self, nxt, resolve=_bare):
        advance = nxt if callable(nxt) else (lambda value=nxt: value)
        sql, params = self.query._render(advance)
        keyword = "NOT EXISTS" if self.negated else "EXISTS"
        return f"{keyword} ({sql})", tuple(params)

    def sources(self):
        return ()

    def __invert__(self):
        return ExistsClause(self.query, not self.negated)

    def __repr__(self):
        return f"<{'NOT EXISTS' if self.negated else 'EXISTS'}>"


def and_(*predicates):
    """Combine predicates with AND. One argument returns it unchanged."""
    return _combine("AND", predicates)


def or_(*predicates):
    """Combine predicates with OR. One argument returns it unchanged."""
    return _combine("OR", predicates)


def _combine(op, predicates):
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


def not_(predicate):
    if not isinstance(predicate, Predicate):
        raise TypeError(
            f"not_() takes a predicate, got {type(predicate).__name__}"
        )
    return ~predicate


def exists(query):
    """`EXISTS (query)`. Negate with `~exists(query)`."""
    if not hasattr(query, "_render"):
        raise TypeError(f"exists() takes a Query, got {type(query).__name__}")
    return ExistsClause(query)
