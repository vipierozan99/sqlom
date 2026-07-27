"""Descriptor-based column and expression primitives.

This is the real (if minimal) implementation of the pattern described in
the README: a metaclass owns both `__slots__` generation and the
descriptor protocol, so `Column` can behave as a live instance accessor
*and* return a query expression at class-level access.
"""


# `x = NULL` is never true in SQL — comparison against NULL yields unknown, so
# a row is neither matched nor excluded. Equality operators against None have to
# become IS / IS NOT instead, which also means they bind no parameter.
_NULL_FORMS = {"=": "IS NULL", "!=": "IS NOT NULL"}


def _bare(model, name):
    """Default column renderer: the bare name, no table qualifier.

    A single-table query needs no qualifier, and adding one would change the SQL
    every existing benchmark emits. `Query` passes a qualifying resolver instead
    as soon as a join makes bare names ambiguous.
    """
    return name


class ColumnExpr:
    """Returned by `Column.__get__` when accessed on the class rather than
    an instance. Comparisons build a `Condition` instead of a bool."""

    __slots__ = ("model", "name", "py_type")

    def __init__(self, model, name, py_type):
        self.model = model
        self.name = name
        self.py_type = py_type

    def __eq__(self, other):
        return Condition(self.model, self.name, "=", other)

    def __ne__(self, other):
        return Condition(self.model, self.name, "!=", other)

    def __gt__(self, other):
        return Condition(self.model, self.name, ">", other)

    def __ge__(self, other):
        return Condition(self.model, self.name, ">=", other)

    def __lt__(self, other):
        return Condition(self.model, self.name, "<", other)

    def __le__(self, other):
        return Condition(self.model, self.name, "<=", other)

    def __hash__(self):
        return hash((self.model, self.name))

    def __repr__(self):
        return f"<ColumnExpr {self.model.__name__}.{self.name}>"


class Condition:
    """A single predicate: `column OP value`, or `column OP other_column`.

    Built by comparing a `ColumnExpr` at class scope — `User.id > 100` for the
    value form, `Post.user_id == User.id` for the column form, which is what an
    ON clause is made of.

    Carries the model it came from so a query can reject a predicate built
    against a different one — two models with an `id` column would otherwise
    produce a silently wrong `WHERE id = $1`.
    """

    __slots__ = ("model", "column_name", "op", "value")

    def __init__(self, model, column_name, op, value):
        self.model = model
        self.column_name = column_name
        self.op = op
        self.value = value

    @property
    def is_column_comparison(self):
        """True when the right-hand side is another column rather than a value."""
        return isinstance(self.value, ColumnExpr)

    def to_sql(self, placeholder="?", resolve=_bare):
        """Return `(clause, params)`.

        `params` is a tuple rather than a single value because a predicate does
        not always bind one. Two cases bind nothing:

        * `User.email == None` must render `email IS NULL`, not `email = $1`
          with NULL bound, which matches no row.
        * `Post.user_id == User.id` is a column-to-column comparison — the
          right-hand side is SQL, not a parameter.

        `resolve(model, name) -> str` renders a column reference. It qualifies
        with the table name once a join is present and returns the bare name
        otherwise.
        """
        left = resolve(self.model, self.column_name)
        if self.is_column_comparison:
            return f"{left} {self.op} {resolve(self.value.model, self.value.name)}", ()
        if self.value is None and self.op in _NULL_FORMS:
            return f"{left} {_NULL_FORMS[self.op]}", ()
        return f"{left} {self.op} {placeholder}", (self.value,)

    def models(self):
        """Every model this predicate references — one, or two for a join clause."""
        if self.is_column_comparison:
            return (self.model, self.value.model)
        return (self.model,)

    def __repr__(self):
        model = getattr(self.model, "__name__", self.model)
        if self.is_column_comparison:
            other = getattr(self.value.model, "__name__", "?")
            return (f"<Condition {model}.{self.column_name} {self.op} "
                    f"{other}.{self.value.name}>")
        return f"<Condition {model}.{self.column_name} {self.op} {self.value!r}>"


class Column:
    def __init__(self, py_type: type):
        self.py_type = py_type
        self.name = None
        self._storage_name = None

    def __set_name__(self, owner, name):
        self.name = name
        self._storage_name = f"_{name}"

    def __get__(self, obj, owner=None):
        if obj is None:
            return ColumnExpr(owner, self.name, self.py_type)
        return getattr(obj, self._storage_name)

    def __set__(self, obj, value):
        setattr(obj, self._storage_name, value)


class ModelMeta(type):
    """Owns slot generation instead of delegating to @dataclass, since a
    Column descriptor and a same-named slot can't coexist (see README)."""

    def __new__(mcs, name, bases, namespace):
        columns = {
            key: value for key, value in namespace.items() if isinstance(value, Column)
        }
        namespace["__slots__"] = tuple(f"_{col_name}" for col_name in columns)
        namespace["__columns__"] = columns
        return super().__new__(mcs, name, bases, namespace)

    def __getattr__(cls, name):
        # Compile the per-model orjson hook on first use, then cache it as a
        # real class attribute so later lookups never reach __getattr__.
        if name == "__json_default__":
            from .compile import compile_json_default

            fn = compile_json_default(cls)
            setattr(cls, name, fn)
            return fn
        raise AttributeError(name)


def hydrate(model_cls, row):
    """Build an instance from a positional row (tuple/Record) without going
    through __init__ or per-field keyword dispatch.

    The width check is not ceremony: `zip` stops at the shorter side, so a row
    with too few values would produce an object with unset slots that only fails
    later at read time, and a row with too many would silently drop columns. The
    compiled hydrators get this for free from tuple unpacking; this reflective
    path has to ask.
    """
    columns = model_cls.__columns__
    if len(row) != len(columns):
        raise ValueError(
            f"{model_cls.__name__} has {len(columns)} columns "
            f"({', '.join(columns)}) but the row has {len(row)} values"
        )
    obj = object.__new__(model_cls)
    for column, value in zip(columns.values(), row):
        setattr(obj, column._storage_name, value)
    return obj


def as_dict(obj):
    """orjson `default=` hook: sqlom models aren't stdlib dataclasses, so
    orjson can't introspect them natively."""
    cls = type(obj)
    if hasattr(cls, "__columns__"):
        return {
            name: getattr(obj, column._storage_name)
            for name, column in cls.__columns__.items()
        }
    raise TypeError(f"Cannot serialize {cls!r}")
