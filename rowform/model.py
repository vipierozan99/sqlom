"""Declaration: one class is both the SQLAlchemy `Table` and the row container.

    class Base(rowform.Base):
        pass

    class User(Base):
        __tablename__ = "users"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        email: Mapped[str | None]

    User.__table__          -> sa.Table, for create_all / Inspector / Alembic
    sa.select(User)         -> works, via __clause_element__ on the metaclass
    User.id > 100           -> a real sa.BinaryExpression
    user.id                 -> int, a plain dataclass attribute

There is no `Mapper`, no `instance_state`, and no instrumentation: instances are
stdlib dataclasses, and the hot read path fills them with `object.__new__` plus
straight attribute stores (`compile.py`).

**Why a base class and not a decorator.** `@sa_model(metadata)` would be a
decorator *factory*, and factories lose field typing entirely — `u.id` infers as
`Any` (docs/PLAN_CORE_COMPILER.md §5b, and the same propagation gap recorded at
docs/FINDINGS.md for `@model(tablename=...)`). A base class needs no arguments at
class-creation time because `metadata` lives on the base, which sidesteps the
factory problem. It is also SQLAlchemy's own shape: `dataclass_transform` sits on
the metaclass, and `DeclarativeBase` declares one.

The cost is a metaclass conflict: `class User(Base, ABC)` and combining with
`Protocol` raise `TypeError`. Accepted, not worked around (§9, R10).
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import types
import typing
import uuid
from typing import (
    Any,
    ClassVar,
    TypeVar,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
)

import sqlalchemy as sa
from sqlalchemy.orm import Mapped

from .errors import DeclarationError

# Where a built model class is recorded on its `Table`, so the statement planner
# (`planner.py`) can recover "these selected columns are a User" without being
# handed a registry. `Table.info` is a public, per-table dict SQLAlchemy never
# writes to itself.
MODEL_KEY = "rowform_model"

# The same record for a from clause that has no `.info` — a subquery or CTE
# handed to `alias(of=...)`. Only schema items carry `.info`.
MODEL_ATTR = "_rowform_model"

_M = TypeVar("_M")

# Set on a class the moment it is fully built, and copied forward by
# `dataclasses`' slots rebuild (which re-creates the class through this same
# metaclass). Its presence is what stops that rebuild re-entering the whole
# build a second time.
_BUILT = "__rowform_built__"

_RESERVED = frozenset(
    {"metadata", "registry", "type_annotation_map", "__table__", "__tablename__"}
)

#: `Mapped[<key>]` -> the SQLAlchemy type used for that column. Override per-base
#: by declaring `type_annotation_map` on your `Base`, or per-column by passing
#: `mapped_column(sa.Text())`.
DEFAULT_TYPE_MAP: dict[Any, sa.types.TypeEngine[Any]] = {
    bool: sa.Boolean(),
    int: sa.Integer(),
    float: sa.Float(),
    str: sa.String(),
    bytes: sa.LargeBinary(),
    datetime.datetime: sa.DateTime(),
    datetime.date: sa.Date(),
    datetime.time: sa.Time(),
    datetime.timedelta: sa.Interval(),
    decimal.Decimal: sa.Numeric(),
    uuid.UUID: sa.Uuid(),
    dict: sa.JSON(),
    list: sa.JSON(),
}


class _MappedColumn:
    """Marker left in the class body by `mapped_column()`.

    Never survives class creation: the metaclass reads it, builds an `sa.Column`
    from it, and rebuilds the namespace without it. That ordering is
    load-bearing — `dataclasses` probes `getattr(cls, field_name)` for a default,
    so a marker still sitting on the class would become every field's default
    value (docs/FINDINGS.md, "The `@model` metaclass").
    """

    __slots__ = ("args", "default", "default_factory", "init", "kwargs")

    def __init__(self, args, kwargs, default, default_factory, init):
        self.args = args
        self.kwargs = kwargs
        self.default = default
        self.default_factory = default_factory
        self.init = init


def mapped_column(
    *args: Any,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    init: bool = True,
    **kwargs: Any,
) -> Any:
    """Per-column overrides. Everything not named below goes straight to `sa.Column`.

        id: Mapped[int] = mapped_column(primary_key=True)
        body: Mapped[str] = mapped_column(sa.Text())
        slug: Mapped[str] = mapped_column("url_slug", unique=True)
        owner: Mapped[int] = mapped_column(sa.ForeignKey("users.id"))

    `default`/`default_factory`/`init` configure the generated dataclass
    `__init__`; `default` is also passed to `sa.Column` so INSERTs see it. A
    field with `init=False` and no default is simply absent from `__init__` and
    left unset until a row hydrates it.

    Returns `Any` rather than a marker type so `id: Mapped[int] = mapped_column()`
    typechecks; it is declared as a `dataclass_transform` field specifier below.
    """
    if default is not dataclasses.MISSING and "default" not in kwargs:
        kwargs["default"] = default
    return _MappedColumn(args, kwargs, default, default_factory, init)


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """`Mapped[str | None]` -> (str, nullable=True)."""
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        present = [a for a in get_args(annotation) if a is not type(None)]
        if len(present) != 1:
            raise DeclarationError(
                f"Mapped[{annotation}] is a union of more than one non-None type; "
                f"rowform maps one Python type to one column"
            )
        return present[0], True
    return annotation, False


def _sa_type(py_type: Any, type_map: dict[Any, Any]) -> sa.types.TypeEngine[Any]:
    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        return sa.Enum(py_type)
    for candidate in (py_type, get_origin(py_type)):
        if candidate is None:
            continue
        try:
            found = type_map.get(candidate)
        except TypeError:  # unhashable annotation
            continue
        if found is not None:
            return found
    raise DeclarationError(
        f"no SQLAlchemy type registered for {py_type!r}. Add it to your Base's "
        f"`type_annotation_map`, or name the type explicitly with "
        f"`mapped_column(sa.SomeType())`."
    )


@dataclass_transform(field_specifiers=(mapped_column,))
class ModelMeta(type):
    """Builds the `Table` and the dataclass from one set of `Mapped[]` annotations.

    `dataclass_transform` sits here rather than on a decorator so field types
    survive into the checker (§5b). Class keyword arguments are forwarded to
    `dataclasses.dataclass`, so `class User(Base, frozen=True)` does what it
    looks like.
    """

    def __new__(mcls, name, bases, ns, **dc_kwargs):
        # A slots rebuild re-enters here with an already-built namespace; let it
        # through untouched or the whole build would run twice.
        if _BUILT in ns:
            return super().__new__(mcls, name, bases, ns)

        # The root `Base` has no ModelMeta ancestor and declares no fields.
        if not any(isinstance(b, ModelMeta) for b in bases):
            return super().__new__(mcls, name, bases, ns)

        # Created only to resolve string annotations against the real MRO;
        # discarded in favour of `built` below.
        probe = super().__new__(mcls, name, bases, dict(ns))
        specs = _collect_specs(probe, bases, ns)
        fields = _build_fields(probe, specs)

        abstract = ns.get("__abstract__", False) or "__tablename__" not in ns
        if not fields:
            if not abstract:
                raise DeclarationError(
                    f"{name} declares __tablename__ but no Mapped[] fields, so it "
                    f"would build a table with no columns"
                )
            # A user's own `Base`: carries `metadata` and nothing else. Left as a
            # plain (non-dataclass) class on purpose — making it a field-less
            # dataclass would make every model inherit dataclass-ness from it, and
            # stdlib then refuses `class User(Base, frozen=True)` with "cannot
            # inherit frozen dataclass from a non-frozen one".
            #
            # `__slots__ = ()` so the base contributes no `__dict__` to the MRO.
            # A model that opts into `slots=True` is then *fully* slotted — no
            # per-instance `__dict__` at all — which is the only layout that
            # actually saves memory and GC-traversal cost (a slotted class under a
            # dict-carrying base keeps the managed-dict overhead and saves
            # neither). A default model declares no `__slots__`, so it re-acquires
            # its own `__dict__` and keeps orjson's fast native-dict path.
            slotted = dict(ns)
            slotted.setdefault("__slots__", ())
            return super().__new__(mcls, name, bases, slotted)

        namespace: dict[str, Any] = {
            key: value
            for key, value in ns.items()
            if key not in fields and key not in ("__dict__", "__weakref__")
        }
        namespace["__annotations__"] = {n: f.py_type for n, f in fields.items()}
        for field_name, field in fields.items():
            declared = field.dataclass_field()
            if declared is not None:
                namespace[field_name] = declared
        namespace["__rowform_specs__"] = specs
        namespace[_BUILT] = True

        built: Any = super().__new__(mcls, name, bases, namespace)
        try:
            built = dataclasses.dataclass(**dc_kwargs)(built)
        except TypeError as err:
            if "follows default argument" not in str(err):
                raise
            # The class body reads fine; what reordered it is that inherited
            # fields sort ahead of own fields (§5b-i). Say so, since the stdlib
            # message names two fields the author never wrote in that order.
            raise DeclarationError(
                f"{name}: {err}. Fields inherited from a base or mixin come before "
                f"this class's own fields ({', '.join(fields)}), so a base field "
                f"with a default blocks every field declared after it. Declare "
                f"`class {name}(..., kw_only=True)`, or give the later fields "
                f"defaults too."
            ) from err

        columns = {n: f.column for n, f in fields.items()}
        if not abstract:
            table = sa.Table(
                ns["__tablename__"],
                built.metadata,
                *columns.values(),
                *ns.get("__table_args__", ()),
            )
            table.info[MODEL_KEY] = built
            built.__table__ = table

        # Enables the metaclass interception below, so `User.id` is the Column.
        # Set last, and only on a class that is finished: while it is absent,
        # `__getattribute__` delegates everything, which is what lets
        # `dataclasses` probe for defaults above without seeing Columns.
        built.__columns__ = columns
        built.__column_order__ = tuple(columns)
        return built

    # Runtime only. A `__getattribute__` returning `Any` would make *every*
    # class-level attribute valid to a type checker, so `User.typo` would stop
    # being an error — and the declared `Mapped[]` fields already resolve
    # correctly without it, through `Mapped.__get__`'s overloads.
    if not typing.TYPE_CHECKING:

        def __getattribute__(cls, key: str) -> Any:
            """`User.id` -> `sa.Column`, `user.id` -> the value.

            Interception has to live on the metaclass because the attribute is
            being read off the *class*. Instance reads never come through here,
            so a hydrated `user.id` is an ordinary attribute load with nothing
            in the way.

            Gated on this class's **own** `__columns__`, which exists only once
            `__new__` has finished. Own rather than inherited is load-bearing:
            while a subclass is still being built, an inherited `__columns__`
            would be visible, so `dataclasses`' `getattr(cls, field_name)`
            default probe would see the *base's* `Column` and make it every
            inherited field's default value (docs/FINDINGS.md, "The `@model`
            metaclass" — the same trap, one level up).
            """
            columns = type.__getattribute__(cls, "__dict__").get("__columns__")
            if columns is not None and key in columns:
                return columns[key]
            return type.__getattribute__(cls, key)

    def __clause_element__(cls) -> Any:
        """The hook that makes `sa.select(User)`, `.join(User)` and
        `select_from(User)` treat the class as its `Table`. SQLAlchemy's coercion
        layer honours `__clause_element__`; on a class it must live on the
        metaclass."""
        try:
            return type.__getattribute__(cls, "__table__")
        except AttributeError:
            raise DeclarationError(
                f"{cls.__name__} is abstract (no __tablename__), so it has no table "
                f"to select from"
            ) from None


class _Spec:
    """A declared field, resolved but not yet turned into an `sa.Column`.

    Kept per-class as `__rowform_specs__` so a subclass can inherit the
    declaration without inheriting the `Column` object — a `Column` belongs to
    exactly one `Table`, so every concrete model has to build its own.
    """

    __slots__ = ("marker", "nullable", "py_type")

    def __init__(self, py_type, nullable, marker):
        self.py_type = py_type
        self.nullable = nullable
        self.marker = marker


class _Field:
    __slots__ = ("column", "default", "default_factory", "init", "py_type")

    def __init__(self, py_type, column, default, default_factory, init):
        self.py_type = py_type
        self.column = column
        self.default = default
        self.default_factory = default_factory
        self.init = init

    def dataclass_field(self):
        """The value to leave in the rebuilt namespace, or None to leave the name
        bare so `dataclasses` makes the field required."""
        if (
            self.default is dataclasses.MISSING
            and self.default_factory is dataclasses.MISSING
            and self.init
        ):
            return None
        kwargs: dict[str, Any] = {"init": self.init}
        if self.default is not dataclasses.MISSING:
            kwargs["default"] = self.default
        if self.default_factory is not dataclasses.MISSING:
            kwargs["default_factory"] = self.default_factory
        return dataclasses.field(**kwargs)


def _collect_specs(cls: type, bases: tuple[type, ...], ns: dict[str, Any]) -> dict[str, _Spec]:
    """Inherited declarations first (reverse MRO), then this class's own.

    Resolved from `__rowform_specs__` on the bases rather than from
    `get_type_hints`: the built class rewrites `__annotations__` to bare Python
    types so `dataclasses` sees them, which erases the `Mapped[]` wrapper a base
    scan would look for.

    **Inherited-first is a migration hazard** (§5b-i, R11): adding a mixin moves
    its columns to the front of `CREATE TABLE`, and Alembic autogenerate does not
    diff column *order*, so the drift is invisible. The order is at least
    deterministic and recorded on the built class as `__column_order__`; pin it
    with an explicit `__column_order__` in the class body when the table already
    exists.

    It does *not* affect hydration — hydrators are planned from
    `stmt.selected_columns`, never from declaration order (`planner.py`, §5c).
    """
    specs: dict[str, _Spec] = {}
    for base in reversed(cls.__mro__[1:]):
        specs.update(getattr(base, "__rowform_specs__", None) or {})

    own_names = ns.get("__annotations__", {})
    hints = get_type_hints(cls, include_extras=True)
    for field_name in own_names:
        hint = hints.get(field_name)
        if get_origin(hint) is not Mapped:
            continue
        if field_name in _RESERVED:
            raise DeclarationError(
                f"{cls.__name__}.{field_name} collides with a reserved name "
                f"({', '.join(sorted(_RESERVED))})"
            )
        py_type, nullable = _unwrap_optional(get_args(hint)[0])
        marker = ns.get(field_name)
        if not isinstance(marker, _MappedColumn):
            marker = _MappedColumn((), {}, dataclasses.MISSING, dataclasses.MISSING, True)
        specs[field_name] = _Spec(py_type, nullable, marker)

    pinned = ns.get("__column_order__")
    if pinned is not None:
        if set(pinned) != set(specs):
            raise DeclarationError(
                f"{cls.__name__}.__column_order__ must list every Mapped[] field "
                f"exactly once; missing {sorted(set(specs) - set(pinned))}, "
                f"unknown {sorted(set(pinned) - set(specs))}"
            )
        specs = {n: specs[n] for n in pinned}
    return specs


def _build_fields(cls: type, specs: dict[str, _Spec]) -> dict[str, _Field]:
    """One fresh `sa.Column` per declared field. Fresh because a `Column` can
    belong to only one `Table`, so an inherited spec cannot reuse its base's."""
    type_map = {**DEFAULT_TYPE_MAP, **getattr(cls, "type_annotation_map", {})}
    fields: dict[str, _Field] = {}
    for field_name, spec in specs.items():
        marker = spec.marker
        kwargs = dict(marker.kwargs)
        kwargs.setdefault("nullable", spec.nullable)
        if kwargs.get("primary_key"):
            kwargs["nullable"] = False
        column = sa.Column(
            *_column_args(field_name, marker.args, spec.py_type, type_map), **kwargs
        )
        fields[field_name] = _Field(
            spec.py_type | None if spec.nullable else spec.py_type,
            column,
            marker.default,
            marker.default_factory,
            marker.init,
        )
    return fields


