"""Mistakes that must be type errors. Checked by mypy and pyright; never run.

Every line below carries both an `# type: ignore[...]` for mypy and a
`# pyright: ignore[...]` for pyright. That is what makes this a *test* rather than a
comment: mypy runs with `warn_unused_ignores` and pyright with
`reportUnnecessaryTypeIgnoreComment`, so if any of these stops being an error the
now-unnecessary suppression fails the run.

A checker reporting nothing on the positive file proves the good cases work. Only
this file proves the bad cases are caught.
"""

from sqlom import (
    Column,
    DatabaseEngine,
    ModelMeta,
    Query,
    and_,
    count,
    exists,
    not_,
    or_,
)


class Author(metaclass=ModelMeta):
    __tablename__ = "authors"

    id = Column(int)
    name = Column(str)
    active = Column(bool)


class Book(metaclass=ModelMeta):
    __tablename__ = "books"

    id = Column(int)
    author_id = Column(int)
    title = Column(str)


# --------------------------------------------------------------------------
# Comparing a column to the wrong type
# --------------------------------------------------------------------------

# Ordering comparisons are caught: there is no fallback for `<` and `>` when the
# operand type does not match.
Author.id > "abc"  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
Author.id < "abc"  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
Author.name > 5  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]

# `Author.name == 5` is NOT an error, and cannot be made one — see the note at the
# bottom of this file. Comparing two columns of different types *is* caught, because
# then neither side's __eq__ accepts the other and there is nothing to fall back to.
Author.id == Book.title  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]

# IN over the wrong element type.
Author.id.in_(["a", "b"])  # type: ignore[list-item]  # pyright: ignore


# --------------------------------------------------------------------------
# A typo on a model class
# --------------------------------------------------------------------------
# This one is why ModelMeta hides its __getattr__ from type checkers: a metaclass
# __getattr__ is a fallback for *any* name, so a checker that saw it would type
# `Author.nope` as whatever it returns and this line would pass.

Author.nope  # type: ignore[attr-defined]  # pyright: ignore


def instance_typo(author: Author) -> None:
    author.nope  # type: ignore[attr-defined]  # pyright: ignore


def instance_field_is_not_an_expression(author: Author) -> None:
    # An instance attribute is the value, so it has no query-builder methods.
    author.name.like("a%")  # type: ignore[attr-defined]  # pyright: ignore


def instance_assignment_is_checked(author: Author) -> None:
    author.id = "not an int"  # type: ignore[assignment]  # pyright: ignore


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------

Query("authors")  # type: ignore[call-overload]  # pyright: ignore

# A subquery is a source, not a select entity.
Query(Query(Author.id).subquery("s"))  # type: ignore[call-overload]  # pyright: ignore


# --------------------------------------------------------------------------
# Predicate combinators take predicates, not columns or values
# --------------------------------------------------------------------------

or_(Author.id, Author.name)  # type: ignore[arg-type]  # pyright: ignore
and_(True, False)  # type: ignore[arg-type]  # pyright: ignore
not_(Author.id)  # type: ignore[arg-type]  # pyright: ignore
exists(Author.id == 1)  # type: ignore[arg-type]  # pyright: ignore

Query(Author).where("id > 5")  # type: ignore[arg-type]  # pyright: ignore
Query(Author).having(count())  # type: ignore[arg-type]  # pyright: ignore


# --------------------------------------------------------------------------
# limit/offset take ints
# --------------------------------------------------------------------------

Query(Author).limit("10")  # type: ignore[arg-type]  # pyright: ignore
Query(Author).offset(1.5)  # type: ignore[arg-type]  # pyright: ignore


# --------------------------------------------------------------------------
# The row type is not Any, so misusing it is caught
# --------------------------------------------------------------------------


async def row_type_is_enforced(engine: DatabaseEngine) -> None:
    authors = await engine.fetch_all(Query(Author))
    # A list of Author cannot be unpacked as a pair.
    author, book = authors[0]  # type: ignore[misc]  # pyright: ignore

    pairs = await engine.fetch_all(
        Query(Author, Book).join(Book, Book.author_id == Author.id)
    )
    # And a tuple row has no model attributes.
    pairs[0].name  # type: ignore[attr-defined]  # pyright: ignore


# --------------------------------------------------------------------------
# What cannot be caught, and why
# --------------------------------------------------------------------------
# `Author.name == 5` is not a type error in either checker, and no annotation makes
# it one. Python resolves `a == b` by trying `a.__eq__(b)` and then the reflected
# `b.__eq__(a)`; when the declared `__eq__` rejects the operand, the checker falls
# back to `object.__eq__`, which accepts anything and returns bool. Ordering
# operators have no such fallback, which is why `>` and `<` above *are* caught.
#
# SQLAlchemy has the same hole for the same reason. The consequence is small — the
# comparison still renders correct SQL and the database rejects or coerces it — but
# it is a hole, so it is written down rather than left to be discovered.
#
# Uncomment either line and both checkers stay silent:
#     Author.name == 5
#     Author.id == "abc"
