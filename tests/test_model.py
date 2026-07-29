"""The `@model` decorator: a real slotted dataclass whose class-scope `Column`
attributes still return query expressions (`User.id > 100`), with no metaclass
anywhere in the class's own MRO."""

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

    def test_instances_are_fully_slotted(self):
        # A stray __dict__ isn't just a memory regression here -- it's what used
        # to make orjson's native serializer silently emit {} (see
        # TestOrjsonSerialization below). This guards against that regressing.
        author = Author(id=1, name="ada", active=True)
        with pytest.raises(AttributeError):
            author.__dict__  # noqa: B018


class TestPublicNamesShowThroughDataclassMachinery:
    """The storage dataclass declares shadow-named fields (`_rf_id`); the model
    re-keys __repr__/__dataclass_fields__ to the public names so introspection
    doesn't leak the storage detail."""

    def test_repr_uses_public_names(self):
        author = Author(id=1, name="ada", active=True)
        assert repr(author) == "Author(id=1, name='ada', active=True)"

    def test_fields_reports_public_names(self):
        assert [f.name for f in dataclasses.fields(Author)] == ["id", "name", "active"]

    def test_asdict_uses_public_names(self):
        author = Author(id=1, name="ada", active=True)
        assert dataclasses.asdict(author) == {"id": 1, "name": "ada", "active": True}


class TestOrjsonSerialization:
    """Regression test for a real bug: the rebuilt model class must declare its
    own `__slots__ = ()`, or it silently gets an empty per-instance `__dict__`
    alongside the inherited slots. That's what made orjson's native serializer
    take its (wrong, for us) `__dict__`-reading fast path instead of its
    `__dataclass_fields__` fallback (which does `getattr()` per field and
    reaches `Column` correctly), producing `b'{}'` instead of raising or
    serializing correctly."""

    def test_bare_orjson_dumps_serializes_correctly(self):
        orjson = pytest.importorskip("orjson")
        author = Author(id=1, name="ada", active=True)
        assert orjson.dumps(author) == b'{"id":1,"name":"ada","active":true}'

    def test_instance_has_no_own_dict(self):
        author = Author(id=1, name="ada", active=True)
        assert not hasattr(author, "__dict__")


class TestCompiledHydratorCompatibility:
    """model.py's storage is an ordinary slotted class, so compile.py's
    object.__new__ + direct slot assignment codegen needs no changes to target
    it -- this is what proves that."""

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
