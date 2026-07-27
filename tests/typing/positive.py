"""Types that must be inferred exactly. Checked by mypy and pyright; never run.

`assert_type` compares for exact equality, so a type that has decayed to `Any`
fails here — which is the failure mode worth guarding, since `Any` silently makes
every other assertion pass.
"""

from typing import Any, assert_type

from sqlom import (
    Aggregate,
    Alias,
    Column,
    ColumnExpr,
    Condition,
    DatabaseEngine,
    InClause,
    ModelMeta,
    Predicate,
    Query,
    Subquery,
    and_,
    avg,
    count,
    exists,
    max_,
    min_,
    not_,
    or_,
    sum_,
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
# The dual return type: the whole reason the descriptor is overloaded
# --------------------------------------------------------------------------

assert_type(Author.id, ColumnExpr[int])
assert_type(Author.name, ColumnExpr[str])
assert_type(Author.active, ColumnExpr[bool])


def instance_attributes(author: Author) -> None:
    assert_type(author.id, int)
    assert_type(author.name, str)
    assert_type(author.active, bool)


# --------------------------------------------------------------------------
# Predicates
# --------------------------------------------------------------------------

assert_type(Author.id > 5, Condition)
assert_type(Author.id == 5, Condition)
assert_type(Author.name == None, Condition)  # noqa: E711
assert_type(Author.name.like("a%"), Condition)
assert_type(Author.id.is_null(), Condition)
assert_type(Book.author_id == Author.id, Condition)

assert_type(or_(Author.id == 1, Author.id == 2), Predicate)
assert_type(and_(Author.id == 1, Author.id == 2), Predicate)
assert_type(not_(Author.id == 1), Predicate)
assert_type(~(Author.id == 1), Predicate)
assert_type((Author.id == 1) | (Author.id == 2), Predicate)
assert_type((Author.id == 1) & (Author.id == 2), Predicate)


# --------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------

assert_type(count(), Aggregate[int])
assert_type(count(Book.id), Aggregate[int])
# min/max keep the column's type; sum/avg do not claim one.
assert_type(min_(Book.id), Aggregate[int])
assert_type(max_(Book.title), Aggregate[str])
assert_type(sum_(Book.id), Aggregate[Any])
assert_type(avg(Book.id), Aggregate[Any])


# --------------------------------------------------------------------------
# Row types
# --------------------------------------------------------------------------

assert_type(Query(Author), Query[Author])
assert_type(Query(Author.name), Query[tuple[str]])
assert_type(Query(Author, Book), Query[tuple[Author, Book]])
assert_type(Query(Author, Book.title), Query[tuple[Author, str]])
assert_type(Query(Author.name, Book.title), Query[tuple[str, str]])
assert_type(Query(Book.author_id, count()), Query[tuple[int, int]])
assert_type(Query(Author, Book, Book.title), Query[tuple[Author, Book, str]])
assert_type(
    Query(Author, Book, Author.name, Book.title),
    Query[tuple[Author, Book, str, str]],
)

# An alias selects as its model.
mgr = Alias(Author, "mgr")
assert_type(mgr, Alias[Author])
assert_type(Query(mgr), Query[Author])
assert_type(Query(Author, mgr), Query[tuple[Author, Author]])


# Builder methods preserve the row type all the way down the chain.
assert_type(
    Query(Author, Book)
    .join(Book, Book.author_id == Author.id)
    .where(Author.active == True)  # noqa: E712
    .order_by(Book.title)
    .limit(10)
    .offset(5)
    .distinct(),
    Query[tuple[Author, Book]],
)
assert_type(
    Query(Book.author_id, count()).group_by(Book.author_id).having(count() > 1),
    Query[tuple[int, int]],
)


# --------------------------------------------------------------------------
# The payoff: what fetch_all gives back
# --------------------------------------------------------------------------


async def fetching(db: DatabaseEngine) -> None:
    authors = await db.fetch_all(Query(Author))
    assert_type(authors, list[Author])
    assert_type(authors[0].name, str)

    pairs = await db.fetch_all(
        Query(Author, Book).join(Book, Book.author_id == Author.id)
    )
    assert_type(pairs, list[tuple[Author, Book]])
    author, book = pairs[0]
    assert_type(author.name, str)
    assert_type(book.title, str)

    mixed = await db.fetch_all(
        Query(Author, Book.title).join(Book, Book.author_id == Author.id)
    )
    assert_type(mixed, list[tuple[Author, str]])

    counts = await db.fetch_all(
        Query(Book.author_id, count()).group_by(Book.author_id)
    )
    assert_type(counts, list[tuple[int, int]])

    assert_type(await db.fetch_json(Query(Author)), bytes)


async def in_a_transaction(db: DatabaseEngine) -> None:
    async with db.transaction() as tx:
        assert_type(await tx.fetch_all(Query(Author)), list[Author])
        assert_type(
            await tx.fetch_all(Query(Author, Book.title)
                               .join(Book, Book.author_id == Author.id)),
            list[tuple[Author, str]],
        )
        assert_type(await tx.fetch_json(Query(Author)), bytes)
        async with tx.transaction() as savepoint:
            assert_type(await savepoint.fetch_all(Query(Book)), list[Book])


# --------------------------------------------------------------------------
# Subqueries
# --------------------------------------------------------------------------

assert_type(Query(Author.id).subquery("s"), Subquery)
assert_type(exists(Query(Book.id)), Predicate)
# in_/not_in return the precise subclass rather than the Predicate base, which is
# what a caller wants and what assert_type demands.
assert_type(Author.id.in_([1, 2, 3]), InClause)
assert_type(Author.id.in_(Query(Book.author_id)), InClause)
assert_type(Author.id.not_in([1]), InClause)

# Columns off a subquery are Any, and are documented as such.
sub = Query(Book.author_id, count().label("n")).group_by(Book.author_id).subquery("s")
assert_type(sub.n, ColumnExpr[Any])

# And off an alias, for the same reason.
assert_type(mgr.id, ColumnExpr[Any])

# to_sql's shape
assert_type(Query(Author).to_sql(), tuple[str, tuple[Any, ...]])
