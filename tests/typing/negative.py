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
    Delete,
    Insert,
    ModelMeta,
    Query,
    Update,
    and_,
    case,
    count,
    excluded,
    exists,
    not_,
    or_,
    recursive_cte,
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
# The new expression surface
# --------------------------------------------------------------------------

# Arithmetic keeps the column's type, so the result is still checked.
Book.id * "two"  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
Book.id + "one"  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]
(Book.id * 2) > "abc"  # type: ignore[operator]  # pyright: ignore[reportOperatorIssue]

# case() takes (predicate, value) pairs, not a bare predicate.
case(Book.id > 1)  # type: ignore[arg-type]  # pyright: ignore

# A window is built from a function, and over() lives on those rather than on a
# plain column.
Book.id.over()  # type: ignore[attr-defined]  # pyright: ignore


# --------------------------------------------------------------------------
# Set operations
# --------------------------------------------------------------------------

Query(Author).union("SELECT 1")  # type: ignore[arg-type]  # pyright: ignore
Query(Author).union(Query(Author)).limit("5")  # type: ignore[arg-type]  # pyright: ignore


# --------------------------------------------------------------------------
# DML
# --------------------------------------------------------------------------

Insert("authors")  # type: ignore[arg-type]  # pyright: ignore
Update(Author).set(name="z").where("id = 1")  # type: ignore[arg-type]  # pyright: ignore
Delete(Author).where("id = 1")  # type: ignore[arg-type]  # pyright: ignore
Insert(Author).values(name="a").returning("id")  # type: ignore[arg-type]  # pyright: ignore


async def execute_only_takes_statements(engine: DatabaseEngine) -> None:
    # execute() is for writes; a select has no rowcount to report.
    await engine.execute(Query(Author))  # type: ignore[arg-type]  # pyright: ignore


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
#
# `-Author.name` is also not caught. `__neg__` takes no operand, so there is no
# argument to constrain by the column's type; expressing "only numeric columns"
# would need per-type descriptor classes rather than one generic ColumnExpr.
#     -Author.name


# --------------------------------------------------------------------------
# CTEs
# --------------------------------------------------------------------------

Query(Author).cte(5)  # type: ignore[arg-type]  # pyright: ignore
Query(Author).with_(Query(Author))  # type: ignore[arg-type]  # pyright: ignore
Query(Author).with_("c")  # type: ignore[arg-type]  # pyright: ignore
recursive_cte("t", "SELECT 1", lambda cte: Query(Author))  # type: ignore[arg-type]  # pyright: ignore


# --------------------------------------------------------------------------
# ON CONFLICT
# --------------------------------------------------------------------------

# set_ is required: on_conflict_do_update() without it is do_nothing() spelled wrong.
Insert(Author).values(name="a").on_conflict_do_update(Author.id)  # type: ignore[call-arg]  # pyright: ignore
# A predicate, not a string.
Insert(Author).values(name="a").on_conflict_do_update(Author.id, set_={"name": "x"}, where="active")  # type: ignore[arg-type]  # pyright: ignore
# An index element is a column or a name, not a model.
Insert(Author).values(name="a").on_conflict_do_nothing(Author)  # type: ignore[arg-type]  # pyright: ignore
# constraint= is a name, not a column.
Insert(Author).values(name="a").on_conflict_do_nothing(constraint=Author.id)  # type: ignore[arg-type]  # pyright: ignore
# excluded() takes a column, not a name.
excluded("name")  # type: ignore[arg-type]  # pyright: ignore
# And it keeps the column's type, so a wrong-typed comparison is still caught.
excluded(Author.id) > "abc"  # type: ignore[operator]  # pyright: ignore


# --------------------------------------------------------------------------
# UPDATE ... FROM and DELETE ... USING
# --------------------------------------------------------------------------

Update(Author).set(name="z").from_("books")  # type: ignore[arg-type]  # pyright: ignore
Update(Author).set(name="z").from_(Author.id)  # type: ignore[arg-type]  # pyright: ignore
Delete(Author).using("books")  # type: ignore[arg-type]  # pyright: ignore
Delete(Author).using(Author.id)  # type: ignore[arg-type]  # pyright: ignore
