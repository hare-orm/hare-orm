"""Prepared conversions preserve live extension hooks, batch contracts and row order."""

import datetime
from enum import IntEnum, StrEnum
from types import SimpleNamespace
from uuid import UUID

import pytest

from hare import Model, fields
from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.exceptions import ValidationError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.containers.map_field import MapField
from hare.fields.data.containers.tuple_field import TupleField
from hare.fields.generated_field import GeneratedField
from hare.models.write.returned_values import ReturnedValues
from hare.query.expressions.value import Value
from hare.query.filters.lookups.value_encoders import ValueEncoders
from hare.query.key_columns import KeyColumns
from tests.utils.model_bindings import ModelBindings

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("key", ("Ab", "aB", "XY"))
def test_map_path_keys_are_python_inputs_instead_of_stored_values(key):
    class SwappedKey(fields.CharField):
        def to_db_value(self, value, instance):
            return super().to_db_value(value, instance).swapcase()

        def from_db_value(self, value):
            return str(value).swapcase()

    field = MapField(SwappedKey(max_length=10), fields.IntField())
    assert field.to_python({key: 1}) == {key: 1}
    assert field.get_path_key(key) == key
    assert field.get_path_key("has_key") is None


@pytest.mark.parametrize(
    "key_field,key,expected",
    [
        (fields.IntField(), "17", 17),
        (fields.CharField(max_length=10), "Ab", "Ab"),
    ],
)
def test_map_path_keys_keep_the_key_fields_input_conversion(key_field, key, expected):
    field = MapField(key_field, fields.IntField())
    assert field.get_path_key(key) == expected
    assert field.get_path_key("has_keys") is None


def test_map_path_keys_keep_enum_and_temporal_input_types():
    class Number(IntEnum):
        VALUE = 17

    class Label(StrEnum):
        VALUE = "Ab"

    cases = (
        (fields.IntEnumField(Number), "17", Number.VALUE),
        (fields.CharEnumField(Label), "Ab", Label.VALUE),
        (fields.UUIDField(), "00000000-0000-0000-0000-000000000007", UUID(int=7)),
        (fields.DateField(), "2024-02-29", datetime.date(2024, 2, 29)),
        (
            fields.DatetimeField(),
            "2024-02-29T12:34:56+00:00",
            datetime.datetime(2024, 2, 29, 12, 34, 56, tzinfo=datetime.UTC),
        ),
    )
    for key_field, segment, expected in cases:
        actual = MapField(key_field, fields.IntField()).get_path_key(segment)
        assert actual == expected
        assert type(actual) is type(expected)


@pytest.mark.parametrize(
    "value_method,selector,factory",
    [
        ("get_db_value", "get_db_converter", "get_db_writer"),
        ("get_lookup_value", "get_lookup_converter", "get_lookup_writer"),
        ("get_python_value", "get_python_converter", "get_python_reader"),
    ],
)
@pytest.mark.parametrize("override_selector", [False, True])
def test_prepared_conversion_preserves_overridden_dispatch(
    monkeypatch, value_method, selector, factory, override_selector
):
    types = TypeRegistry()
    field = fields.IntField(null=True)
    method_name = selector if override_selector else value_method
    original = getattr(types, method_name)
    events = []

    def tracked(*args):
        events.append(args)
        return original(*args)

    monkeypatch.setattr(types, method_name, tracked)
    convert = getattr(types, factory)(field)
    assert not events
    values = [1, None, 3]
    read = value_method == "get_python_value"
    expected = [
        getattr(types, value_method)(field, value) if read else getattr(types, value_method)(field, value, None)
        for value in values
    ]
    expected_events = list(events)
    events.clear()
    actual = [convert(value) if read else convert(value, None) for value in values]
    assert actual == expected
    assert events == expected_events


@pytest.mark.parametrize("values", [[1, 2, 3], [1, None, 3]])
def test_lookup_list_uses_batch_hook_without_mutating_input(values):
    events = []

    class BatchInt(fields.IntField):
        def to_lookup_values(self, values, instance):
            events.append(tuple(values))
            return super().to_lookup_values(values, instance)

    types = TypeRegistry()
    field = BatchInt(null=True)
    before = list(values)
    dialect = SimpleNamespace(types=types)
    encode = ValueEncoders.prepare(ValueEncoders.encode_list, field, types)
    result = encode(values, None, field, dialect)
    assert result == before
    assert result is not values
    assert values == before
    assert events == ([] if None in values else [tuple(values)])


