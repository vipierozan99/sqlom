"""The `@model` decorator: a real dataclass (non-slotted by default, opt into
`slots=True`) whose class-scope `Column` attributes still return query
expressions (`User.id > 100`), via a metaclass on the built class."""

import dataclasses

import pytest

from rowform import Column, ColumnExpr, compile_batch_hydrator, compile_hydrator, model
from tests.conftest import Author


class TestValidation:
    def test_a_class_with_no_columns_is_refused(self):
        with pytest.raises(ValueError, match="declares no columns"):

            @model
            class Empty:
                __tablename__ = "empty"


class TestDescriptorAccess:
    def test_class_level_access_returns_a_column_expression(self):
        assert isinstance(Author.id, ColumnExpr)

    def test_instance_level_access_returns_the_value(self):
        author = Author(id=1, name="ada", active=True)
        assert author.id == 1
        assert author.name == "ada"

    def test_class_level_access_is_unaffected_by_instances(self):
        Author(id=1, name="ada", active=True)
        assert isinstance(Author.id, ColumnExpr)


class TestConstruction:
    def test_keyword_construction(self):
        author = Author(id=1, name="ada", active=True)
        assert (author.id, author.name, author.active) == (1, "ada", True)

    def test_positional_construction(self):
        author = Author(1, "ada", True)
        assert (author.id, author.name, author.active) == (1, "ada", True)

    def test_instances_have_a_dict_by_default(self):
        # @model defaults to slots=False: instances get a normal __dict__ and
        # arbitrary attributes can be set, same as a plain @dataclass.
        author = Author(id=1, name="ada", active=True)
        assert author.__dict__ == {"id": 1, "name": "ada", "active": True}
        author.surprise = 1
        assert author.surprise == 1


class TestPublicNamesShowThroughDataclassMachinery:
    """Fields are real, public-named dataclass fields (no shadow naming), so
    `__repr__`/`__dataclass_fields__`/`asdict` all work unmodified."""

    def test_repr_uses_public_names(self):
        author = Author(id=1, name="ada", active=True)
        assert repr(author) == "Author(id=1, name='ada', active=True)"

    def test_fields_reports_public_names(self):
        assert [f.name for f in dataclasses.fields(Author)] == ["id", "name", "active"]

    def test_asdict_uses_public_names(self):
        author = Author(id=1, name="ada", active=True)
        assert dataclasses.asdict(author) == {"id": 1, "name": "ada", "active": True}


class TestOrjsonSerialization:
    """@model defaults to slots=False, which is orjson's native *fast* path
    (it reads `__dict__` directly) -- no `default=` hook or option needed.
    See docs/FINDINGS.md#the-orjson-dataclass-trap for why `slots=True` is
    much slower to serialize instead."""

    def test_bare_orjson_dumps_serializes_correctly(self):
        orjson = pytest.importorskip("orjson")
        author = Author(id=1, name="ada", active=True)
        assert orjson.dumps(author) == b'{"id":1,"name":"ada","active":true}'


class TestSlotsOptIn:
    """`@model(slots=True)` is still available for callers who want smaller
    instances and are willing to pay for it -- both in memory (no `__dict__`,
    real slot descriptors) and in orjson serialization (native path has no
    fast route for slotted dataclasses; see docs/FINDINGS.md#the-orjson-dataclass-trap)."""

    def test_slotted_instances_have_no_dict(self):
        @model(slots=True)
        class SlottedUser:
            __tablename__ = "slotted_user"
            id: Column[int] = Column(int)

        user = SlottedUser(id=1)
        assert not hasattr(user, "__dict__")
        with pytest.raises(AttributeError):
            user.surprise = 1

    def test_slotted_instances_still_serialize_correctly(self):
        orjson = pytest.importorskip("orjson")

        @model(slots=True)
        class SlottedUser:
            __tablename__ = "slotted_user"
            id: Column[int] = Column(int)

        user = SlottedUser(id=1)
        assert orjson.dumps(user) == b'{"id":1}'


class TestCompiledHydratorCompatibility:
    """model.py's storage is an ordinary dataclass (slotted or not), so
    compile.py's object.__new__ + direct attribute assignment codegen needs no
    changes to target it -- this is what proves that."""

    def test_compile_hydrator(self):
        obj = compile_hydrator(Author)((1, "ada", True))
        assert (obj.id, obj.name, obj.active) == (1, "ada", True)

    def test_compile_batch_hydrator(self):
        objs = compile_batch_hydrator(Author)([(1, "ada", True), (2, "bo", False)])
        assert [(o.id, o.name, o.active) for o in objs] == [(1, "ada", True), (2, "bo", False)]


class TestUserDefinedMethodsAreNotClobbered:
    def test_a_user_defined_repr_is_kept(self):
        @model
        class Custom:
            __tablename__ = "custom"
            id: Column[int] = Column(int)

            def __repr__(self):
                return "CUSTOM"

        assert repr(Custom(id=1)) == "CUSTOM"

    def test_a_user_defined_init_is_kept(self):
        @model
        class Custom:
            __tablename__ = "custom"
            id: Column[int] = Column(int)

            def __init__(self, id):
                self.id = id * 2

        assert Custom(5).id == 10
