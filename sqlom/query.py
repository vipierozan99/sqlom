from .column import ColumnExpr, Condition, _bare

# How each backend spells "aggregate these rows into one JSON array", plus
# how it renders a boolean. sqlite has no bool type (columns come back as
# 0/1), so booleans need an explicit cast to get real JSON true/false.
DIALECTS = {
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


def json_bytes(payload):
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


class Query:
    """A SELECT over one or more models.

        Query(User)                                   -> [User]
        Query(User, Post).join(Post, Post.user_id == User.id)
                                                      -> [(User, Post)]
        Query(User, Post.title).join(Post, ...)        -> [(User, str)]
        Query(User).outer_join(Post, ...)              -> [User]  (filtering join)

    A multi-entity query yields tuples in select order, like SQLAlchemy's
    `select(User, Post)`. Entities reached by an outer join arrive as `None` when
    the row has no match.
    """

    def __init__(self, *entities):
        if not entities:
            raise TypeError("Query() needs at least one model or column to select")

        self._entities = []
        for entity in entities:
            if isinstance(entity, ColumnExpr):
                self._entities.append(("column", entity))
            elif hasattr(entity, "__columns__"):
                self._entities.append(("model", entity))
            else:
                raise TypeError(
                    f"Query() takes models or columns (e.g. Query(User) or "
                    f"Query(User, Post.title)), got {entity!r}"
                )

        # `self.model` stays the primary/leftmost model: it is the FROM table, the
        # target of `where()` validation for unqualified use, and what every
        # existing caller expects.
        first = self._entities[0][1]
        self.model = first.model if self._entities[0][0] == "column" else first

        self._conditions = []
        self._joins = []          # list of (model, on_condition, is_outer)
        self._limit = None
        self._order_by = []
        # Compiled SQL is cached per (kind, dialect/placeholder). A hot endpoint
        # builds the same query shape on every request, and regenerating the
        # string each time is pure overhead — it measured as ~4% of throughput.
        self._sql_cache = {}
        self._hydration_key = None
        self._recompute_key()

    # ------------------------------------------------------------------ shape

    def _sources(self):
        """Every model that can be referenced: the FROM table plus joined ones."""
        return [self.model] + [model for model, _, _ in self._joins]

    def _recompute_key(self):
        """The cache key an engine uses to look up this query's hydrator.

        Deliberately just the model class whenever exactly one model is selected,
        so the engine's dict lookup stays as cheap as it was before joins existed
        (hashing a class beats hashing a tuple). Joins do not enter the key: a
        join changes the SQL, not the row *shape*, and the hydrator only depends
        on the shape. A filtering join — `Query(Author).join(Book, ...)`, where
        Book is joined but not selected — therefore reuses the plain single-model
        hydrator, which is exactly right, and must not produce 1-tuples.
        """
        if not self.is_multi_entity:
            self._hydration_key = self.model
            return
        outer = {model for model, _, is_outer in self._joins if is_outer}
        parts = []
        for kind, entity in self._entities:
            if kind == "model":
                parts.append(("model", entity, entity in outer))
            else:
                parts.append(("column", entity.model, entity.name))
        self._hydration_key = tuple(parts)

    def hydration_spec(self):
        """The entity specs `compile_join_hydrator` needs, in select order."""
        outer = {model for model, _, is_outer in self._joins if is_outer}
        specs = []
        for kind, entity in self._entities:
            if kind == "model":
                specs.append(("model", entity, entity in outer))
            else:
                specs.append(("column", entity.py_type))
        return specs

    @property
    def is_multi_entity(self):
        """True when rows hydrate to tuples rather than single instances."""
        return not (len(self._entities) == 1 and self._entities[0][0] == "model")

    def _invalidate(self):
        self._sql_cache.clear()
        self._recompute_key()

    # ------------------------------------------------------------------ joins

    def _add_join(self, model, on, is_outer):
        if not hasattr(model, "__columns__"):
            raise TypeError(f"join() takes a model, got {model!r}")
        if model is self.model or any(model is m for m, _, _ in self._joins):
            # Rendering is qualified by table name, so the same table twice would
            # make every reference to it ambiguous. Aliases would fix this; sqlom
            # has none yet, so refuse rather than emit wrong SQL.
            raise ValueError(
                f"{model.__name__} is already in this query; self-joins need "
                f"table aliases, which sqlom does not support yet"
            )
        if not isinstance(on, Condition):
            raise TypeError(
                f"join() needs an ON condition comparing two columns "
                f"(e.g. Post.user_id == User.id), got {type(on).__name__}"
            )
        if not on.is_column_comparison:
            raise ValueError(
                f"join() ON clause must compare two columns, not a column to a "
                f"value; got {on!r}. Use where() for value predicates."
            )
        known = set(self._sources()) | {model}
        for referenced in on.models():
            if referenced not in known:
                raise ValueError(
                    f"ON clause references {referenced.__name__}, which is not "
                    f"part of this query"
                )
        if model not in on.models():
            raise ValueError(
                f"ON clause does not reference {model.__name__}, the table being "
                f"joined; it would produce a cross join"
            )
        self._joins.append((model, on, is_outer))
        self._invalidate()
        return self

    def join(self, model, on):
        """INNER JOIN `model` ON `on` (a column-to-column comparison)."""
        return self._add_join(model, on, False)

    def outer_join(self, model, on):
        """LEFT OUTER JOIN. If `model` is also selected, unmatched rows hydrate
        that slot as `None` rather than an object of Nones."""
        return self._add_join(model, on, True)

    # ------------------------------------------------------------- predicates

    def _check_column(self, model, name, what):
        sources = self._sources()
        if model is not None and not any(model is source for source in sources):
            raise ValueError(
                f"{what} references {model.__name__}, which is not part of this "
                f"query (selecting from "
                f"{', '.join(m.__name__ for m in sources)}). Add a join first."
            )
        owner = model if model is not None else self.model
        if name not in owner.__columns__:
            raise ValueError(f"{owner.__name__} has no column {name!r}")

    def where(self, condition: Condition):
        if not isinstance(condition, Condition):
            raise TypeError(
                f"where() takes a Condition built from a column comparison "
                f"(e.g. {self.model.__name__}.id > 100), got {type(condition).__name__}"
            )
        # A predicate from a model this query does not select would render as a
        # bare or unknown column name and silently filter the wrong table.
        self._check_column(condition.model, condition.column_name, "condition")
        if condition.is_column_comparison:
            self._check_column(condition.value.model, condition.value.name, "condition")
        self._conditions.append(condition)
        self._invalidate()
        return self

    def order_by(self, *columns, descending=False):
        """Order the result set. Accepts `ColumnExpr`s or column-name strings.

        Worth being explicit about why this exists: `LIMIT` without `ORDER BY`
        returns an arbitrary subset. Postgres is free to pick a different plan
        for a differently-spelled but equivalent query and hand back different
        rows, so any query that limits without ordering is only reproducible by
        luck. Anything paginating, or comparing two implementations byte for
        byte, needs a total order.

        A bare string names a column on the primary model; pass a `ColumnExpr`
        (`Post.created_at`) to order by a joined one.
        """
        for column in columns:
            if isinstance(column, ColumnExpr):
                self._check_column(column.model, column.name, "order_by")
                self._order_by.append((column.model, column.name, descending))
            else:
                self._check_column(None, column, "order_by")
                self._order_by.append((self.model, column, descending))
        self._invalidate()
        return self

    def limit(self, n: int):
        # sqlite reads a negative LIMIT as "no limit" while Postgres raises, so
        # the same query would either silently return everything or blow up
        # depending on the backend. Neither is a useful thing to ship.
        if not isinstance(n, int) or isinstance(n, bool):
            raise TypeError(f"limit() takes an int, got {type(n).__name__}")
        if n < 0:
            raise ValueError(f"limit() must be >= 0, got {n}")
        self._limit = n
        self._invalidate()
        return self

    # ------------------------------------------------------------- rendering

    def _resolver(self):
        """How to render a column reference.

        With no joins, bare names — which keeps the SQL of every existing
        single-table query byte-identical. With joins, qualify by table name,
        because `id` would otherwise be ambiguous across the joined tables.
        """
        if not self._joins:
            return _bare
        return lambda model, name: f"{model.__tablename__}.{name}"

    def _where_clause(self, placeholder, resolve=None):
        """`placeholder` of "$" means Postgres-style numbering: $1, $2, ...

        Numbering here rather than post-processing with a regex avoids a
        substitution pass over the SQL on every call.
        """
        # "$" means asyncpg-style numbering ($1, $2, ...). "?" (sqlite) and
        # "%s" (psycopg) are positional and repeat unchanged.
        if resolve is None:
            resolve = self._resolver()
        numbered = placeholder == "$"
        sql = ""
        params = []

        def next_placeholder():
            return f"${len(params) + 1}" if numbered else placeholder

        # JOIN clauses come before WHERE, and their ON parameters (there are none
        # today, since an ON clause must compare two columns) would number first.
        for model, on, is_outer in self._joins:
            clause, values = on.to_sql(next_placeholder(), resolve)
            kind = "LEFT OUTER JOIN" if is_outer else "JOIN"
            sql += f" {kind} {model.__tablename__} ON {clause}"
            params.extend(values)

        if self._conditions:
            clauses = []
            for condition in self._conditions:
                # `params` is extended, not appended to: a NULL predicate and a
                # column-to-column comparison both bind nothing, and numbering
                # has to stay in step with what is actually bound.
                clause, values = condition.to_sql(next_placeholder(), resolve)
                clauses.append(clause)
                params.extend(values)
            sql += " WHERE " + " AND ".join(clauses)
        if self._order_by:
            terms = ", ".join(
                f"{resolve(model, name)} DESC" if desc else resolve(model, name)
                for model, name, desc in self._order_by
            )
            sql += f" ORDER BY {terms}"
        if self._limit is not None:
            sql += f" LIMIT {next_placeholder()}"
            params.append(self._limit)
        return sql, params

    def _select_list(self, resolve):
        """The selected columns, flattened in entity order.

        The hydrator relies on this order and width exactly — it unpacks the row
        tuple positionally — so the two are generated from the same entity list.
        """
        parts = []
        for kind, entity in self._entities:
            if kind == "model":
                parts.extend(resolve(entity, name) for name in entity.__columns__)
            else:
                parts.append(resolve(entity.model, entity.name))
        return parts

    def to_sql(self, placeholder="?"):
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
        resolve = self._resolver()
        columns = self._select_list(resolve)
        sql = f"SELECT {', '.join(columns)} FROM {self.model.__tablename__}"
        tail, params = self._where_clause(placeholder, resolve)
        result = (sql + tail, tuple(params))
        self._sql_cache[("select", placeholder)] = result
        return result

    def to_json_sql(self, dialect="postgres"):
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
        if self._joins or self.is_multi_entity:
            raise NotImplementedError(
                "to_json_sql() supports a single-model query only; this query "
                "selects "
                f"{len(self._entities)} entities with {len(self._joins)} join(s). "
                "Use fetch_all() and serialize in Python."
            )
        spec = DIALECTS[dialect]
        placeholder = spec["placeholder"]
        columns = self.model.__columns__

        object_args = ", ".join(
            f"'{name}', {spec['bool'](name) if column.py_type is bool else name}"
            for name, column in columns.items()
        )

        inner = f"SELECT {', '.join(columns)} FROM {self.model.__tablename__}"
        tail, params = self._where_clause(placeholder)
        inner += tail

        agg = f"{spec['agg']}({spec['object']}({object_args}))"
        if spec["coalesce"]:
            agg = f"coalesce({agg}, '[]'::json)"
        if spec.get("text_cast"):
            agg = f"({agg})::text"

        result = (f"SELECT {agg} FROM ({inner}) AS t", tuple(params))
        self._sql_cache[("json", dialect)] = result
        return result
