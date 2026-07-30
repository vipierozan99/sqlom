"""Single-table shape: the `users` table every published single-table figure uses.

**This file is the rewrite's most visible dividend.** It used to carry four
parallel declarations of the same four columns — a rowform `@model`, a bare
`Table`, a `DeclarativeBase` model and a `MappedAsDataclass` one — plus two
hand-written `CREATE TABLE` strings, one per dialect. It now carries two: the
rowform model, which *is* the `Table`, and the ORM models it is measured
against. The DDL is generated from the first of those (`harness/seed.py`), so a
benchmark can no longer seed a table that differs from the one it queries.

Two ORM declarations remain on purpose. `UserORM` is stock declarative and
`UserDC` is `MappedAsDataclass`; the second exists because the first returns
instrumented objects carrying loader state, and comparing against only that
would overstate the win — `MappedAsDataclass` is the closest thing the ORM has
to what rowform produces.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

import rowform as rf

TABLE_NAME = "users"


class Base(rf.Base):
    metadata = sa.MetaData()


class User(Base):
    """The thing under test — and the table definition, and the row container."""

    __tablename__ = TABLE_NAME

    id: Mapped[int] = rf.mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


metadata = Base.metadata

#: Still a name, because the whole suite talks about "the table". It is no
#: longer a second declaration of one.
users_table = User.__table__

# The index the postgres runs need: without it the `is_active`/`id` predicate
# scans, and the benchmark measures the query plan rather than the row layer.
sa.Index(f"{TABLE_NAME}_active_id", users_table.c.is_active, users_table.c.id)

FIELDS = [str(c.name) for c in users_table.columns]


class ORMBase(DeclarativeBase):
    """Its own `MetaData`: the ORM models describe the same table, and two
    declarations of `users` in one `MetaData` is an error, not a comparison."""


class UserORM(ORMBase):
    __tablename__ = TABLE_NAME

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


class DCBase(MappedAsDataclass, DeclarativeBase):
    pass


class UserDC(DCBase):
    __tablename__ = TABLE_NAME

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


def generate_rows(rng, n, start=1):
    """Deterministic row generator: id, name, email, is_active.

    `is_active` uses the `rng.random() > 0.1` idiom shared (as a copy-pasted
    literal) by 8 of the old suite's seed functions — consolidated here rather
    than in each backend.
    """
    for i in range(start, start + n):
        yield (i, f"user-{i}", f"user-{i}@example.com", rng.random() > 0.1)
