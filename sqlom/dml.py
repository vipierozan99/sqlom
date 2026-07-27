"""INSERT, UPDATE and DELETE, with RETURNING.

Each builder renders to `(sql, params)` exactly as `Query` does, and when
`.returning(...)` is set it also exposes the hydration interface — so a statement
with RETURNING goes through `engine.fetch_all()` and its rows hydrate into models or
tuples like any select. Without RETURNING it goes through `engine.execute()`, which
reports how many rows were affected.

**Bulk inserts are one statement, not `executemany`.** `values([...])` renders a
multi-row `VALUES (...), (...)`, which is a single round trip and — unlike
`executemany` on asyncpg — supports RETURNING. The cost is a parameter per column
per row against Postgres's 65535-parameter limit, so `max_rows_per_statement()`
computes the ceiling for a given model and `values()` refuses to exceed it rather
than letting the server reject the batch.

**Why these are safe for the conditional session reset.** `DatabaseEngine` skips the
pool's `RESET ALL` unless a connection was dirtied, on the grounds that a
parameterised statement from this library cannot leave session state behind. That
holds for DML as much as for SELECT: an INSERT/UPDATE/DELETE built here has no `SET`,
no `LISTEN`, no temp table and no advisory lock. What it *does* do is write, and both
pools run in autocommit for a lone statement — so a DML statement executed outside
`transaction()` commits on its own. Group writes in a transaction when they need to
be atomic.
"""

from __future__ import annotations

from typing import Any, Iterable, Self

from .expr import Alias, ColumnExpr, Expression, Predicate, source_name
from .query import _and_join, _columns_in, _non_negative_int, _placeholders

# Postgres refuses a statement with more than 65535 bound parameters. sqlite's
# default SQLITE_MAX_VARIABLE_NUMBER is 32766 on recent builds, so the lower bound
# is used: exceeding it is a server error either way, and the point is to fail where
# the batch is built rather than where it is sent.
MAX_PARAMETERS = 32766


