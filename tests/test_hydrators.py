"""Code generation: what the compilers emit and how it behaves."""

from typing import ClassVar

import pytest

from rowform import (
    SQLITE_CONVERTERS,
    Query,
    as_dict,
    compile_batch_hydrator,
    compile_hydrator,
    compile_join_hydrator,
    hydrate,
)
from tests.conftest import Author, Book


class TestSingleModelHydrators:
    def test_per_row_hydrator(self):
        obj = compile_hydrator(Author)((1, "ada", True))
        assert (obj.id, obj.name, obj.active) == (1, "ada", True)

    def test_batch_hydrator(self):
        objs = compile_batch_hydrator(Author)([(1, "ada", True), (2, "bo", False)])
        assert [(o.id, o.name) for o in objs] == [(1, "ada"), (2, "bo")]

    def test_converters_are_applied(self):
        # sqlite hands back 0/1 for booleans; without the converter that leaks
        # into JSON as an int, which is the bug METHODOLOGY correction 1 records.
        raw = compile_batch_hydrator(Author)([(1, "ada", 1)])[0]
        converted = compile_batch_hydrator(Author, SQLITE_CONVERTERS)([(1, "ada", 1)])[0]
        assert raw.active == 1 and raw.active is not True
        assert converted.active is True

    def test_generated_source_is_attached_for_inspection(self):
        source = compile_batch_hydrator(Author).__source__
        assert source.startswith("def _hydrate_all(rows):")
        assert "_new(_cls)" in source

    def test_instances_have_a_dict_by_default(self):
        obj = compile_hydrator(Author)((1, "ada", True))
        assert obj.__dict__ == {"id": 1, "name": "ada", "active": True}
        obj.surprise = 1
        assert obj.surprise == 1

    def test_slotted_model_instances_have_no_dict(self):
        from rowform import Column, model

        @model(slots=True)
        class SlottedAuthor:
            __tablename__ = "slotted_author"
            id: Column[int] = Column(int)

        obj = compile_hydrator(SlottedAuthor)((1,))
        assert not hasattr(obj, "__dict__")
        with pytest.raises(AttributeError):
            obj.surprise = 1

    def test_a_single_column_model_is_not_nested(self):
        """Same trailing-comma trap as the join hydrator: a one-column model used
        to hydrate its only field from the whole row tuple."""
        from rowform import Column, model

        @model
        class Single:
            __tablename__ = "single"
            id: Column[int] = Column(int)

        objs = compile_batch_hydrator(Single)([(1,), (2,)])
        assert [o.id for o in objs] == [1, 2]

    def test_a_model_with_no_columns_is_refused(self):
        class Empty:
            __tablename__ = "empty"
            __columns__: ClassVar = {}

        with pytest.raises(ValueError, match="declares no columns"):
            compile_batch_hydrator(Empty)

    def test_per_row_hydrator_applies_converters_too(self):
        # test_converters_are_applied above only exercises the batch hydrator;
        # the singular one builds its converter call the same way but from a
        # separate code path (compile_hydrator vs compile_batch_hydrator).
        raw = compile_hydrator(Author)((1, "ada", 1))
        converted = compile_hydrator(Author, SQLITE_CONVERTERS)((1, "ada", 1))
        assert raw.active == 1 and raw.active is not True
        assert converted.active is True

    def test_setting_a_column_through_the_descriptor(self):
        # hydrate()/the compiled hydrators write the storage slot directly and
        # never go through Column.__set__; this is the only path that does.
        obj = compile_hydrator(Author)((1, "ada", True))
        obj.name = "beatrice"
        assert obj.name == "beatrice"


class TestReflectiveHydrate:
    def test_round_trip(self):
        obj = hydrate(Author, (1, "ada", True))
        assert as_dict(obj) == {"id": 1, "name": "ada", "active": True}

    @pytest.mark.parametrize("row", [(1, "ada"), (1, "ada", True, "extra")])
    def test_wrong_width_rows_are_rejected(self, row):
        # zip() would silently truncate or leave slots unset, producing an object
        # that only fails later at read time.
        with pytest.raises(ValueError, match="but the row has"):
            hydrate(Author, row)

    def test_as_dict_refuses_a_non_model(self):
        with pytest.raises(TypeError, match="Cannot serialize"):
            as_dict(object())


class TestAsDictAsOrjsonHook:
    """`as_dict` is the one generic `default=` hook, useful for heterogeneous
    payloads mixing several model types or for `@model(slots=True)` instances
    (the slots=False default needs no hook at all -- see test_model.py)."""

    def test_dispatches_across_model_types(self):
        author = hydrate(Author, (1, "ada", True))
        book = hydrate(Book, (10, 1, "structures"))
        assert as_dict(author)["name"] == "ada"
        assert as_dict(book)["title"] == "structures"


