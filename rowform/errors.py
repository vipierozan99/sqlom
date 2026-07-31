"""Every error rowform raises deliberately, under one catchable base.

    try:
        rows = await db.fetch_all(statement)
    except rowform.RowformError:
        ...

Before this module the library raised bare `TypeError`, `ValueError` and
`RuntimeError`, so a caller could neither catch "something rowform rejected"
nor tell it apart from a driver failure or an ordinary bug in their own code.

**Each class also inherits the builtin it replaces**, so `except ValueError`
around a statement misuse and `except TypeError` around a bad declaration keep
working exactly as before — the hierarchy is added underneath existing
behaviour rather than substituted for it. That is also why the classes are
grouped by *what is wrong* rather than by module: the grouping has to survive
being flattened onto four builtins.

Errors raised by the driver (a lost connection, a constraint violation) are
**not** wrapped. They are the driver's own exceptions, they mean what that
driver's documentation says they mean, and re-raising them under a rowform name
would hide which server actually refused what.
"""

from __future__ import annotations


class RowformError(Exception):
    """Base for every error rowform raises on purpose."""


class DeclarationError(RowformError, TypeError):
    """A model declaration cannot be turned into a table, or an alias of one.

    An unmappable annotation, a `Mapped[]` union of several types, a reserved
    field name, an incomplete `__column_order__`, a field-order conflict from an
    inherited default, or selecting from a class with no `__tablename__`. Those
    are raised at class-creation time, so they fire on import rather than on
    first query.

    `alias()` raises it too, for a from clause whose columns are not exactly that
    model's — the case where the rows would hydrate differently from what the
    statement's type says.
    """


class ConfigurationError(RowformError, TypeError, ValueError):
    """An engine or transaction was given an option it cannot honour.

    Both `TypeError` and `ValueError` are bases because both were raised here
    before: an unexpected keyword argument is a `TypeError` in Python, and an
    unknown isolation-level *name* is a `ValueError`. One class covers the same
    ground without changing what either site catches.
    """


class UnsupportedError(RowformError, NotImplementedError):
    """The backend has no way to express what was asked.

    Distinct from `ConfigurationError`: the option is spelled correctly and
    means something on another engine. sqlite's lack of session-level isolation
    levels is the case that exists — accepting them as no-ops would let a caller
    believe they took effect.
    """


class StatementError(RowformError, ValueError):
    """The statement is wrong for the method it was passed to.

    `execute()` given something that returns rows would discard them;
    `fetch_all()` given something that returns none would answer `[]`, which
    reads as "nothing matched". Both fail loudly instead.
    """


class PlanError(RowformError, ValueError):
    """rowform cannot say what this statement's rows mean.

    A statement selecting no columns, or a result whose column count disagrees
    with the plan — the case where hydrating anyway would mis-assign fields, and
    return plausible values in the wrong attributes.
    """


class EngineStateError(RowformError, RuntimeError):
    """The engine is in no state to run this.

    `db.fetch_all()` called inside `db.connect()` or `db.begin()`, where it would
    take a different pooled connection and miss the scope's uncommitted writes.
    """
