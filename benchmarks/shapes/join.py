"""Two-table shape for the join benchmark.

Separate from `shapes/flat.py` on purpose. That module's `users` table backs
every published single-table figure, and adding a second table plus a foreign
key to it would change the file the sqlite suite seeds and re-times — a change
made for a new benchmark must not be able to move an existing number.

The shape is chosen so a join has something to be slow about:

* Two entities to hydrate per row, not one, so the join hydrator's per-row work
  is double the single-table case.
* A `bool` on **both** sides. sqlite has no boolean type, so 0/1 comes back and
  every contender has to coerce it. Putting one on each side means a mapper that
  only converts the driving entity's columns is caught by the equivalence gate.
* Short strings and small ints otherwise, matching the single-table benchmark,
  so the difference between the two suites is the join and not the row width.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

import rowform

AUTHORS_TABLE = "j_authors"
POSTS_TABLE = "j_posts"
POSTS_PER_AUTHOR = 5


class Base(rowform.Base):
    metadata = sa.MetaData()


class Author(Base):
    __tablename__ = AUTHORS_TABLE

    id: Mapped[int] = rowform.mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


class Post(Base):
    __tablename__ = POSTS_TABLE

    id: Mapped[int] = rowform.mapped_column(primary_key=True, autoincrement=False)
    author_id: Mapped[int] = rowform.mapped_column(sa.ForeignKey(f"{AUTHORS_TABLE}.id"))
    title: Mapped[str]
    score: Mapped[int]
    published: Mapped[bool]


metadata = Base.metadata
authors_table = Author.__table__
posts_table = Post.__table__

# The join predicate's index. Without it sqlite/postgres scan j_posts per author
# and the benchmark measures the query plan rather than the row layer — for
# *every* contender equally, but it would swamp the difference being looked for.
sa.Index("j_posts_author", posts_table.c.author_id)

# Column name order, used by every contender to build identical JSON. Read off
# the tables so a schema edit cannot leave one contender emitting a different
# shape.
AUTHOR_FIELDS = [str(c.name) for c in authors_table.columns]
POST_FIELDS = [str(c.name) for c in posts_table.columns]


class ORMBase(DeclarativeBase):
    pass


class AuthorORM(ORMBase):
    __tablename__ = AUTHORS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


class PostORM(ORMBase):
    __tablename__ = POSTS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(sa.ForeignKey(f"{AUTHORS_TABLE}.id"))
    title: Mapped[str]
    score: Mapped[int]
    published: Mapped[bool]


class DCBase(MappedAsDataclass, DeclarativeBase):
    pass


class AuthorDC(DCBase):
    __tablename__ = AUTHORS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


class PostDC(DCBase):
    __tablename__ = POSTS_TABLE

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(sa.ForeignKey(f"{AUTHORS_TABLE}.id"))
    title: Mapped[str]
    score: Mapped[int]
    published: Mapped[bool]


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
