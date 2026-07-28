"""Pure-function engine internals that need no database connection."""

from sqlom import DatabaseEngine


class TestNumberPlaceholders:
    """Deprecated shim kept for benchmarks calling it directly (see the
    docstring on DatabaseEngine._number_placeholders); to_sql(placeholder="$")
    numbers placeholders itself now, so nothing else in the suite reaches it."""

    def test_bare_dollar_placeholders_are_numbered(self):
        sql = "SELECT * FROM t WHERE x = $ AND y = $"
        assert (DatabaseEngine._number_placeholders(sql)
                == "SELECT * FROM t WHERE x = $1 AND y = $2")

    def test_already_numbered_sql_is_a_no_op(self):
        sql = "SELECT * FROM t WHERE x = $1 AND y = $2"
        assert DatabaseEngine._number_placeholders(sql) is sql

    def test_sql_with_no_dollar_is_a_no_op(self):
        sql = "SELECT * FROM t WHERE x = ?"
        assert DatabaseEngine._number_placeholders(sql) is sql
