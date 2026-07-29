"""Two-table schema for the join benchmark, defined for rowform, SA Core and SA ORM.

Separate from `benchmarks/models.py` on purpose. That module's `users` table backs
every published single-table figure, and adding a second table plus a foreign key to
it would change the file the sqlite suite seeds and re-time — a change made for a new
benchmark must not be able to move an existing number.

The shape is chosen so a join has something to be slow about:

* Two entities to hydrate per row, not one, so the join hydrator's per-row work is
  double the single-table case.
* A `bool` on **both** sides. sqlite has no boolean type, so 0/1 comes back and every
  contender has to coerce it. Putting one on each side means a mapper that only
  converts the driving entity's columns is caught by the equivalence gate.
* Short strings and small ints otherwise, matching the single-table benchmark, so the
  difference between the two suites is the join and not the row width.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from rowform import Column, ModelMeta

AUTHORS_TABLE = "j_authors"
POSTS_TABLE = "j_posts"

DDL = [
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
    # The join predicate's index. Without it sqlite scans j_posts per author and the
    # benchmark measures the query plan rather than the mapper — for *every*
    # contender equally, but it would swamp the difference being looked for.
    f"CREATE INDEX j_posts_author ON {POSTS_TABLE} (author_id)",
]


# --- rowform ----------------------------------------------------------------


class Author(metaclass=ModelMeta):
    __tablename__ = AUTHORS_TABLE

    id = Column(int)
    name = Column(str)
    email = Column(str)
    is_active = Column(bool)


class Post(metaclass=ModelMeta):
    __tablename__ = POSTS_TABLE

    id = Column(int)
    author_id = Column(int)
    title = Column(str)
    score = Column(int)
    published = Column(bool)


# --- SQLAlchemy Core ------------------------------------------------------

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


# --- SQLAlchemy ORM -------------------------------------------------------


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


# Column name order, used by every contender to build identical JSON. Read off the
# ORM tables so a schema edit cannot leave one contender emitting a different shape.
AUTHOR_FIELDS = [str(c.name) for c in AuthorORM.__table__.columns]
POST_FIELDS = [str(c.name) for c in PostORM.__table__.columns]
