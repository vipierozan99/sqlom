"""Single-table shape: the `users` table every published single-table figure
uses. Ported from the old `benchmarks/models.py`, kept separate from
`shapes/join.py` on purpose — see that module's docstring.
"""

from sqlalchemy import Boolean, Integer, MetaData, String, Table
from sqlalchemy import Column as SAColumn
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from rowform import Column, ModelMeta, model

TABLE_NAME = "users"

DDL_SQLITE = [
    f"""
    CREATE TABLE {TABLE_NAME} (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        is_active INTEGER NOT NULL
    )
    """,
]

DDL_POSTGRES = [
    f"""
    CREATE TABLE {TABLE_NAME} (
        id integer PRIMARY KEY,
        name text NOT NULL,
        email text NOT NULL,
        is_active boolean NOT NULL
    )
    """,
    f"CREATE INDEX {TABLE_NAME}_active_id ON {TABLE_NAME} (is_active, id)",
]


class User(metaclass=ModelMeta):
    """rowform model — the thing under test."""

    __tablename__ = TABLE_NAME

    id = Column(int)
    name = Column(str)
    email = Column(str)
    is_active = Column(bool)


metadata = MetaData()

users_table = Table(
    TABLE_NAME,
    metadata,
    SAColumn("id", Integer, primary_key=True),
    SAColumn("name", String, nullable=False),
    SAColumn("email", String, nullable=False),
    SAColumn("is_active", Boolean, nullable=False),
)


class Base(DeclarativeBase):
    pass


class UserORM(Base):
    __tablename__ = TABLE_NAME

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    is_active: Mapped[bool]


@model
class UserDC:
    """Same schema as `User`, but a real stdlib @dataclass(slots=True) whose
    class-level attribute access still yields query expressions."""

    __tablename__ = TABLE_NAME

    id: int
    name: str
    email: str
    is_active: bool


def generate_rows(rng, n, start=1):
    """Deterministic row generator: id, name, email, is_active.

    `is_active` uses the `rng.random() > 0.1` idiom shared (as a copy-pasted
    literal) by 8 of the old suite's seed functions — consolidated here rather
    than in each backend.
    """
    for i in range(start, start + n):
        yield (i, f"user-{i}", f"user-{i}@example.com", rng.random() > 0.1)
