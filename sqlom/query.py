from .column import Condition

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
    def __init__(self, model):
        self.model = model
        self._conditions = []
        self._limit = None
        self._order_by = []
        # Compiled SQL is cached per (kind, dialect/placeholder). A hot endpoint
        # builds the same query shape on every request, and regenerating the
        # string each time is pure overhead — it measured as ~4% of throughput.
        self._sql_cache = {}

    def _invalidate(self):
        self._sql_cache.clear()

    def where(self, condition: Condition):
        if not isinstance(condition, Condition):
            raise TypeError(
                f"where() takes a Condition built from a column comparison "
                f"(e.g. {self.model.__name__}.id > 100), got {type(condition).__name__}"
            )
        # A predicate from another model would render as a bare column name and
        # silently filter this table's same-named column instead.
        if condition.model is not None and condition.model is not self.model:
            raise ValueError(
                f"condition {condition!r} was built from "
                f"{condition.model.__name__}, not {self.model.__name__}"
            )
        if condition.column_name not in self.model.__columns__:
            raise ValueError(
                f"{self.model.__name__} has no column {condition.column_name!r}"
            )
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
        """
        for column in columns:
            name = getattr(column, "name", column)
            if name not in self.model.__columns__:
                raise ValueError(f"{self.model.__name__} has no column {name!r}")
            model = getattr(column, "model", None)
            if model is not None and model is not self.model:
                raise ValueError(
                    f"column {name!r} belongs to {model.__name__}, "
                    f"not {self.model.__name__}"
                )
            self._order_by.append((name, descending))
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

    def _where_clause(self, placeholder):
        """`placeholder` of "$" means Postgres-style numbering: $1, $2, ...

        Numbering here rather than post-processing with a regex avoids a
        substitution pass over the SQL on every call.
        """
        # "$" means asyncpg-style numbering ($1, $2, ...). "?" (sqlite) and
        # "%s" (psycopg) are positional and repeat unchanged.
        numbered = placeholder == "$"
        sql = ""
        params = []

        def next_placeholder():
            return f"${len(params) + 1}" if numbered else placeholder

        if self._conditions:
            clauses = []
            for condition in self._conditions:
                # `params` is extended, not appended to: a NULL predicate binds
                # nothing, and numbering has to stay in step with what is bound.
                clause, values = condition.to_sql(next_placeholder())
                clauses.append(clause)
                params.extend(values)
            sql += " WHERE " + " AND ".join(clauses)
        if self._order_by:
            terms = ", ".join(
                f"{name} DESC" if desc else name for name, desc in self._order_by
            )
            sql += f" ORDER BY {terms}"
        if self._limit is not None:
            sql += f" LIMIT {next_placeholder()}"
            params.append(self._limit)
        return sql, params

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
        columns = list(self.model.__columns__.keys())
        sql = f"SELECT {', '.join(columns)} FROM {self.model.__tablename__}"
        tail, params = self._where_clause(placeholder)
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