def _column_args(
    field_name: str, args: tuple[Any, ...], py_type: Any, type_map: dict[Any, Any]
) -> tuple[Any, ...]:
    """`sa.Column` reads its positionals by kind and wants them in the order
    (name, type, *schema items), so the annotation-derived type has to be spliced
    into the middle rather than appended.

    A leading string renames the column; an explicit `TypeEngine` overrides the
    annotation; `ForeignKey`/`Constraint`/... pass through untouched.
    """
    name = field_name
    rest = list(args)
    if rest and isinstance(rest[0], str):
        name = rest.pop(0)

    explicit = next(
        (
            a
            for a in rest
            if isinstance(a, sa.types.TypeEngine)
            or (isinstance(a, type) and issubclass(a, sa.types.TypeEngine))
        ),
        None,
    )
    if explicit is None:
        return (name, _sa_type(py_type, type_map), *rest)
    rest.remove(explicit)
    return (name, explicit, *rest)


class Base(metaclass=ModelMeta):
    """Subclass this to make your own base, then declare models against it.

    `metadata` is the single thing Alembic needs — `target_metadata = Base.metadata`
    is the most-copied line in every `env.py`. A subclass that declares its own
    `metadata = sa.MetaData()` gets a separate schema; otherwise every model in
    the process shares this one.

    `__slots__ = ()` so the base itself contributes no `__dict__`; the metaclass
    does the same for the field-less abstract base a user derives (see
    `__new__`), which is what lets a `slots=True` model be fully slotted.
    """

    __slots__ = ()

    metadata = sa.MetaData()

    #: Extends (and overrides) `DEFAULT_TYPE_MAP` for models under this base.
    #: Read at class creation and never mutated, so one shared empty mapping is
    #: the right default; declare your own on your Base to add entries.
    type_annotation_map: ClassVar[dict[Any, sa.types.TypeEngine[Any]]] = {}

    if typing.TYPE_CHECKING:
        # Present on every concrete model; declared here so callers and the
        # planner can read them without a per-class ignore. ClassVar is
        # load-bearing, not decoration: `dataclass_transform` turns a bare
        # annotation into a field, so these would become required constructor
        # parameters on every model.
        __table__: ClassVar[sa.Table]
        __tablename__: ClassVar[str]
        __columns__: ClassVar[dict[str, sa.Column[Any]]]
        __column_order__: ClassVar[tuple[str, ...]]


