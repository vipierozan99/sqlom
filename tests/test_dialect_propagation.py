"""`to_sql(dialect=...)`'s propagation mechanism: a contextvar set once at the
outermost `to_sql()` call, read via `current_dialect()` from anywhere in the
tree — proven here to actually reach deeply nested positions, since the
naive "hang it off the placeholder callable" alternative design does not
(`Query._render()` re-wraps the placeholder generator in a bare closure on
every render, which would silently lose an attribute at the very first
WHERE/JOIN/GROUP BY/ORDER BY hop — see sqlom/dialects.py's module docstring).

sqlom-original tests (no SQLAlchemy equivalent) — this is sqlom's own
propagation mechanism, not a SQLAlchemy-ported behaviour.
"""

from sqlom import POSTGRES, Query, SQLITE, exists
from sqlom.dialects import current_dialect
from sqlom.expr import Expression, _bare
from tests.conftest import Author, Book


class _DialectSpy(Expression):
    """Delegates rendering to `inner`, recording `current_dialect()` into
    `sink` at the moment it's rendered — a probe planted at a specific tree
    position, not a real expression type."""

    __slots__ = ("inner", "sink")

    def __init__(self, inner, sink):
        self.inner = inner
        self.sink = sink

    def to_sql(self, nxt, resolve=_bare):
        self.sink.append(current_dialect())
        return self.inner.to_sql(nxt, resolve)

    def sources(self):
        return self.inner.sources()


def _build_query(sink):
    """One query with a spy planted at five different nesting positions: a
    plain select column, a WHERE clause, a joined subquery's body, a joined
    CTE's body, and a correlated EXISTS subquery's WHERE clause."""
    sub = Query(_DialectSpy(Book.author_id, sink).label("aid")).subquery("s")
    cte = Query(_DialectSpy(Book.id, sink).label("bid")).cte("c")
    inner_exists = (
        Query(Book).correlate(Author)
        .where(_DialectSpy(Book.author_id, sink) == Author.id)
    )
    return (
        Query(Author.id, _DialectSpy(Author.name, sink))
        .join(sub, sub.aid == Author.id)
        .join(cte, cte.bid == Author.id)
        .where(_DialectSpy(Author.active, sink) == True)  # noqa: E712
        .where(exists(inner_exists))
    )


class TestPropagation:
    # sqlom-original test (no SQLAlchemy equivalent)
    def test_dialect_reaches_every_nested_position(self):
        sink = []
        _build_query(sink).to_sql(dialect=POSTGRES)
        assert len(sink) == 5
        assert all(dialect is POSTGRES for dialect in sink)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_a_different_dialect_reaches_every_position_too(self):
        sink = []
        _build_query(sink).to_sql(dialect=SQLITE)
        assert len(sink) == 5
        assert all(dialect is SQLITE for dialect in sink)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_no_dialect_means_current_dialect_is_none_everywhere(self):
        sink = []
        _build_query(sink).to_sql()
        assert len(sink) == 5
        assert all(dialect is None for dialect in sink)

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_current_dialect_is_none_again_after_the_render_finishes(self):
        sink = []
        query = _build_query(sink)
        query.to_sql(dialect=POSTGRES)
        assert current_dialect() is None


class TestCaching:
    # sqlom-original test (no SQLAlchemy equivalent)
    def test_different_dialects_do_not_collide_in_the_cache(self):
        query = Query(Author).where(Author.id == 1)
        default_sql, _ = query.to_sql()
        postgres_sql, _ = query.to_sql(dialect=POSTGRES)
        sqlite_sql, _ = query.to_sql(dialect=SQLITE)
        assert default_sql == "SELECT id, name, active FROM t_authors WHERE id = ?"
        assert postgres_sql == "SELECT id, name, active FROM t_authors WHERE id = $1"
        assert sqlite_sql == default_sql
        assert len(query._sql_cache) == 3

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_repeat_calls_hit_the_cache_rather_than_growing_it(self):
        query = Query(Author).where(Author.id == 1)
        query.to_sql(dialect=POSTGRES)
        query.to_sql(dialect=POSTGRES)
        query.to_sql(dialect=SQLITE)
        query.to_sql(dialect=SQLITE)
        assert len(query._sql_cache) == 2

    # sqlom-original test (no SQLAlchemy equivalent)
    def test_explicit_placeholder_overrides_the_dialect_default(self):
        # dialect=POSTGRES defaults to "$", but an explicit placeholder wins.
        query = Query(Author).where(Author.id == 1)
        sql, _ = query.to_sql(placeholder="%s", dialect=POSTGRES)
        assert sql == "SELECT id, name, active FROM t_authors WHERE id = %s"