class TestJoinHydrator:
    """A joined row arrives as one flat tuple; the hydrator slices it positionally."""

    def test_two_models_produce_a_tuple_each(self):
        spec = [("model", Author, False), ("model", Book, False)]
        rows = [(1, "ada", True, 10, 1, "structures")]
        (author, book), = compile_join_hydrator(spec)(rows)
        assert (author.id, author.name) == (1, "ada")
        assert (book.id, book.title) == (10, "structures")

    def test_model_plus_scalar_column(self):
        spec = [("model", Author, False), ("column", str)]
        (author, title), = compile_join_hydrator(spec)([(1, "ada", True, "structures")])
        assert author.name == "ada"
        assert title == "structures"

    def test_converters_apply_per_entity(self):
        spec = [("model", Author, False), ("model", Book, False)]
        (author, _), = compile_join_hydrator(spec, SQLITE_CONVERTERS)(
            [(1, "ada", 1, 10, 1, "structures")]
        )
        assert author.active is True

    def test_outer_join_all_null_entity_becomes_none(self):
        spec = [("model", Author, False), ("model", Book, True)]
        (author, book), = compile_join_hydrator(spec)([(4, "dan", True, None, None, None)])
        assert author.name == "dan"
        assert book is None

    def test_outer_join_partial_null_still_builds_the_object(self):
        # Only an entirely NULL entity means "no match"; a real row with some
        # NULL columns must still hydrate.
        spec = [("model", Author, False), ("model", Book, True)]
        (_, book), = compile_join_hydrator(spec)([(1, "ada", True, 10, None, None)])
        assert book is not None and book.id == 10

    def test_inner_join_entity_is_not_null_checked(self):
        source = compile_join_hydrator(
            [("model", Author, False), ("model", Book, False)]
        ).__source__
        assert "is None" not in source

    def test_outer_join_entity_is_null_checked_on_every_column(self):
        source = compile_join_hydrator(
            [("model", Author, False), ("model", Book, True)]
        ).__source__
        assert "if f3 is None and f4 is None and f5 is None:" in source

    def test_three_entities(self):
        from tests.conftest import Tag

        spec = [("model", Author, False), ("model", Book, False), ("model", Tag, False)]
        rows = [(1, "ada", True, 10, 1, "structures", 100, 10, "classic")]
        (author, book, tag), = compile_join_hydrator(spec)(rows)
        assert (author.id, book.id, tag.label) == (1, 10, "classic")

    def test_row_width_must_match_the_spec(self):
        spec = [("model", Author, False), ("model", Book, False)]
        with pytest.raises(ValueError):  # unpack error from the for statement
            compile_join_hydrator(spec)([(1, "ada", True)])

    def test_a_single_selected_column_is_not_nested(self):
        """`for f0 in rows` binds each row *tuple* to f0 instead of unpacking it,
        so a one-column select used to come back as [((1,),), ((2,),)]. The
        generated loop carries a trailing comma to force a tuple pattern."""
        rows = compile_join_hydrator([("column", int)])([(1,), (2,)])
        assert rows == [(1,), (2,)]

    def test_wrap_false_returns_the_entity_directly(self):
        # A one-model query yields instances even when nullable, so the hydrator
        # must be able to skip the tuple.
        spec = [("model", Author, True)]
        rows = compile_join_hydrator(spec, wrap=False)([(1, "ada", True), (None, None, None)])
        assert rows[0].name == "ada"
        assert rows[1] is None

    def test_wrap_false_is_ignored_for_multiple_entities(self):
        spec = [("model", Author, False), ("column", str)]
        rows = compile_join_hydrator(spec, wrap=False)([(1, "ada", True, "x")])
        assert isinstance(rows[0], tuple) and len(rows[0]) == 2

    def test_empty_entity_list_is_refused(self):
        with pytest.raises(ValueError, match="at least one entity"):
            compile_join_hydrator([])

    def test_a_model_entity_with_no_columns_is_refused(self):
        class Empty:
            __tablename__ = "empty"
            __columns__: ClassVar = {}

        with pytest.raises(ValueError, match="declares no columns"):
            compile_join_hydrator([("model", Empty, False)])

    def test_source_is_attached(self):
        source = compile_join_hydrator([("model", Author, False)]).__source__
        assert source.startswith("def _hydrate_join(rows):")

    def test_spec_matches_the_select_list_width(self):
        """The hydrator unpacks positionally, so its width must equal the number
        of selected columns — this is the invariant that keeps the two in step."""
        query = (Query(Author, Book.title)
                 .join(Book, Book.author_id == Author.id))
        select_list = query.to_sql()[0].split(" FROM ")[0].removeprefix("SELECT ")
        columns = len(select_list.split(", "))
        source = compile_join_hydrator(query.hydration_spec()).__source__
        unpacked = source.split("for ")[1].split(" in rows")[0]
        assert len(unpacked.split(", ")) == columns