def model_for(from_clause: Any) -> type[Any] | None:
    """The model class a `FromClause` yields rows of, if any.

    A `Table` carries it in `.info`; a subquery or CTE passed to `alias(of=...)`
    carries it in `MODEL_ATTR`, since only schema items have an `.info`. An alias
    of either resolves through `.element`.
    """
    info = getattr(from_clause, "info", None)
    if isinstance(info, dict) and MODEL_KEY in info:
        return info[MODEL_KEY]
    marked = getattr(from_clause, MODEL_ATTR, None)
    if marked is not None:
        return marked
    element = getattr(from_clause, "element", None)
    if element is not None and element is not from_clause:
        return model_for(element)
    return None


class _Alias:
    """Runtime half of `alias()`: coerces to the from clause, resolves field names.

    Field names rather than `.c` names, because the two differ whenever a column
    was renamed — `slug: Mapped[str] = mapped_column("url_slug")` is `a.slug`
    here and `a.c.url_slug` on the from clause.
    """

    __slots__ = ("_columns", "_from", "_model")

    def __init__(self, model: type[Any], from_clause: Any):
        self._model = model
        self._from = from_clause
        declared = type.__getattribute__(model, "__columns__")
        self._columns = {name: from_clause.columns[col.key] for name, col in declared.items()}

    def __clause_element__(self) -> Any:
        return self._from

    def __getattr__(self, key: str) -> Any:
        try:
            return self._columns[key]
        except KeyError:
            raise AttributeError(
                f"{self._model.__name__} has no column {key!r}; an alias exposes "
                f"that model's fields and nothing else"
            ) from None

    def __repr__(self) -> str:
        name = getattr(self._from, "name", None) or "<unnamed>"
        return f"<alias {self._model.__name__} AS {name}>"


