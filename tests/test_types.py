"""Values must survive the round trip identically to stock SQLAlchemy.

This is the gate docs/METHODOLOGY.md correction 11 asks for, and the reason the
library reads SQLAlchemy's own per-column processors instead of keeping a type
table of its own.

Measured before that decision, over the `Wide` shape: bypassing `Row` on sqlite
returned 8 of 13 columns as something else — `DateTime`/`Date`/`Time` as strings,
`Numeric` as float, `Enum` as its member name, `Uuid` as hex, `JSON` as text,
`Boolean` as int. The hand-written converter table this replaced covered exactly
one of them.

The oracle is stock SQLAlchemy: every assertion compares against what
`Connection.execute(stmt).all()` returns for the same statement, so the claim is
"identical to SQLAlchemy", not "matches what we happened to write down".
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest
import sqlalchemy as sa
from conftest import WIDE_ROW, Base, Colour, Wide


@pytest.fixture
def oracle(sqlite_path):
    """Stock SQLAlchemy Core over the same file, with its full result layer."""
    engine = sa.create_engine(f"sqlite:///{sqlite_path}")
    yield engine
    engine.dispose()


async def test_every_column_matches_stock_core(sqlite_engine, oracle):
    statement = sa.select(Wide).where(Wide.id == 1)

    ours = (await sqlite_engine.fetch_all(statement))[0]
    with oracle.connect() as conn:
        theirs = conn.execute(statement).one()

    mismatches = []
    for field, reference in zip(Wide.__column_order__, tuple(theirs)):
        mine = getattr(ours, field)
        if mine != reference or type(mine) is not type(reference):
            mismatches.append((field, type(mine).__name__, type(reference).__name__))
    assert not mismatches


async def test_values_survive_the_round_trip(sqlite_engine):
    """Not just "same as Core" — the values that went in are the values that
    come out, so a bind-processor bug cannot cancel a result-processor bug."""
    row = (await sqlite_engine.fetch_all(sa.select(Wide).where(Wide.id == 1)))[0]
    for field, sent in WIDE_ROW.items():
        assert getattr(row, field) == sent, field


class TestPythonTypes:
    """Each of these is a type sqlite does not store natively."""

    @pytest.fixture
    async def row(self, engine):
        return (await engine.fetch_all(sa.select(Wide).where(Wide.id == 1)))[0]

    def test_datetime(self, row):
        assert isinstance(row.when, dt.datetime)
        assert row.when.microsecond == 123456

    def test_date(self, row):
        assert isinstance(row.day, dt.date) and not isinstance(row.day, dt.datetime)

    def test_time(self, row):
        assert isinstance(row.clock, dt.time)

    def test_numeric_keeps_its_scale(self, row):
        assert isinstance(row.amount, decimal.Decimal)
        assert row.amount == decimal.Decimal("19.990")

    def test_boolean(self, row):
        assert row.flag is True

    def test_enum_is_the_member_not_its_name(self, row):
        assert row.colour is Colour.RED

    def test_uuid(self, row):
        assert isinstance(row.uid, uuid.UUID)
        assert row.uid == uuid.UUID("12345678-1234-5678-1234-567812345678")

    def test_json_is_parsed(self, row):
        assert row.payload == {"a": [1, 2], "b": "x"}

    def test_bytes(self, row):
        assert row.blob == b"\x00\x01binary"

    def test_null_stays_none(self, row):
        assert row.note is None

    def test_float(self, row):
        assert row.ratio == 1.5


async def test_scalar_selects_convert_too(engine):
    """A column selected on its own is planned as a scalar, not part of a model,
    so it takes a different path through the codegen and must still convert."""
    when = await engine.fetch_value(sa.select(Wide.when).where(Wide.id == 1))
    assert when == WIDE_ROW["when"]

    colour = await engine.fetch_value(sa.select(Wide.colour).where(Wide.id == 1))
    assert colour is Colour.RED


async def test_a_nullable_column_still_converts_when_present(engine):
    """The converter table this replaced was keyed by exact Python type, so
    `bool | None` never matched `bool` and a nullable column silently skipped its
    conversion. Processors come from the column, which has no such problem."""
    await engine.execute(
        sa.update(Wide.__table__).where(Wide.id == 1).values(note="present")
    )
    row = (await engine.fetch_all(sa.select(Wide).where(Wide.id == 1)))[0]
    assert row.note == "present"


async def test_writes_round_trip_through_bind_processors(engine):
    """Values are encoded on the way in by the same processors that decode them
    on the way out — sqlite cannot bind a Decimal, UUID or dict at all."""
    second = dict(WIDE_ROW, id=2, when=dt.datetime(1999, 12, 31, 23, 59, 58, 7))
    await engine.execute_many(sa.insert(Wide.__table__), [second])

    row = (await engine.fetch_all(sa.select(Wide).where(Wide.id == 2)))[0]
    assert row.when == second["when"]
    assert row.amount == second["amount"]
    assert row.uid == second["uid"]


async def test_the_ddl_and_the_data_agree(sqlite_engine):
    """Reflection reads back the table `create_all` wrote, which is the same
    declaration the hydrator was planned from."""
    reflected = sa.MetaData()
    engine = sa.create_engine(f"sqlite:///{sqlite_engine.path}")
    try:
        with engine.connect() as conn:
            reflected.reflect(bind=conn)
    finally:
        engine.dispose()

    assert set(reflected.tables) == set(Base.metadata.tables)
    assert reflected.tables["t_wide"].c.keys() == list(Wide.__column_order__)