def test_key_transfer_uses_source_reader_and_destination_writer():
    source = TypeRegistry()
    target = TypeRegistry()
    field = fields.IntField()
    source.register(
        fields.IntField,
        TypeMapping(
            to_db=lambda field, value, instance: f"source:{value}",
            to_python=lambda field, value: int(value.removeprefix("source:")),
        ),
    )
    target.register(fields.IntField, TypeMapping(to_db=lambda field, value, instance: f"target:{value}"))
    assert KeyColumns.normalize_values((field,), (3,), source, None) == (3,)
    assert KeyColumns.transfer_rows((field,), ("key",), [{"key": "source:3"}], source, target, None) == [("target:3",)]


def test_key_reading_keeps_all_null_composite_components():
    fields_in_order = (fields.IntField(null=True), fields.IntField(null=True))
    rows = [{"first": None, "second": None}, {"first": 1, "second": None}]
    assert KeyColumns.read_rows(fields_in_order, ("first", "second"), rows, TypeRegistry()) == [
        (None, None),
        (1, None),
    ]


def test_key_writing_stops_at_first_failure_in_row_order():
    visited = []
    types = TypeRegistry()

    def write(field, value, instance):
        visited.append((field.source_field, value))
        if value == "bad":
            raise ValidationError("refused")
        return value

    types.register(fields.IntField, TypeMapping(to_db=write))
    columns = (fields.IntField(source_field="first"), fields.IntField(source_field="second"))
    with pytest.raises(ValidationError, match="refused"):
        KeyColumns.write_rows(columns, [(1, 2), ("bad", 4), (5, 6)], types, None)
    assert visited == [("first", 1), ("second", 2), ("first", "bad")]


@pytest.mark.parametrize("count", [1, 8])
def test_stored_keys_do_not_skip_overridden_conversion_of_native_values(monkeypatch, count):

    class Book(Model):
        name = fields.TextField()

    prepared_models = ModelBindings.prepare(Book)

    types = TypeRegistry()
    field = prepared_models.get_binding(Book).fields_map["name"]
    # Populate identity metadata before replacing the value conversion.
    assert types.get_native_key_type(field) is str
    original = types.get_db_value

    def write(field, value, instance):
        return original(field, value, instance).casefold()

    monkeypatch.setattr(types, "get_db_value", write)
    objs = [prepared_models.new(Book, name="UPPER") for _ in range(count)]
    assert ReturnedValues.get_stored_keys(types, (field,), ("name",), objs) == [("upper",)] * count


@pytest.mark.parametrize("container", ["array", "map", "tuple"])
@pytest.mark.parametrize("operation", ["write", "read"])
def test_container_preparation_preserves_per_element_registry_hooks(monkeypatch, container, operation):
    values = [None if index % 3 == 0 else index for index in range(12)]
    if container == "array":
        field = ArrayField(fields.IntField(null=True))
        value = values
        expected_events = [element for element in values if element is not None]
    elif container == "map":
        field = MapField(fields.TextField(), fields.IntField(null=True))
        value = {str(index): element for index, element in enumerate(values)}
        expected_events = [part for pair in value.items() for part in pair if part is not None]
    else:
        field = TupleField([fields.IntField(null=True) for _ in values])
        value = tuple(values)
        expected_events = [element for element in values if element is not None]
    types = TypeRegistry()
    method = "get_db_value" if operation == "write" else "get_python_value"
    original = getattr(types, method)
    events = []

    def tracked(child, element, *args):
        events.append(element)
        return original(child, element, *args)

    monkeypatch.setattr(types, method, tracked)
    result = (
        field.encode_value(value, None, types, "payload") if operation == "write" else field.decode_value(value, types)
    )
    assert result == value
    assert events == expected_events


