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

**Three qualification rules, and they are not the same rule.** A lone-table statement
leaves every column bare, which is what keeps previously rendered SQL byte-identical.
`from_()`/`using()` add a table, so references get qualified — but a SET target never
does, because Postgres rejects `SET t.col = ...`. And inside `ON CONFLICT DO UPDATE`
references are qualified even with one table, because `excluded` is also in scope
there and Postgres calls a bare name ambiguous. sqlite accepts the bare form in that
last case, so this is a difference the sqlite tests cannot see.

**Column references are validated when the statement renders**, not in the builder
method that received them, so `from_()` may come before or after `set()`/`where()`.
Names that can never become valid — a SET target, a `values()` column, an index
element — are still rejected on the spot.

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

from .expr import (
    Alias,
    ColumnExpr,
    Excluded,
    Expression,
    Predicate,
    source_name,
    source_prefix,
    walk_nodes,
)
from .query import (
    _and_join,
    _columns_in,
    _non_negative_int,
    _placeholders,
    _with_clause,
)

# Postgres refuses a statement with more than 65535 bound parameters. sqlite's
# default SQLITE_MAX_VARIABLE_NUMBER is 32766 on recent builds, so the lower bound
# is used: exceeding it is a server error either way, and the point is to fail where
# the batch is built rather than where it is sent.
MAX_PARAMETERS = 32766


