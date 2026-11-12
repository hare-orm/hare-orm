"""Bounded native hydration keeps the Python callback, descriptor and error ordering contracts."""

from types import SimpleNamespace

import pytest

from hare.dialects.base.results.statement_result import StatementResult
from hare.models.instances.dirty_fields import DirtyFields
from hare.native.native_modules import NativeModules
from hare.query.rows.model_rows.model_columns import ModelColumns
from hare.query.rows.model_rows.model_rows import ModelRows

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("by_position", [False, True])
def test_batch_keeps_row_order_null_callbacks_and_custom_construction(monkeypatch, fail, by_position):
    calls = []

    class Loaded:
        _meta = SimpleNamespace(default_custom_generated_pk=True, track_dirty_fields=True)

        def __new__(cls):
            calls.append("new")
            return object.__new__(cls)

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, value):
            calls.append(("set", value))
            self._value = value

    def decode(value):
        calls.append(("decode", value))
        if fail and value == "bad":
            raise ValueError("bad value")
        return value

    def snapshot(obj):
        calls.append(("snapshot", obj.value))

    monkeypatch.setattr(DirtyFields, "snapshot_dirty_fields", snapshot)
    keys = (0, 1, 2) if by_position else ("base", "child", "note")
    columns = [
        ModelColumns(Loaded, (("value", None, decode),), (keys[0],), True),
        ModelColumns(Loaded, (("value", None, decode),), (keys[1],), False, ("child",), True),
    ]
    raw = [("one", None, None), ("bad" if fail else "two", "child", "note"), ("last", None, "end")]
    rows = raw if by_position else [dict(zip(("base", "child", "note"), row, strict=True)) for row in raw]
    result = StatementResult(3, rows, description=(("base",), ("child",), ("note",)))
    connection = SimpleNamespace(
        connection_alias="selected",
        features=SimpleNamespace(supports_positional_rows=by_position),
        dialect=SimpleNamespace(types=SimpleNamespace(get_python_reader=lambda field: None)),
    )
    reader = ModelRows(Loaded, connection.dialect.types, annotations=("note",))
    reader.annotation_decoders = {"note": decode}
    layout = reader.get_result_layout(columns, reader.get_annotation_layout(connection, result.column_names))
    if fail:
        with pytest.raises(ValueError, match="bad value"):
            reader.read_batch(connection, result, layout, start=0, stop=3)
        assert calls == [
            "new",
            ("decode", "one"),
            ("set", "one"),
            ("snapshot", "one"),
            ("decode", None),
            "new",
            ("decode", "bad"),
        ]
    else:
        loaded = reader.read_batch(connection, result, layout, start=0, stop=2)
        assert [obj.value for obj in loaded] == ["one", "two"]
        assert loaded[0]._child is None
        assert loaded[1]._child.value == "child"
        assert [obj.note for obj in loaded] == [None, "note"]
        assert all(obj._partial and obj._saved_in_db and obj._custom_generated_pk for obj in loaded)
        assert all(obj._connection_alias == "selected" for obj in loaded)
        assert calls == [
            "new",
            ("decode", "one"),
            ("set", "one"),
            ("snapshot", "one"),
            ("decode", None),
            "new",
            ("decode", "two"),
            ("set", "two"),
            ("snapshot", "two"),
            "new",
            ("decode", "child"),
            ("set", "child"),
            ("snapshot", "child"),
            ("decode", "note"),
        ]


def test_model_batch_observes_a_replaced_constructor_on_the_next_row():
    calls = []

    class Loaded:
        _meta = SimpleNamespace(default_custom_generated_pk=False, track_dirty_fields=False)

    def replacement(model):
        calls.append("replacement")
        return object.__new__(model)

    def decode(value):
        Loaded.__new__ = staticmethod(replacement)
        return value

    columns = ModelColumns(Loaded, (("value", None, decode),), (0,), False)
    loaded = NativeModules.rows.ModelResultLayout([columns], ()).read_batch([(1,), (2,)], 0, 2, None, lambda obj: None)
    assert [obj.value for obj in loaded] == [1, 2]
    assert calls == ["replacement"]


def test_native_serialization_keeps_object_column_order_and_first_error():
    first, second = SimpleNamespace(value=None, other=2), SimpleNamespace(value="bad", other=4)
    calls = []

    def writer(value, obj):
        calls.append((value, obj))
        if value == "bad":
            raise ValueError("bad value")
        return value

    with pytest.raises(ValueError, match="bad value"):
        NativeModules.rows.serialize_instances([first, second], (("value", writer), ("other", writer)))
    assert calls == [(None, first), (2, first), ("bad", second)]


