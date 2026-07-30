"""The shape that makes the equivalence gate mean something.

`shapes/flat.py` is `int/str/str/bool`, and every published figure came from it.
That is the one layout where bypassing SQLAlchemy's `Row` looks free on sqlite:
the only column needing conversion is a boolean, which a single `bool()` call
covers. docs/PLAN_CORE_COMPILER.md §7 R1 flagged this as the plan's most likely
disqualifier, and measuring a widened shape showed 8 of 13 columns coming back
wrong without the right per-column processor — temporal types as strings,
`Numeric` as float, `Enum` as its member name, `Uuid` as hex, `JSON` as text.

So this shape exists to be *expensive and easy to get wrong*, not to be fast:

* `DateTime`/`Date`/`Time`, which sqlite stores as strings and postgres does not,
  so the two backends exercise genuinely different processor paths.
* `Numeric`, whose postgres processor **raises** unless it is handed the DBAPI
  type code — the reason hydrators are planned after the first execute rather
  than at compile time.
* A nullable column, which the retired converter table could not have handled at
  all: it was keyed by exact Python type, so `str | None` never matched `str`.
* `Uuid` and `JSON`, where sqlite and asyncpg disagree about who decodes.

Numbers from this shape are not comparable with `flat`'s — that is the point.
Read it as "what does correctness cost when the columns are not trivial", and
read `flat` as "what does the row layer cost when they are".
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

import rowform as rf

TABLE_NAME = "w_events"

_EPOCH = dt.datetime(2020, 1, 1, 0, 0, 0)


class Severity(enum.Enum):
    LOW = "low"
    HIGH = "high"


class Base(rf.Base):
    metadata = sa.MetaData()


class Event(Base):
    __tablename__ = TABLE_NAME

    id: Mapped[int] = rf.mapped_column(primary_key=True, autoincrement=False)
    label: Mapped[str]
    seen: Mapped[bool]
    at: Mapped[dt.datetime]
    day: Mapped[dt.date]
    amount: Mapped[decimal.Decimal] = rf.mapped_column(sa.Numeric(12, 3))
    severity: Mapped[Severity]
    trace: Mapped[uuid.UUID]
    note: Mapped[str | None]


metadata = Base.metadata
events_table = Event.__table__
FIELDS = [str(c.name) for c in events_table.columns]

sa.Index(f"{TABLE_NAME}_seen_id", events_table.c.seen, events_table.c.id)


class ORMBase(DeclarativeBase):
    pass


class EventORM(ORMBase):
    __tablename__ = TABLE_NAME

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]
    seen: Mapped[bool]
    at: Mapped[dt.datetime]
    day: Mapped[dt.date]
    amount: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(12, 3))
    severity: Mapped[Severity]
    trace: Mapped[uuid.UUID]
    note: Mapped[str | None]


class DCBase(MappedAsDataclass, DeclarativeBase):
    pass


class EventDC(DCBase):
    __tablename__ = TABLE_NAME

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]
    seen: Mapped[bool]
    at: Mapped[dt.datetime]
    day: Mapped[dt.date]
    amount: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(12, 3))
    severity: Mapped[Severity]
    trace: Mapped[uuid.UUID]
    note: Mapped[str | None]


def generate_rows(rng, n, start=1):
    """Deterministic rows. Every non-trivial column varies per row, so a
    contender that hoisted one conversion out of the loop would be caught."""
    for i in range(start, start + n):
        yield (
            i,
            f"event-{i}",
            rng.random() > 0.1,
            _EPOCH + dt.timedelta(seconds=i, microseconds=i % 1000),
            (_EPOCH + dt.timedelta(days=i % 365)).date(),
            decimal.Decimal(i) / 1000,
            Severity.HIGH if i % 3 else Severity.LOW,
            uuid.UUID(int=i),
            None if i % 4 == 0 else f"note-{i}",
        )