def _excluded_in(node: Any) -> list[Excluded[Any]]:
    """Every `excluded.col` reference inside an expression.

    `_columns_in` deliberately does not see these — `excluded` is not a table in the
    statement, so treating it as a source would make every validator demand a join
    for it. They still need checking, hence a second walk.
    """
    # `walk_nodes` yields what is *inside* a node, so a bare `excluded(col)` — the
    # common case, `set_={"hits": excluded(User.hits)}` — has to be added here.
    found = [node] if isinstance(node, Excluded) else []
    found.extend(inner for inner in walk_nodes(node) if isinstance(inner, Excluded))
    return found


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
        self._extra_sources: list[Any] = []
        self._sql_cache: dict[Any, Any] = {}

    @property
    def model(self) -> Any:
        return getattr(self.source, "model", self.source)

    def _sources(self) -> list[Any]:
        """Every table a predicate or value may reference.

        More than one only for `UPDATE ... FROM` and `DELETE ... USING`.
        """
        return [self.source, *self._extra_sources]

    def _add_source(self, source: Any, keyword: str) -> None:
        if isinstance(source, Expression) or not hasattr(source, "__columns__"):
            raise TypeError(
                f"{keyword} takes a model or Alias, got {source!r}"
            )
        for existing in self._sources():
            if source is existing:
                raise ValueError(
                    f"{source_name(source)} is already part of this statement"
                )
            if source_prefix(source) == source_prefix(existing):
                # Two sources rendering to the same qualifier makes every column
                # reference ambiguous. Alias one of them.
                raise ValueError(
                    f"{source_name(source)} and {source_name(existing)} both "
                    f"qualify as {source_prefix(source)!r}; alias one of them"
                )
        self._extra_sources.append(source)
        self._invalidate()

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
                self._returning.append(("expr", entity))
            elif hasattr(entity, "__columns__"):
                if entity is not self.source:
                    # A whole model other than the target: `UPDATE ... FROM other
                    # RETURNING other.*` is legal SQL, but hydrating a second model
                    # out of RETURNING is the join-hydration feature, not this one.
                    # Its columns individually are fine.
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

    def _reference_nodes(self) -> list[Any]:
        """Nodes whose column references are checked when the statement renders.

        Checked then rather than in the builder method, so `from_()` and `using()`
        can be called in any order relative to `set()` and `where()`. A reference
        to a table the statement never names is still caught before anything is
        sent — `to_sql()` runs client-side — just not at the exact call that wrote
        it. Names that can never become valid (a SET target, a column in
        `values()`, an index element) are still rejected on the spot.
        """
        return [entity for kind, entity in self._returning if kind == "expr"]

    def _check_references(self) -> None:
        for node in self._reference_nodes():
            for column in _columns_in(node):
                self._check_column(column)

    def _check_column(self, column: ColumnExpr[Any]) -> None:
        if not any(column.source is source for source in self._sources()):
            extra = ("" if not self._extra_sources else
                     " or any of " +
                     ", ".join(source_name(s) for s in self._extra_sources))
            raise ValueError(
                f"{source_name(column.source)} is not the table being written to "
                f"({source_name(self.source)}){extra}"
            )
        if column.name not in column.source.__columns__:
            raise ValueError(
                f"{source_name(column.source)} has no column {column.name!r}"
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

    def _extra_sources_sql(self, keyword: str) -> str:
        """The `FROM a, b` / `USING a, b` list, or `""` when there is none."""
        if not self._extra_sources:
            return ""
        names = []
        for source in self._extra_sources:
            if isinstance(source, Alias):
                names.append(f"{source.__tablename__} AS {source.alias}")
            else:
                names.append(source.__tablename__)
        return f" {keyword} " + ", ".join(names)

    def _resolver(self) -> Any:
        # A DML statement usually names exactly one table, so columns are never
        # ambiguous and stay bare — which keeps RETURNING portable and every
        # previously rendered statement byte-identical. `UPDATE ... FROM` and
        # `DELETE ... USING` bring a second table, and then they must qualify.
        if not self._extra_sources:
            return lambda source, name: name
        return lambda source, name: f"{source_prefix(source)}.{name}"

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

    # `_rows` holds bound values, never expression nodes, so the CTE walk skips it.
    # Without this it traverses every value of a bulk insert one at a time.
    __value_fields__ = ("_rows",)

    def __init__(self, target: type[Any] | Alias[Any]) -> None:
        super().__init__(target)
        self._columns: list[str] = []
        self._rows: list[tuple[Any, ...]] = []
        self._conflict: dict[str, Any] | None = None

    # --- ON CONFLICT -------------------------------------------------------

    def _conflict_target(
        self,
        index_elements: tuple[Any, ...],
        constraint: str | None,
        action: str,
    ) -> str:
        """Render the `(a, b)` or `ON CONSTRAINT name` part after ON CONFLICT."""
        if self._conflict is not None:
            raise ValueError("this Insert already has an ON CONFLICT clause")
        if index_elements and constraint is not None:
            raise ValueError(
                "give either index_elements or constraint=, not both: they are "
                "two ways of naming the same unique index"
            )
        if constraint is not None:
            if not isinstance(constraint, str) or not constraint:
                raise TypeError("constraint= takes a non-empty constraint name")
            # Not quoted: an identifier from calling code, and quoting it would
            # imply this accepts arbitrary text, which it does not.
            if not constraint.replace("_", "").isalnum():
                raise ValueError(
                    f"{constraint!r} is not a plain identifier; ON CONSTRAINT "
                    f"takes a constraint name"
                )
            return f" ON CONSTRAINT {constraint}"
        if not index_elements:
            if action == "update":
                # Postgres and sqlite both require a target for DO UPDATE — there
                # is no way to write "the row that lost" without knowing which
                # index it lost on. Refuse here rather than at the server.
                raise ValueError(
                    "on_conflict_do_update() needs the conflicting column(s) or "
                    "constraint=, so the database knows which row to update"
                )
            return ""
        names = []
        for element in index_elements:
            if isinstance(element, str):
                name = element
            elif isinstance(element, ColumnExpr):
                self._check_column(element)
                name = element.name
            else:
                raise TypeError(
                    f"index_elements takes columns or column names, got "
                    f"{element!r}"
                )
            if name not in self.source.__columns__:
                raise ValueError(
                    f"{source_name(self.source)} has no column {name!r}"
                )
            names.append(name)
        return f" ({', '.join(names)})"

    def on_conflict_do_nothing(
        self, *index_elements: ColumnExpr[Any] | str, constraint: str | None = None
    ) -> Self:
        """`ON CONFLICT ... DO NOTHING` — skip rows that would violate a unique index.

            Insert(User).values(email="a@b.c").on_conflict_do_nothing(User.email)

        With no arguments it swallows *every* unique violation on the table, which
        both dialects allow. Naming the columns is better: it is the difference
        between "this email is taken, fine" and "silently discarded, cause unknown".

        The affected-row count reflects what was actually inserted, so it is how you
        find out whether the row was new.
        """
        target = self._conflict_target(index_elements, constraint, "nothing")
        self._conflict = {"target": target, "action": "nothing"}
        self._invalidate()
        return self

    def on_conflict_do_update(
        self,
        *index_elements: ColumnExpr[Any] | str,
        set_: dict[str, Any],
        where: Predicate | None = None,
        constraint: str | None = None,
    ) -> Self:
        """`ON CONFLICT ... DO UPDATE SET ...` — an upsert.

            Insert(Counter).values(key="hits", n=1).on_conflict_do_update(
                Counter.key, set_={"n": Counter.n + excluded(Counter.n)}
            )

        In `set_`, a bare column means the **stored** row and `excluded(col)` the row
        that failed to insert, matching SQL. Values may be plain Python objects,
        which bind as parameters.

        `where=` makes the update conditional; the row is left alone when it does not
        match, exactly as with DO NOTHING.

        `set_` is a dict rather than `**kwargs` because `where` and `constraint`
        would otherwise be unreachable as column names.
        """
        if not isinstance(set_, dict) or not set_:
            raise TypeError(
                "on_conflict_do_update() needs a non-empty set_={...}; use "
                "on_conflict_do_nothing() to skip the row instead"
            )
        if where is not None and not isinstance(where, Predicate):
            raise TypeError(
                f"where= takes a predicate, got {type(where).__name__}"
            )
        target = self._conflict_target(index_elements, constraint, "update")
        assignments: list[tuple[str, Any]] = []
        for name, value in set_.items():
            if name not in self.source.__columns__:
                raise ValueError(
                    f"{source_name(self.source)} has no column {name!r}"
                )
            assignments.append((name, value))
        self._conflict = {
            "target": target,
            "action": "update",
            "set": assignments,
            "where": where,
        }
        self._invalidate()
        return self

    def _reference_nodes(self) -> list[Any]:
        nodes = super()._reference_nodes()
        if self._conflict is not None and self._conflict["action"] == "update":
            nodes.extend(value for _, value in self._conflict["set"]
                         if isinstance(value, Expression))
            if self._conflict["where"] is not None:
                nodes.append(self._conflict["where"])
        return nodes

    def _check_references(self) -> None:
        super()._check_references()
        # `excluded` is the row this INSERT tried to write, so its columns can only
        # come from the target table. Checked by source identity, not just by name:
        # `excluded(Post.id)` on a User insert would otherwise render `excluded.id`
        # and quietly mean a different table's column that happens to share a name.
        allowed = (self.source, self.model)
        for node in self._reference_nodes():
            for reference in _excluded_in(node):
                if not any(reference.column.source is one for one in allowed):
                    raise ValueError(
                        f"excluded() takes a column of {source_name(self.source)}, "
                        f"the table being inserted into; got "
                        f"{source_name(reference.column.source)}."
                        f"{reference.column.name}"
                    )
                if reference.column.name not in self.source.__columns__:
                    raise ValueError(
                        f"{source_name(self.source)} has no column "
                        f"{reference.column.name!r}"
                    )

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
        self._check_references()
        if nxt is None:
            def _nxt():
                return "?"
            nxt = _nxt
        params: list[Any] = []
        with_sql = _with_clause(self, nxt, params)
        groups = []
        for row in self._rows:
            placeholders = []
            for value in row:
                placeholders.append(nxt())
                params.append(value)
            groups.append(f"({', '.join(placeholders)})")
        sql = (f"{with_sql}INSERT INTO {self._table_sql()} "
               f"({', '.join(self._columns)}) VALUES {', '.join(groups)}")
        sql += self._conflict_sql(nxt, params)
        sql += self._returning_sql(nxt, params)
        return sql, params

    def _conflict_sql(self, nxt: Any, params: list[Any]) -> str:
        conflict = self._conflict
        if conflict is None:
            return ""
        sql = f" ON CONFLICT{conflict['target']}"
        if conflict["action"] == "nothing":
            return sql + " DO NOTHING"
        # Column *references* inside DO UPDATE are qualified by the target table,
        # even though a lone-table statement leaves them bare everywhere else.
        # Postgres has both the table and `excluded` in scope here, so a bare
        # `score` in `SET score = score + excluded.score` is an
        # AmbiguousColumnError. (sqlite accepts the bare form, so the sqlite tests
        # alone would not have found this.) The assignment *target* still has to be
        # unqualified: Postgres rejects `SET t.col = ...`.
        def resolve(source: Any, name: str) -> str:
            return f"{source_prefix(self.source)}.{name}"
        parts = []
        for name, value in conflict["set"]:
            if isinstance(value, Expression):
                fragment, extra = value.to_sql(nxt, resolve)
                params.extend(extra)
                parts.append(f"{name} = {fragment}")
            else:
                parts.append(f"{name} = {nxt()}")
                params.append(value)
        sql += " DO UPDATE SET " + ", ".join(parts)
        if conflict["where"] is not None:
            fragment, extra = conflict["where"].to_sql(nxt, resolve)
            params.extend(extra)
            sql += f" WHERE {fragment}"
        return sql

    def __repr__(self) -> str:
        return f"<Insert {source_name(self.source)} x{len(self._rows)}>"


class Update(_Statement):
    """`UPDATE t SET ... WHERE ...`.

        Update(User).set(active=False).where(User.id == 1).returning(User.id)

    Values may be expressions, so a read-modify-write becomes one statement:

        Update(Post).set(score=Post.score + 1).where(Post.id == 1)

    `from_()` brings in another table, so one statement can copy across a join:

        Update(Post).set(author=Author.name).from_(Author) \\
                    .where(Author.id == Post.author_id)
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
            self._assignments.append((name, value))
        self._invalidate()
        return self

    # SQLAlchemy's Update spells this .values(), the same name Insert uses.
    values = set

    def _reference_nodes(self) -> list[Any]:
        return (super()._reference_nodes()
                + [value for _, value in self._assignments
                   if isinstance(value, Expression)]
                + list(self._conditions))

    def from_(self, *sources: type[Any] | Alias[Any]) -> Self:
        """`UPDATE t SET ... FROM other WHERE ...` — update from a joined table.

        There is no ON clause: the join condition goes in `where()`, which is how
        SQL spells it. A missing condition is a cross product that updates every
        row, so `where()` is required as soon as `from_()` is used.

        Once a second table is in play every column reference gets qualified,
        including in `where()` and `returning()`. The SET targets stay unqualified
        because Postgres rejects `SET t.col = ...`.

        Portability: Postgres has always had this; sqlite added `UPDATE ... FROM` in
        3.33 (2020). Older sqlite needs a correlated subquery instead.
        """
        if not sources:
            raise TypeError("from_() needs at least one table")
        for source in sources:
            self._add_source(source, "from_()")
        return self

    def where(self, *predicates: Predicate) -> Self:
        for predicate in predicates:
            if not isinstance(predicate, Predicate):
                raise TypeError(
                    f"where() takes a predicate, got {type(predicate).__name__}"
                )
            self._conditions.append(predicate)
        self._invalidate()
        return self

    def _render(self, nxt: Any = None, resolve: Any = None) -> tuple[str, list[Any]]:
        if not self._assignments:
            raise ValueError("Update has no assignments; call set() first")
        self._check_references()
        if nxt is None:
            def _nxt():
                return "?"
            nxt = _nxt
        if self._extra_sources and not self._conditions:
            raise ValueError(
                "Update has from_() but no where(): with no join condition that "
                "is a cross product, which updates every row. Add the condition "
                "that links the tables."
            )
        resolve = self._resolver()
        params: list[Any] = []
        with_sql = _with_clause(self, nxt, params)
        parts = []
        for name, value in self._assignments:
            if isinstance(value, Expression):
                sql, extra = value.to_sql(nxt, resolve)
                params.extend(extra)
                # The assignment target is never qualified: Postgres rejects
                # `SET t.col = ...` outright, and it is unambiguous anyway.
                parts.append(f"{name} = {sql}")
            else:
                parts.append(f"{name} = {nxt()}")
                params.append(value)
        sql = f"{with_sql}UPDATE {self._table_sql()} SET {', '.join(parts)}"
        sql += self._extra_sources_sql("FROM")
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

    `using()` brings in another table so the condition can span a join:

        Delete(Post).using(Author).where(
            Author.id == Post.author_id, Author.banned == True
        )
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
            self._conditions.append(predicate)
        self._invalidate()
        return self

    def _reference_nodes(self) -> list[Any]:
        return super()._reference_nodes() + list(self._conditions)

    def using(self, *sources: type[Any] | Alias[Any]) -> Self:
        """`DELETE FROM t USING other WHERE ...` — delete by a condition on a join.

        As with `Update.from_()`, the join condition goes in `where()` and columns
        become qualified once a second table is present.

        **Postgres only.** sqlite has no `USING` for DELETE; there, express it as
        `Delete(Post).where(Post.author_id.in_(Query(Author.id).where(...)))`, which
        both dialects accept.
        """
        if not sources:
            raise TypeError("using() needs at least one table")
        for source in sources:
            self._add_source(source, "using()")
        return self

    def all_rows(self) -> Self:
        """Delete every row, deliberately."""
        self._all_rows = True
        self._invalidate()
        return self

    def _render(self, nxt: Any = None, resolve: Any = None) -> tuple[str, list[Any]]:
        if self._extra_sources and not self._conditions:
            # Checked before the all_rows() rule, and not satisfied by it: `USING
            # other` with no condition deletes every row of the target once per
            # row of `other`, which is not what "delete everything" asked for.
            raise ValueError(
                "Delete has using() but no where(): with no join condition that "
                "is a cross product. Add the condition that links the tables."
            )
        if not self._conditions and not self._all_rows:
            raise ValueError(
                "Delete has no where(): that would empty the table. Call "
                "all_rows() if that is the intent."
            )
        self._check_references()
        if nxt is None:
            def _nxt():
                return "?"
            nxt = _nxt
        resolve = self._resolver()
        params: list[Any] = []
        with_sql = _with_clause(self, nxt, params)
        sql = f"{with_sql}DELETE FROM {self._table_sql()}"
        sql += self._extra_sources_sql("USING")
        if self._conditions:
            sql += " WHERE " + _and_join(self._conditions, nxt, resolve, params)
        sql += self._returning_sql(nxt, params)
        return sql, params

    def __repr__(self) -> str:
        return f"<Delete {source_name(self.source)}>"


def insert(target: type[Any] | Alias[Any]) -> Insert:
    """SQLAlchemy-style constructor for `Insert` — `insert(User)` is exactly
    `Insert(User)`."""
    return Insert(target)


def update(target: type[Any] | Alias[Any]) -> Update:
    """SQLAlchemy-style constructor for `Update` — `update(User)` is exactly
    `Update(User)`."""
    return Update(target)


def delete(target: type[Any] | Alias[Any]) -> Delete:
    """SQLAlchemy-style constructor for `Delete` — `delete(User)` is exactly
    `Delete(User)`."""
    return Delete(target)


__all__ = [
    "Insert", "Update", "Delete", "insert", "update", "delete",
    "MAX_PARAMETERS", "max_rows_per_statement",
]
