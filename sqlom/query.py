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
    "postgres": {
        "placeholder": "$",
        "agg": "json_agg",
        "object": "json_build_object",
        "bool": lambda col: col,
        "coalesce": True,
    },
}


class Query:
    def __init__(self, model):
        self.model = model
        self._conditions = []
        self._limit = None
        # Compiled SQL is cached per (kind, dialect/placeholder). A hot endpoint
        # builds the same query shape on every request, and regenerating the
        # string each time is pure overhead — it measured as ~4% of throughput.
        self._sql_cache = {}

    def _invalidate(self):
        self._sql_cache.clear()

    def where(self, condition: Condition):
        self._conditions.append(condition)
        self._invalidate()
        return self

    def limit(self, n: int):
        self._limit = n
        self._invalidate()
        return self

    def _where_clause(self, placeholder):
        """`placeholder` of "$" means Postgres-style numbering: $1, $2, ...

        Numbering here rather than post-processing with a regex avoids a
        substitution pass over the SQL on every call.
        """
        numbered = placeholder == "$"
        sql = ""
        params = []

        def next_placeholder():
            return f"${len(params) + 1}" if numbered else placeholder

        if self._conditions:
            clauses = []
            for condition in self._conditions:
                clause, value = condition.to_sql(next_placeholder())
                clauses.append(clause)
                params.append(value)
            sql += " WHERE " + " AND ".join(clauses)
        if self._limit is not None:
            sql += f" LIMIT {next_placeholder()}"
            params.append(self._limit)
        return sql, params

    def to_sql(self, placeholder="?"):
        cached = self._sql_cache.get(("select", placeholder))
        if cached is not None:
            return cached
        columns = list(self.model.__columns__.keys())
        sql = f"SELECT {', '.join(columns)} FROM {self.model.__tablename__}"
        tail, params = self._where_clause(placeholder)
        result = (sql + tail, params)
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

        result = (f"SELECT {agg} FROM ({inner}) AS t", params)
        self._sql_cache[("json", dialect)] = result
        return result
