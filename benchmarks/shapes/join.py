"""Two-table shape for the join benchmark, defined for rowform, SA Core and SA
ORM. Ported from the old `benchmarks/join_models.py`.

Separate from `shapes/flat.py` on purpose. That module's `users` table backs
every published single-table figure, and adding a second table plus a foreign
key to it would change the file the sqlite suite seeds and re-time — a change
made for a new benchmark must not be able to move an existing number.

The shape is chosen so a join has something to be slow about:

* Two entities to hydrate per row, not one, so the join hydrator's per-row work
  is double the single-table case.
* A `bool` on **both** sides. sqlite has no boolean type, so 0/1 comes back and
  every contender has to coerce it. Putting one on each side means a mapper
  that only converts the driving entity's columns is caught by the
  equivalence gate.
* Short strings and small ints otherwise, matching the single-table benchmark,
  so the difference between the two suites is the join and not the row width.
"""

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
)
from sqlalchemy import (
    Column as SAColumn,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

from rowform import Column, model

AUTHORS_TABLE = "j_authors"
POSTS_TABLE = "j_posts"

# The join predicate's index. Without it sqlite/postgres scan j_posts per
# author and the benchmark measures the query plan rather than the mapper —
# for *every* contender equally, but it would swamp the difference being
# looked for.
_POSTS_INDEX = f"CREATE INDEX j_posts_author ON {POSTS_TABLE} (author_id)"

DDL_SQLITE = [
    f"""
    CREATE TABLE {AUTHORS_TABLE} (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        is_active INTEGER NOT NULL
    )
    """,
    f"""
    CREATE TABLE {POSTS_TABLE} (
        id INTEGER PRIMARY KEY,
        author_id INTEGER NOT NULL REFERENCES {AUTHORS_TABLE}(id),
        title TEXT NOT NULL,
        score INTEGER NOT NULL,
        published INTEGER NOT NULL
    )
    """,
    _POSTS_INDEX,
]

DDL_POSTGRES = [
    f"""
    CREATE TABLE {AUTHORS_TABLE} (
        id integer PRIMARY KEY,
        name text NOT NULL,
        email text NOT NULL,
        is_active boolean NOT NULL
    )
    """,
    f"""
    CREATE TABLE {POSTS_TABLE} (
        id integer PRIMARY KEY,
        author_id integer NOT NULL REFERENCES {AUTHORS_TABLE}(id),
        title text NOT NULL,
        score integer NOT NULL,
        published boolean NOT NULL
    )
    """,
    _POSTS_INDEX,
]

POSTS_PER_AUTHOR = 5


# --- rowform ----------------------------------------------------------------


@model
class Author:
    __tablename__ = AUTHORS_TABLE

    id: Column[int] = Column(int)
    name: Column[str] = Column(str)
    email: Column[str] = Column(str)
    is_active: Column[bool] = Column(bool)


@model
class Post:
    __tablename__ = POSTS_TABLE

    id: Column[int] = Column(int)
    author_id: Column[int] = Column(int)
    title: Column[str] = Column(str)
    score: Column[int] = Column(int)
    published: Column[bool] = Column(bool)


# --- SQLAlchemy Core ---------------------------------------------------------

metadata = MetaData()

authors_table = Table(
    AUTHORS_TABLE,
    metadata,
    SAColumn("id", Integer, primary_key=True),
    SAColumn("name", String, nullable=False),
    SAColumn("email", String, nullable=False),
    SAColumn("is_active", Boolean, nullable=False),
)

posts_table = Table(
    POSTS_TABLE,
    metadata,
    SAColumn("id", Integer, primary_key=True),
    SAColumn("author_id", Integer, ForeignKey(f"{AUTHORS_TABLE}.id"), nullable=False),
    SAColumn("title", String, nullable=False),
    SAColumn("score", Integer, nullable=False),
    SAColumn("published", Boolean, nullable=False),
)


# --- SQLAlchemy ORM -----------------------------------------------------------


class Base(DeclarativeBase):
    pass


class AuthorORM(Base):
    __tablename__ = AUTHORS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


class PostORM(Base):
    __tablename__ = POSTS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTHORS_TABLE}.id"))
    title: Mapped[str]
    score: Mapped[int]
    published: Mapped[bool]


# --- SQLAlchemy ORM (Dataclass) -----------------------------------------------------------


class BaseDC(MappedAsDataclass, DeclarativeBase):
    pass


class AuthorDC(BaseDC):
    __tablename__ = AUTHORS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


class PostDC(BaseDC):
    __tablename__ = POSTS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey(f"{AUTHORS_TABLE}.id"))
    title: Mapped[str]
    score: Mapped[int]
    published: Mapped[bool]


# Column name order, used by every contender to build identical JSON. Read off
# the ORM tables so a schema edit cannot leave one contender emitting a
# different shape.
AUTHOR_FIELDS = [str(c.name) for c in AuthorORM.__table__.columns]
POST_FIELDS = [str(c.name) for c in PostORM.__table__.columns]


def generate_authors(rng, n, start=1):
    """Deterministic author rows: 10% inactive, matching `shapes.flat`."""
    for i in range(start, start + n):
        yield (i, f"author-{i}", f"author-{i}@example.com", rng.random() > 0.1)


def generate_posts(rng, n_authors, posts_per_author=POSTS_PER_AUTHOR):
    """Deterministic post rows, `posts_per_author` per author: 20% unpublished."""
    for author_id in range(1, n_authors + 1):
        first = (author_id - 1) * posts_per_author + 1
        for post_id in range(first, first + posts_per_author):
            yield (post_id, author_id, f"post-{post_id}", post_id % 1000, rng.random() > 0.2)