def max_rows_per_statement(model: Any, columns: Iterable[str] | None = None) -> int:
    """How many rows one bulk INSERT can carry for this model."""
    width = len(list(columns)) if columns is not None else len(model.__columns__)
    return max(1, MAX_PARAMETERS // max(1, width))


class _Statement:
    """Shared plumbing: the target table, RETURNING, and the engine interface.

    Not generic in a row type. `returning()` is chained after construction, so it
    cannot re-parameterise the statement the way `Query`'s constructor overloads
    can — a checker would have to re-type an existing object, which it cannot. So
    `fetch_all()` on a statement with RETURNING is `list[Any]`, stated rather than
    faked with a type variable that resolves to Never.
    """

    def __init__(self, target: type[Any] | Alias[Any]) -> None:
        if isinstance(target, Expression) or not hasattr(target, "__columns__"):
            raise TypeError(
                f"expected a model or Alias to write to, got {target!r}"
            )
        self.source = target
        self._returning: list[tuple[str, Any]] = []
        self._sql_cache: dict[Any, Any] = {}

    @property
    def model(self) -> Any:
        return getattr(self.source, "model", self.source)

    def _invalidate(self) -> None:
        self._sql_cache.clear()

    # --- RETURNING ---------------------------------------------------------

    def returning(self, *entities: type[Any] | Alias[Any] | Expression[Any]) -> Self:
        """Ask the database for the affected rows.

        Takes the same things a select does — a model for whole rows, columns for
        scalars — and the result hydrates identically, so
        `await engine.fetch_all(stmt)` gives `list[Model]` or `list[tuple[...]]`.
        """
        for entity in entities:
            if isinstance(entity, Expression):
                for column in _columns_in(entity):
                    self._check_column(column)
                self._returning.append(("expr", entity))
            elif hasattr(entity, "__columns__"):
                if entity is not self.source:
                    raise ValueError(
                        f"returning() can only reference {source_name(self.source)}, "
                        f"the table being written to"
                    )
                self._returning.append(("model", entity))
            else:
                raise TypeError(
                    f"returning() takes the model or its columns, got {entity!r}"
                )
        self._invalidate()
        return self

    def _check_column(self, column: ColumnExpr[Any]) -> None:
        if column.source is not self.source:
            raise ValueError(
                f"{source_name(column.source)} is not the table being written to "
                f"({source_name(self.source)})"
            )
        if column.name not in self.source.__columns__:
            raise ValueError(
                f"{source_name(self.source)} has no column {column.name!r}"
            )

    @property
    def returns_rows(self) -> bool:
        return bool(self._returning)

    # --- the interface the engines rely on, mirroring Query ----------------

    @property
    def is_multi_entity(self) -> bool:
        return not (len(self._returning) == 1 and self._returning[0][0] == "model")

    @property
    def _hydration_key(self) -> Any:
        if not self.is_multi_entity:
            return self.model
        return tuple(
            ("model", getattr(entity, "model", entity), False) if kind == "model"
            else ("expr", id(entity), getattr(entity, "py_type", None))
            for kind, entity in self._returning
        )

    def hydration_spec(self) -> list[tuple[Any, ...]]:
        specs: list[tuple[Any, ...]] = []
        for kind, entity in self._returning:
            if kind == "model":
                specs.append(("model", getattr(entity, "model", entity), False))
            else:
                specs.append(("column", getattr(entity, "py_type", None)))
        return specs

    def output_columns(self) -> list[tuple[str, Any]]:
        columns: list[tuple[str, Any]] = []
        for kind, entity in self._returning:
            if kind == "model":
                columns.extend(
                    (name, column.py_type)
                    for name, column in entity.__columns__.items()
                )
            else:
                columns.append((entity.output_name(), getattr(entity, "py_type", None)))
        return columns

    # --- rendering ---------------------------------------------------------

    def _table_sql(self) -> str:
        if isinstance(self.source, Alias):
            return f"{self.source.__tablename__} AS {self.source.alias}"
        return self.source.__tablename__

    def _resolver(self) -> Any:
        # A DML statement names exactly one table, so columns are never ambiguous
        # and stay bare — which also keeps RETURNING portable.
        return lambda source, name: name

    def _returning_sql(self, advance: Any, params: list[Any]) -> str:
        if not self._returning:
            return ""
        resolve = self._resolver()
        parts: list[str] = []
        for kind, entity in self._returning:
            if kind == "model":
                parts.extend(resolve(entity, name) for name in entity.__columns__)
            else:
                render = getattr(entity, "select_sql", entity.to_sql)
                sql, extra = render(advance, resolve)
                params.extend(extra)
                parts.append(sql)
        return " RETURNING " + ", ".join(parts)

    def to_sql(self, placeholder: str = "?") -> tuple[str, tuple[Any, ...]]:
        cached = self._sql_cache.get(placeholder)
        if cached is not None:
            return cached
        sql, params = self._render(_placeholders(placeholder))
        result = (sql, tuple(params))
        self._sql_cache[placeholder] = result
        return result

    def _render(self, nxt: Any = None, resolve: Any = None) -> tuple[str, list[Any]]:
        raise NotImplementedError


class Insert(_Statement):
    """`INSERT INTO t (...) VALUES (...)`, one row or many.

        Insert(User).values(name="ada", email="a@b.c")
        Insert(User).values([{"name": "ada"}, {"name": "bo"}]).returning(User.id)
    """

    def __init__(self, target: type[Any] | Alias[Any]) -> None:
        super().__init__(target)
        self._columns: list[str] = []
        self._rows: list[tuple[Any, ...]] = []

    def values(self, rows: Any = None, /, **kwargs: Any) -> Self:
        """One row from keyword arguments, or many from a sequence of dicts.

        Every row must have the same keys. A missing key in a later row would
        otherwise either shift values into the wrong columns or need a per-row
        statement, and silently doing either is worse than refusing.
        """
        if rows is not None and kwargs:
            raise TypeError("values() takes either a sequence of rows or keywords")
        if rows is None:
            if not kwargs:
                raise TypeError("values() needs at least one column")
            batch = [kwargs]
        elif isinstance(rows, dict):
            batch = [rows]
        else:
            batch = list(rows)
            if not batch:
                raise ValueError("values() got an empty sequence of rows")
            if not all(isinstance(row, dict) for row in batch):
                raise TypeError("values() takes a sequence of dicts")

        columns = list(batch[0])
        for name in columns:
            if name not in self.source.__columns__:
                raise ValueError(
                    f"{source_name(self.source)} has no column {name!r}"
                )
        for index, row in enumerate(batch):
            if list(row) != columns:
                raise ValueError(
                    f"every row must set the same columns; row {index} has "
                    f"{sorted(row)} against {sorted(columns)}"
                )

        if self._columns and self._columns != columns:
            raise ValueError(
                f"values() was already called with columns {self._columns}; "
                f"cannot add rows setting {columns}"
            )
        self._columns = columns

        ceiling = max_rows_per_statement(self.model, columns)
        if len(self._rows) + len(batch) > ceiling:
            raise ValueError(
                f"{len(self._rows) + len(batch)} rows x {len(columns)} columns "
                f"exceeds the {MAX_PARAMETERS}-parameter statement limit "
                f"({ceiling} rows max for this shape). Split the batch."
            )
        self._rows.extend(tuple(row[name] for name in columns) for row in batch)
        self._invalidate()
        return self

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def _render(self, nxt: Any = None, resolve: Any = None) -> tuple[str, list[Any]]:
        if not self._rows:
            raise ValueError("Insert has no rows; call values() first")
        if nxt is None:
            def nxt():
                return "?"
        params: list[Any] = []
        groups = []
        for row in self._rows:
            placeholders = []
            for value in row:
                placeholders.append(nxt())
                params.append(value)
            groups.append(f"({', '.join(placeholders)})")
        sql = (f"INSERT INTO {self._table_sql()} ({', '.join(self._columns)}) "
               f"VALUES {', '.join(groups)}")
        sql += self._returning_sql(nxt, params)
        return sql, params

    def __repr__(self) -> str:
        return f"<Insert {source_name(self.source)} x{len(self._rows)}>"


class Update(_Statement):
    """`UPDATE t SET ... WHERE ...`.

        Update(User).set(active=False).where(User.id == 1).returning(User.id)

    Values may be expressions, so a read-modify-write becomes one statement:

        Update(Post).set(score=Post.score + 1).where(Post.id == 1)
    """

    def __init__(self, target: type[Any] | Alias[Any]) -> None:
        super().__init__(target)
        self._assignments: list[tuple[str, Any]] = []
        self._conditions: list[Predicate] = []

    def set(self, **kwargs: Any) -> Self:
        for name, value in kwargs.items():
            if name not in self.source.__columns__:
                raise ValueError(
                    f"{source_name(self.source)} has no column {name!r}"
                )
            if isinstance(value, Expression):
                for column in _columns_in(value):
                    self._check_column(column)
            self._assignments.append((name, value))
        self._invalidate()
        return self

    def where(self, *predicates: Predicate) -> Self:
        for predicate in predicates:
            if not isinstance(predicate, Predicate):
                raise TypeError(
                    f"where() takes a predicate, got {type(predicate).__name__}"
                )
            for column in _columns_in(predicate):
                self._check_column(column)
            self._conditions.append(predicate)
        self._invalidate()
        return self

    def _render(self, nxt: Any = None, resolve: Any = None) -> tuple[str, list[Any]]:
        if not self._assignments:
            raise ValueError("Update has no assignments; call set() first")
        if nxt is None:
            def nxt():
                return "?"
        resolve = self._resolver()
        params: list[Any] = []
        parts = []
        for name, value in self._assignments:
            if isinstance(value, Expression):
                sql, extra = value.to_sql(nxt, resolve)
                params.extend(extra)
                parts.append(f"{name} = {sql}")
            else:
                parts.append(f"{name} = {nxt()}")
                params.append(value)
        sql = f"UPDATE {self._table_sql()} SET {', '.join(parts)}"
        if self._conditions:
            sql += " WHERE " + _and_join(self._conditions, nxt, resolve, params)
        sql += self._returning_sql(nxt, params)
        return sql, params

    def __repr__(self) -> str:
        return f"<Update {source_name(self.source)}>"


class Delete(_Statement):
    """`DELETE FROM t WHERE ...`.

    An unconditional delete must say so via `.all_rows()`. A forgotten `where()`
    that empties a table is not a mistake worth making easy.
    """

    def __init__(self, target: type[Any] | Alias[Any]) -> None:
        super().__init__(target)
        self._conditions: list[Predicate] = []
        self._all_rows = False

    def where(self, *predicates: Predicate) -> Self:
        for predicate in predicates:
            if not isinstance(predicate, Predicate):
                raise TypeError(
                    f"where() takes a predicate, got {type(predicate).__name__}"
                )
            for column in _columns_in(predicate):
                self._check_column(column)
            self._conditions.append(predicate)
        self._invalidate()
        return self

    def all_rows(self) -> Self:
        """Delete every row, deliberately."""
        self._all_rows = True
        self._invalidate()
        return self

    def _render(self, nxt: Any = None, resolve: Any = None) -> tuple[str, list[Any]]:
        if not self._conditions and not self._all_rows:
            raise ValueError(
                "Delete has no where(): that would empty the table. Call "
                "all_rows() if that is the intent."
            )
        if nxt is None:
            def nxt():
                return "?"
        resolve = self._resolver()
        params: list[Any] = []
        sql = f"DELETE FROM {self._table_sql()}"
        if self._conditions:
            sql += " WHERE " + _and_join(self._conditions, nxt, resolve, params)
        sql += self._returning_sql(nxt, params)
        return sql, params

    def __repr__(self) -> str:
        return f"<Delete {source_name(self.source)}>"


__all__ = ["Insert", "Update", "Delete", "MAX_PARAMETERS", "max_rows_per_statement"]
