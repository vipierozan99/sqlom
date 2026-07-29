"""Dialects: a common core (`Dialect`) plus a per-dialect override, covering
Postgres and sqlite — the minimum needed to catch a real class of mistake at
build time rather than at the server.

Until this module, every "Postgres-only" feature in rowform (`ILIKE`,
`FOR UPDATE`, `DELETE ... USING`, `ON CONFLICT ... ON CONSTRAINT`) was
documentation-only: `to_sql()` rendered identical SQL regardless of what
backend you actually intended to run it against, and a sqlite target would
only find out it was unsupported when the server rejected it. `Dialect`
gives those checks somewhere to live, and gives `IS DISTINCT FROM` — which
sqlite and Postgres spell completely differently, not just as a feature
toggle — a place to render the right thing for each.

This is deliberately *not* a full visitor/compiler rewrite the way
SQLAlchemy's real dialect system is: rowform still renders one dialect-*less*
string by default (every existing caller of `to_sql()` is unaffected), and a
`Dialect` only comes into play where a caller opts in with `to_sql(dialect=
SQLITE)`/`to_sql(dialect=POSTGRES)`. That is the right scope for what rowform
actually needs two dialects for today; see `query.py`/`dml.py` for how the
handful of dialect-sensitive spots consult `current_dialect()`.

**Propagation.** `to_sql(dialect=...)` sets a `contextvars.ContextVar` once,
at the outermost call, rather than threading a `dialect` argument through
every `Expression.to_sql(nxt, resolve)` in the tree (which would touch ~20
classes and ~22 call sites for no benefit — `resolve` already isn't threaded
that way, see query.py's docstrings on why nested renders recompute their
own). A contextvar is read from anywhere in the tree regardless of nesting
depth, mirrors the pattern `transaction.py` already uses for tracking the
active transaction, and is safe here because rendering is fully synchronous
— there is no `await` between the top of a render and the bottom.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CURRENT_DIALECT: ContextVar[Any] = ContextVar("rowform_current_dialect", default=None)


def current_dialect() -> Dialect | None:
    """The `Dialect` a render is currently happening under, or `None` outside
    any render, or when `to_sql()` was called with no `dialect=`. Dialect-
    sensitive nodes (`IsDistinctFrom`, the `ilike()`/`with_for_update()`/etc.
    validation in query.py/dml.py) read this rather than taking a `dialect`
    parameter of their own.
    """
    return _CURRENT_DIALECT.get()


@contextmanager
def dialect_scope(dialect):
    """Set `current_dialect()` for the duration of one top-level render.

    Used only by `Query.to_sql`/`CompoundSelect.to_sql`/`_Statement.to_sql` —
    the three *outermost* entry points — never by a nested `._render()` call,
    so a subquery/CTE/scalar-subquery mid-tree does not re-set (or clear) the
    dialect its parent render is already running under.
    """
    token = _CURRENT_DIALECT.set(dialect)
    try:
        yield
    finally:
        _CURRENT_DIALECT.reset(token)


def resolve_placeholder(placeholder, dialect):
    """The placeholder style `to_sql(placeholder=, dialect=)` should actually
    use: an explicit `placeholder` always wins; otherwise the dialect's own
    default; otherwise `"?"`, `to_sql()`'s long-standing default."""
    if placeholder is not None:
        return placeholder
    if dialect is not None:
        return dialect.default_placeholder
    return "?"


class Dialect:
    """The common core: default, permissive behaviour every dialect starts
    from. A concrete dialect overrides only what actually differs — the
    handful of `supports_*` flags below, and how `IS DISTINCT FROM` is
    spelled out.
    """

    name: str = "generic"
    default_placeholder: str = "?"
    supports_ilike: bool = True
    supports_for_update: bool = True
    supports_delete_using: bool = True
    supports_on_conflict_constraint: bool = True

    def is_distinct_from_sql(self, left_sql: str, right_sql: str,
                              negated: bool) -> str:
        """`left IS DISTINCT FROM right` (`negated=False`) or
        `left IS NOT DISTINCT FROM right` (`negated=True`) — the ANSI/Postgres
        spelling. `SqliteDialect` overrides this; every other dialect
        (including this generic base) uses it as-is.
        """
        keyword = "IS NOT DISTINCT FROM" if negated else "IS DISTINCT FROM"
        return f"{left_sql} {keyword} {right_sql}"

    def __repr__(self) -> str:
        return f"<Dialect {self.name}>"


class PostgresDialect(Dialect):
    """Postgres: supports everything rowform models a difference for."""

    name = "postgres"
    default_placeholder = "$"


class SqliteDialect(Dialect):
    """sqlite: no `ILIKE`, no row-locking clause at all, no `DELETE ...
    USING`, no `ON CONFLICT ... ON CONSTRAINT` — and `IS DISTINCT FROM` isn't
    a real keyword here at all. sqlite's own `IS`/`IS NOT` are *already*
    null-safe (`a IS b` is true when both are NULL, unlike `a = b`), so they
    are the direct equivalent: `IS NOT` answers "are these different,
    null-safe" (`is_distinct_from`), `IS` answers "are these the same,
    null-safe" (`is_not_distinct_from`).
    """

    name = "sqlite"
    default_placeholder = "?"
    supports_ilike = False
    supports_for_update = False
    supports_delete_using = False
    supports_on_conflict_constraint = False

    def is_distinct_from_sql(self, left_sql: str, right_sql: str,
                              negated: bool) -> str:
        keyword = "IS" if negated else "IS NOT"
        return f"{left_sql} {keyword} {right_sql}"


#: Singletons — one instance per dialect is all rowform ever needs, and sharing
#: them means `dialect is SQLITE` is a valid identity check.
POSTGRES = PostgresDialect()
SQLITE = SqliteDialect()
