from .column import Condition


class Query:
    def __init__(self, model):
        self.model = model
        self._conditions = []
        self._limit = None

    def where(self, condition: Condition):
        self._conditions.append(condition)
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def to_sql(self, placeholder="?"):
        columns = list(self.model.__columns__.keys())
        sql = f"SELECT {', '.join(columns)} FROM {self.model.__tablename__}"
        params = []

        if self._conditions:
            clauses = []
            for condition in self._conditions:
                clause, value = condition.to_sql(placeholder)
                clauses.append(clause)
                params.append(value)
            sql += " WHERE " + " AND ".join(clauses)

        if self._limit is not None:
            sql += f" LIMIT {placeholder}"
            params.append(self._limit)

        return sql, params
