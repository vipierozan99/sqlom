"""Types that must be inferred exactly. Checked by mypy and pyright; never run.

`assert_type` compares for exact equality, so a type that has decayed to `Any`
fails here — which is the failure mode worth guarding, since `Any` silently makes
every other assertion pass.
"""

from typing import Any, assert_type

from rowform import (
    CTE,
    Aggregate,
    Alias,
    BinaryOp,
    Case,
    Column,
    ColumnExpr,
    CompoundSelect,
    Condition,
    DatabaseEngine,
    Delete,
    Excluded,
    FunctionCall,
    InClause,
    Insert,
    ModelMeta,
    Over,
    Predicate,
    Query,
    Subquery,
    UnaryOp,
    Update,
    and_,
    avg,
    case,
    count,
    dense_rank,
    excluded,
    exists,
    first_value,
    func,
    lag,
    last_value,
    lead,
    max_,
    max_rows_per_statement,
    min_,
    not_,
    ntile,
    or_,
    rank,
    recursive_cte,
    row_number,
    select,
    sql_function,
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
assert_type(Author.name == None, Condition)
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
    .where(Author.active == True)
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


async def executing_sqlalchemy_style(db: DatabaseEngine) -> None:
    # execute() is the single entry point: a select(), like SQLAlchemy's
    # `conn.execute(stmt)`, hydrates and returns its rows.
    authors = await db.execute(select(Author))
    assert_type(authors, list[Author])
    assert_type(authors[0].name, str)


async def in_a_transaction(db: DatabaseEngine) -> None:
    async with db.transaction() as tx:
        assert_type(await tx.fetch_all(Query(Author)), list[Author])
        assert_type(
            await tx.fetch_all(Query(Author, Book.title)
                               .join(Book, Book.author_id == Author.id)),
            list[tuple[Author, str]],
        )
        assert_type(await tx.fetch_json(Query(Author)), bytes)
        assert_type(await tx.execute(select(Author)), list[Author])
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


# --------------------------------------------------------------------------
# Arithmetic, functions, CASE, windows
# --------------------------------------------------------------------------

# Arithmetic keeps the operand's type, so the result still compares against it.
assert_type(Book.id + 1, BinaryOp[int])
assert_type(Book.id - 1, BinaryOp[int])
assert_type(Book.id * 2, BinaryOp[int])
assert_type(Book.id % 2, BinaryOp[int])
assert_type(1 + Book.id, BinaryOp[int])
assert_type(-Book.id, UnaryOp[int])
assert_type(Book.id * 2 > 10, Condition)
# Division does not preserve the type: integer division is not an integer in
# Postgres for every operand pairing, so it is Any rather than a guess.
assert_type(Book.id / 2, BinaryOp[Any])
# `||` yields text whatever went in.
assert_type(Book.title.concat("!"), BinaryOp[str])
assert_type(Book.id.operate("#>>", "x"), BinaryOp[Any])

# A function's return type is unknowable without a signature database, so it is
# Any unless stated.
assert_type(func.lower(Book.title), FunctionCall[Any])
assert_type(sql_function("lower", Book.title), FunctionCall[Any])

assert_type(case((Book.id > 1, "a"), else_="b"), Case[Any])

# Window functions that are inherently integers say so.
assert_type(row_number(), FunctionCall[int])
assert_type(rank(), FunctionCall[int])
assert_type(dense_rank(), FunctionCall[int])
assert_type(ntile(4), FunctionCall[int])
# lag/lead/first/last carry the column's type through.
assert_type(lag(Book.id), FunctionCall[int])
assert_type(lead(Book.title), FunctionCall[str])
assert_type(first_value(Book.title), FunctionCall[str])
assert_type(last_value(Book.id), FunctionCall[int])

assert_type(row_number().over(order_by=Book.id), Over[int])
assert_type(count().over(), Over[int])
assert_type(sum_(Book.id).over(), Over[Any])

# And they flow into a row type.
assert_type(
    Query(Book.id, row_number().over(order_by=Book.id)),
    Query[tuple[int, int]],
)
assert_type(Query(Book.id, Book.id * 2), Query[tuple[int, int]])
assert_type(Query(count(Book)), Query[tuple[int]])


# --------------------------------------------------------------------------
# Set operations preserve the row type
# --------------------------------------------------------------------------

assert_type(Query(Author).union(Query(Author)), CompoundSelect[Author])
assert_type(Query(Author).union_all(Query(Author)), CompoundSelect[Author])
assert_type(Query(Author).intersect(Query(Author)), CompoundSelect[Author])
assert_type(Query(Author).except_(Query(Author)), CompoundSelect[Author])
assert_type(
    Query(Author.id).union(Query(Author.id)).order_by("id").limit(5),
    CompoundSelect[tuple[int]],
)


async def fetching_a_compound(db: DatabaseEngine) -> None:
    rows = await db.fetch_all(Query(Author).union(Query(Author)))
    assert_type(rows, list[Author])
    assert_type(rows[0].name, str)


# --------------------------------------------------------------------------
# DML
# --------------------------------------------------------------------------

assert_type(Insert(Author).values(name="ada"), Insert)
assert_type(Update(Author).set(name="z"), Update)
assert_type(Delete(Author).all_rows(), Delete)
assert_type(Insert(Author).values(name="a").returning(Author.id), Insert)
assert_type(Insert(Author).values(name="a").row_count, int)
assert_type(Insert(Author).values(name="a").returns_rows, bool)
assert_type(max_rows_per_statement(Author), int)


async def writing(db: DatabaseEngine) -> None:
    # No RETURNING: execute, which reports the driver's own status.
    await db.execute(Insert(Author).values(name="ada"))
    await db.execute(Update(Author).set(name="z").where(Author.id == 1))
    await db.execute(Delete(Author).where(Author.id == 1))

    # With RETURNING: fetch_all, and the rows hydrate.
    # list[Any], not list[int]: returning() is chained after construction, so a
    # checker cannot re-parameterise the statement the way Query's constructor
    # overloads do. Stated rather than faked — see tests/typing/README.md.
    ids = await db.fetch_all(Insert(Author).values(name="a").returning(Author.id))
    assert_type(ids, list[Any])


# --------------------------------------------------------------------------
# CTEs
# --------------------------------------------------------------------------

assert_type(Query(Author).cte("c"), CTE)
assert_type(Query(Author).with_(Query(Author).cte("c")), Query[Author])
assert_type(Query(Author.id).cte("c").alias, str)
assert_type(Query(Author.id).cte("c").recursive, bool)
assert_type(Query(Author.id).cte("c").column_names, list[str])
assert_type(Query(Author.id).cte("c").referenced_ctes(), list[CTE])

assert_type(
    recursive_cte(
        "tree",
        Query(Book.id, Book.author_id),
        lambda cte: Query(Book.id, Book.author_id).join(cte, Book.author_id == cte.id),
    ),
    CTE,
)

# A CTE's columns are ColumnExpr[Any], like a Subquery's and for the same reason:
# the names come from a runtime column map a checker cannot enumerate. Reach the
# column off the model when the precise type matters.
assert_type(Query(Author.id).cte("c").id, ColumnExpr[Any])


def cte_as_a_source() -> None:
    counts = Query(Book.author_id).cte("counts")
    # No assert_type for `Query(counts.author_id)`: the two checkers genuinely
    # disagree. A CTE column is ColumnExpr[Any], and mypy lets Any match the
    # whole-model overload first (Query[Any]) while pyright picks the single-column
    # one (Query[tuple[Any]]). Both are defensible readings of an Any argument, and
    # asserting either would fail the other, so this is recorded rather than picked.
    # Reach the column off the model when the row type has to be precise.
    # Joining one to a model keeps the model's own type precise.
    assert_type(
        Query(Author).join(counts, counts.author_id == Author.id),
        Query[Author],
    )


# --------------------------------------------------------------------------
# ON CONFLICT
# --------------------------------------------------------------------------

assert_type(
    Insert(Author).values(name="a").on_conflict_do_nothing(Author.id),
    Insert,
)
assert_type(
    Insert(Author).values(name="a").on_conflict_do_nothing(constraint="authors_pkey"),
    Insert,
)
assert_type(
    Insert(Author).values(name="a").on_conflict_do_update(
        Author.id, set_={"name": excluded(Author.name)}
    ),
    Insert,
)
assert_type(
    Insert(Author).values(name="a").on_conflict_do_update(
        "id", set_={"name": "x"}, where=Author.active == True
    ),
    Insert,
)

# excluded() keeps the column's type, so it composes with typed arithmetic and
# comparisons rather than decaying to Any.
assert_type(excluded(Author.id), Excluded[int])
assert_type(excluded(Author.name), Excluded[str])
assert_type(excluded(Author.id) > 5, Condition)
assert_type(Author.id + excluded(Author.id), BinaryOp[int])
assert_type(excluded(Author.id).py_type, Any)


# --------------------------------------------------------------------------
# UPDATE ... FROM and DELETE ... USING
# --------------------------------------------------------------------------

assert_type(
    Update(Book).set(title=Author.name).from_(Author).where(Author.id == Book.author_id),
    Update,
)
assert_type(Update(Book).from_(Author, Alias(Author, "a2")), Update)
assert_type(Delete(Book).using(Author).where(Author.id == Book.author_id), Delete)
assert_type(Delete(Book).using(Alias(Author, "a2")), Delete)