@pytest.mark.parametrize("prepared", [False, True])
def test_map_invalid_key_does_not_convert_its_value(prepared):
    converted_values = []
    field = MapField(fields.IntField(), fields.TextField(validators=[converted_values.append]))
    value = {"bad": "tracked"}
    with pytest.raises(ValidationError, match=r"payload\['bad'\]"):
        if prepared:
            field.get_value_encoder(None)(value, None, "payload")
        else:
            field.encode_value(value, None, None, "payload")
    assert converted_values == []


@pytest.mark.parametrize("container", ["array", "map", "tuple", "generated"])
@pytest.mark.parametrize("operation", ["write", "read"])
def test_wrapped_fields_keep_registry_selector_overrides(monkeypatch, container, operation):
    types = TypeRegistry()
    child = fields.IntField(null=True)
    if container == "array":
        field, value, expected = ArrayField(child), [2, None, 9], [102, None, 109]
    elif container == "map":
        field = MapField(fields.TextField(), child)
        value, expected = {"first": 2, "empty": None}, {"first": 102, "empty": None}
    elif container == "tuple":
        field, value, expected = TupleField([child, child]), (2, None), (102, None)
    else:
        field, value, expected = GeneratedField(expression=Value(2), output_field=child), 2, 102
    selector = "get_db_writer" if operation == "write" else "get_python_reader"
    original = getattr(types, selector)

    def select(selected):
        convert = original(selected)
        if not isinstance(selected, fields.IntField):
            return convert
        if operation == "write":
            return lambda value, instance: None if value is None else convert(value, instance) + 100
        return lambda value: None if value is None else convert(value) + 100

    monkeypatch.setattr(types, selector, select)
    if container == "generated":
        result = (
            types.get_db_value(field, value, None) if operation == "write" else types.get_python_value(field, value)
        )
    else:
        result = (
            field.encode_value(value, None, types, "payload")
            if operation == "write"
            else field.decode_value(value, types)
        )
    assert result == expected


def test_native_hydration_keeps_registry_reader_selector(monkeypatch):
    types = TypeRegistry()
    field = fields.IntField()
    original = types.get_python_reader
    assert types.get_native_reader(field, frozenset({int})) is None
    monkeypatch.setattr(types, "get_python_reader", lambda selected: lambda value: original(selected)(value) + 100)
    read = types.get_native_reader(field, frozenset({int}))
    assert read is not None
    assert read(2) == 102


@pytest.mark.parametrize("selector", ["get_db_value", "get_db_writer"])
def test_default_lookup_follows_storage_hooks_after_cache_warmup(monkeypatch, selector):
    types = TypeRegistry()
    field = fields.IntField()
    assert types.get_lookup_value(field, 2, None) == 2
    original = getattr(types, selector)
    if selector == "get_db_writer":
        monkeypatch.setattr(
            types, selector, lambda selected: lambda value, instance: original(selected)(value, instance) + 100
        )
    else:
        monkeypatch.setattr(
            types, selector, lambda selected, value, instance: original(selected, value, instance) + 100
        )
    assert types.get_lookup_value(field, 2, None) == 102
    assert types.get_lookup_writer(field)(2, None) == 102
    assert types.get_lookup_values(field, [2, None, 9], None) == [102, None, 109]


@pytest.mark.parametrize("count", [1, 2, 7, 8, 9])
@pytest.mark.parametrize("selector", ["get_db_writer", "get_python_reader"])
def test_stored_keys_keep_registry_selectors_at_every_batch_size(monkeypatch, count, selector):

    class Book(Model):
        key = fields.TextField()

    prepared_models = ModelBindings.prepare(Book)

    types = TypeRegistry()
    field = prepared_models.get_binding(Book).fields_map["key"]
    assert types.get_native_key_type(field) is str
    original = getattr(types, selector)
    if selector == "get_db_writer":
        monkeypatch.setattr(
            types, selector, lambda selected: lambda value, instance: original(selected)(value, instance).casefold()
        )
    else:
        monkeypatch.setattr(types, selector, lambda selected: lambda value: original(selected)(value).casefold())
    objs = [prepared_models.new(Book, key=f"BOOK-{index}") for index in range(count)]
    assert ReturnedValues.get_stored_keys(types, (field,), ("key",), objs) == [
        (f"book-{index}",) for index in range(count)
    ]