def test_native_relation_grouping_keeps_duplicates_nulls_and_last_single_row():
    first, second, missing = SimpleNamespace(key=1), SimpleNamespace(key=1), SimpleNamespace(key=None)
    assert NativeModules.rows.group_by_attribute([first, second, missing], "key", True) == {
        1: [first, second],
        None: [missing],
    }
    grouped = NativeModules.rows.group_by_attribute([first, second, missing], "key", False)
    assert grouped[1] is second and grouped[None] is missing


@pytest.mark.parametrize("by_position", [False, True])
def test_combined_union_rows_hydrate_each_model_and_annotations_natively(by_position):
    layout = SimpleNamespace(
        entry_by_column={"value": ("value", None, lambda raw: f"decoded:{raw}")},
        entry_by_field_name={},
        full_column_count=1,
    )

    class FirstBranch:
        # Model's class default.
        _custom_generated_pk = False
        _meta = SimpleNamespace(
            app="models",
            default_custom_generated_pk=False,
            track_dirty_fields=False,
            get_hydration_layout=lambda _connection: layout,
        )

    class SecondBranch:
        # Model's class default.
        _custom_generated_pk = False
        _meta = SimpleNamespace(
            app="models",
            default_custom_generated_pk=True,
            track_dirty_fields=False,
            get_hydration_layout=lambda _connection: layout,
        )

    column_names = ("value", "note", "__app", "__model")
    raw_rows = [("a", "first", "models", "FirstBranch"), ("b", "second", "models", "SecondBranch")]
    rows = raw_rows if by_position else [dict(zip(column_names, row, strict=True)) for row in raw_rows]
    connection = SimpleNamespace(
        connection_alias="selected",
        features=SimpleNamespace(supports_positional_rows=by_position),
        dialect=SimpleNamespace(types=SimpleNamespace(get_python_reader=lambda _field: None)),
    )
    reader = ModelRows(FirstBranch, connection.dialect.types, annotations=("note",))
    reader.annotation_decoders = {"note": str.upper}
    loaded = list(
        reader.read_combined(connection, rows, column_names, (FirstBranch, SecondBranch), "__app", "__model")
    )
    assert [type(obj) for obj in loaded] == [FirstBranch, SecondBranch]
    assert [obj.value for obj in loaded] == ["decoded:a", "decoded:b"]
    assert [obj.note for obj in loaded] == ["FIRST", "SECOND"]
    assert not loaded[0]._custom_generated_pk and loaded[1]._custom_generated_pk


@pytest.mark.parametrize("names", [("x", "x"), ()])
def test_statement_result_retains_explicit_names_for_empty_rows(names):
    result = StatementResult(0, [], column_names=names)
    assert result.column_names is names
    assert "description" not in result.__dict__
    assert result.description == tuple((name,) for name in names)
    assert result.column_names is names


def test_statement_result_preserves_driver_description_and_named_rows():
    description = (("x", int, None, None, None, None, False),)
    result = StatementResult(1, [(5,)], description=description)
    assert result.description is description
    assert result.column_names == ("x",)
    named = StatementResult(1, [{"value": 5}])
    assert named.column_names == ("value",)
    assert named.description is None


@pytest.mark.parametrize("by_position", [False, True])
def test_grouped_models_decode_every_occurrence_and_share_normalized_keys(by_position):
    calls = []

    class Loaded:
        _meta = SimpleNamespace(default_custom_generated_pk=False, track_dirty_fields=False)

        def __new__(cls):
            calls.append("new")
            return object.__new__(cls)

    def decode(value):
        calls.append(value)
        return int(value)

    keys = (0, 1, 2) if by_position else ("left", "right", "owner")
    columns = ModelColumns(Loaded, (("left", None, decode), ("right", None, decode)), keys[:2], False)
    raw = [("01", "2", "10"), ("1", "02", "11"), ("2", "3", "10")]
    rows = raw if by_position else [dict(zip(keys, row, strict=True)) for row in raw]
    shared = {}
    layout = NativeModules.rows.GroupedModelLayout(columns, ("left", "right"), keys[2], int)
    first = layout.read_batch(rows, 0, 1, "selected", lambda obj: None, shared)
    rest = layout.read_batch(rows, 1, 3, "selected", lambda obj: None, shared)
    assert [pair[0] for pair in first + rest] == [10, 11, 10]
    assert first[0][1] is rest[0][1]
    assert rest[1][1] is not first[0][1]
    assert (first[0][1].left, first[0][1].right) == (1, 2)
    assert calls == ["01", "2", "new", "1", "02", "2", "3", "new"]
