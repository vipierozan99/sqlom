"""Every error rowform raises deliberately, under one catchable base.

Each class also inherits the builtin it replaces (`TypeError`, `ValueError`,
`RuntimeError`), so existing `except` clauses keep working. Driver errors are
not wrapped: they mean what the driver's documentation says.
"""

from __future__ import annotations


class RowformError(Exception):
    """Base for every error rowform raises on purpose."""


class DeclarationError(RowformError, TypeError):
    """A model declaration cannot become a table, or `alias(of=...)` was given a
    from clause whose columns are not exactly the model's. Raised at import time.
    """


class ConfigurationError(RowformError, TypeError, ValueError):
    """An engine, scope or statement was given an option it cannot honour."""


class UnsupportedError(RowformError, NotImplementedError):
    """The backend has no way to express what was asked (COPY, pipeline, isolation)."""


class StatementError(RowformError, ValueError):
    """The statement is wrong for the method: `fetch_all()` of something returning
    no rows would answer `[]` and read as "nothing matched".
    """


class PlanError(RowformError, ValueError):
    """rowform cannot say what this statement's rows mean, or the driver's column count disagrees."""


class EngineStateError(RowformError, RuntimeError):
    """`db.fetch_all()` inside `db.connect()`/`begin()`, where it would miss the scope's state."""
