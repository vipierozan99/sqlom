"""The `@model` decorator: real stdlib dataclasses that still support
`User.id > 100`, via a data descriptor on a per-model metaclass."""

import pytest

from rowform import compile_json_default, model
from rowform.dataclass_model import DATACLASS_DUMP_OPTION, ColumnDescriptor
from tests.conftest import AuthorDC


class TestValidation:
    def test_a_class_with_no_annotations_is_refused(self):
        with pytest.raises(ValueError, match="declares no annotated columns"):
            @model
            class Empty:
                __tablename__ = "empty"

    def test_assigning_a_column_on_the_class_is_refused(self):
        # AuthorDC.id is a query expression; setting it would silently replace
        # that with a plain value instead of raising, so __set__ refuses.
        with pytest.raises(AttributeError, match="would replace the query expression"):
            AuthorDC.id = 5


class TestDescriptorAccess:
    def test_reached_off_the_metaclass_itself_returns_the_descriptor(self):
        # `AuthorDC.id` looks up `id` on an *instance* of AuthorDC's metaclass
        # (AuthorDC itself), so __get__ sees obj=AuthorDC and returns a
        # ColumnExpr. Reaching the descriptor off the metaclass directly —
        # `type(AuthorDC).id` — is attribute access on the class that defines
        # it, so __get__ sees obj=None and must return itself rather than
        # recursing or raising.
        descriptor = type(AuthorDC).id
        assert isinstance(descriptor, ColumnDescriptor)
        assert descriptor.name == "id"


class TestOrjsonSerialization:
    """orjson recognizes stdlib dataclasses natively and will *ignore* a
    `default=` hook for them unless told otherwise — see dataclass_model.py's
    module docstring. `DATACLASS_DUMP_OPTION` is what routes serialization of
    a `@model` instance back through `compile_json_default`; nothing else
    exercises that wiring end to end."""

    def test_passthrough_option_round_trips_through_the_compiled_hook(self):
        orjson = pytest.importorskip("orjson")
        author = AuthorDC(id=1, name="ada", active=True)
        payload = orjson.dumps(
            author, option=DATACLASS_DUMP_OPTION, default=compile_json_default(AuthorDC)
        )
        assert orjson.loads(payload) == {"id": 1, "name": "ada", "active": True}

    def test_option_is_orjsons_passthrough_flag(self):
        orjson = pytest.importorskip("orjson")
        assert DATACLASS_DUMP_OPTION == orjson.OPT_PASSTHROUGH_DATACLASS
