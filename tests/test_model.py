"""The declaration layer: one class, three jobs (table, expressions, storage)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import uuid
from abc import ABC

import pytest
import sqlalchemy as sa
from conftest import Author, Base, Book, Colour, Wide
from sqlalchemy.orm import Mapped

import rowform
from rowform import mapped_column


def make_base():
    """A throwaway Base, so a test that declares tables cannot collide with
    another test's names in a shared MetaData."""

    class Scratch(rowform.Base):
        metadata = sa.MetaData()

    return Scratch


class TestTableConstruction:
    def test_the_model_is_the_table(self):
        assert isinstance(Author.__table__, sa.Table)
        assert Author.__table__.name == "t_authors"
        assert Author.__table__ in Base.metadata.sorted_tables

    def test_class_access_yields_the_column(self):
        assert Author.id is Author.__table__.c.id
        assert isinstance(Author.id, sa.Column)

    def test_instance_access_yields_the_value(self):
        author = Author(id=1, name="ada", active=True)
        assert author.id == 1
        assert author.name == "ada"

    def test_class_access_is_unaffected_by_instances(self):
        Author(id=1, name="ada", active=True)
        assert Author.id is Author.__table__.c.id

    def test_columns_carry_the_annotation_derived_type(self):
        types = {c.name: type(c.type) for c in Wide.__table__.columns}
        assert types["id"] is sa.Integer
        assert types["text"] is sa.String
        assert types["flag"] is sa.Boolean
        assert types["when"] is sa.DateTime
        assert types["day"] is sa.Date
        assert types["clock"] is sa.Time
        assert types["ratio"] is sa.Float
        assert types["uid"] is sa.Uuid
        assert types["payload"] is sa.JSON
        assert types["blob"] is sa.LargeBinary

    def test_optional_annotation_makes_the_column_nullable(self):
        assert Wide.__table__.c.note.nullable is True
        assert Wide.__table__.c.text.nullable is False

    def test_primary_key_is_never_nullable(self):
        assert Wide.__table__.c.id.primary_key is True
        assert Wide.__table__.c.id.nullable is False

    def test_enum_becomes_a_sa_enum_over_the_same_class(self):
        assert isinstance(Wide.__table__.c.colour.type, sa.Enum)
        assert Wide.__table__.c.colour.type.enum_class is Colour

    def test_explicit_type_overrides_the_annotation(self):
        assert Wide.__table__.c.amount.type.precision == 12
        assert Wide.__table__.c.amount.type.scale == 3

    def test_foreign_keys_pass_through(self):
        fks = list(Book.__table__.c.author_id.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "t_authors.id"

    def test_a_leading_string_renames_the_column(self):
        Scratch = make_base()

        class Renamed(Scratch):
            __tablename__ = "renamed"
            id: Mapped[int] = mapped_column(primary_key=True)
            title: Mapped[str] = mapped_column("headline", index=True)

        assert Renamed.__table__.c.keys() == ["id", "headline"]
        assert Renamed.title.name == "headline"
        # The attribute keeps the Python name; only the DB column is renamed.
        assert Renamed(id=1, title="t").title == "t"

    def test_table_args_pass_through(self):
        Scratch = make_base()

        class Constrained(Scratch):
            __tablename__ = "constrained"
            __table_args__ = (sa.UniqueConstraint("a", "b"),)
            id: Mapped[int] = mapped_column(primary_key=True)
            a: Mapped[int]
            b: Mapped[int]

        uniques = [
            c for c in Constrained.__table__.constraints if isinstance(c, sa.UniqueConstraint)
        ]
        assert len(uniques) == 1

    def test_type_annotation_map_extends_the_defaults(self):
        class Scratch(rowform.Base):
            metadata = sa.MetaData()
            type_annotation_map = {str: sa.Text()}

        class Doc(Scratch):
            __tablename__ = "doc"
            id: Mapped[int] = mapped_column(primary_key=True)
            body: Mapped[str]

        assert isinstance(Doc.__table__.c.body.type, sa.Text)

    def test_an_unmapped_python_type_is_refused_with_a_way_out(self):
        Scratch = make_base()

        with pytest.raises(TypeError, match="no SQLAlchemy type registered"):

            class Odd(Scratch):
                __tablename__ = "odd"
                id: Mapped[int] = mapped_column(primary_key=True)
                thing: Mapped[complex]


class TestExpressions:
    def test_comparison_builds_real_sql(self):
        clause = Author.id > 100
        assert isinstance(clause, sa.sql.elements.BinaryExpression)
        assert "t_authors.id > " in str(clause)

    def test_select_treats_the_class_as_its_table(self):
        compiled = str(sa.select(Author))
        assert compiled.startswith("SELECT t_authors.id, t_authors.name, t_authors.active")

    def test_join_and_select_from_accept_the_class(self):
        compiled = str(sa.select(Author, Book).join(Book))
        assert "FROM t_authors JOIN t_books ON t_authors.id = t_books.author_id" in compiled
        assert "FROM t_authors" in str(sa.select(sa.func.count()).select_from(Author))

    def test_an_abstract_model_has_no_table_to_select_from(self):
        Scratch = make_base()

        class Mixin(Scratch):
            created: Mapped[int]

        with pytest.raises(TypeError, match="abstract"):
            sa.select(Mixin)


class TestInstances:
    def test_no_orm_instance_state(self):
        author = Author(id=1, name="ada", active=True)
        assert not hasattr(author, "_sa_instance_state")

    def test_is_a_real_dataclass(self):
        assert dataclasses.is_dataclass(Author)
        assert [f.name for f in dataclasses.fields(Author)] == ["id", "name", "active"]

    def test_repr_and_eq_come_from_dataclasses(self):
        one = Author(id=1, name="ada", active=True)
        two = Author(id=1, name="ada", active=True)
        assert one == two
        assert repr(one) == "Author(id=1, name='ada', active=True)"

    def test_positional_construction(self):
        assert Author(1, "ada", True).name == "ada"

    def test_instances_have_a_dict_so_orjson_can_read_them(self):
        # docs/FINDINGS.md#the-orjson-dataclass-trap: a slotted model forces
        # orjson onto a much slower fallback, so non-slotted is the default.
        assert Author(id=1, name="ada", active=True).__dict__ == {
            "id": 1,
            "name": "ada",
            "active": True,
        }

    def test_class_keywords_reach_dataclasses(self):
        Scratch = make_base()

        class Frozen(Scratch, frozen=True):
            __tablename__ = "frozen"
            id: Mapped[int] = mapped_column(primary_key=True)

        with pytest.raises(dataclasses.FrozenInstanceError):
            Frozen(id=1).id = 2

    def test_defaults_make_a_field_optional(self):
        Scratch = make_base()

        class Defaulted(Scratch):
            __tablename__ = "defaulted"
            id: Mapped[int] = mapped_column(primary_key=True)
            active: Mapped[bool] = mapped_column(default=True)

        assert Defaulted(id=1).active is True
        assert Defaulted.__table__.c.active.default.arg is True

    def test_init_false_keeps_a_field_out_of_the_constructor(self):
        Scratch = make_base()

        class Generated(Scratch):
            __tablename__ = "generated"
            id: Mapped[int] = mapped_column(primary_key=True, init=False)
            name: Mapped[str]

        instance = Generated(name="x")
        assert not hasattr(instance, "id")


class TestMixinsAndOrder:
    def test_mixin_fields_are_inherited_as_real_columns(self):
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[dt.datetime]

        class Note(Timestamped):
            __tablename__ = "note"
            id: Mapped[int] = mapped_column(primary_key=True)
            body: Mapped[str]

        assert Note.__table__.c.keys() == ["created", "id", "body"]
        assert [f.name for f in dataclasses.fields(Note)] == ["created", "id", "body"]

    def test_a_mixin_builds_no_table_of_its_own(self):
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[dt.datetime]

        assert "created" in Timestamped.__columns__
        assert not hasattr(Timestamped, "__table__")
        assert Scratch.metadata.tables == {}

    def test_each_model_gets_its_own_column_objects(self):
        """A Column belongs to exactly one Table, so an inherited declaration
        cannot reuse its base's."""
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[dt.datetime]

        class A(Timestamped):
            __tablename__ = "a"
            id: Mapped[int] = mapped_column(primary_key=True)

        class B(Timestamped):
            __tablename__ = "b"
            id: Mapped[int] = mapped_column(primary_key=True)

        assert A.created is not B.created
        assert A.created.table is A.__table__

    def test_inherited_fields_come_first_and_are_recorded(self):
        """R11: adding a mixin moves its columns to the front of CREATE TABLE,
        and Alembic autogenerate does not diff column order — so the order is at
        least deterministic and readable off the class."""
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[dt.datetime]

        class Note(Timestamped):
            __tablename__ = "note"
            id: Mapped[int] = mapped_column(primary_key=True)

        assert Note.__column_order__ == ("created", "id")

    def test_column_order_can_be_pinned(self):
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[dt.datetime]

        class Note(Timestamped):
            __tablename__ = "note"
            __column_order__ = ("id", "created")
            id: Mapped[int] = mapped_column(primary_key=True)

        assert Note.__table__.c.keys() == ["id", "created"]
        # The pin governs the *column* order — CREATE TABLE, and what a plain
        # `select(Note)` returns. The constructor's parameter order stays
        # stdlib dataclass semantics, where inherited fields always come first.
        assert [f.name for f in dataclasses.fields(Note)] == ["created", "id"]

    def test_an_incomplete_pin_is_refused(self):
        Scratch = make_base()

        with pytest.raises(TypeError, match=r"every Mapped\[\] field exactly once"):

            class Note(Scratch):
                __tablename__ = "note"
                __column_order__ = ("id",)
                id: Mapped[int] = mapped_column(primary_key=True)
                body: Mapped[str]

    def test_a_reordering_default_explains_itself(self):
        """The stdlib message names two fields in an order the author never
        wrote, because the reordering is inherited rather than declared."""
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[int] = mapped_column(default=0)

        with pytest.raises(TypeError, match="inherited from a base or mixin"):

            class Note(Timestamped):
                __tablename__ = "note"
                id: Mapped[int] = mapped_column(primary_key=True)

    def test_kw_only_is_the_documented_way_out(self):
        Scratch = make_base()

        class Timestamped(Scratch):
            created: Mapped[int] = mapped_column(default=0)

        class Note(Timestamped, kw_only=True):
            __tablename__ = "note"
            id: Mapped[int] = mapped_column(primary_key=True)

        assert Note(id=1).created == 0


class TestGuards:
    def test_a_reserved_name_is_refused(self):
        Scratch = make_base()

        with pytest.raises(TypeError, match="collides with a reserved name"):

            class Bad(Scratch):
                __tablename__ = "bad"
                metadata: Mapped[str]

    def test_a_multi_type_union_is_refused(self):
        Scratch = make_base()

        with pytest.raises(TypeError, match="union of more than one non-None type"):

            class Bad(Scratch):
                __tablename__ = "bad"
                id: Mapped[int] = mapped_column(primary_key=True)
                thing: Mapped[int | str]

    def test_metaclass_conflict_is_real_and_documented(self):
        """R10: every model carries ModelMeta, so combining with ABC or Protocol
        raises. Accepted rather than worked around — a decorator would compose
        freely, but the decorator route erases field types (§5b)."""
        Scratch = make_base()

        with pytest.raises(TypeError, match="metaclass conflict"):

            class Bad(Scratch, ABC):
                __tablename__ = "bad"
                id: Mapped[int] = mapped_column(primary_key=True)


class TestModelLookup:
    def test_a_table_knows_the_model_it_was_built_from(self):
        assert rowform.model_for(Author.__table__) is Author

    def test_an_alias_resolves_through_to_the_model(self):
        assert rowform.model_for(sa.alias(Author.__table__, "a2")) is Author

    def test_a_foreign_table_has_no_model(self):
        assert rowform.model_for(sa.table("elsewhere", sa.column("id"))) is None


class TestDdl:
    def test_create_table_renders_from_the_declaration(self):
        ddl = str(sa.schema.CreateTable(Wide.__table__))
        assert "CREATE TABLE t_wide" in ddl
        assert "PRIMARY KEY (id)" in ddl
        assert "note VARCHAR" in ddl

    def test_sqlite_renders_the_types_it_has_no_native_form_for(self):
        ddl = str(
            sa.schema.CreateTable(Wide.__table__).compile(
                dialect=rowform.SqliteEngine("x").dialect
            )
        )
        assert "CHAR(32)" in ddl  # sqlite has no native uuid
        assert "JSON" in ddl


def test_default_type_map_covers_the_types_the_wide_shape_uses():
    assert isinstance(rowform.DEFAULT_TYPE_MAP[uuid.UUID], sa.Uuid)
    assert isinstance(rowform.DEFAULT_TYPE_MAP[decimal.Decimal], sa.Numeric)
    assert isinstance(rowform.DEFAULT_TYPE_MAP[dt.datetime], sa.DateTime)


def test_enum_members_survive_declaration():
    assert list(Colour) == [Colour.RED, Colour.BLUE]
    assert issubclass(Colour, enum.Enum)
