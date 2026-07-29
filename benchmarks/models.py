"""Shared schema for the sqlite micro-benchmark, defined three ways so all
three approaches query the exact same table over the exact same connection
type — only the Python-side hydration/serialization path differs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Boolean, Integer, MetaData, String, Table
from sqlalchemy import Column as SAColumn
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from rowform import Column, ModelMeta, model

TABLE_NAME = "users"

DDL = f"""
CREATE TABLE {TABLE_NAME} (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    is_active INTEGER NOT NULL
)
"""


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