def alias(model: type[_M], name: str | None = None, *, of: Any = None) -> type[_M]:
    """A second reference to a model's rows: another alias of its table, or a
    subquery/CTE that yields them.

        mgr = rowform.alias(User, "mgr")
        sa.select(User, mgr).join(mgr, User.manager_id == mgr.id)

        newest = sa.select(User).order_by(User.id.desc()).limit(10).subquery()
        top = rowform.alias(User, of=newest)
        sa.select(top).where(top.active)

    `sa.orm.aliased()` cannot serve here: it inspects its argument for a `Mapper`,
    and a rowform model has none (`NoInspectionAvailable`). `sa.alias(User)` does
    work and already hydrates — `planner.py` resolves each declared column through
    the `FromClause` actually selected — but its columns are reached as
    `a.c.name`, typed `Column[Any]`, so the entity degrades to `Any` in
    `fetch_all`'s row type.

    Declared as returning `type[_M]` so that `mgr.name` and `select(User, mgr)`
    infer exactly as the model does. That is the same type-level fiction as
    `User.id`, an `sa.Column` declared as `InstrumentedAttribute`, and it is the
    only shape that keeps per-field types: an alias class of its own could only
    offer `__getattr__`, which erases them.

    `of=` records the model **on the from clause given**, not on a wrapper of it,
    so `of.c.id` and the returned alias's `.id` stay the same column — wrapping
    would make `select(top, newest.c.id)` two from clauses and a cartesian
    product. The mark is a statement of fact about those rows, and it is why
    `_require_exact_columns` refuses anything but an exact match.
    """
    if of is None:
        try:
            table = type.__getattribute__(model, "__table__")
        except AttributeError:
            raise DeclarationError(
                f"{model.__name__} is abstract (no __tablename__), so it has no table to alias"
            ) from None
        source = table.alias(name)
    else:
        if name is not None:
            raise DeclarationError(
            "pass a name to .subquery()/.cte() itself, not to alias(of=...)"
        )
        source = of
        _require_exact_columns(model, source)
        setattr(source, MODEL_ATTR, model)
    return typing.cast("type[_M]", _Alias(model, source))


def _require_exact_columns(model: type[Any], from_clause: Any) -> None:
    """`of=` demands the model's columns, in order, and nothing else.

    `select(alias)` expands to every column of its from clause — SQLAlchemy's
    coercion has no notion of "the entity's columns" without a `Mapper`. So a
    subquery carrying one extra column would hydrate as `(User, extra)` while
    still typed `Select[tuple[User]]`, and a reordered one would degrade to
    scalars. Both are silent, so both are refused here instead.
    """
    if not isinstance(from_clause, sa.FromClause):
        raise DeclarationError(
            f"alias(of=...) needs a FromClause — a subquery, CTE, alias or table — "
            f"not {type(from_clause).__name__}. A Select becomes one with "
            f".subquery() or .cte()."
        )

    declared = type.__getattribute__(model, "__columns__")
    want = [col.key for col in declared.values()]
    got = list(from_clause.columns.keys())
    if got != want:
        raise DeclarationError(
            f"alias({model.__name__}, of=...) needs exactly that model's columns, "
            f"in order: expected {want}, got {got}. `select()` on a from clause "
            f"expands to all of its columns, so an extra or reordered one would "
            f"change the rows without changing the type. Narrow the subquery to "
            f"these columns — filter on the extras inside it."
        )
