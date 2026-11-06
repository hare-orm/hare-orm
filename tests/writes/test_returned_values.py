import json
from collections.abc import Mapping

import pytest

from hare import Model, fields
from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.dialect_registry import DialectRegistry
from hare.models.write.returned_values import ReturnedValues

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("key", [{"a": [1, {"b": 2}]}, [1, {"a": [2]}]])
@pytest.mark.parametrize(
    "match,expected_indices",
    [(ReturnedValues.match_rows, [2, 0]), (ReturnedValues.match_rows_by_stored_values, [2, 0, 1])],
    ids=["first_instance", "each_unmatched_instance"],
)
def test_returned_rows_match_container_keys_in_input_order(key, match, expected_indices):
    class Book(Model):
        key = fields.JSONField(source_field="book_key")

    first = Book(key=key)
    repeated = Book(key=key)
    second = Book(key={"other": [3]})
    rows = [
        {"book_key": json.dumps(second.key)},
        {"book_key": json.dumps(key)},
        {"book_key": json.dumps(key)},
        {"book_key": json.dumps({"absent": 4})},
    ]
    instances = [first, repeated, second]
    matched = list(match(Book, DialectRegistry.get_dialect("sqlite").types, instances, rows, ["key"]))
    assert len(matched) == len(expected_indices)
    assert all(obj is instances[index] for (obj, _row), index in zip(matched, expected_indices, strict=True))
    assert [row for _, row in matched] == rows[: len(expected_indices)]


@pytest.mark.parametrize("count", [0, 1, 8])
@pytest.mark.parametrize(
    "conversion",
    ["field", "mapping", "get_db_value", "get_python_value", "get_db_converter", "get_python_converter"],
)
def test_stored_keys_preserve_conversion_dispatch_and_order(count, conversion, monkeypatch):
    events = []

    class FoldedText(fields.TextField):
        def to_db_value(self, value, instance):
            events.append(("write", value, instance))
            return super().to_db_value(value, instance).casefold()

        def from_db_value(self, value):
            events.append(("read", value))
            return super().from_db_value(value)

    class Book(Model):
        name = FoldedText(null=True)
        payload = fields.JSONField(null=True)

    types = TypeRegistry()
    if conversion == "mapping":

        def write(field, value, instance):
            events.append(("mapped_write", value, instance))
            return field.to_db_value(value, instance)

        def read(field, value):
            events.append(("mapped_read", value))
            return field.from_db_value(value)

        types.register(FoldedText, TypeMapping(to_db=write, to_python=read))
    elif conversion != "field":
        original = getattr(types, conversion)

        def tracked(*args):
            events.append((conversion, *args))
            return original(*args)

        monkeypatch.setattr(types, conversion, tracked)

    selected = [Book._meta.fields_map[name] for name in ("name", "payload")]
    # Metadata preparation precedes conversion, including when there are no objects.
    for field in selected:
        types.get_native_key_type(field)
    objs = [Book(name="BOOK" if index != 1 else None, payload={"a": [index, None]}) for index in range(count)]
    events.clear()
    expected = [ReturnedValues.get_stored_key(types, selected, ("name", "payload"), obj) for obj in objs]
    expected_events = list(events)
    events.clear()
    assert ReturnedValues.get_stored_keys(types, selected, ("name", "payload"), objs) == expected
    assert events == expected_events


def test_hashable_keys_freeze_nested_containers_and_canonicalize_every_nan():
    class CustomFloat(float):
        pass

    nan_key = ReturnedValues.NAN_KEY
    value = [float("nan"), CustomFloat("nan"), 1, True, None, "text", b"bytes", (2,), {"nested": [3]}, {4}]
    assert ReturnedValues.get_hashable(value) == (
        nan_key,
        nan_key,
        1,
        True,
        None,
        "text",
        b"bytes",
        (2,),
        frozenset({("nested", (3,))}),
        frozenset({4}),
    )


def test_hashable_keys_keep_mapping_semantics_for_scalar_subclasses():
    class TextMapping(str, Mapping):
        def __iter__(self):
            return iter(("numbers",))

        def __getitem__(self, key):
            return {"numbers": [1, 2]}[key]

        def __len__(self):
            return 1

    assert ReturnedValues.get_hashable(TextMapping("label")) == frozenset({("numbers", (1, 2))})
